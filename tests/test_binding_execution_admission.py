"""New registrations require their actual assembled execution binding."""
from pathlib import Path
from dataclasses import replace
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent_line.dispatcher import Dispatcher
from agent_line.service import ServiceConfig, create_app
from agent_line.store import TaskStore


class IdleGateway:
    def __init__(self, workspace, role='native-role'):
        self.workspace, self.role = workspace, role
        self.calls = []

    def submit(self, **kwargs):
        self.calls.append('submit')
        raise AssertionError('Registration must never execute')


class BindingExecutionAdmissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / 'workspace'; self.workspace.mkdir()
        self.store = TaskStore.initialize(self.root / 'tasks.sqlite')
        self.gateway = IdleGateway(self.workspace)
        self.config = ServiceConfig(tokens={'owner': {'project'}, 'other': {'other'}},
            projects={'project': {'workspace': str(self.workspace)}},
            bindings={'active': {'projectId': 'project', 'roleAgentId': 'native-role'},
                      'legacy': {'projectId': 'project', 'roleAgentId': 'old-native-role'},
                      'disabled': {'projectId': 'project', 'roleAgentId': 'native-role', 'enabled': False}},
            controlled_execution='confirmed')
        self.body = {'schemaVersion': '1.0', 'idempotencyKey': 'original-registration',
            'workflow': {'id': 'workflow', 'revision': '1', 'runId': 'original-run', 'nodeId': 'node',
                         'nodeExecutionId': 'original-activation', 'attemptId': '1'},
            'projectId': 'project', 'roleBindingId': 'active', 'input': {'request': 'test'},
            'memoryPolicyId': 'native-role', 'outputSchemaId': 'task-result'}
        self.dispatcher = Dispatcher(self.store, {('project', 'active'): self.gateway})

    def open(self, dispatcher=True):
        http = TestClient(create_app(self.store, self.config,
                                    dispatcher=self.dispatcher if dispatcher else None))
        self.addCleanup(http.close)
        return http

    def register(self, http, body=None, token='owner'):
        headers = {} if token is None else {'Authorization': 'Bearer ' + token}
        return http.post('/extension/v1/node-tasks', headers=headers, json=body or self.body)

    def assert_empty(self):
        with self.store.connect() as db:
            for table in ('tasks', 'events', 'workspace_leases'):
                self.assertEqual(db.execute('SELECT count(*) FROM ' + table).fetchone()[0], 0)
        self.assertEqual(self.gateway.calls, [])

    def assert_denied(self, http, body=None):
        response = self.register(http, body)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertNotIn(str(self.root), response.text)
        self.assertEqual(response.json()['detail'], '此角色尚未接通可用的执行服务，暂不接受新任务')
        self.assert_empty()

    def test_matching_actual_gateway_registers_trusted_role_without_execution_or_lease(self):
        response = self.register(self.open())
        self.assertEqual(response.status_code, 202)
        task = self.store.get(response.json()['taskId'])
        self.assertEqual(task['payload']['roleAgentId'], 'native-role')
        self.assertEqual(task['payload']['workspace'], str(self.workspace))
        self.assertEqual(task['executionStatus'], 'registered')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM workspace_leases').fetchone()[0], 0)
        self.assertEqual(self.gateway.calls, [])

    def test_missing_gateway_never_registers_legacy_binding(self):
        self.assert_denied(self.open(), {**self.body, 'roleBindingId': 'legacy'})

    def test_empty_actual_dispatcher_never_registers_new_request(self):
        self.dispatcher.gateways = {}
        self.assert_denied(self.open())

    def test_explicit_tuple_registry_never_falls_back_to_project_gateway(self):
        self.dispatcher.gateways = {('project', 'another-binding'): self.gateway, 'project': self.gateway}
        self.assert_denied(self.open())

    def test_role_mismatch_is_fixed_503_without_native_execution(self):
        self.gateway.role = 'different-private-role'
        self.assert_denied(self.open())

    def test_workspace_mismatch_is_fixed_503_without_leaking_path(self):
        self.gateway.workspace = self.root / 'different-private-workspace'
        self.assert_denied(self.open())

    def test_project_gateway_still_works_without_explicit_tuple_registry(self):
        self.dispatcher.gateways = {'project': self.gateway}
        self.assertEqual(self.register(self.open()).status_code, 202)
        self.assertEqual(self.gateway.calls, [])

    def test_auth_scope_disabled_binding_and_capability_checks_precede_gateway_lookup(self):
        http = self.open()
        with patch.object(self.dispatcher, '_gateway', wraps=self.dispatcher._gateway) as lookup:
            for token, body, status in ((None, self.body, 401), ('invalid', self.body, 401),
                ('other', self.body, 403), ('owner', {**self.body, 'roleBindingId': 'disabled'}, 403),
                ('owner', {**self.body, 'roleBindingId': 'missing'}, 403)):
                self.assertEqual(self.register(http, body, token).status_code, status)
            lookup.assert_not_called()
        self.config = replace(self.config, controlled_execution='unknown')
        with patch.object(self.dispatcher, '_gateway', wraps=self.dispatcher._gateway) as lookup:
            self.assertEqual(self.register(self.open()).status_code, 503)
            lookup.assert_not_called()
        self.assert_empty()

    def test_exact_history_replays_during_maintenance_with_gateway_removed_without_new_writes(self):
        http = self.open()
        first = self.register(http); self.assertEqual(first.status_code, 202)
        task_id = first.json()['taskId']
        task_before, events_before = self.store.get(task_id), self.store.events(task_id)
        self.dispatcher.gateways = {}
        self.store.enter_maintenance()
        replay = self.register(http)
        self.assertEqual(replay.status_code, 202)
        self.assertEqual(replay.json()['taskId'], task_id)
        self.assertTrue(replay.json()['reusedExistingTask'])
        self.assertEqual(self.store.get(task_id), task_before)
        self.assertEqual(self.store.events(task_id), events_before)
        conflict = self.register(http, {**self.body, 'input': {'request': 'different'}})
        self.assertEqual(conflict.status_code, 409)
        new = self.register(http, {**self.body, 'idempotencyKey': 'new-registration',
            'workflow': {**self.body['workflow'], 'nodeExecutionId': 'new-activation'}})
        self.assertEqual(new.status_code, 503)
        self.assertEqual(self.gateway.calls, [])

    def test_trusted_standalone_registration_contract_remains_available(self):
        self.assertEqual(self.register(self.open(dispatcher=False)).status_code, 202)


if __name__ == '__main__':
    unittest.main()
