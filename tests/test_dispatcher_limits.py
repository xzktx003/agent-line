"""Deadline enforcement requests cancellation; it never invents a native exit."""
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock

from agent_line.dispatcher import Dispatcher
from agent_line.store import TaskStore


class DispatcherLimitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = TaskStore.initialize(Path(self.tmp.name) / 'tasks.sqlite')
        self.payload = {
            'projectId': 'project', 'roleAgentId': 'role', 'workspace': self.tmp.name,
            'workflow': {'id': 'workflow', 'revision': 'rev', 'runId': 'run',
                         'nodeId': 'node', 'nodeExecutionId': 'activation', 'attemptId': '1'},
            'limits': {'maxExecutionSeconds': 5, 'maxBusinessAttempts': 2},
            'input': {'instruction': 'fixture'},
        }
        self.task = self.store.register('key', self.payload)[0]
        self.gateway = Mock()
        self.gateway.submit.return_value = {'status': 'accepted', 'identity': {'upstreamTaskId': 'native'}}
        self.gateway.observe.return_value = {'status': 'unknown'}
        self.gateway.cancel.return_value = {'status': 'unknown'}
        self.dispatcher = Dispatcher(self.store, {'project': self.gateway})

    def age_intent(self, seconds):
        with self.store.connect() as db:
            db.execute("UPDATE events SET created_at=? WHERE task_id=? AND kind='submission_intent'",
                       (time.time() - seconds, self.task['id']))

    def test_expired_unknown_requests_cancel_keeps_identity_lease_and_never_resubmits(self):
        self.dispatcher.submit(self.task['id'])
        self.age_intent(20)
        result = self.dispatcher.reconcile(self.task['id'])
        self.assertEqual(result['executionStatus'], 'cancel_requested')
        self.assertFalse(result['resourcesReleased'])
        self.assertEqual(result['executionIdentity'], {'upstreamTaskId': 'native'})
        self.gateway.cancel.assert_called_once_with(self.task['id'])
        restarted = Dispatcher(TaskStore(self.store.path), {'project': self.gateway})
        restarted.reconcile(self.task['id'])
        restarted.submit(self.task['id'])
        self.assertEqual(self.gateway.submit.call_count, 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM workspace_leases').fetchone()[0], 1)

    def test_unexpired_task_is_only_observed(self):
        self.dispatcher.submit(self.task['id'])
        self.age_intent(1)
        self.dispatcher.reconcile(self.task['id'])
        self.gateway.cancel.assert_not_called()

    def test_terminal_receipt_is_applied_before_deadline_cancellation(self):
        self.dispatcher.submit(self.task['id'])
        self.age_intent(20)
        self.gateway.observe.return_value = {
            'status': 'succeeded', 'identity': {'upstreamTaskId': 'native'},
            'terminalConfirmed': True, 'resourcesReleased': True, 'output': {'file': 'retained'},
        }
        result = self.dispatcher.reconcile(self.task['id'])
        self.assertEqual(result['executionStatus'], 'succeeded')
        self.assertEqual(result['output'], {'file': 'retained'})
        self.gateway.cancel.assert_not_called()

    def test_continue_cannot_advance_expired_active_task(self):
        self.dispatcher.submit(self.task['id'])
        self.age_intent(20)
        self.gateway.observe.return_value = {'status': 'running', 'identity': {'upstreamTaskId': 'native'}}
        self.assertFalse(self.dispatcher.verify_continue(self.task['id']))
        self.assertEqual(self.store.get(self.task['id'])['executionStatus'], 'cancel_requested')

    def test_corrupt_deadline_fails_closed_without_canceling_or_releasing(self):
        self.dispatcher.submit(self.task['id'])
        with self.store.connect() as db:
            db.execute("DELETE FROM events WHERE task_id=? AND kind='submission_intent'", (self.task['id'],))
        self.gateway.observe.return_value = {'status': 'running', 'identity': {'upstreamTaskId': 'native'}}
        with self.assertRaises(ValueError):
            self.dispatcher.reconcile(self.task['id'])
        self.assertFalse(self.dispatcher.verify_continue(self.task['id']))
        self.gateway.cancel.assert_not_called()
        self.assertFalse(self.store.get(self.task['id'])['resourcesReleased'])

    def test_wait_in_registration_does_not_start_execution_clock(self):
        with self.store.connect() as db:
            db.execute('UPDATE tasks SET created_at=? WHERE id=?', (time.time() - 1000, self.task['id']))
        self.dispatcher.submit(self.task['id'])
        self.dispatcher.reconcile(self.task['id'])
        self.gateway.cancel.assert_not_called()
