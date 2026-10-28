"""Authoring HTTP behavior with real durable ownership and maintenance guards."""
import copy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import time
import unittest

from types import SimpleNamespace
from fastapi.testclient import TestClient

from agent_line.authoring_api import AuthoringAPI, AuthoringAccess, namespace, state_hash
from agent_line.approvals import TrustedPrincipal
from agent_line.sim_authoring import AuthoringError, compile_document
from agent_line.sim_journal import SimJournal
from agent_line.service import ServiceConfig, create_app
from agent_line.store import TaskStore


def document(human=False):
    def node(i, kind, config):
        return {'id': i, 'type': kind, 'name': i, 'position': {'x': 10, 'y': 20}, 'config': config}
    nodes = [node('start', 'start', {}), node('end', 'end', {'output': {'result': 'ok'}})]
    edges = [{'id': 'direct', 'source': 'start', 'target': 'end'}]
    if human:
        nodes.insert(1, node('approval', 'human', {'requiredBoolean': 'approved'}))
        edges = [{'id': 'one', 'source': 'start', 'target': 'approval'},
                 {'id': 'two', 'source': 'approval', 'target': 'end'}]
    return {'schemaVersion': 1, 'nodes': nodes, 'edges': edges, 'variables': {}}


class FakeSimAuthoringClient:
    """Only upstream operations are fake; ownership/authorization/journal are real."""
    def __init__(self):
        self.workspace_id = 'shared-native-workspace'
        self.sim = SimpleNamespace(_key='sim-service-key')
        self.states = {}; self.details = {}; self.calls = []
        self.create_error = None; self.read_error = None
        self.readback_override = None; self.version_override = None

    def create(self, name, description):
        self.calls.append(('create', name))
        if self.create_error: raise self.create_error
        workflow = 'wf-'+str(len(self.details)+1)
        self.details[workflow] = {'id': workflow, 'name': name, 'description': description, 'isDeployed': False}
        self.states[workflow] = {'blocks': {}, 'edges': [], 'variables': {}}
        return copy.deepcopy(self.details[workflow])

    def get_detail(self, workflow):
        self.calls.append(('detail', workflow))
        if self.read_error: raise self.read_error
        return copy.deepcopy(self.details[workflow])

    def get_deployment(self, workflow):
        self.calls.append(('deployment', workflow))
        return {'id': workflow, 'isDeployed': False, 'activeDeployment': None,
                'needsRedeployment': False}

    def get_state(self, workflow):
        self.calls.append(('state', workflow))
        if self.read_error: raise self.read_error
        return copy.deepcopy(self.states[workflow])

    def save_state(self, workflow, state):
        self.calls.append(('save', workflow))
        self.states[workflow] = copy.deepcopy(self.readback_override if self.readback_override is not None else state)
        return {'needsRedeployment': True}

    def deploy(self, workflow):
        self.calls.append(('deploy', workflow))
        return {'activeDeployment': {'deploymentVersionId': 'deployment-1'}, 'version': 1}

    def get_version(self, workflow, revision):
        self.calls.append(('version', revision))
        return {'id': 'deployment-1', 'version': 1, 'isActive': True,
                'state': copy.deepcopy(self.version_override if self.version_override is not None else self.states[workflow])}

    def count(self, kind):
        return sum(item[0] == kind for item in self.calls)


class AuthoringAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tasks = TaskStore.initialize(self.root/'tasks.sqlite')
        self.journal = SimJournal.initialize(self.root/'sim.sqlite')
        self.journal.mutation_guard = self.tasks.workflow_admission
        self.remote = FakeSimAuthoringClient()
        self.now = 100
        self.credentials = {
            'edit': TrustedPrincipal('editor', frozenset({'one'}), frozenset({'edit_workflows'}), 200),
            'publish': TrustedPrincipal('publisher', frozenset({'one'}), frozenset({'edit_workflows', 'publish_workflows'}), 200),
            'other': TrustedPrincipal('other', frozenset({'two'}), frozenset({'edit_workflows'}), 200)}
        self.access = AuthoringAccess(self.credentials, clock=lambda: self.now)
        self.access.reject_service_tokens(['service-token'])
        self.projects = {'one': {'workspace': str(self.root)}, 'two': {'workspace': str(self.root)}}
        self.open_app()

    def open_app(self):
        self.api = AuthoringAPI(self.access, self.journal, {'one': self.remote, 'two': self.remote}, projects=self.projects)
        app = create_app(self.tasks, ServiceConfig(tokens={'service-token': {'one', 'two'}},
            projects=self.projects, bindings={}), authoring=self.api)
        self.http = TestClient(app); self.addCleanup(self.http.close)

    def request(self, method, path, *, token='edit', body=None):
        headers = {} if token is None else {'Authorization': 'Bearer '+token}
        return self.http.request(method, '/extension/v1/authoring'+path, headers=headers, json=body)

    def create(self, op='create-1', project='one', token='edit', name='中文流程'):
        return self.request('POST', f'/projects/{project}/workflows', token=token,
                            body={'operationId': op, 'name': name, 'description': ''})

    def owned(self, doc=None):
        response = self.create(); self.assertEqual(response.status_code, 202)
        workflow = response.json()['receipt']['workflowId']
        if doc is not None: self.remote.states[workflow] = compile_document(doc, workflow)
        return workflow

    def save(self, workflow, *, digest=None, doc=None, op='save-1'):
        return self.request('POST', f'/projects/one/workflows/{workflow}/save', body={
            'operationId': op, 'expectedStateSha256': digest or state_hash(self.remote.states[workflow]),
            'document': document() if doc is None else doc})

    def publish(self, workflow, token='publish', op='publish-1'):
        return self.request('POST', f'/projects/one/workflows/{workflow}/publish', token=token,
            body={'operationId': op, 'expectedStateSha256': state_hash(self.remote.states[workflow])})

    def test_anonymous_service_expired_and_revoked_credentials_rejected(self):
        for token in (None, 'service-token'):
            self.assertEqual(self.request('GET', '/projects', token=token).status_code, 401)
        self.now = 201
        self.assertEqual(self.request('GET', '/projects').status_code, 403)
        self.now = 100; self.access.revoke('edit')
        self.assertEqual(self.request('GET', '/projects').status_code, 401)
        self.assertEqual(self.remote.calls, [])

    def test_scope_even_shared_native_workspace_and_no_unowned_draft_exposure(self):
        workflow = self.owned(); self.remote.create('unrelated-native-draft', '')
        response = self.request('GET', '/projects/one/workflows')
        self.assertEqual([x['id'] for x in response.json()['workflows']], [workflow])
        self.assertEqual(self.request('GET', '/projects/two/workflows').status_code, 404)
        self.assertEqual(self.request('GET', '/projects/two/workflows', token='other').json()['workflows'], [])
        before = list(self.remote.calls)
        for path, token in ((f'/projects/two/workflows/{workflow}', 'other'),
                            ('/projects/one/workflows/wf-2', 'edit')):
            self.assertEqual(self.request('GET', path, token=token).status_code, 404)
        self.assertEqual(self.remote.calls, before)

    def test_only_accepted_create_confers_ownership(self):
        self.journal.mutate(namespace('one'), 'lost', 'create', {}, lambda: (_ for _ in ()).throw(TimeoutError()))
        self.remote.details['unowned'] = {'id': 'unowned', 'name': 'native'}
        self.remote.states['unowned'] = {'blocks': {}, 'edges': [], 'variables': {}}
        self.assertEqual(self.request('GET', '/projects/one/workflows/unowned').status_code, 404)
        self.assertEqual(self.save('unowned').status_code, 404)
        self.assertEqual(self.remote.count('save'), 0)

    def test_duplicate_create_payload_conflict_and_reopen_preserves_ownership(self):
        first = self.create(); self.assertEqual(self.create().json(), first.json())
        self.assertEqual(self.create(name='changed').status_code, 409)
        self.assertEqual(self.remote.count('create'), 1)
        self.journal = SimJournal(self.journal.path)
        self.journal.mutation_guard = self.tasks.workflow_admission
        self.open_app()
        self.assertEqual(self.create().json(), first.json())
        workflow = first.json()['receipt']['workflowId']
        self.assertEqual(self.request('GET', f'/projects/one/workflows/{workflow}').status_code, 200)
        self.assertEqual(self.remote.count('create'), 1)
        self.assertFalse(first.json()['safeToResubmit'])

    def test_uncertain_create_never_repeats_or_leaks_upstream_exception(self):
        self.remote.create_error = TimeoutError('TOP_SECRET upstream token')
        first = self.create(); self.assertEqual(first.status_code, 202)
        self.assertEqual(first.json()['status'], 'uncertain')
        self.assertIsNone(first.json()['receipt']); self.assertNotIn('TOP_SECRET', first.text)
        self.assertEqual(self.create().json(), first.json())
        self.assertEqual(self.remote.count('create'), 1)
        self.assertFalse(first.json()['safeToResubmit'])
        self.assertEqual(self.request('GET', '/projects/one/workflows').json()['workflows'], [])

    def test_stale_hash_conflict_has_no_put_and_is_not_retried(self):
        workflow = self.owned()
        response = self.save(workflow, digest='0'*64)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['receipt']['outcome'], 'conflict')
        self.assertEqual(self.remote.count('save'), 0)
        self.assertFalse(response.json()['safeToResubmit'])
        self.assertEqual(self.save(workflow, digest='0'*64).json(), response.json())
        self.assertEqual(self.remote.count('save'), 0)

    def test_valid_save_readback_and_duplicate_receipt(self):
        workflow = self.owned(); digest = state_hash(self.remote.states[workflow])
        saved = self.save(workflow, digest=digest)
        self.assertEqual(saved.status_code, 202)
        self.assertEqual(saved.json()['receipt']['outcome'], 'saved')
        self.assertEqual(self.remote.count('save'), 1)
        self.assertEqual(self.request('GET', f'/projects/one/workflows/{workflow}').json()['document'], document())
        self.assertEqual(self.save(workflow, digest=digest).json(), saved.json())
        self.assertEqual(self.remote.count('save'), 1)

    def test_concurrent_distinct_operations_share_compare_and_write_lock(self):
        workflow = self.owned()
        digest = state_hash(self.remote.states[workflow])
        gate = threading.Barrier(2)
        original = self.remote.save_state
        def slow_save(workflow, state):
            # Widen the read/write race; without the adapter lock both requests
            # observe the old digest and would issue a native PUT.
            time.sleep(.1)
            return original(workflow, state)
        self.remote.save_state = slow_save
        def save_one(index):
            gate.wait(timeout=5)
            return self.save(workflow, digest=digest, op='parallel-'+str(index))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save_one, range(2)))
        self.assertTrue(all(result.status_code == 202 for result in results))
        self.assertCountEqual([r.json()['receipt']['outcome'] for r in results], ['saved', 'conflict'])
        self.assertEqual(self.remote.count('save'), 1)

    def test_unknown_native_graph_cannot_be_overwritten(self):
        workflow = self.owned(document())
        next(iter(self.remote.states[workflow]['blocks'].values()))['type'] = 'unknown_native_node'
        self.assertFalse(self.request('GET', f'/projects/one/workflows/{workflow}').json()['editable'])
        response = self.save(workflow)
        self.assertEqual(response.json()['status'], 'uncertain')
        self.assertEqual(self.remote.count('save'), 0)

    def test_readback_mismatch_never_reports_saved(self):
        workflow = self.owned()
        changed = document(); changed['nodes'][-1]['config']['output'] = {'result': 'different'}
        self.remote.readback_override = compile_document(changed, workflow)
        response = self.save(workflow)
        self.assertEqual(response.json()['receipt']['outcome'], 'readback_mismatch')

    def test_native_variables_without_blocks_are_not_an_empty_owned_draft(self):
        workflow = self.owned()
        self.remote.states[workflow]['variables'] = {'native-secret-variable': {'value': 'preserve'}}
        read = self.request('GET', f'/projects/one/workflows/{workflow}')
        self.assertFalse(read.json()['editable'])
        response = self.save(workflow)
        self.assertNotEqual((response.json().get('receipt') or {}).get('outcome'), 'saved')
        self.assertEqual(self.remote.count('save'), 0)

    def test_publish_requires_capability_and_human_binding_blocks_deploy(self):
        workflow = self.owned(document(human=True))
        self.assertEqual(self.publish(workflow, token='edit').status_code, 403)
        response = self.publish(workflow)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['receipt']['outcome'], 'approval_binding_required')
        self.assertEqual(self.remote.count('deploy'), 0)

    def test_published_version_graph_mismatch_not_published(self):
        workflow = self.owned(document())
        changed = document(); changed['nodes'][-1]['config']['output'] = {'result': 'different'}
        self.remote.version_override = compile_document(changed, workflow)
        response = self.publish(workflow)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['receipt']['outcome'], 'publication_mismatch')
        self.assertIn(('version', 1), self.remote.calls)

    def test_publication_uses_verified_version_and_repeat_does_not_deploy(self):
        workflow = self.owned(document())
        first = self.publish(workflow)
        self.assertEqual(first.json()['receipt']['outcome'], 'published')
        self.assertEqual(first.json()['receipt']['revision'], 1)
        self.assertEqual(first.json()['receipt']['deploymentVersionId'], 'deployment-1')
        self.assertEqual(self.publish(workflow).json(), first.json())
        self.assertEqual(self.remote.count('deploy'), 1)
        self.assertIn(('version', 1), self.remote.calls)

    def test_maintenance_blocks_new_intent_but_old_operation_readable(self):
        first = self.create(); self.tasks.enter_maintenance()
        self.assertEqual(self.create().json(), first.json())
        self.assertEqual(self.create(op='new-op').status_code, 503)
        observed = self.request('GET', '/projects/one/operations/create-1?kind=create')
        self.assertEqual(observed.json(), first.json())
        self.assertEqual(self.remote.count('create'), 1)
        with self.assertRaises(KeyError): self.journal.get(namespace('one'), 'new-op', 'author-create')

    def test_read_errors_do_not_expose_upstream_secret(self):
        workflow = self.owned(); self.remote.read_error = AuthoringError('TOP_SECRET upstream credential')
        response = self.request('GET', f'/projects/one/workflows/{workflow}')
        self.assertEqual(response.status_code, 422)
        self.assertNotIn('TOP_SECRET', response.text)

    def test_decodable_and_empty_drafts_do_not_project_native_waits(self):
        empty = self.owned()
        empty_read = self.request('GET', f'/projects/one/workflows/{empty}')
        self.assertEqual(empty_read.status_code, 200)
        self.assertIsNone(empty_read.json()['nativeWaits'])
        workflow = self.owned(document(human=True))
        read = self.request('GET', f'/projects/one/workflows/{workflow}')
        self.assertEqual(read.status_code, 200)
        self.assertIsNotNone(read.json()['document'])
        self.assertIsNone(read.json()['nativeWaits'])

    def test_native_wait_projection_omits_task_input_values_and_variable_secrets(self):
        task_secret = 'task-input-secret-do-not-project'
        variable_secret = 'variable-secret-do-not-project'
        extra_secret = 'sibling-field-secret-do-not-project'
        workflow = self.owned()
        self.remote.states[workflow] = {
            'blocks': {
                'wait_object': {
                    'id': 'wait_object', 'type': 'human_in_the_loop_v2', 'name': '等待确认',
                    'subBlocks': {'inputFormat': {'id': 'inputFormat', 'type': 'input-format', 'value': [
                        {'id': 'task', 'name': 'taskInput', 'type': 'object', 'value': task_secret},
                        {'id': 'note', 'name': 'note', 'type': 'string', 'value': extra_secret},
                    ]}},
                },
                'wait_string': {
                    'id': 'wait_string', 'type': 'human_in_the_loop_v2', 'name': '文本等待',
                    'subBlocks': {'inputFormat': {'value': [
                        {'name': 'taskInput', 'type': 'string', 'value': task_secret},
                    ]}},
                },
                'wait_plain': {
                    'id': 'wait_plain', 'type': 'human_in_the_loop_v2', 'name': '人工确认',
                    'subBlocks': {'inputFormat': {'value': [
                        {'name': 'approved', 'type': 'boolean', 'value': True},
                    ]}},
                },
                'mystery': {'id': 'mystery', 'type': 'unknown_native_node', 'name': '不可解码'},
            },
            'edges': [],
            'variables': {'apiToken': {'value': variable_secret}},
        }
        read = self.request('GET', f'/projects/one/workflows/{workflow}')
        self.assertEqual(read.status_code, 200, read.text)
        body = read.json()
        self.assertIsNone(body['document'])
        self.assertFalse(body['editable'])
        self.assertEqual(body['nativeWaits'], [
            {'id': 'wait_object', 'name': '等待确认', 'taskInput': 'object'},
            {'id': 'wait_plain', 'name': '人工确认', 'taskInput': None},
            {'id': 'wait_string', 'name': '文本等待', 'taskInput': 'string'},
        ])
        self.assertNotIn(task_secret, read.text)
        self.assertNotIn(variable_secret, read.text)
        self.assertNotIn(extra_secret, read.text)
        # A second taskInput is ambiguous even when its type is unsupported.
        fields = self.remote.states[workflow]['blocks']['wait_object']['subBlocks']['inputFormat']['value']
        for kind in ('boolean', 'object'):
            with self.subTest(duplicate_type=kind):
                fields.append({'name': ' taskInput ', 'type': kind, 'value': task_secret})
                duplicate = self.request('GET', f'/projects/one/workflows/{workflow}')
                self.assertEqual(duplicate.status_code, 200)
                wait = next(item for item in duplicate.json()['nativeWaits'] if item['id'] == 'wait_object')
                self.assertIsNone(wait['taskInput'])
                self.assertNotIn(task_secret, duplicate.text)
                fields.pop()
