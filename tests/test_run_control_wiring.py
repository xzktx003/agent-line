"""Run gates cover actual durable submission boundaries, not just polling."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock

from agent_line.approvals import ApprovalBinding, ApprovalCoordinator, ApprovalLedger, TrustedPrincipal
from agent_line.completion_relay import CompletionRelay, ResumeBinding
from agent_line.dispatcher import Dispatcher, DispatchLoop
from agent_line.run_control import RunControl, RunControlClosed
from agent_line.sim_journal import SimJournal
from agent_line.store import TaskStore


class RunControlWiringTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.tasks = TaskStore.initialize(self.root/'tasks.sqlite')
        self.journal = SimJournal.initialize(self.root/'sim.sqlite')
        self.control = RunControl(self.journal, self.tasks)
        self.journal.run_control = self.control
        self.sim = Mock()
        self.sim.execute.return_value = {'transportStatus': 'accepted'}
        self.sim.resume.return_value = {'transportStatus': 'accepted'}
        self.sim.cancel.return_value = {'transportStatus': 'accepted', 'verifiedStopped': False}
        self.sim.observe.side_effect = lambda w, r: {'response': {'data': {
            'workflowId': w, 'runId': r, 'status': 'paused', 'paused': {
                'contextId': 'wait', 'blockedOnBlockId': 'wait', 'pauseKind': 'human',
                'pausePointCount': 1, 'resumedCount': 0}}}}

    def apply(self, action, operation='op', version=0, **kw):
        if action == 'pause' and 'active_execution' not in kw:
            kw['active_execution'] = 'continue'
        return self.control.apply(project_id='project', workflow_id='workflow', run_id='run',
            action=action, operation_id=operation, expected_version=version, validate_continue=lambda: True, **kw)

    def register(self, run='run', node='wait'):
        key = run if node == 'wait' else run + ':' + node
        return self.tasks.register(key, {'projectId': 'project', 'roleAgentId': 'role',
            'workspace': str(self.root), 'input': {}, 'workflow': {
                'id': 'workflow', 'runId': run, 'nodeId': node, 'nodeExecutionId': node,
                'revision': '1', 'attemptId': '1'}})[0]

    def test_pause_blocks_new_resume_without_intent_but_existing_receipt_is_readable(self):
        old = self.journal.resume(self.sim, 'workflow', 'run', 'first', {'done': True})
        self.apply('pause')
        with self.assertRaises(RunControlClosed):
            self.journal.resume(self.sim, 'workflow', 'run', 'second', {'done': True})
        with self.assertRaises(KeyError): self.journal.get('workflow', 'run', 'second')
        self.assertEqual(self.journal.resume(self.sim, 'workflow', 'run', 'first', {'done': True}), old)
        self.sim.resume.assert_called_once()
        self.assertEqual(self.journal.resume_summary('workflow', 'run')['latestAttempt']['contextId'], 'first')

    def test_stop_can_cancel_sim_once_without_misreading_control_as_resume(self):
        self.apply('stop')
        first = self.journal.cancel(self.sim, 'workflow', 'run')
        self.assertEqual(first['status'], 'accepted')
        self.assertEqual(self.journal.cancel(self.sim, 'workflow', 'run'), first)
        self.sim.cancel.assert_called_once_with('workflow', 'run')
        self.assertIsNone(self.journal.resume_summary('workflow', 'run'))

    def test_queued_task_stays_queued_on_pause_and_can_start_after_continue(self):
        task = self.register(); gateway = Mock()
        gateway.submit.return_value = {'status': 'accepted', 'identity': {'upstreamTaskId': 'native'}}
        dispatcher = Dispatcher(self.tasks, {'project': gateway}, run_control=self.control)
        self.apply('pause')
        self.assertEqual(dispatcher.submit(task['id'])['executionStatus'], 'registered')
        gateway.submit.assert_not_called()
        self.apply('continue', 'continue', 1)
        self.assertEqual(dispatcher.submit(task['id'])['executionStatus'], 'accepted')
        gateway.submit.assert_called_once()

    def test_stop_converges_queued_tasks_even_with_submission_capability_disabled(self):
        task = self.register(); later = self.register(node='later'); other = self.register('other')
        gateway = Mock()
        gateway.observe.return_value = gateway.cancel.return_value = {'status': 'unknown'}
        dispatcher = Dispatcher(self.tasks, {'project': gateway}, run_control=self.control)
        self.apply('stop')  # Mimics crash before the HTTP handler scans tasks.
        first = min(task['id'], later['id'])
        original = dispatcher.store.dispatch_candidates
        dispatcher.store.dispatch_candidates = lambda **kwargs: [
            item for item in original(**kwargs) if item['id'] == first]
        DispatchLoop(dispatcher, allow_submissions=False).tick()
        for queued in (task, later):
            stored = self.tasks.get(queued['id'])
            self.assertEqual(stored['executionStatus'], 'canceled')
            self.assertTrue(stored['resourcesReleased'])
            self.assertEqual(stored['payload']['workflow']['runId'], 'run')
        self.assertEqual(self.tasks.get(other['id'])['executionStatus'], 'registered')
        self.assertNotIn(later['id'], {item['id'] for item in original()})
        gateway.submit.assert_not_called(); gateway.cancel.assert_not_called()
        if gateway.observe.called:
            self.assertEqual(gateway.observe.call_args.args, (first,))

    def test_stop_of_active_task_requests_cancel_without_claiming_resource_exit(self):
        task = self.register(); gateway = Mock()
        gateway.submit.return_value = {'status': 'accepted', 'identity': {'upstreamTaskId': 'native'}}
        gateway.observe.return_value = gateway.cancel.return_value = {'status': 'unknown'}
        dispatcher = Dispatcher(self.tasks, {'project': gateway}, run_control=self.control)
        dispatcher.submit(task['id']); self.apply('stop')
        current = dispatcher.reconcile(task['id'])
        self.assertEqual(current['executionStatus'], 'cancel_requested')
        self.assertFalse(current['resourcesReleased'])
        gateway.cancel.assert_called_once_with(task['id'])

    def test_approval_cannot_record_a_new_decision_while_paused(self):
        ledger = ApprovalLedger.initialize(self.root/'approvals.sqlite')
        binding = ApprovalBinding('approval', 'project', 'workflow', 'run', 'wait', 'wait',
            '1', 'run', 'one', 'human_approval', 200)
        ledger.bind(binding)
        coordinator = ApprovalCoordinator(ledger, self.sim, self.journal, authorize=lambda p: True,
            clock=lambda: 100)
        coordinator.run_control = self.control
        principal = TrustedPrincipal('owner', frozenset({'project'}), frozenset({'approve'}), 300)
        self.apply('pause')
        with self.assertRaises(RunControlClosed):
            coordinator.decide('approval', decision_id='decision', decision='approve', principal=principal)
        self.assertIsNone(ledger.get('approval')['decision']); self.sim.resume.assert_not_called()
        self.apply('continue', 'continue', 1)
        self.assertEqual(coordinator.decide('approval', decision_id='decision', decision='approve',
            principal=principal)['deliveryStatus'], 'accepted')

    def test_completed_result_waits_without_reexecution_during_pause(self):
        task = self.register(); task = self.tasks.get(task['id'])
        task.update(executionStatus='succeeded', verificationStatus='passed', resourcesReleased=True,
            executionIdentity={'upstreamTaskId': 'native'})
        binding = ResumeBinding(task['id'], 'workflow', 'run', 'wait', 'wait', 'external_task_result', False)
        self.apply('pause')
        relay = CompletionRelay(self.sim, self.journal)
        self.assertEqual(relay.deliver(task, binding), {'status': 'waiting', 'reason': 'run_paused'})
        self.sim.resume.assert_not_called()
        self.apply('continue', 'continue', 1)
        self.assertEqual(relay.deliver(task, binding)['status'], 'accepted')
        self.apply('stop', 'stop', 2)
        self.assertEqual(relay.deliver(task, binding)['status'], 'accepted')
        self.sim.resume.assert_called_once()

    def test_pause_drains_inflight_resume_and_blocks_successor_context(self):
        entered = threading.Event(); release = threading.Event(); paused = threading.Event()
        def resume(*args):
            entered.set(); self.assertTrue(release.wait(3)); return {'transportStatus': 'accepted'}
        self.sim.resume.side_effect = resume
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.journal.resume, self.sim, 'workflow', 'run', 'first', {})
            self.assertTrue(entered.wait(2))
            def pause():
                result = self.apply('pause'); paused.set(); return result
            command = pool.submit(pause)
            self.assertFalse(paused.wait(.05))
            release.set(); self.assertEqual(first.result(timeout=3)['status'], 'accepted')
            self.assertEqual(command.result(timeout=3)['state'], 'paused')
        with self.assertRaises(RunControlClosed):
            self.journal.resume(self.sim, 'workflow', 'run', 'second', {})

    def test_late_resolved_input_is_rechecked_before_task_registration(self):
        from tests import test_task_discovery as fixtures
        fixture = fixtures.TaskDiscoveryTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        control = RunControl(self.journal, fixture.tasks)
        fixture.discovery.run_control = control
        original = fixture.wait.read_input
        def paused_during_resolution(*args):
            resolved = original(*args)
            control.apply(project_id='project', workflow_id='workflow', run_id='run',
                action='pause', operation_id='pause', expected_version=0, active_execution='continue')
            return resolved
        fixture.wait.read_input = paused_during_resolution
        self.assertEqual(fixture.discovery.tick()[0]['status'], 'external_task_admission_closed')
        self.assertEqual(fixture.count(), 0)

    def test_stop_drains_claimed_submission_then_existing_task_reconciles_cancel(self):
        task = self.register(); entered = threading.Event(); release = threading.Event()
        stopped = threading.Event(); gateway = Mock()
        def submit(**kwargs):
            entered.set(); self.assertTrue(release.wait(3))
            return {'status': 'accepted', 'identity': {'upstreamTaskId': 'native'}}
        gateway.submit.side_effect = submit
        gateway.observe.return_value = gateway.cancel.return_value = {'status': 'unknown'}
        dispatcher = Dispatcher(self.tasks, {'project': gateway}, run_control=self.control)
        with ThreadPoolExecutor(max_workers=2) as pool:
            submitted = pool.submit(dispatcher.submit, task['id'])
            self.assertTrue(entered.wait(2))
            def stop():
                receipt = self.apply('stop'); stopped.set(); return receipt
            command = pool.submit(stop)
            self.assertFalse(stopped.wait(.05)); release.set()
            self.assertEqual(submitted.result(timeout=3)['executionStatus'], 'accepted')
            self.assertEqual(command.result(timeout=3)['state'], 'stop_requested')
        result = dispatcher.reconcile(task['id'])
        self.assertEqual(result['executionStatus'], 'cancel_requested')
        self.assertFalse(result['resourcesReleased'])
        self.assertEqual(dispatcher.submit(task['id'])['executionStatus'], 'cancel_requested')
        gateway.submit.assert_called_once(); gateway.cancel.assert_called_once()

    def test_human_discovery_recovers_stop_committed_before_cancel_intent(self):
        from agent_line.approval_discovery import WorkflowRunLedger, ApprovalDiscovery
        runs = WorkflowRunLedger.initialize(self.root/'runs.sqlite')
        runs.reserve(project_id='project', workflow_id='workflow', run_id='run', revision='1',
            deployment_version_id='deployment', human_block_ids=['wait'], ttl_seconds=600)
        ledger = ApprovalLedger.initialize(self.root/'approvals.sqlite')
        verifier = Mock(); verifier.verify_run.return_value = True
        discovery = ApprovalDiscovery(runs, ledger, self.sim, publication_verifier=verifier)
        discovery.run_control = RunControl(SimJournal(self.journal.path), self.tasks)
        self.apply('stop')  # Process died here, before any cancel journal intent.
        self.sim.cancel.assert_not_called()
        discovery.tick(); discovery.tick()
        self.sim.cancel.assert_called_once_with('workflow', 'run')
        self.assertEqual(self.journal.get('workflow', 'run', 'run-cancel')['status'], 'accepted')

    def test_stop_recovery_does_not_retry_an_uncertain_cancel(self):
        from agent_line.adapters.sim import SubmissionUncertain
        self.apply('stop')
        self.sim.cancel.side_effect = SubmissionUncertain('run')
        self.journal.cancel(self.sim, 'workflow', 'run')
        self.control.reconcile_stop('project', 'workflow', 'run', self.sim)
        self.sim.cancel.assert_called_once()
        self.assertEqual(self.journal.get('workflow', 'run', 'run-cancel')['status'], 'uncertain')
