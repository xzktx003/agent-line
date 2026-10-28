import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock
from agent_line.completion_worker import CompletionLoop, CompletionWorker
from agent_line.completion_relay import CompletionRelay, ResumeBinding
from agent_line.pause_bindings import PauseBindings
from agent_line.sim_journal import SimJournal
from agent_line.store import TaskStore

class CompletionWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.store=TaskStore.initialize(self.root/'tasks.sqlite')
        self.bindings=PauseBindings.initialize(self.root/'bindings.sqlite')
        self.journal=SimJournal.initialize(self.root/'journal.sqlite')
        self.journal.mutation_guard=self.store.workflow_admission
        self.client=Mock()
        self.client.resume.return_value={'status':'accepted'}
        self.client.observe.side_effect=self.observe
        self.paused=False
        self.relay=CompletionRelay(self.client,self.journal)
        self.loop=CompletionLoop(self.store,self.bindings,self.relay)

    def observe(self,workflow,run):
        return {'response':{'data':{'workflowId':workflow,'runId':run,'status':'paused' if self.paused else 'running','paused':{'contextId':'context','blockedOnBlockId':'wait','pausePointCount':1,'pauseKind':'human'}}}}

    def task(self,run='run',verified=True,released=True,failure_branch=False,policy=False,memory=False):
        task,_=self.store.register(run,{'projectId':'project','roleAgentId':'role','workspace':str(self.root),'workflow':{'id':'workflow','runId':run,'nodeId':'wait','nodeExecutionId':'context','attemptId':'one'},'input':{}})
        tid=task['id']; identity={'codexThreadId':'thread','codexTurnId':'turn'}
        self.store.claim(tid,str(self.root)); self.store.accept(tid,identity)
        evidence=[{'kind':'execution_terminal','executionId':'turn','confirmed':True}]
        if policy:
            evidence.append({'kind':'execution_policy','source':'qwenpaw-plugin-task-binding','executionId':'turn','controls':{'sandbox':'read-only','approval_policy':'never'}})
        self.store.finish(tid,'succeeded',{'ok':True},evidence)
        if released:self.store.confirm_resources_released(tid,'turn')
        if verified:self.store.record_verification(tid,{'kind':'verification','taskId':tid,'executionIdentity':identity,'status':'passed','checks':[{'status':'passed'}]})
        self.bindings.bind(ResumeBinding(tid,'workflow',run,'wait','context','external_task_result',memory,failure_branch))
        return tid

    def test_completion_before_pause_retries_observation_then_restart_never_reposts(self):
        tid=self.task(); self.assertEqual(self.loop.tick()[0]['status'],'waiting')
        self.client.resume.assert_not_called(); self.paused=True
        self.assertEqual(self.loop.tick()[0]['status'],'accepted')
        self.loop.tick()
        reopened=CompletionLoop(TaskStore(self.store.path),PauseBindings(self.bindings.path),CompletionRelay(self.client,SimJournal(self.journal.path)))
        reopened.tick(); self.client.resume.assert_called_once()
        self.assertEqual(self.store.get(tid)['executionStatus'],'succeeded')

    def test_unverified_and_unreleased_wait(self):
        self.task(verified=False,released=False); self.paused=True
        self.assertEqual(self.loop.tick()[0]['status'],'waiting'); self.client.resume.assert_not_called()

    def test_maintenance_guards_new_resume_but_existing_receipt_replays(self):
        self.task(); self.paused=True; owner=self.store.enter_maintenance()
        self.assertEqual(self.loop.tick()[0]['status'],'guarded'); self.client.resume.assert_not_called()
        self.store.leave_maintenance(owner); self.loop.tick(); self.client.resume.assert_called_once()
        owner=self.store.enter_maintenance()
        self.assertEqual(self.loop.tick()[0]['status'],'accepted'); self.client.resume.assert_called_once()
        self.store.leave_maintenance(owner)

    def test_bad_task_does_not_starve_next_binding(self):
        self.bindings.bind(ResumeBinding('aaa-missing','workflow','missing','wait','context','external_task_result',False))
        tid=self.task(); self.paused=True
        loop=CompletionLoop(self.store,self.bindings,self.relay,batch_size=1)
        first=loop.tick(); self.assertEqual(first,[{'taskId':'aaa-missing','status':'error','reason':'task_unavailable'}])
        self.assertEqual(loop.tick()[0]['taskId'],tid)
        loop.tick(); self.assertEqual(loop.tick()[0]['taskId'],'aaa-missing')

    def test_worker_lifecycle_and_undrained_request(self):
        entered=threading.Event(); release=threading.Event()
        loop=CompletionLoop(self.store,self.bindings,self.relay); loop.tick=Mock(side_effect=lambda:(entered.set(),release.wait(5),[])[-1])
        worker=CompletionWorker(loop,interval=.01)
        worker.start(); self.assertTrue(entered.wait(2))
        try:
            with self.assertRaises(RuntimeError):worker.start()
            with self.assertRaises(RuntimeError):worker.stop(timeout=.01)
            self.assertTrue(worker.thread.is_alive())
        finally:release.set();worker.stop(timeout=2)
        self.assertFalse(worker.thread.is_alive())

    def test_loop_reports_bounded_errors_and_validation(self):
        with self.assertRaises(ValueError):CompletionLoop(self.store,self.bindings,self.relay,batch_size=True)
        with self.assertRaises(ValueError):CompletionWorker(self.loop,interval=float('nan'))
        self.task(); self.relay.deliver=Mock(side_effect=RuntimeError('sensitive text'))
        self.assertNotIn('sensitive',str(self.loop.tick()))

    def test_transport_timeout_restart_retains_original_uncertain_identity(self):
        from agent_line.adapters.sim import SubmissionUncertain
        self.task(); self.paused=True
        self.client.resume.side_effect=SubmissionUncertain('run','context')
        self.assertEqual(self.loop.tick()[0]['status'],'uncertain')
        original=self.journal.get('workflow','run','context')
        restarted=CompletionLoop(TaskStore(self.store.path),PauseBindings(self.bindings.path),CompletionRelay(self.client,SimJournal(self.journal.path)))
        self.assertEqual(restarted.tick()[0]['status'],'uncertain')
        self.assertEqual(self.journal.get('workflow','run','context'),original)
        self.client.resume.assert_called_once()

    def test_verified_resource_flag_false_still_never_resumes(self):
        tid=self.task();self.paused=True
        # Persist an inconsistent flag to independently exercise the relay's resource fence.
        with self.store.connect() as db:db.execute('UPDATE tasks SET resources_released=0 WHERE id=?',(tid,))
        self.assertEqual(self.loop.tick()[0]['status'],'waiting')
        self.client.resume.assert_not_called()

    def test_real_worker_reports_and_stops_without_late_tick(self):
        self.task();self.paused=True
        reported=threading.Event()
        worker=CompletionWorker(self.loop,interval=.01)
        original=worker._report
        def report(rows):original(rows);reported.set()
        worker._report=report
        worker.start()
        try:self.assertTrue(reported.wait(2))
        finally:worker.stop(timeout=2)
        self.assertEqual(worker.last_report[0]['status'],'accepted')
        self.client.resume.assert_called_once()
        self.assertFalse(worker.thread.is_alive())

    def test_scan_failure_is_reported_without_sensitive_exception(self):
        stop=threading.Event(); reports=[]
        self.bindings.page=Mock(side_effect=RuntimeError('secret from storage'))
        def report(rows):reports.extend(rows);stop.set()
        self.loop.run(stop,interval=.01,report=report)
        self.assertEqual(reports,[{'status':'error','reason':'binding_scan_unavailable'}])

    def test_human_output_handoff_is_reported_and_other_reasons_stay_hidden(self):
        self.task()
        self.relay.deliver = Mock(return_value={'status': 'waiting', 'reason': 'declared_output_needs_human'})
        row = self.loop.tick()[0]
        self.assertEqual(row['status'], 'waiting')
        self.assertEqual(row['reason'], 'declared_output_needs_human')
        self.client.observe.assert_not_called()
        self.client.resume.assert_not_called()
        self.relay.deliver = Mock(return_value={'status': 'waiting', 'reason': 'verification_failure_needs_human'})
        failed = self.loop.tick()[0]
        self.assertEqual(failed['reason'], 'verification_failure_needs_human')
        self.relay.deliver = Mock(return_value={'status': 'waiting', 'reason': 'permission_denied_needs_human'})
        denied = self.loop.tick()[0]
        self.assertEqual(denied['reason'], 'permission_denied_needs_human')
        self.relay.deliver = Mock(return_value={'status': 'waiting', 'reason': 'execution_failure_needs_human'})
        failed_execution = self.loop.tick()[0]
        self.assertEqual(failed_execution['reason'], 'execution_failure_needs_human')
        self.relay.deliver = Mock(return_value={'status': 'waiting', 'reason': 'execution_interrupted_needs_human'})
        interrupted = self.loop.tick()[0]
        self.assertEqual(interrupted['reason'], 'execution_interrupted_needs_human')
        self.relay.deliver = Mock(return_value={'status': 'waiting', 'reason': 'stale_activation_not_current'})
        stale = self.loop.tick()[0]
        self.assertEqual(stale['reason'], 'stale_activation_not_current')
        self.relay.deliver = Mock(return_value={'status': 'waiting', 'reason': 'memory_write_needs_retry'})
        memory = self.loop.tick()[0]
        self.assertEqual(memory['reason'], 'memory_write_needs_retry')
        self.relay.deliver = Mock(return_value={'status': 'waiting', 'reason': 'secret token'})
        hidden = self.loop.tick()[0]
        self.assertEqual(hidden, {'taskId': row['taskId'], 'status': 'waiting'})
        self.assertNotIn('secret', str(hidden))

    def test_stored_permission_rejection_keeps_controls_and_does_not_resubmit(self):
        tid=self.task(run='denied',policy=True)
        current=self.store.get(tid)
        version=current['version']
        identity=current['executionIdentity']
        noted=self.store.note_approval_decided(tid,'request-1','decision-1','reject',binding={
            'taskId':tid,'executionIdentity':identity,'deliveryStatus':'accepted'})
        self.assertEqual(noted['payload']['deliveryStatus'],'accepted')
        self.assertIsNone(self.store.note_approval_decided(tid,'request-1','decision-1','reject',binding={
            'taskId':'other','executionIdentity':identity,'deliveryStatus':'accepted'}))
        stored=self.store.get(tid)
        self.assertEqual(stored['version'],version)
        self.assertEqual(stored['executionStatus'],'succeeded')
        self.assertEqual(stored['evidence'][1]['controls'],{'sandbox':'read-only','approval_policy':'never'})
        self.paused=True
        row=self.loop.tick()[0]
        self.assertEqual(row['status'],'waiting')
        self.assertEqual(row['reason'],'permission_denied_needs_human')
        self.client.observe.assert_not_called()
        self.client.resume.assert_not_called()
        self.client.execute.assert_not_called()
        repair=self.task(run='repair',policy=True,failure_branch=True)
        self.store.note_approval_decided(repair,'request-2','decision-2','reject',binding={
            'taskId':repair,'executionIdentity':self.store.get(repair)['executionIdentity'],
            'deliveryStatus':'accepted'})
        self.client.observe.reset_mock()
        delivered=self.loop.tick()
        repair_row=next(item for item in delivered if item['taskId']==repair)
        self.assertEqual(repair_row['status'],'accepted')
        self.assertNotIn('reason',repair_row)
        self.client.observe.assert_not_called()
        self.client.execute.assert_not_called()
        self.client.resume.assert_called_once()

    def test_confirmed_memory_failure_is_reported_without_reexecuting(self):
        tid=self.task(run='memory-failed', memory=True)
        self.store.set_memory_status(tid, 'failed')
        self.paused=True
        row=self.loop.tick()[0]
        self.assertEqual(row['status'], 'waiting')
        self.assertEqual(row['reason'], 'memory_write_needs_retry')
        self.client.observe.assert_not_called()
        self.client.resume.assert_not_called()
        self.client.execute.assert_not_called()
        stored=self.store.get(tid)
        self.assertEqual(stored['executionStatus'], 'succeeded')
        self.assertEqual(stored['memoryStatus'], 'failed')
        self.assertEqual(stored['verificationStatus'], 'passed')
