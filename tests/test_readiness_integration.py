"""Admission uses current trusted evidence; history never needs a new execution."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from agent_line.dispatcher import Dispatcher
from agent_line.execution_runtime import BoundRoleGateway
from agent_line.service import ServiceConfig, create_app
from agent_line.store import TaskStore
from agent_line.task_discovery import ExternalTaskDiscovery


class Readiness:
    def __init__(self, confirmed=True):
        self.confirmed = confirmed
        self.reason = None if confirmed else '执行验收证据尚未通过，请先核对当前部署。'
        self.stale = False

    def assert_current(self):
        if not self.confirmed or self.stale:
            raise ValueError('execution_readiness_stale')


class ReadinessIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'workspace'; self.workspace.mkdir()
        self.evidence = self.root / 'evidence'; self.evidence.mkdir(mode=0o700)
        self.store = TaskStore.initialize(self.root / 'tasks.sqlite')
        self.readiness = Readiness()
        self.projects = {'project': {'workspace': str(self.workspace)}}
        self.bindings = {'binding': {'projectId': 'project', 'roleAgentId': 'role',
                                     'roleRevision': 'qwenpaw-role-v1:' + 'a' * 64}}
        self.body = {'schemaVersion': '1.0', 'idempotencyKey': 'new',
            'workflow': {'id': 'wf', 'revision': '1', 'runId': 'run', 'nodeId': 'node',
                         'nodeExecutionId': 'activation', 'attemptId': '1'},
            'projectId': 'project', 'roleBindingId': 'binding', 'input': {},
            'memoryPolicyId': 'native', 'outputSchemaId': 'task-result'}
        self.headers = {'Authorization': 'Bearer test-owner'}

    def client(self):
        return TestClient(create_app(self.store, ServiceConfig(tokens={'test-owner': {'project'}},
            projects=self.projects, bindings=self.bindings,
            controlled_execution='confirmed' if self.readiness.confirmed else 'unknown'),
            execution_readiness=self.readiness))

    def test_confirmed_evidence_allows_registration_using_server_role_and_workspace(self):
        with self.client() as client:
            self.assertTrue(client.get('/extension/v1/health', headers=self.headers).json()['readyForExecution'])
            response = client.post('/extension/v1/node-tasks', headers=self.headers, json=self.body)
        self.assertEqual(response.status_code, 202)
        task = self.store.get(response.json()['taskId'])
        self.assertEqual(task['payload']['roleAgentId'], 'role')
        self.assertEqual(task['payload']['workspace'], str(self.workspace))
        self.assertEqual(task['executionStatus'], 'registered')

    def test_evidence_never_removes_original_maintenance_guard(self):
        self.store.enter_maintenance()
        before = self.store.maintenance_status()
        with self.client() as client:
            health = client.get('/extension/v1/health', headers=self.headers).json()
            self.assertEqual(health['controlledExecution'], 'confirmed')
            self.assertFalse(health['readyForExecution'])
            response = client.post('/extension/v1/node-tasks', headers=self.headers, json=self.body)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.store.maintenance_status(), before)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM tasks').fetchone()[0], 0)

    def test_stale_live_evidence_disables_new_registration_but_preserves_history(self):
        with self.client() as client:
            response = client.post('/extension/v1/node-tasks', headers=self.headers, json=self.body)
            task_id = response.json()['taskId']; before = self.store.get(task_id)
            events = self.store.events(task_id)
            self.readiness.stale = True
            health = client.get('/extension/v1/health', headers=self.headers).json()
            self.assertEqual(health['controlledExecution'], 'unknown')
            self.assertFalse(health['readyForExecution'])
            self.assertIn('失效', health['executionReadinessReason'])
            rejected = client.post('/extension/v1/node-tasks', headers=self.headers,
                json={**self.body, 'idempotencyKey': 'next'})
            self.assertEqual(rejected.status_code, 503)
            self.assertEqual(client.get('/extension/v1/node-tasks/' + task_id, headers=self.headers).status_code, 200)
        self.assertEqual(self.store.get(task_id), before)
        self.assertEqual(self.store.events(task_id), events)

    def test_invalid_proof_reason_is_visible_without_bypassing_auth_or_project_scope(self):
        self.readiness.confirmed = False; self.readiness.reason = '执行验收证据尚未通过，请先核对当前部署。'
        with self.client() as client:
            self.assertEqual(client.get('/extension/v1/health').status_code, 401)
            health = client.get('/extension/v1/health', headers=self.headers).json()
            self.assertEqual(health['executionReadinessReason'], self.readiness.reason)
            response = client.post('/extension/v1/node-tasks', headers=self.headers,
                                   json={**self.body, 'projectId': 'unauthorized'})
            self.assertEqual(response.status_code, 403)

    def bound_gateway(self):
        native = Mock()
        native.ledger.get.side_effect = KeyError
        gateway = BoundRoleGateway('project', 'binding', 'role', str(self.workspace),
            'install', native, Mock(), self.bindings['binding']['roleRevision'], self.readiness.assert_current)
        return gateway, native

    def test_stale_preflight_blocks_before_claim_and_records_bounded_diagnostic(self):
        gateway, native = self.bound_gateway()
        payload = {k: v for k, v in self.body.items() if k != 'idempotencyKey'}
        payload.update(roleAgentId='role', workspace=str(self.workspace),
                       roleRevision=self.bindings['binding']['roleRevision'])
        task, _ = self.store.register('new', payload)
        self.readiness.stale = True
        dispatcher = Dispatcher(self.store, {('project', 'binding'): gateway})
        for _ in range(2):
            result = dispatcher.submit(task['id'])
            self.assertEqual(result['executionStatus'], 'registered')
            self.assertEqual(result['submissionBlock'], 'execution_readiness_stale')
        native.preflight.assert_not_called(); native.submit.assert_not_called()
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM workspace_leases').fetchone()[0], 0)
        self.assertEqual(sum(e['kind'] == 'execution_readiness_stale'
                             for e in self.store.events(task['id'])), 1)

    def test_historical_identity_still_observes_when_evidence_is_stale(self):
        gateway, native = self.bound_gateway(); self.readiness.stale = True
        native.ledger.get.side_effect = None; native.ledger.get.return_value = {'taskId': 'old'}
        native.observe.return_value = {'status': 'unknown', 'identity': {'upstreamTaskId': 'original'}}
        self.assertEqual(gateway.observe('old')['identity']['upstreamTaskId'], 'original')
        gateway._check_role_revision(task_id='old', role_revision=None)
        native.preflight.assert_not_called(); native.submit.assert_not_called()

    def test_discovery_does_not_read_input_or_register_tasks_after_proof_expires(self):
        discovery = object.__new__(ExternalTaskDiscovery)
        discovery.projects = self.projects; discovery.role_bindings = self.bindings
        discovery.allow_submissions = True; discovery.execution_readiness = self.readiness
        discovery.wait_client = Mock(); self.readiness.stale = True
        policy = {'roleBindingId': 'binding', 'roleAgentId': 'role',
                  'roleRevision': self.bindings['binding']['roleRevision']}
        result = discovery._bind_task({'projectId': 'project', 'agentPolicies': {'node': policy}},
                                     {'blockedOnBlockId': 'node'}, verified=True)
        self.assertEqual(result['status'], 'external_task_admission_closed')
        discovery.wait_client.read_input.assert_not_called()

    def test_runtime_only_uses_evaluated_proof_and_keeps_boolean_switch_forbidden(self):
        from agent_line.automatic_verification import AutomaticVerifier
        from agent_line.runtime import load_app
        gateway, native = self.bound_gateway()
        gateway = replace(gateway, readiness_check=None)
        config = {'schemaVersion': 1, 'database': str(self.store.path),
            'projects': self.projects, 'bindings': self.bindings,
            'tokens': {'test-owner': ['project']}, 'executionFile': 'execution.json',
            'verification': {'evidenceDirectory': str(self.evidence)}, 'readinessFile': 'readiness.json'}
        path = self.root / 'service.json'; path.write_text(json.dumps(config)); path.chmod(0o600)
        verifier = AutomaticVerifier(self.store, self.evidence)
        with patch('agent_line.execution_runtime.load_execution_gateways', return_value={('project', 'binding'): gateway}), \
                patch('agent_line.verification_runtime.load_verifier', return_value=verifier), \
                patch('agent_line.execution_readiness.evaluate_readiness', return_value=self.readiness) as evaluate:
            app = load_app(path)
            self.assertIs(app.state.execution_readiness, self.readiness)
            self.assertTrue(app.state.execution_worker.loop.allow_submissions)
            self.assertEqual(evaluate.call_args.kwargs['tasks'].path, self.store.path)
            self.assertEqual(evaluate.call_args.kwargs['verification_directory'], self.evidence)
        native.submit.assert_not_called()
        config['controlled_execution'] = 'confirmed'; path.write_text(json.dumps(config))
        with self.assertRaises(ValueError): load_app(path)


if __name__ == '__main__': unittest.main()
