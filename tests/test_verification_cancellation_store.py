import copy
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from tests import test_test_verification_policy as fixtures
from agent_line.store import TaskStore, Conflict
from agent_line.verification_policy import policy_hash


class VerificationCancellationTests(unittest.TestCase):
    setUp=fixtures.CommandStoreTests.setUp

    def cancellation(self):
        return {'kind':'verification_cancellation','schemaVersion':1,'taskId':self.task,
                'executionIdentity':self.identity,'policySha256':policy_hash(self.policy),
                'canceledAt':time.time(),'evidenceFile':'cancellation-'+'e'*64+'.json',
                'tests':[{'testId':'unit','disposition':'not_started','taskId':self.task,
                    'executionIdentity':self.identity,'policySha256':policy_hash(self.policy),
                    'environmentId':self.policy['testEnvironmentId']}]}

    def test_cancel_intent_is_durable_and_does_not_change_native_success(self):
        before=self.store.get(self.task)
        self.assertTrue(self.store.request_cancel(self.task));self.assertFalse(self.store.request_cancel(self.task))
        after=TaskStore(self.store.path).get(self.task)
        self.assertTrue(after['cancellationRequested'])
        for key in ('executionStatus','output','resourcesReleased','executionIdentity'):self.assertEqual(before[key],after[key])
        self.assertEqual([e['kind'] for e in self.store.events(self.task)].count('verification_cancel_requested'),1)
        with self.assertRaises(Conflict):self.store.record_verification(self.task,self.receipt)

    def test_cancellation_atomic_release_and_replay_does_not_touch_successor(self):
        self.store.request_cancel(self.task);receipt=self.cancellation()
        self.assertTrue(self.store.record_verification_cancellation(self.task,receipt))
        self.assertEqual(self.store.get(self.task)['verificationStatus'],'canceled')
        next_task,_=self.store.register('next',{'projectId':'project','roleAgentId':'role','workspace':str(self.workspace),
            'workflow':{'runId':'next','nodeExecutionId':'node','attemptId':'one'}})
        self.assertTrue(self.store.claim(next_task['id'],self.workspace))
        self.assertFalse(TaskStore(self.store.path).record_verification_cancellation(self.task,receipt))
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT task_id FROM workspace_leases').fetchone()[0],next_task['id'])
        changed=copy.deepcopy(receipt);changed['canceledAt']+=1
        with self.assertRaises(Conflict):self.store.record_verification_cancellation(self.task,changed)

    def test_cancel_requires_intent_policy_identity_and_all_tests(self):
        receipt=self.cancellation()
        with self.assertRaises(Conflict):self.store.record_verification_cancellation(self.task,receipt)
        self.store.request_cancel(self.task)
        for modify in (lambda r:r.update(tests=[]),lambda r:r.update(policySha256='f'*64),
                       lambda r:r['tests'][0].update(disposition='exited'),lambda r:r['tests'][0].update(extra=True),
                       lambda r:r.update(canceledAt=float('nan'))):
            bad=copy.deepcopy(receipt);modify(bad)
            with self.assertRaises((Conflict,ValueError)):self.store.record_verification_cancellation(self.task,bad)
        self.assertEqual(self.store.verification_lease(self.task),str(self.workspace))

    def test_real_exit_check_cancellation_preserves_test_result(self):
        self.store.request_cancel(self.task);receipt=self.cancellation();check=copy.deepcopy(self.receipt['checks'][0])
        receipt['tests'][0].update(disposition='exited',containerId=check['containerId'],
            resourceExitReceipt=check['resourceExitReceipt'],check=check)
        self.assertTrue(self.store.record_verification_cancellation(self.task,receipt))
        task=self.store.get(self.task)
        self.assertEqual(task['executionStatus'],'succeeded')
        self.assertEqual(task['evidence'][-1]['tests'][0]['check']['status'],'passed')
        self.assertEqual(task['verificationStatus'],'canceled')

    def test_cancel_and_verification_race_has_one_committed_outcome(self):
        def verify():
            try:return self.store.record_verification(self.task,self.receipt)
            except Conflict:return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            canceled=pool.submit(self.store.request_cancel,self.task);verified=pool.submit(verify)
            cancel_won,verify_won=canceled.result(),verified.result()
        self.assertNotEqual(cancel_won,verify_won)
        if cancel_won:
            self.store.record_verification_cancellation(self.task,self.cancellation())
            self.assertEqual(self.store.get(self.task)['verificationStatus'],'canceled')
        else:self.assertEqual(self.store.get(self.task)['verificationStatus'],'passed')

    def test_missing_owned_lease_or_native_exit_keeps_cancellation_pending(self):
        self.store.request_cancel(self.task)
        with self.store.connect() as db:
            lease=tuple(db.execute('SELECT * FROM workspace_leases WHERE task_id=?',(self.task,)).fetchone())
            db.execute('DELETE FROM workspace_leases WHERE task_id=?',(self.task,))
        with self.assertRaises(Conflict):self.store.record_verification_cancellation(self.task,self.cancellation())
        self.assertEqual(self.store.get(self.task)['verificationStatus'],'pending')
        with self.store.connect() as db:db.execute('INSERT INTO workspace_leases VALUES (?,?,?)',lease)
        with self.store.connect() as db:
            import json
            evidence=self.store.get(self.task)['evidence']
            db.execute('UPDATE tasks SET evidence=? WHERE id=?',(json.dumps([e for e in evidence if e.get('kind')!='resource_exit']),self.task))
        with self.assertRaises(Conflict):self.store.record_verification_cancellation(self.task,self.cancellation())

    def test_finished_verification_retains_old_terminal_cancel_behavior(self):
        self.store.record_verification(self.task,self.receipt)
        self.assertFalse(self.store.request_cancel(self.task))
        self.assertFalse(self.store.get(self.task)['cancellationRequested'])
        with self.assertRaises(Conflict):self.store.record_verification_cancellation(self.task,self.cancellation())

    def test_full_order_and_no_invented_not_started_exit_fields(self):
        from agent_line.verification_policy import validate_cancellation_receipt,validate_policy
        policy=copy.deepcopy(self.policy);policy['tests'].append(dict(policy['tests'][0],id='second'));policy=validate_policy(policy)
        receipt=self.cancellation();receipt['policySha256']=policy_hash(policy)
        receipt['tests'][0]['policySha256']=policy_hash(policy)
        receipt['tests'].append(dict(receipt['tests'][0],testId='second'))
        validate_cancellation_receipt(receipt,policy,self.task,self.identity,str(self.workspace))
        receipt['tests'].reverse()
        with self.assertRaises(ValueError):validate_cancellation_receipt(receipt,policy,self.task,self.identity,str(self.workspace))
        receipt['tests'].reverse();receipt['tests'][0]['containerId']='c'*64
        with self.assertRaises(ValueError):validate_cancellation_receipt(receipt,policy,self.task,self.identity,str(self.workspace))


    def test_cancel_waits_for_verification_scope_then_commits_once(self):
        import threading
        entered=threading.Event();release=threading.Event();cancel_started=threading.Event()
        def checking():
            with self.store.verification_scope(self.task):
                entered.set();self.assertTrue(release.wait(5))
        def canceling():
            cancel_started.set()
            return TaskStore(self.store.path).request_cancel(self.task)
        with ThreadPoolExecutor(max_workers=2) as pool:
            check=pool.submit(checking);self.assertTrue(entered.wait(5))
            cancel=pool.submit(canceling);self.assertTrue(cancel_started.wait(5))
            self.assertFalse(cancel.done())
            self.assertFalse(self.store.get(self.task)['cancellationRequested'])
            release.set();check.result();self.assertTrue(cancel.result())
        self.assertTrue(self.store.get(self.task)['cancellationRequested'])
        self.assertFalse(self.store.request_cancel(self.task))

    def test_verification_lock_rejects_invalid_file_and_unknown_task(self):
        with self.store.verification_scope(self.task):pass
        path=next(self.workspace.glob('*.verification-*.lock'));path.chmod(0o644)
        with self.assertRaises(Conflict):
            with self.store.verification_scope(self.task):self.fail('unsafe lock')
        with self.assertRaises(KeyError):
            with self.store.verification_scope('../unknown'):self.fail('unknown task')


    def test_prior_native_cancel_intent_also_prevents_later_pass(self):
        self.store.record_verification(self.task,self.receipt)
        task,_=self.store.register('prior-cancel',{'projectId':'project','roleAgentId':'role','workspace':str(self.workspace),
            'verificationPolicy':self.policy,'workflow':{'runId':'prior','nodeExecutionId':'node','attemptId':'one'}})
        self.task=task['id'];self.store.claim(self.task,self.workspace);self.store.accept(self.task,self.identity)
        self.assertTrue(self.store.request_cancel(self.task))
        self.store.finish(self.task,'succeeded',{'actual':'native completed'},
            [{'kind':'execution_terminal','executionId':'turn','confirmed':True}])
        self.store.confirm_resources_released(self.task,'turn',{'source':'docker-inspect-before-remove','running':False,
            'pid':0,'status':'exited','owner':'a'*32,'containerId':'b'*64,'finishedAt':'time','workspace':str(self.workspace),
            'identities':[self.identity]})
        receipt=dict(self.receipt,taskId=self.task,checks=[fixtures.receipt_check(self.policy,self.task,self.identity,str(self.workspace))])
        self.assertTrue(self.store.get(self.task)['cancellationRequested'])
        with self.assertRaises(Conflict):self.store.record_verification(self.task,receipt)
        self.assertTrue(self.store.record_verification_cancellation(self.task,self.cancellation()))
        self.assertEqual(self.store.get(self.task)['executionStatus'],'succeeded')


    def test_cancel_after_native_success_before_exit_proof_prevents_test_start(self):
        from unittest.mock import patch
        from tests.test_automatic_test_verification import AutomaticTestVerificationTests
        f=AutomaticTestVerificationTests();f.setUp();self.addCleanup(f.doCleanups)
        workspace=f.root/'awaiting-exit';workspace.mkdir()
        task,_=f.store.register('awaiting-exit',{'projectId':'project','roleAgentId':'awaiting-exit-role',
            'workspace':str(workspace),'verificationPolicy':f.policy,
            'workflow':{'runId':'awaiting-exit','nodeExecutionId':'node','attemptId':'one'}})
        task_id=task['id'];self.assertTrue(f.store.claim(task_id,workspace));f.store.accept(task_id,f.identity)
        output={'actual':'encoding finished before exit observation'}
        f.store.finish(task_id,'succeeded',output,
            [{'kind':'execution_terminal','executionId':'turn','confirmed':True}])
        before=f.store.get(task_id)
        self.assertFalse(before['resourcesReleased'])
        self.assertTrue(f.store.request_cancel(task_id))
        after=TaskStore(f.store.path).get(task_id)
        self.assertTrue(after['cancellationRequested'])
        for key in ('executionStatus','output','resourcesReleased','executionIdentity'):
            self.assertEqual(before[key],after[key])
        with patch.object(f.executor,'check',side_effect=AssertionError('canceled test must never start')), \
             patch('agent_line.test_executor._command',side_effect=AssertionError('no Docker required')):
            self.assertEqual(f.verifier.verify_pending(task_id)['status'],'waiting')
            self.assertFalse(list(f.directory.glob('cancellation-*.json')))
            f.store.confirm_resources_released(task_id,'turn',{
                'source':'docker-inspect-before-remove','running':False,'pid':0,'status':'exited',
                'owner':'a'*32,'containerId':'b'*64,'finishedAt':'time','workspace':str(workspace),
                'identities':[f.identity]})
            self.assertEqual(f.verifier.verify_pending(task_id)['verificationStatus'],'canceled')
        final=f.store.get(task_id)
        self.assertEqual(final['executionStatus'],'succeeded');self.assertEqual(final['output'],output)
        self.assertTrue(final['resourcesReleased'])
        self.assertEqual(final['evidence'][-1]['tests'][0]['disposition'],'not_started')
