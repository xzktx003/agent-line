"""Cancellation intent survives native terminal results and forbids repair."""
import threading
import unittest
from unittest.mock import Mock

from agent_line.completion_relay import CompletionRelay
from agent_line.store import TaskStore
from tests import test_loop_task_discovery as fixtures


class LoopCancellationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.LoopTaskDiscoveryTests()
        self.fixture.setUp()
        self.fixture.sim.resume.return_value = {
            "workflowId": "workflow", "runId": "run", "contextId": "body_loop0",
            "resumedRunId": "child-body-loop0", "transportStatus": "accepted"}
        self.addCleanup(self.fixture.doCleanups)

    def accepted(self):
        f = self.fixture
        task_id = f.bound()
        identity = {'upstreamTaskId': 'native-' + task_id,
                    'codexThreadId': 'thread-' + task_id, 'codexTurnId': 'turn-' + task_id}
        f.tasks.claim(task_id, str(f.root))
        f.tasks.accept(task_id, identity)
        return task_id, identity

    def finish(self, task_id, identity, status):
        f = self.fixture
        f.tasks.finish(task_id, status, {'nativeResult': status, 'cancellationRequested': False}, [
            {'kind': 'execution_terminal', 'executionId': identity['codexTurnId'], 'confirmed': True}])
        f.tasks.confirm_resources_released(task_id, identity['codexTurnId'])
        if status == 'succeeded':
            f.tasks.record_verification(task_id, {'kind': 'verification', 'taskId': task_id,
                'executionIdentity': identity, 'status': 'failed',
                'checks': [{'status': 'failed', 'detail': 'independent artifact mismatch'}]})
        return f.tasks.get(task_id)

    def test_cancel_then_native_failure_preserves_truth_and_blocks_relay(self):
        f = self.fixture
        task_id, identity = self.accepted()
        self.assertTrue(f.tasks.request_cancel(task_id))
        task = self.finish(task_id, identity, 'failed')
        self.assertEqual(task['executionStatus'], 'failed')
        self.assertTrue(task['resourcesReleased'])
        result = CompletionRelay(f.sim, f.journal).deliver(task, f.pauses.get(task_id))
        self.assertEqual(result, {'status': 'waiting', 'reason': 'task_cancel_requested'})
        f.sim.resume.assert_not_called()
        self.assertIs(task.get('cancellationRequested'), True)
        with self.assertRaises(KeyError):
            f.journal.get('workflow', 'run', 'body_loop0')
        reopened = TaskStore(f.tasks.path).get(task_id)
        self.assertIs(reopened.get('cancellationRequested'), True)
        self.assertEqual(reopened['executionStatus'], 'failed')
        self.assertEqual(reopened['executionIdentity'], identity)

    def test_cancel_then_native_success_verification_failure_does_not_repair(self):
        f = self.fixture
        task_id, identity = self.accepted()
        f.tasks.request_cancel(task_id)
        task = self.finish(task_id, identity, 'succeeded')
        self.assertEqual((task['executionStatus'], task['verificationStatus']), ('succeeded', 'failed'))
        self.assertTrue(task['resourcesReleased'])
        result = CompletionRelay(f.sim, f.journal).deliver(task, f.pauses.get(task_id))
        self.assertEqual(result, {'status': 'waiting', 'reason': 'task_cancel_requested'})
        self.assertIs(TaskStore(f.tasks.path).get(task_id).get('cancellationRequested'), True)
        f.sim.resume.assert_not_called()
        self.assertEqual(f.count(), 1)

    def test_historical_accepted_receipt_cannot_authorize_next_after_cancel(self):
        f = self.fixture
        task_id, identity = self.accepted()
        f.tasks.request_cancel(task_id)
        self.finish(task_id, identity, 'failed')
        # Model the accepted durable record left by the old faulty relay. Native
        # progress is observed; discovery still must refuse a new repair task.
        f.resume(task_id)
        f.iteration = 1
        f.discovery = f.reopen()
        result = f.tick()
        self.assertNotEqual(result['status'], 'external_task_bound', result)
        self.assertEqual(f.count(), 1)
        f.sim.execute.assert_not_called()
        f.sim.resume.assert_not_called()

    def test_committed_cancel_in_other_thread_precedes_native_finish(self):
        f = self.fixture
        task_id, identity = self.accepted()
        committed = threading.Event()
        errors = []

        def cancel():
            try:
                store = TaskStore(f.tasks.path)
                store.request_cancel(task_id)
                committed.set()
            except Exception as exc:
                errors.append(exc)
                committed.set()

        thread = threading.Thread(target=cancel)
        thread.start()
        self.assertTrue(committed.wait(5))
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        task = self.finish(task_id, identity, 'failed')
        self.assertIs(task.get('cancellationRequested'), True)
        result = CompletionRelay(f.sim, f.journal).deliver(task, f.pauses.get(task_id))
        self.assertEqual(result['reason'], 'task_cancel_requested')
        f.sim.resume.assert_not_called()
        f.resume(task_id)  # historical prior acceptance remains insufficient
        f.iteration = 1
        self.assertNotEqual(f.tick()['status'], 'external_task_bound')
        self.assertEqual(f.count(), 1)

    def test_accepted_receipt_replay_is_readonly_despite_later_state_changes(self):
        f = self.fixture
        task_id, identity = self.accepted()
        f.tasks.request_cancel(task_id)
        task = self.finish(task_id, identity, 'failed')
        accepted = f.resume(task_id)  # pre-fix persisted outbound operation
        self.assertEqual(accepted['status'], 'accepted')
        before = f.journal.get('workflow', 'run', 'body_loop0')
        f.tasks.set_memory_status(task_id, 'committed')
        current = TaskStore(f.tasks.path).get(task_id)
        self.assertEqual(current['executionStatus'], task['executionStatus'])
        self.assertIs(current.get('cancellationRequested'), True)
        remote = Mock()
        replay = CompletionRelay(remote, f.journal).deliver(current, f.pauses.get(task_id))
        self.assertEqual(replay, before)
        self.assertEqual(f.journal.get('workflow', 'run', 'body_loop0'), before)
        remote.resume.assert_not_called()
        remote.observe.assert_not_called()
        self.assertEqual(f.count(), 1)

    def test_model_output_cannot_clear_or_create_trusted_cancel_intent(self):
        f = self.fixture
        task_id, identity = self.accepted()
        f.tasks.finish(task_id, 'failed', {'cancellationRequested': True}, [
            {'kind': 'execution_terminal', 'executionId': identity['codexTurnId'], 'confirmed': True}])
        self.assertIs(f.tasks.get(task_id).get('cancellationRequested'), False)

    def test_registered_cancellation_survives_reopen_without_native_execution(self):
        f = self.fixture
        task_id = f.bound()
        self.assertTrue(f.tasks.request_cancel(task_id))
        task = TaskStore(f.tasks.path).get(task_id)
        self.assertEqual(task['executionStatus'], 'canceled')
        self.assertIs(task.get('cancellationRequested'), True)
        self.assertTrue(task['resourcesReleased'])
        self.assertEqual(f.count(), 1)
        f.sim.resume.assert_not_called()
