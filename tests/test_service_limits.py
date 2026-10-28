"""Authenticated limits projection preserves unknown usage and project scopes."""
import copy
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
from agent_line.service import ServiceConfig, create_app
from agent_line.store import TaskStore


class ServiceLimitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = TaskStore.initialize(Path(self.tmp.name) / 'tasks.sqlite')
        config = ServiceConfig(tokens={'owner': {'a'}, 'other': {'b'}},
            projects={'a': {'workspace': self.tmp.name}},
            bindings={'dev': {'projectId': 'a', 'roleAgentId': 'role'}},
            controlled_execution='confirmed')
        self.client = TestClient(create_app(self.store, config))
        self.headers = {'Authorization': 'Bearer owner'}
        self.body = {'schemaVersion': '1.0', 'idempotencyKey': 'key', 'projectId': 'a',
            'roleBindingId': 'dev', 'input': {}, 'memoryPolicyId': 'memory', 'outputSchemaId': 'task-result',
            'workflow': {'id': 'wf', 'revision': 'rev', 'runId': 'run', 'nodeId': 'dev',
                         'nodeExecutionId': 'activation', 'attemptId': 'one'},
            'limits': {'maxExecutionSeconds': 10, 'maxBusinessAttempts': 1}}

    def register(self):
        result = self.client.post('/extension/v1/node-tasks', headers=self.headers, json=self.body)
        self.assertEqual(result.status_code, 202)
        return result.json()['taskId']

    def test_read_only_projection_and_unknown_usage_with_scope_enforcement(self):
        task_id = self.register()
        before = self.store.events(task_id)
        response = self.client.get('/extension/v1/node-tasks/' + task_id, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        limits = response.json()['executionLimits']
        self.assertEqual(limits['attemptsUsed'], 0)
        self.assertEqual(limits['maxBusinessAttempts'], 1)
        self.assertEqual(limits['limitSeconds'], 10)
        self.assertIsNone(limits['deadlineAt'])
        self.assertEqual(limits['usage'], {'tokens': None, 'modelCalls': None, 'cost': None})
        self.assertNotIn(self.tmp.name, str(limits))
        self.assertEqual(self.store.events(task_id), before)
        hidden = self.client.get('/extension/v1/node-tasks/' + task_id, headers={'Authorization': 'Bearer other'})
        self.assertEqual(hidden.status_code, 404)
        self.assertEqual(self.client.get('/extension/v1/node-tasks/' + task_id).status_code, 401)

    def test_retry_cannot_change_frozen_limit_or_create_another_task(self):
        task_id = self.register()
        self.body['idempotencyKey'] = 'second'
        self.body['workflow']['attemptId'] = 'two'
        self.body['workflow']['nodeExecutionId'] = 'activation-two'
        self.body['limits']['maxBusinessAttempts'] = 100
        response = self.client.post('/extension/v1/node-tasks', headers=self.headers, json=self.body)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.store.list_tasks({'a'})['tasks'][0]['id'], task_id)
        self.assertEqual(len(self.store.list_tasks({'a'})['tasks']), 1)

    def test_invalid_limits_rejected_before_registration(self):
        for invalid in [True, 0, 101, '3', None]:
            body = copy.deepcopy(self.body)
            body['limits']['maxBusinessAttempts'] = invalid
            response = self.client.post('/extension/v1/node-tasks', headers=self.headers, json=body)
            self.assertEqual(response.status_code, 422)
        self.assertEqual(self.store.list_tasks({'a'})['tasks'], [])
