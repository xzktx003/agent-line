"""Machine runs use frozen local intent before touching Sim."""
import uuid
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi.testclient import TestClient
from agent_line.adapters.sim import SubmissionUncertain
from agent_line.approval_discovery import WorkflowRunLedger
from agent_line.pause_bindings import PauseBindings
from agent_line.publication import PublicationUnverified
from agent_line.service import ServiceConfig, create_app
from agent_line.sim_journal import SimJournal
from agent_line.store import TaskStore, canonical
from agent_line.workflows import WorkflowGateway


class MachineWorkflowGatewayTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.tasks = TaskStore.initialize(self.root / 'tasks.sqlite')
        self.runs = WorkflowRunLedger.initialize(self.root / 'runs.sqlite')
        self.pauses = PauseBindings.initialize(self.root / 'pauses.sqlite')
        self.journal = SimJournal.initialize(self.root / 'journal.sqlite')
        self.sim = Mock()
        self.events = []
        self.policy = dict(roleBindingId='role', roleAgentId='parent', instruction='Implement task',
                           memoryPolicyId='native', outputSchemaId='artifact',
                           requireMemoryCommit=False, allowFailureBranch=False)
        external = dict(revision='1', deploymentVersionId='deployment',
                        agentPolicies={'wait': self.policy}, ttlSeconds=600)
        self.gateway = WorkflowGateway(self.sim, self.journal,
            {'flow': dict(projectId='project', workflowId='workflow', externalTasks=external)},
            installation_id='test-installation')
        self.flow = SimpleNamespace(runs=self.runs, pauses=self.pauses, workers=(),
                                   discovery=SimpleNamespace(allow_submissions=False))
        self.gateway.task_flow = self.flow
        self.gateway.publication_verifier = Mock()
        self.gateway.publication_verifier.check_definition.side_effect = lambda *a, **k: self.events.append('proof')
        def execute(workflow, run, payload):
            self.assertEqual(self.events, ['proof'])
            frozen = WorkflowRunLedger(self.runs.path).get(workflow, run)
            self.assertEqual(frozen['agentPolicies']['wait'], self.policy)
            self.assertEqual(frozen['purpose'], 'external_task_result')
            self.events.append('execute')
            return {'accepted': True}
        self.sim.execute.side_effect = execute
        config = ServiceConfig(tokens={'key': {'project'}},
            projects={'project': {'workspace': str(self.root)}}, bindings={}, controlled_execution='confirmed')
        self.app = create_app(self.tasks, config, workflows=self.gateway, task_flow=self.flow)
        self.client = TestClient(self.app)
        self.headers = {'Authorization': 'Bearer key'}
        self.url = '/extension/v1/workflows/flow/runs'

    def start(self):
        return self.client.post(self.url, headers=self.headers,
                                json={'idempotencyKey': 'trigger', 'input': {'value': 1}})

    def test_proof_and_durable_reservation_precede_execute_and_replay(self):
        first = self.start(); self.assertEqual(first.status_code, 202, first.text)
        second = self.start(); self.assertEqual(second.status_code, 202, second.text)
        self.assertEqual(first.json()['runId'], second.json()['runId'])
        self.assertEqual(self.events, ['proof', 'execute'])
        self.assertEqual(self.sim.execute.call_count, 1)

    def test_changed_policy_is_conflict(self):
        self.assertEqual(self.start().status_code, 202)
        self.gateway.bindings['flow']['externalTasks']['agentPolicies']['wait']['instruction'] = 'changed'
        self.assertEqual(self.start().status_code, 409)
        self.assertEqual(self.sim.execute.call_count, 1)

    def test_frozen_machine_run_forbids_raw_resume_after_config_removed(self):
        run = self.start().json()['runId']
        del self.gateway.bindings['flow']['externalTasks']
        result = self.client.post(self.url + '/' + run + '/resume', headers=self.headers,
                                 json={'contextId': 'wait', 'input': {}})
        self.assertEqual(result.status_code, 403, result.text)
        self.sim.resume.assert_not_called()

    def test_missing_flow_or_verifier_never_executes(self):
        for field in ('task_flow', 'publication_verifier'):
            original = getattr(self.gateway, field); setattr(self.gateway, field, None)
            self.assertEqual(self.start().status_code, 503)
            setattr(self.gateway, field, original)
        self.sim.execute.assert_not_called()
        with self.journal.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM operations').fetchone()[0], 0)

    def test_maintenance_prevents_reservation_and_remote_write(self):
        self.tasks.enter_maintenance()
        self.assertEqual(self.start().status_code, 503)
        self.sim.execute.assert_not_called()
        with self.runs.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM runs').fetchone()[0], 0)

    def test_uncertain_reopen_never_resubmits(self):
        self.sim.execute.side_effect = SubmissionUncertain('timeout')
        first = self.start(); self.assertEqual(first.status_code, 202, first.text)
        self.gateway.journal = SimJournal(self.journal.path)
        again = self.start()
        self.assertEqual(again.status_code, 202, again.text)
        self.assertFalse(again.json()['safeToResubmit'])
        self.assertEqual(self.sim.execute.call_count, 1)

    def test_crashed_intent_reopen_never_resubmits(self):
        run = str(uuid.uuid5(uuid.NAMESPACE_URL, canonical(
            ['test-installation', 'project', 'workflow', 'trigger'])))
        policy = self.gateway.bindings['flow']['externalTasks']
        self.runs.reserve_tasks(project_id='project', workflow_id='workflow', run_id=run,
            revision='1', deployment_version_id='deployment',
            agent_policies=policy['agentPolicies'], ttl_seconds=600)
        self.sim.execute.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.journal.execute(self.sim, 'workflow', run, {'value': 1})
        self.gateway.journal = SimJournal(self.journal.path)
        self.assertEqual(self.start().status_code, 202)
        self.assertEqual(self.sim.execute.call_count, 1)

    def test_unverified_publication_never_reserves_or_executes(self):
        self.gateway.publication_verifier.check_definition.side_effect = PublicationUnverified('unavailable')
        self.assertEqual(self.start().status_code, 503)
        self.sim.execute.assert_not_called()
        with self.runs.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM runs').fetchone()[0], 0)

    def test_changed_frozen_ttl_is_conflict(self):
        self.assertEqual(self.start().status_code, 202)
        self.gateway.bindings['flow']['externalTasks']['ttlSeconds'] = 601
        self.assertEqual(self.start().status_code, 409)
        self.assertEqual(self.sim.execute.call_count, 1)
