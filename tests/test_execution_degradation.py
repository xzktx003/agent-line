"""An incompatible remote keeps authenticated history without background writes."""
import json
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent_line.execution_runtime import load_execution_gateways
from agent_line.approvals import ApprovalLedger
from agent_line.approval_discovery import WorkflowRunLedger
from agent_line.pause_bindings import PauseBindings
from agent_line.role_dispatch import RoleDispatchLedger
from agent_line.runtime import load_app
from agent_line.service import create_app
from agent_line.sim_journal import SimJournal
from agent_line.store import TaskStore
from tests import test_memory_deletion_api as memory_fixtures


class ExecutionDegradationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.calls = []
        self.descriptor_status = 200
        self.descriptor = {'schemaVersion': 999, 'private': 'remote-private-value'}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def respond(self, status, body):
                encoded = json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def do_POST(self):
                owner.calls.append(('POST', self.path))
                self.rfile.read(int(self.headers.get('Content-Length', 0)))
                if self.path == '/api/auth/login':
                    self.respond(200, {'access_token': 'temporary-host-token'})
                else:
                    self.respond(500, {'detail': 'Unexpected mutation'})

            def do_GET(self):
                owner.calls.append(('GET', self.path))
                if self.path == '/api/agent-line/execution-binding':
                    self.respond(owner.descriptor_status, owner.descriptor)
                elif self.path.startswith('/api/v2/workflows/workflow/runs/old-run'):
                    self.respond(200, {'data': {'workflowId': 'workflow',
                        'runId': 'old-run', 'status': 'completed', 'output': None}})
                else:
                    self.respond(404, {})

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.shutdown)
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)
        self.store = TaskStore.initialize(self.root / 'tasks.sqlite')
        self.payload = {'projectId': 'project', 'roleAgentId': 'role',
            'roleBindingId': 'binding', 'workspace': str(self.workspace),
            'workflow': {'id': 'workflow', 'revision': '1', 'runId': 'old-run',
                'nodeId': 'node', 'nodeExecutionId': 'activation', 'attemptId': '1'},
            'input': {}, 'memoryPolicyId': 'none', 'outputSchemaId': 'result'}
        self.task, _ = self.store.register('old', self.payload)
        self.store.claim(self.task['id'], self.workspace)
        self.before = self.store.get(self.task['id'])
        self.events = self.store.events(self.task['id'])
        RoleDispatchLedger.initialize(self.root / 'roles.sqlite')
        self.private('login.json', {'username': 'fixture', 'password': 'fixture-only'})
        (self.root / 'token').write_text('fixture-service-token-' * 3)
        (self.root / 'token').chmod(0o600)
        self.entry = {'projectId': 'project', 'roleBindingId': 'binding',
            'roleAgentId': 'role', 'workspace': str(self.workspace),
            'installationId': 'fixture-installation', 'ledgerPath': str(self.root / 'roles.sqlite'),
            'baseURL': self.base, 'hostCredentialsFile': str(self.root / 'login.json'),
            'serviceTokenFile': str(self.root / 'token'), 'memoryQuery': 'fixture-only'}
        self.private('execution.json', {'schemaVersion': 1, 'gateways': [self.entry]})
        self.projects = {'project': {'workspace': str(self.workspace)}}
        self.bindings = {'binding': {'projectId': 'project', 'roleAgentId': 'role'}}
        self.config = {'schemaVersion': 1, 'database': 'tasks.sqlite',
            'tokens': {'fixture-service': ['project']}, 'projects': self.projects,
            'bindings': self.bindings, 'executionFile': 'execution.json'}
        self.headers = {'Authorization': 'Bearer fixture-service'}
        self.write_config()

    def private(self, filename, value):
        path = self.root / filename
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    def write_config(self):
        self.private('service.json', self.config)

    def app(self):
        return load_app(self.root / 'service.json')

    def test_remote_version_failure_has_distinct_bounded_exception(self):
        from agent_line.execution_runtime import ExecutionCompatibilityError
        with self.assertRaises(ExecutionCompatibilityError) as caught:
            load_execution_gateways(self.root / 'execution.json',
                projects=self.projects, bindings=self.bindings)
        self.assertIsInstance(caught.exception, ValueError)
        self.assertEqual(caught.exception.code, 'binding_incompatible')
        self.assertNotIn('remote-private-value', str(caught.exception))
        self.assertEqual(self.calls, [('POST', '/api/auth/login'),
            ('GET', '/api/agent-line/execution-binding')])

    def test_incompatible_host_preserves_history_and_lease_without_workers(self):
        app = self.app()
        with TestClient(app) as client:
            self.assertEqual(client.get('/extension/v1/health').status_code, 401)
            health = client.get('/extension/v1/health', headers=self.headers)
            self.assertEqual(health.status_code, 200)
            self.assertFalse(health.json()['readyForExecution'])
            self.assertEqual(health.json()['controlledExecution'], 'unsupported')
            self.assertIn('不兼容', health.json()['executionBlockReason'])
            self.assertNotIn('remote-private-value', health.text)
            self.assertIsNone(app.state.execution_worker)
            self.assertIsNone(app.state.approval_discovery_worker)
            self.assertIsNone(app.state.task_flow)
            self.assertEqual(client.get('/extension/v1/node-tasks/' + self.task['id'],
                headers=self.headers).status_code, 200)
            self.assertEqual(client.get('/extension/v1/node-tasks/' + self.task['id'] + '/events',
                headers=self.headers).status_code, 200)
            for method, path in [('POST', '/node-tasks'), ('POST', '/workflows/binding/runs'),
                    ('POST', '/triggers/trigger/webhook'), ('POST', '/authoring/projects/project/workflows'),
                    ('POST', '/node-tasks/' + self.task['id'] + '/cancel')]:
                self.assertEqual(client.request(method, '/extension/v1' + path,
                    headers=self.headers, json={}).status_code, 503, path)
            self.assertEqual(client.post('/extension/v1/node-tasks', json={}).status_code, 401)
        self.assertEqual(self.store.get(self.task['id']), self.before)
        self.assertEqual(self.store.events(self.task['id']), self.events)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM workspace_leases').fetchone()[0], 1)
        self.assertEqual(self.calls, [('POST', '/api/auth/login'),
            ('GET', '/api/agent-line/execution-binding')])

    def test_remote_route_unavailable_is_unknown_and_not_retried(self):
        self.descriptor_status = 404
        with TestClient(self.app()) as client:
            health = client.get('/extension/v1/health', headers=self.headers).json()
            self.assertEqual(health['controlledExecution'], 'unknown')
            self.assertIn('不可用', health['executionBlockReason'])
        self.assertEqual(len(self.calls), 2)

    def test_local_configuration_and_storage_errors_remain_startup_failures(self):
        for error in [ValueError('local config invalid'), FileNotFoundError('local ledger missing')]:
            with self.subTest(error=type(error).__name__), patch(
                    'agent_line.execution_runtime.load_execution_gateways', side_effect=error):
                with self.assertRaises(type(error)):
                    self.app()

    def test_incompatible_remote_does_not_hide_other_invalid_local_configuration(self):
        self.config['verification'] = {'unexpected': 'invalid'}
        self.write_config()
        with self.assertRaises(ValueError):
            self.app()

    def test_all_configured_background_paths_remain_stopped_in_degraded_app(self):
        self.configure_sim()
        ApprovalLedger.initialize(self.root / 'approvals.sqlite')
        WorkflowRunLedger.initialize(self.root / 'runs.sqlite')
        PauseBindings.initialize(self.root / 'pauses.sqlite')
        self.private('approval.json', {'schemaVersion': 1,
            'ledgerPath': str(self.root / 'approvals.sqlite'),
            'runLedgerPath': str(self.root / 'runs.sqlite'), 'credentials': [{
                'token': 'fixture-human-approval-' * 3, 'subject': 'approver',
                'projectScopes': ['project'], 'capabilities': ['approve'],
                'expiresAt': time.time() + 600}]})
        self.private('flow.json', {'schemaVersion': 1,
            'runLedgerPath': str(self.root / 'runs.sqlite'),
            'pauseBindingsPath': str(self.root / 'pauses.sqlite')})
        evidence = self.root / 'evidence'
        evidence.mkdir(mode=0o700)
        self.config.update(approvalFile='approval.json', taskFlowFile='flow.json',
            verification={'evidenceDirectory': str(evidence)})
        self.write_config()
        with patch('agent_line.dispatcher.Dispatcher', side_effect=AssertionError('dispatcher created')), \
                patch('agent_line.worker.ExecutionWorker.start') as execution, \
                patch('agent_line.completion_worker.CompletionWorker.start') as completion, \
                patch('agent_line.approval_runtime.ApprovalDiscoveryWorker.start') as discovery:
            app = self.app()
            with TestClient(app) as client:
                self.assertIn('executionBlockReason', client.get(
                    '/extension/v1/health', headers=self.headers).json())
                self.assertIsNone(app.state.execution_worker)
                self.assertIsNone(app.state.task_flow)
                self.assertIsNone(app.state.approval_discovery_worker)
                self.assertIsNotNone(app.state.verifier)
            execution.assert_not_called()
            completion.assert_not_called()
            discovery.assert_not_called()
        self.assertEqual(self.store.get(self.task['id']), self.before)
        self.assertEqual(self.store.events(self.task['id']), self.events)

    def test_incompatibility_does_not_hide_later_invalid_authority_file(self):
        self.configure_sim()
        self.private('broken-authoring.json', {'schemaVersion': 999})
        self.config['authoringFile'] = 'broken-authoring.json'
        self.write_config()
        with self.assertRaisesRegex(ValueError, 'Invalid authoring deployment'):
            self.app()
        self.assertEqual(self.calls, [('POST', '/api/auth/login'),
            ('GET', '/api/agent-line/execution-binding')])

    def configure_sim(self):
        SimJournal.initialize(self.root / 'sim.sqlite')
        self.private('sim-auth.json', {'baseUrl': self.base, 'apiKey': 'fixture-only-key'})
        self.config['sim'] = {'authFile': 'sim-auth.json', 'journal': 'sim.sqlite',
            'bindings': {'known': {'projectId': 'project', 'workflowId': 'workflow'}},
            'installationId': 'fixture-installation'}

    def test_invalid_workflow_constructor_closes_new_sim_transport_once(self):
        self.configure_sim()
        self.config['sim']['bindings']['known']['workflowId'] = 'invalid/workflow'
        self.write_config()
        from agent_line.adapters.sim import SimClient
        original = SimClient.close
        closed = []

        def close(client):
            closed.append(client)
            original(client)

        with patch.object(SimClient, 'close', close):
            with self.assertRaisesRegex(ValueError, 'Invalid upstream identifier'):
                self.app()
        self.assertEqual(len(closed), 1)
        self.assertTrue(closed[0]._client.is_closed)
        self.assertEqual(self.calls, [])

    def test_workflow_history_and_authoring_auth_survive_incompatibility(self):
        journal = SimJournal.initialize(self.root / 'sim.sqlite')
        with journal.connect() as db:
            db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?)',
                ('workflow', 'old-run', '', '{}', 'accepted',
                 json.dumps({'workflowId': 'workflow', 'runId': 'old-run',
                    'transportStatus': 'accepted', 'verifiedComplete': False}), time.time()))
        self.private('sim-auth.json', {'baseUrl': self.base, 'apiKey': 'fixture-only-key'})
        self.config['sim'] = {'authFile': 'sim-auth.json', 'journal': 'sim.sqlite',
            'bindings': {'known': {'projectId': 'project', 'workflowId': 'workflow'}},
            'installationId': 'fixture-installation'}
        self.private('authoring.json', {'schemaVersion': 1, 'workspaces': {'project': 'workspace'},
            'credentials': [{'token': 'fixture-human-editor-' * 3, 'subject': 'editor',
                'projectScopes': ['project'], 'capabilities': ['edit_workflows', 'publish_workflows'],
                'expiresAt': time.time() + 600}]})
        self.config['authoringFile'] = 'authoring.json'
        self.write_config()
        with TestClient(self.app()) as client:
            result = client.get('/extension/v1/workflows/known/runs/old-run', headers=self.headers)
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()['execution']['status'], 'completed')
            path = '/extension/v1/authoring/projects/project/workflows/workflow/publish'
            self.assertEqual(client.post(path, headers={'Authorization':
                'Bearer ' + 'fixture-human-editor-' * 3}, json={}).status_code, 503)
            self.assertEqual(client.post(path, json={}).status_code, 401)
        self.assertTrue(all(method == 'GET' or path == '/api/auth/login'
            for method, path in self.calls))


