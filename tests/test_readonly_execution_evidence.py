"""Trusted plugin policy is an execution fact; model output is never a grant."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from agent_line.dispatcher import Dispatcher
from agent_line.store import Conflict, TaskStore


class ReadonlyExecutionEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = TaskStore.initialize(self.root / 'tasks.sqlite')
        self.task, _ = self.store.register('one', {
            'projectId': 'project', 'roleAgentId': 'reviewer', 'workspace': str(self.root),
            'workflow': {'runId': 'run', 'nodeExecutionId': 'review', 'attemptId': 'first'},
            'input': {'instruction': 'review this task'},
        })
        self.gateway = Mock()
        self.gateway.submit.return_value = {'status': 'accepted', 'identity': {'upstreamTaskId': 'native-task'}}
        self.dispatcher = Dispatcher(self.store, {'project': self.gateway})
        self.dispatcher.submit(self.task['id'])
        self.identity = {'upstreamTaskId': 'native-task', 'codexThreadId': 'thread', 'codexTurnId': 'turn'}
        self.result = {'status': 'succeeded', 'identity': self.identity,
                       'terminalConfirmed': True, 'resourcesReleased': False, 'output': {'text': 'reviewed'}}
        self.controls = {'sandbox': 'read-only', 'approval_policy': 'never'}

    def reconcile(self):
        self.gateway.observe.return_value = self.result
        return self.dispatcher.reconcile(self.task['id'])

    def test_frozen_native_policy_is_visible_in_persisted_task_evidence(self):
        self.result['executionControls'] = self.controls
        task = self.reconcile()
        self.assertIn({'kind': 'execution_policy', 'source': 'qwenpaw-plugin-task-binding',
                       'executionId': 'turn', 'controls': self.controls}, task['evidence'])
        reopened = TaskStore(self.store.path).get(task['id'])
        self.assertEqual(reopened['evidence'], task['evidence'])
        self.controls['sandbox'] = 'danger-full-access'
        self.assertEqual(TaskStore(self.store.path).get(task['id'])['evidence'][1]['controls']['sandbox'], 'read-only')

    def test_model_output_cannot_create_policy_evidence(self):
        self.result['output']['executionControls'] = self.controls
        task = self.reconcile()
        self.assertFalse(any(e['kind'] == 'execution_policy' for e in task['evidence']))

    def test_unrecognized_native_policy_cannot_finish_task(self):
        for controls in (None, {}, {'sandbox': 'danger-full-access'},
                         {'sandbox': 'read-only', 'approval_policy': 'on-request'}):
            with self.subTest(controls=controls):
                self.result['executionControls'] = controls
                with self.assertRaises(Conflict):
                    self.reconcile()
                self.assertNotEqual(self.store.get(self.task['id'])['executionStatus'], 'succeeded')

    def test_readonly_policy_requires_actual_turn_identity(self):
        self.result['executionControls'] = self.controls
        self.result['identity'] = {'upstreamTaskId': 'native-task'}
        with self.assertRaises(Conflict):
            self.reconcile()
        self.assertNotEqual(self.store.get(self.task['id'])['executionStatus'], 'succeeded')
