import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from agent_line.store import TaskStore, Conflict
from agent_line.dispatcher import Dispatcher


class ExecutorInterruptionTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.store=TaskStore.initialize(self.root/'tasks.sqlite')
        self.task,_=self.store.register('k',{'projectId':'p','roleAgentId':'role','workspace':str(self.root),'workflow':{'runId':'run','nodeExecutionId':'node','attemptId':'one'},'input':{}})
        self.tid=self.task['id'];self.identity={'upstreamTaskId':'native','codexThreadId':'thread','codexTurnId':'turn'}
        self.store.claim(self.tid,str(self.root));self.store.accept(self.tid,self.identity)
        self.receipt={'source':'docker-inspect-before-remove','owner':'a'*32,'containerId':'b'*64,'workspace':str(self.root),'running':False,'pid':0,'status':'exited','finishedAt':'2026-10-21T00:00:00Z','identities':[self.identity.copy()]}
    def resolve(self,receipt=None):return self.store.resolve_executor_interruption(self.tid,self.identity,receipt or self.receipt)
    def test_interruption_is_failed_not_native_terminal_and_idempotent(self):
        self.assertTrue(self.resolve());self.assertFalse(self.resolve())
        task=self.store.get(self.tid)
        self.assertEqual(task['executionStatus'],'failed');self.assertTrue(task['resourcesReleased'])
        self.assertEqual(task['verificationStatus'],'pending');self.assertIsNone(task['output'])
        self.assertEqual([e['kind'] for e in task['evidence']],['execution_interrupted','resource_exit'])
        self.assertFalse(task['evidence'][0]['cancellationRequested'])
        self.assertFalse(TaskStore(self.store.path).resolve_executor_interruption(self.tid,self.identity,self.receipt))
    def test_cancel_intent_survives_failed_interruption(self):
        self.store.request_cancel(self.tid);self.resolve()
        self.assertTrue(self.store.get(self.tid)['evidence'][0]['cancellationRequested'])
        self.assertFalse(self.resolve())
    def test_invalid_receipts_preserve_state(self):
        for key,value in [('containerId','short'),('owner','bad'),('source','model'),('pid',False),('running',True),('status','running'),('workspace','/other'),('identities',[{'codexThreadId':'thread','codexTurnId':'other'}]),('finishedAt','')]:
            with self.subTest(key=key):
                receipt={**self.receipt,key:value}
                with self.assertRaises(Conflict):self.resolve(receipt)
                self.assertFalse(self.store.get(self.tid)['resourcesReleased'])
    def test_identity_mismatch_or_incomplete_rejected(self):
        for identity in ({**self.identity,'codexTurnId':'other'},{'upstreamTaskId':'native'}):
            with self.assertRaises(Conflict):self.store.resolve_executor_interruption(self.tid,identity,self.receipt)
    def test_quarantine_cannot_be_overridden(self):
        self.store.quarantine_resources(self.tid)
        with self.assertRaises(Conflict):self.resolve()
    def test_conflicting_terminal_and_receipt_rejected(self):
        self.resolve()
        with self.assertRaises(Conflict):self.resolve({**self.receipt,'containerId':'c'*64})
        with self.assertRaises(Conflict):self.store.finish(self.tid,'failed',None,[{'kind':'execution_terminal','executionId':'turn','confirmed':True}])
    def test_existing_native_terminal_not_replaced(self):
        self.store.finish(self.tid,'succeeded',{},[{'kind':'execution_terminal','executionId':'turn','confirmed':True}])
        with self.assertRaises(Conflict):self.resolve()
    def test_dispatcher_consumes_only_top_level_trusted_adapter_evidence(self):
        gateway=Mock();gateway.observe.return_value={'identity':self.identity,'executorInterrupted':True,'nativeStatus':'unknown','terminalConfirmed':False,'resourcesReleased':True,'resourceExitReceipt':self.receipt}
        dispatcher=Dispatcher(self.store,{'p':gateway});result=dispatcher.reconcile(self.tid)
        self.assertEqual(result['executionStatus'],'failed');self.assertTrue(result['resourcesReleased'])
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM workspace_leases').fetchone()[0],0)
        self.assertEqual(dispatcher.reconcile(self.tid)['executionStatus'],'failed')
    def test_model_output_does_not_trigger_interruption(self):
        gateway=Mock();gateway.observe.return_value={'identity':self.identity,'output':{'executorInterrupted':True,'resourceExitReceipt':self.receipt},'terminalConfirmed':False}
        result=Dispatcher(self.store,{'p':gateway}).reconcile(self.tid)
        self.assertEqual(result['executionStatus'],'accepted');self.assertFalse(result['resourcesReleased'])

    def test_concurrent_reconciliation_commits_one_interruption(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=3) as pool:
            outcomes=list(pool.map(lambda _:self.resolve(),range(3)))
        self.assertEqual(outcomes.count(True),1)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE task_id=? AND kind='execution_interrupted'",(self.tid,)).fetchone()[0],1)

    def test_dispatcher_contradictory_terminal_or_missing_release_never_closes(self):
        for terminal,released in ((True,True),(False,False),(None,True)):
            gateway=Mock();gateway.observe.return_value={'identity':self.identity,'executorInterrupted':True,'nativeStatus':'unknown','terminalConfirmed':terminal,'resourcesReleased':released,'resourceExitReceipt':self.receipt}
            self.assertEqual(Dispatcher(self.store,{'p':gateway}).reconcile(self.tid)['executionStatus'],'accepted')

    def test_dispatcher_canceled_interruption_does_not_invent_native_canceled(self):
        self.store.request_cancel(self.tid)
        gateway=Mock();gateway.observe.return_value={'identity':self.identity,'executorInterrupted':True,'nativeStatus':'unknown','terminalConfirmed':False,'resourcesReleased':True,'resourceExitReceipt':self.receipt}
        result=Dispatcher(self.store,{'p':gateway}).reconcile(self.tid)
        self.assertEqual(result['executionStatus'],'failed');self.assertTrue(result['evidence'][0]['cancellationRequested'])
        gateway.cancel.assert_not_called()

    def test_interruption_flag_cannot_contradict_native_status(self):
        gateway=Mock();gateway.observe.return_value={'identity':self.identity,'executorInterrupted':True,'nativeStatus':'completed','terminalConfirmed':False,'resourcesReleased':True,'resourceExitReceipt':self.receipt}
        self.assertEqual(Dispatcher(self.store,{'p':gateway}).reconcile(self.tid)['executionStatus'],'accepted')

    def test_duplicate_identity_exit_receipt_is_rejected(self):
        with self.assertRaises(Conflict):self.resolve({**self.receipt,'identities':[self.identity,self.identity]})

    def test_replay_preserves_compatible_verification_evidence_and_status(self):
        from agent_line.store import canonical
        self.resolve()
        task=self.store.get(self.tid)
        verification={'kind':'verification','taskId':self.tid,'executionIdentity':self.identity,'status':'failed','checks':[{'status':'failed','name':'artifact'}]}
        # Persistence compatibility fixture: current public checker only accepts succeeded.
        with self.store.connect() as db:
            db.execute("UPDATE tasks SET evidence=?, verification_status='failed' WHERE id=?",(canonical(task['evidence']+[verification]),self.tid))
        self.assertFalse(self.resolve())
        current=self.store.get(self.tid)
        self.assertEqual(current['verificationStatus'],'failed');self.assertEqual(current['evidence'][-1],verification)
        verification['executionIdentity']={'upstreamTaskId':'other'}
        with self.store.connect() as db:
            db.execute('UPDATE tasks SET evidence=? WHERE id=?',(canonical(task['evidence']+[verification]),self.tid))
        with self.assertRaises(Conflict):self.resolve()

    def test_store_resolution_atomically_releases_original_lease(self):
        self.assertTrue(self.resolve())
        reopened=TaskStore(self.store.path)
        with reopened.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM workspace_leases WHERE task_id=?',(self.tid,)).fetchone()[0],0)
        self.assertFalse(reopened.resolve_executor_interruption(self.tid,self.identity,self.receipt))

    def test_replay_never_deletes_new_task_workspace_lease(self):
        self.resolve()
        payload=copy.deepcopy(self.task['payload']);payload['workflow']['nodeExecutionId']='next-node'
        new,_=self.store.register('next-key',payload)
        self.assertTrue(self.store.claim(new['id'],str(self.root)))
        restarted=TaskStore(self.store.path)
        self.assertFalse(restarted.resolve_executor_interruption(self.tid,self.identity,self.receipt))
        gateway=Mock();gateway.observe.return_value={'identity':self.identity,'executorInterrupted':True,'nativeStatus':'unknown','terminalConfirmed':False,'resourcesReleased':True,'resourceExitReceipt':self.receipt}
        self.assertEqual(Dispatcher(restarted,{'p':gateway}).reconcile(self.tid)['executionStatus'],'failed')
        with restarted.connect() as db:
            self.assertEqual(db.execute('SELECT task_id FROM workspace_leases WHERE workspace=?',(str(self.root),)).fetchone()[0],new['id'])

    def test_transaction_failure_rolls_back_status_and_lease_together(self):
        original=self.store._event
        def fail(db,task_id,kind):
            original(db,task_id,kind)
            if kind=='resources_released':raise RuntimeError('simulated transaction abort')
        self.store._event=fail
        with self.assertRaises(RuntimeError):self.resolve()
        self.store._event=original
        reopened=TaskStore(self.store.path)
        self.assertEqual(reopened.get(self.tid)['executionStatus'],'accepted')
        with reopened.connect() as db:
            self.assertEqual(db.execute('SELECT task_id FROM workspace_leases').fetchone()[0],self.tid)
        self.assertTrue(reopened.resolve_executor_interruption(self.tid,self.identity,self.receipt))
