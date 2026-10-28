"""Publish-to-run HTTP contract using real ownership and mutation journals."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from agent_line.approvals import TrustedPrincipal
from agent_line.authoring_api import AuthoringAPI, AuthoringAccess, state_hash, namespace
from agent_line.service import ServiceConfig, create_app
from agent_line.sim_journal import SimJournal
from agent_line.store import TaskStore
from agent_line.workflows import WorkflowGateway
from tests.test_authoring_api import FakeSimAuthoringClient, document
from tests.test_publication_capabilities import verification_policy


class PublishedRemote(FakeSimAuthoringClient):
    def __init__(self, sim):
        super().__init__()
        self.sim = sim
        self.versions = {}
        self.active = {}
        self.deploy_error = None

    def deploy(self, workflow):
        self.calls.append(('deploy', workflow))
        if self.deploy_error:
            raise self.deploy_error
        revision = 1 + len(self.versions.get(workflow, {}))
        deployment = f'deployment-{workflow}-{revision}'
        self.active[workflow] = revision
        self.versions.setdefault(workflow, {})[revision] = {
            'id': deployment, 'version': revision,
            'state': copy.deepcopy(self.states[workflow])}
        return {'activeDeployment': {'deploymentVersionId': deployment}, 'version': revision}

    def get_version(self, workflow, revision):
        self.calls.append(('version', revision))
        value = copy.deepcopy(self.versions[workflow][revision])
        value['isActive'] = self.active[workflow] == revision
        return value


class PublishedWorkflowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.tasks = TaskStore.initialize(self.root / 'tasks.sqlite')
        self.journal = SimJournal.initialize(self.root / 'journal.sqlite')
        self.sim = Mock(_key='sim-secret')
        self.sim.execute.return_value = {'accepted': True}
        self.sim.observe.side_effect = lambda workflow, run: {'response': {
            'data': {'workflowId': workflow, 'runId': run, 'status': 'completed'}}}
        self.remote = PublishedRemote(self.sim)
        self.projects = {p: {'workspace': str(self.root)} for p in ('one', 'two')}
        principal = TrustedPrincipal('editor', frozenset({'one'}),
            frozenset({'edit_workflows', 'publish_workflows'}), 200)
        self.access = AuthoringAccess({'editor-token': principal}, clock=lambda: 100)
        self.api = AuthoringAPI(self.access, self.journal,
                               {'one': self.remote, 'two': self.remote}, projects=self.projects)
        self.gateway = WorkflowGateway(self.sim, self.journal, {}, installation_id='installation')
        self.open_app()

    def open_app(self):
        app = create_app(self.tasks, ServiceConfig(tokens={'run-one': {'one'}, 'run-two': {'two'}},
            projects=self.projects, bindings={}), authoring=self.api, workflows=self.gateway)
        self.http = TestClient(app); self.addCleanup(self.http.close)

    def edit(self, path, body):
        return self.http.post('/extension/v1/authoring/projects/one/workflows' + path,
            json=body, headers={'Authorization': 'Bearer editor-token'})

    def publish(self, workflow, operation='publish-1'):
        return self.edit('/'+workflow+'/publish', {'operationId': operation,
            'expectedStateSha256': state_hash(self.remote.states[workflow])})

    def published(self):
        created = self.edit('', {'operationId': 'create-1', 'name': '正式流程', 'description': ''})
        self.assertEqual(created.status_code, 202, created.text)
        workflow = created.json()['receipt']['workflowId']
        saved = self.edit('/'+workflow+'/save', {'operationId': 'save-1',
            'expectedStateSha256': state_hash(self.remote.states[workflow]), 'document': document()})
        self.assertEqual(saved.json()['receipt']['outcome'], 'saved')
        published = self.publish(workflow)
        self.assertEqual(published.json()['receipt']['outcome'], 'published', published.text)
        return workflow, published.json()['receipt']['bindingId']

    def catalog(self, token='run-one'):
        return self.http.get('/extension/v1/workflows', headers={'Authorization': 'Bearer '+token})

    def native_publication(self):
        from probes.sim.workflow_probe import graph
        workflow = self.edit('', {'operationId': 'create-native', 'name': '原版Sim流程'}).json()['receipt']['workflowId']
        self.remote.states[workflow] = graph(True)
        self.remote.states[workflow]['blocks']['human-approval']['subBlocks']['inputFormat']['value'] = [
            {'id': 'native-field-id', 'name': 'taskInput', 'type': 'object', 'value': '<start.input>'}]
        self.api.verification_enabled = True
        self.api.role_bindings['developer'] = {'projectId': 'one', 'roleAgentId': 'existing-agent',
                                              'verificationPolicy': verification_policy()}
        self.api.publication_check = lambda project, policies: None
        body = {'operationId': 'publish-native', 'expectedStateSha256': state_hash(self.remote.states[workflow]),
                'nativeBindings': {'human-approval': {'roleBindingId': 'developer', 'instruction': '完成本轮任务'}}}
        return workflow, body

    def test_native_publication_uses_existing_catalog_without_graph_compiler(self):
        workflow, body = self.native_publication()
        original = self.remote.get_version

        def version(wid, revision):
            result = original(wid, revision)
            result['state']['lastSaved'] = 'transport metadata'
            for edge in result['state']['edges']:
                edge['data'] = None
            return result

        with patch.object(self.remote, 'get_version', side_effect=version), \
                patch('agent_line.authoring_api.decode_document', side_effect=AssertionError('native graph must stay native')), \
                patch('agent_line.published_workflows.compile_document', side_effect=AssertionError('no second compiler')):
            result = self.edit('/'+workflow+'/publish', body)
            self.assertEqual(result.status_code, 202, result.text)
            receipt = result.json()['receipt']
            self.assertEqual(receipt['outcome'], 'published', result.text)
            self.assertEqual(receipt['graphFormat'], 'sim-native-v1')
            policy = receipt['externalTasks']['agentPolicies']['human-approval']
            self.assertEqual(policy['roleAgentId'], 'existing-agent')
            self.assertEqual(policy['memoryPolicyId'], 'native-role')
            bindings = self.gateway.available({'one'})
            binding = bindings[receipt['bindingId']]
            self.gateway.catalog.check_active(binding)
            self.assertEqual(self.edit('/'+workflow+'/publish', body).json(), result.json())
            changed = {**body, 'nativeBindings': {'human-approval': {**body['nativeBindings']['human-approval'], 'instruction': '不同任务'}}}
            self.assertEqual(self.edit('/'+workflow+'/publish', changed).status_code, 409)
            self.assertEqual(self.remote.count('deploy'), 1)
            self.remote.versions[workflow][1]['state']['blocks']['human-approval']['enabled'] = False
            with self.assertRaisesRegex(ValueError, 'published_version_changed'):
                self.gateway.catalog.check_active(binding)

    def test_native_publication_rejects_unbound_nodes_roles_and_foreign_ownership(self):
        workflow, body = self.native_publication()
        missing = {**body, 'nativeBindings': {'unknown-node': body['nativeBindings']['human-approval']}}
        rejected = self.edit('/'+workflow+'/publish', missing)
        self.assertEqual(rejected.status_code, 202)
        self.assertEqual(rejected.json()['status'], 'accepted')
        self.assertEqual(rejected.json()['receipt']['outcome'], 'native_binding_required')
        original = copy.deepcopy(self.remote.states[workflow])
        self.remote.states[workflow]['blocks']['human-approval']['subBlocks']['inputFormat']['value'] = []
        invalid_input = {**body, 'operationId': 'missing-input',
                         'expectedStateSha256': state_hash(self.remote.states[workflow])}
        self.assertEqual(self.edit('/'+workflow+'/publish', invalid_input).json()['receipt']['outcome'], 'native_binding_required')
        self.remote.states[workflow] = copy.deepcopy(original)
        self.remote.states[workflow]['edges'].append({'id': 'repeat', 'source': 'human-approval', 'target': 'human-approval'})
        repeated = {**body, 'operationId': 'repeat-node',
                    'expectedStateSha256': state_hash(self.remote.states[workflow])}
        self.assertEqual(self.edit('/'+workflow+'/publish', repeated).json()['receipt']['outcome'], 'native_binding_required')
        self.remote.states[workflow] = original
        unknown_role = {**body, 'operationId': 'unknown-role', 'nativeBindings': {
            'human-approval': {'roleBindingId': 'not-registered', 'instruction': '任务'}}}
        rejected = self.edit('/'+workflow+'/publish', unknown_role)
        self.assertEqual(rejected.json()['receipt']['outcome'], 'role_binding_required')
        forged = {**body, 'nativeBindings': {'human-approval': {
            **body['nativeBindings']['human-approval'], 'roleAgentId': 'attacker'}}}
        self.assertEqual(self.edit('/'+workflow+'/publish', forged).status_code, 422)
        self.assertEqual(self.edit('/foreign/publish', body).status_code, 404)
        self.assertEqual(self.edit('/'+workflow+'/save', {**body, 'document': document()}).status_code, 422)
        self.assertEqual(self.remote.count('deploy'), 0)

    def test_native_human_waits_remain_separate_in_mixed_and_human_only_flows(self):
        workflow, body = self.native_publication()
        state = self.remote.states[workflow]
        review = copy.deepcopy(state['blocks']['human-approval'])
        review['id'] = 'human-review'
        review['subBlocks']['inputFormat']['value'] = [
            {'id': 'approved', 'name': 'approved', 'type': 'boolean', 'value': True}]
        state['blocks']['human-review'] = review
        state['edges'][-1]['target'] = 'human-review'
        state['edges'].append({'id': 'review-end', 'source': 'human-review', 'target': 'human-done'})
        self.api.approval_enabled = True
        body.update(expectedStateSha256=state_hash(state), humanBlockIds=['human-review'])
        mixed = self.edit('/'+workflow+'/publish', body).json()['receipt']
        self.assertEqual(mixed['outcome'], 'published')
        self.assertEqual(mixed['humanApproval']['blockIds'], ['human-review'])
        self.assertEqual(set(mixed['externalTasks']['agentPolicies']), {'human-approval'})
        human_only = {**body, 'operationId': 'human-only', 'nativeBindings': {},
                      'humanBlockIds': ['human-approval', 'human-review']}
        receipt = self.edit('/'+workflow+'/publish', human_only).json()['receipt']
        self.assertEqual(receipt['outcome'], 'published')
        self.assertNotIn('externalTasks', receipt)
        self.assertEqual(receipt['humanApproval']['blockIds'], ['human-approval', 'human-review'])

    def start(self, binding, key='trigger', token='run-one'):
        return self.http.post('/extension/v1/workflows/'+binding+'/runs',
            headers={'Authorization': 'Bearer '+token}, json={'idempotencyKey': key, 'input': {}})

    def query(self, binding, run, token='run-one'):
        return self.http.get('/extension/v1/workflows/'+binding+'/runs/'+run,
                             headers={'Authorization': 'Bearer '+token})

    def test_http_create_save_publish_lists_starts_and_queries_without_manual_binding(self):
        workflow, binding = self.published()
        self.assertTrue(binding.startswith('published-'))
        self.assertIn(binding, [x['bindingId'] for x in self.catalog().json()['workflows']])
        started = self.start(binding)
        self.assertEqual(started.status_code, 202, started.text)
        run = started.json()['runId']
        self.assertEqual(self.query(binding, run).status_code, 200)
        self.assertEqual(self.start(binding).json(), started.json())
        self.assertEqual(self.sim.execute.call_count, 1)
        frozen = self.journal.get_publication(workflow, run)
        self.assertEqual(frozen['bindingId'], binding)
        self.assertEqual(frozen['projectId'], 'one')

    def test_project_scope_and_disabled_or_unconfigured_project_hide_catalog(self):
        _, binding = self.published()
        self.assertEqual(self.catalog('run-two').json()['workflows'], [])
        self.assertEqual(self.start(binding, token='run-two').status_code, 404)
        self.api.projects['one']['enabled'] = False
        self.assertEqual(self.catalog().json()['workflows'], [])
        self.assertEqual(self.start(binding).status_code, 404)
        self.api.projects['one']['enabled'] = True
        del self.api.clients['one']
        self.assertEqual(self.catalog().json()['workflows'], [])
        self.sim.execute.assert_not_called()

    def test_uncertain_publication_does_not_create_runnable_binding(self):
        self.remote.deploy_error = TimeoutError('private upstream detail')
        created = self.edit('', {'operationId': 'create', 'name': 'name'})
        workflow = created.json()['receipt']['workflowId']
        self.edit('/'+workflow+'/save', {'operationId': 'save', 'document': document(),
            'expectedStateSha256': state_hash(self.remote.states[workflow])})
        result = self.publish(workflow)
        self.assertEqual(result.json()['status'], 'uncertain')
        self.assertNotIn('private upstream detail', result.text)
        self.assertEqual(self.catalog().json()['workflows'], [])
        self.sim.execute.assert_not_called()

    def test_hash_or_active_changes_refuse_new_execution(self):
        workflow, binding = self.published()
        self.remote.active[workflow] = 0
        self.assertEqual(self.start(binding).status_code, 503)
        self.remote.active[workflow] = 1
        self.remote.versions[workflow][1]['state']['variables']['unexpected'] = {'value': 3}
        self.assertEqual(self.start(binding).status_code, 503)
        self.sim.execute.assert_not_called()

    def test_new_publication_does_not_hide_old_run_or_reissue_same_trigger(self):
        workflow, old = self.published()
        first = self.start(old); self.assertEqual(first.status_code, 202, first.text)
        new = self.publish(workflow, 'publish-2').json()['receipt']['bindingId']
        self.assertNotEqual(old, new)
        before = len(self.remote.calls)
        self.assertEqual(self.start(old).json(), first.json())
        self.assertEqual(len(self.remote.calls), before)
        self.assertEqual(self.query(old, first.json()['runId']).status_code, 200)
        self.assertEqual(self.start(old, 'new-trigger').status_code, 503)
        second = self.start(new)
        self.assertEqual(second.status_code, 202, second.text)
        self.assertNotEqual(first.json()['runId'], second.json()['runId'])
        self.assertEqual(self.query(new, first.json()['runId']).status_code, 404)
        self.assertEqual(self.sim.execute.call_count, 2)

    def test_maintenance_permits_existing_run_read_and_replay_only(self):
        _, binding = self.published()
        first = self.start(binding)
        self.tasks.enter_maintenance()
        self.assertEqual(self.query(binding, first.json()['runId']).status_code, 200)
        self.assertEqual(self.start(binding).json(), first.json())
        self.assertEqual(self.start(binding, 'new').status_code, 503)
        self.assertEqual(self.sim.execute.call_count, 1)

    def test_legacy_publish_receipt_is_not_automatically_enabled(self):
        workflow, _ = self.published()
        with self.journal.connect() as db:
            import json
            row = db.execute("SELECT receipt FROM operations WHERE workflow=? AND context='author-publish'",
                             (namespace('one'),)).fetchone()
            receipt = json.loads(row[0]); receipt.pop('bindingId'); receipt.pop('graphSha256')
            db.execute("UPDATE operations SET receipt=? WHERE workflow=? AND context='author-publish'",
                       (json.dumps(receipt), namespace('one')))
        self.assertEqual(self.catalog().json()['workflows'], [])
        self.sim.execute.assert_not_called()

    def test_publish_without_accepted_create_ownership_is_hidden(self):
        _, binding = self.published()
        with self.journal.connect() as db:
            db.execute("UPDATE operations SET status='uncertain',receipt=NULL "
                       "WHERE workflow=? AND context='author-create'", (namespace('one'),))
        self.assertEqual(self.catalog().json()['workflows'], [])
        self.assertEqual(self.start(binding).status_code, 404)
        self.sim.execute.assert_not_called()

    def test_app_refuses_separate_sim_transport_or_journal(self):
        config = ServiceConfig(tokens={'run': {'one'}}, projects=self.projects, bindings={})
        original_sim = self.remote.sim
        self.remote.sim = Mock(_key='another-key')
        with self.assertRaises(ValueError):
            create_app(self.tasks, config, authoring=self.api, workflows=self.gateway)
        self.remote.sim = original_sim
        self.api.journal = SimJournal.initialize(self.root / 'unrelated.sqlite')
        with self.assertRaises(ValueError):
            create_app(self.tasks, config, authoring=self.api, workflows=self.gateway)
        self.sim.execute.assert_not_called()

    def test_version_identity_or_numeric_revision_mismatch_is_not_executable(self):
        workflow, binding = self.published()
        native = self.remote.versions[workflow][1]
        for field, value in [('id', 'different-deployment'), ('version', 2), ('version', True)]:
            old = native[field]; native[field] = value
            with self.subTest(field=field, value=value):
                self.assertEqual(self.start(binding).status_code, 503)
            native[field] = old
        self.sim.execute.assert_not_called()
        with self.journal.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM operations WHERE context='run-publication'").fetchone()[0], 0)

    def test_reopen_catalog_and_journal_preserves_publication_and_duplicate_identity(self):
        workflow, binding = self.published()
        first = self.start(binding)
        self.journal = SimJournal(self.journal.path)
        self.api.journal = self.journal
        self.gateway = WorkflowGateway(self.sim, self.journal, {}, installation_id='installation')
        self.open_app()
        self.assertEqual(self.start(binding).json(), first.json())
        self.assertEqual(self.query(binding, first.json()['runId']).status_code, 200)
        self.assertEqual(self.sim.execute.call_count, 1)
        self.assertEqual(self.journal.get_publication(workflow, first.json()['runId'])['bindingId'], binding)

    def machine_publication(self):
        from types import SimpleNamespace
        from agent_line.approval_discovery import WorkflowRunLedger
        from agent_line.pause_bindings import PauseBindings
        self.api.role_bindings['developer'] = {'projectId': 'one', 'roleAgentId': 'parent', 'enabled': True,
                                               'verificationPolicy': verification_policy()}
        # Synthetic execution capability isolates catalog/run snapshot behavior.
        self.api.verification_enabled = True
        self.api.publication_check = lambda project, policies: None
        from agent_line.automatic_verification import AutomaticVerifier
        evidence = tempfile.TemporaryDirectory(); self.addCleanup(evidence.cleanup)
        self.gateway.verifier = AutomaticVerifier(self.tasks, Path(evidence.name))
        created = self.edit('', {'operationId': 'create-machine', 'name': '机器流程'})
        workflow = created.json()['receipt']['workflowId']
        doc = document()
        doc['nodes'].insert(1, {'id': 'worker', 'type': 'agent', 'name': '实现',
            'position': {'x': 20, 'y': 20}, 'config': {'roleBindingId': 'developer',
                'instruction': '实现任务', 'inputFrom': {'nodeId': 'start', 'field': 'input'},
                'memoryPolicyId': 'native-role', 'outputSchemaId': 'task-result',
                'requireMemoryCommit': False, 'allowFailureBranch': False}})
        doc['edges'] = [{'id': 'a', 'source': 'start', 'target': 'worker'},
                        {'id': 'b', 'source': 'worker', 'target': 'end'}]
        saved = self.edit('/'+workflow+'/save', {'operationId': 'save-machine', 'document': doc,
            'expectedStateSha256': state_hash(self.remote.states[workflow])})
        self.assertEqual(saved.json()['receipt']['outcome'], 'saved', saved.text)
        receipt = self.publish(workflow, 'publish-machine').json()['receipt']
        self.assertEqual(receipt['outcome'], 'published')
        runs = WorkflowRunLedger.initialize(self.root / 'runs.sqlite')
        flow = SimpleNamespace(runs=runs, pauses=PauseBindings.initialize(self.root / 'pauses.sqlite'),
                               workers=(), discovery=SimpleNamespace(allow_submissions=True))
        self.gateway.task_flow = flow
        self.gateway.publication_verifier = Mock()
        return workflow, receipt['bindingId'], receipt, runs

    def test_machine_publication_freezes_policy_before_sim_execute(self):
        workflow, binding, receipt, runs = self.machine_publication()
        self.assertIn(binding, self.gateway.catalog.bindings({'one'}))
        def execute(wid, run, inputs):
            frozen = runs.get(wid, run)
            self.assertEqual(frozen['agentPolicies'], receipt['externalTasks']['agentPolicies'])
            self.assertEqual(frozen['purpose'], 'external_task_result')
            self.assertEqual(self.journal.get_publication(wid, run)['bindingId'], binding)
            return {'accepted': True}
        self.sim.execute.side_effect = execute
        response = self.start(binding)
        self.assertEqual(response.status_code, 202, response.text)
        self.gateway.publication_verifier.check_definition.assert_called_once()
        self.assertEqual(runs.get(workflow, response.json()['runId'])['submissionStatus'], 'accepted')

    def test_machine_without_runtime_and_maintenance_never_executes(self):
        _, binding, _, runs = self.machine_publication()
        flow = self.gateway.task_flow; self.gateway.task_flow = None
        self.assertEqual(self.start(binding).status_code, 503)
        self.gateway.task_flow = flow
        self.tasks.enter_maintenance()
        self.assertEqual(self.start(binding).status_code, 503)
        self.sim.execute.assert_not_called()
        with runs.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM runs').fetchone()[0], 0)

    def test_uncertain_dynamic_submission_reopen_never_executes_again(self):
        from agent_line.adapters.sim import SubmissionUncertain
        _, binding = self.published()
        self.sim.execute.side_effect = SubmissionUncertain('lost response')
        first = self.start(binding)
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(first.json()['status'], 'uncertain')
        self.journal = SimJournal(self.journal.path)
        self.api.journal = self.journal
        self.gateway = WorkflowGateway(self.sim, self.journal, {}, installation_id='installation')
        self.open_app()
        self.assertEqual(self.start(binding).json(), first.json())
        self.assertEqual(self.sim.execute.call_count, 1)

    def test_crashed_dynamic_submission_reopen_never_executes_again(self):
        import uuid
        from agent_line.store import canonical
        workflow, binding = self.published()
        run = str(uuid.uuid5(uuid.NAMESPACE_URL, canonical(
            ['installation', 'one', workflow, 'trigger', binding])))
        snapshot = {'bindingId': binding, **self.gateway.catalog.bindings({'one'})[binding]}
        self.journal.reserve_publication(workflow, run, snapshot)
        self.sim.execute.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.journal.execute(self.sim, workflow, run, {})
        self.journal = SimJournal(self.journal.path)
        self.api.journal = self.journal
        self.gateway = WorkflowGateway(self.sim, self.journal, {}, installation_id='installation')
        self.open_app()
        response = self.start(binding)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()['status'], 'intent')
        self.assertFalse(response.json()['safeToResubmit'])
        self.assertEqual(self.sim.execute.call_count, 1)

    def test_incomplete_native_version_returns_bounded_unverified_response(self):
        workflow, binding = self.published()
        del self.remote.versions[workflow][1]['state']
        self.http = TestClient(self.http.app, raise_server_exceptions=False)
        self.addCleanup(self.http.close)
        response = self.start(binding)
        self.assertEqual(response.status_code, 503, response.text)
        self.sim.execute.assert_not_called()