class DegradedMemoryReadTests(unittest.TestCase):
    def test_deletion_status_cannot_reconcile_but_memory_history_remains_readable(self):
        fixture = memory_fixtures.MemoryDeletionAPITests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture.native_receipt.update(status='deleting', fileAbsent=False, indexSourceAbsent=False)
        self.assertEqual(fixture.delete().status_code, 202)
        fixture.native_calls.clear()
        # A normal GET would now confirm deletion and project it into TaskStore.
        fixture.native_receipt.update(status='deleted', fileAbsent=True, indexSourceAbsent=True)
        fixture.api.admission = fixture.tasks.workflow_admission

        def snapshot():
            with fixture.commits.connect() as db:
                memory_events = [tuple(row) for row in db.execute('SELECT * FROM memory_events')]
            return {'task': fixture.tasks.get(fixture.task['id']),
                'taskEvents': fixture.tasks.events(fixture.task['id']),
                'commit': fixture.commits.get('role', fixture.task['id']),
                'deletion': fixture.commits.get_deletion('role', fixture.task['id']),
                'review': fixture.reviews.get('role', fixture.task['id']),
                'memoryEvents': memory_events}

        before = snapshot()
        from agent_line.execution_runtime import ExecutionCompatibilityError
        reason = ExecutionCompatibilityError.REASONS['binding_incompatible']
        config = replace(fixture.config, controlled_execution='unsupported', execution_block_reason=reason)
        with TestClient(create_app(fixture.tasks, config, memories=fixture.api)) as client:
            path = '/extension/v1/memories/binding/' + fixture.task['id']
            for suffix in ('/deletion', '/deletion/'):
                for token in ('human-token', 'review-only'):
                    response = client.get(path + suffix,
                        headers={'Authorization': 'Bearer ' + token})
                    self.assertEqual(response.status_code, 503, response.text)
                    self.assertEqual(response.json()['detail'], reason)
                self.assertEqual(client.get(path + suffix).status_code, 401)
                self.assertEqual(client.get(path + suffix,
                    headers={'Authorization': 'Bearer service-token'}).status_code, 401)
            for route in ('/extension/v1/memories', path):
                response = client.get(route, headers={'Authorization': 'Bearer review-only'})
                self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(snapshot(), before)
        self.assertEqual(fixture.native_calls, [])
