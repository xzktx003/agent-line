import hashlib
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock
from agent_line.memory import MemoryCommitStore, MemoryCoordinator, MemoryDeletionCoordinator
from agent_line.adapters.qwenpaw_memory import MemoryApproval
from agent_line.store import Conflict


class MemoryDeletionTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.path=Path(tmp.name)/'memory.sqlite'
        self.store=MemoryCommitStore.initialize(self.path);self.text='verified fact';self.digest=hashlib.sha256(self.text.encode()).hexdigest()
        self.client=Mock();self.client.role='role';self.client.write_approved.return_value={'roleAgentId':'role','taskId':'task','contentSha256':self.digest,'status':'committed'}
        self.approval=MemoryApproval('task',self.digest,'approve')
        self.writer=MemoryCoordinator(self.store,self.client);self.writer.approve('task',self.text,self.approval);self.writer.commit('task')
        self.receipt={'roleAgentId':'role','taskId':'task','contentHash':self.digest,'status':'deleted','fileAbsent':True,'indexSourceAbsent':True}
        self.client.delete_checked.return_value=self.receipt;self.client.observe_deletion.return_value=self.receipt
        self.deleter=MemoryDeletionCoordinator(self.store,self.client)
    def delete(self,**kw):return self.deleter.request_delete('task',**{'actor':'human','decision_id':'delete','expected_hash':self.digest,**kw})
    def test_delete_persists_tombstone_and_old_approval_never_resurrects(self):
        self.assertEqual(self.delete()['status'],'deleted')
        self.assertEqual(self.store.get('role','task')['content'],'')
        restarted=MemoryCoordinator(MemoryCommitStore(self.path),self.client)
        self.assertEqual(restarted.approve('task',self.text,self.approval)['status'],'deleted')
        self.assertEqual(restarted.commit('task')['status'],'deleted');self.client.write_approved.assert_called_once()
        self.assertEqual(self.delete()['status'],'deleted');self.client.delete_checked.assert_called_once();self.client.observe_deletion.assert_called_once()
    def test_explicit_upgrade_preserves_committed_body_and_v1_works_until_delete(self):
        with self.store.connect() as db:
            db.execute('DROP TABLE memory_deletions');db.execute('UPDATE installation SET schema_version=1')
        old=MemoryCommitStore(self.path);self.assertEqual(old.get('role','task')['content'],self.text)
        with self.assertRaises(ValueError):old.reserve_delete('role','task','human','delete',self.digest)
        upgraded=MemoryCommitStore.upgrade(self.path)
        self.assertEqual(upgraded.get('role','task')['content'],self.text)
        self.assertEqual(MemoryCommitStore.upgrade(self.path).get('role','task')['content_hash'],self.digest)
    def test_crash_after_intent_reopens_with_get_only(self):
        self.store.reserve_delete('role','task','human','delete',self.digest)
        reboot=MemoryDeletionCoordinator(MemoryCommitStore(self.path),self.client)
        self.assertEqual(reboot.request_delete('task',actor='human',decision_id='delete',expected_hash=self.digest)['status'],'deleted')
        self.client.delete_checked.assert_not_called();self.client.observe_deletion.assert_called_once()
    def test_timeout_never_retries_delete(self):
        self.client.delete_checked.side_effect=TimeoutError()
        self.assertEqual(self.delete()['status'],'deleting');self.assertEqual(self.delete()['status'],'deleted')
        self.client.delete_checked.assert_called_once();self.client.observe_deletion.assert_called_once()
    def test_receipt_identity_and_two_absence_proofs_required(self):
        for change in ({'roleAgentId':'other'},{'fileAbsent':False},{'indexSourceAbsent':1},{'contentHash':'0'*64}):
            self.client.delete_checked.return_value={**self.receipt,**change};self.client.observe_deletion.return_value={**self.receipt,**change}
            self.assertEqual(self.delete()['status'],'deleting')
        self.assertEqual(self.store.get('role','task')['content'],'')
    def test_changed_delete_authority_is_conflict(self):
        self.delete()
        for changes in ({'actor':'other'},{'decision_id':'other'},{'expected_hash':'0'*64}):
            with self.assertRaises(Conflict):self.delete(**changes)
    def test_late_unknown_never_regresses_deleted(self):
        self.delete();self.client.observe_deletion.return_value={'status':'deleting'}
        self.assertEqual(self.deleter.reconcile_delete('task')['status'],'deleted')
        self.assertEqual(self.store.get('role','task')['status'],'deleted')
    def test_exclusive_delete_waits_for_inflight_writer_scope(self):
        entered=threading.Event();release=threading.Event();deleted=threading.Event()
        def existing_writer():
            with self.store.memory_scope('role','task'):
                entered.set();release.wait(2)
        def deleting():self.delete();deleted.set()
        with ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(existing_writer);self.assertTrue(entered.wait(1));second=pool.submit(deleting)
            self.assertFalse(deleted.wait(.1));self.client.delete_checked.assert_not_called()
            release.set();first.result();second.result()
        self.assertTrue(deleted.is_set())
    def test_late_commit_receipt_cannot_restore_deleted_body(self):
        version=self.store.get('role','task')['version'];self.delete()
        self.assertFalse(self.store.record('role','task',version,{'status':'committed'}))
        self.assertEqual(self.store.get('role','task')['content'],'')

    def test_actual_inflight_put_finishes_before_exclusive_deletion(self):
        started=threading.Event();release=threading.Event()
        approval=MemoryApproval('task-two',self.digest,'approve-two')
        self.writer.approve('task-two',self.text,approval)
        def write(task,content,**kw):
            started.set();release.wait(2)
            return {'roleAgentId':'role','taskId':task,'contentSha256':self.digest,'status':'committed'}
        self.client.write_approved.side_effect=write
        self.client.delete_checked.return_value={**self.receipt,'taskId':'task-two'}
        with ThreadPoolExecutor(max_workers=2) as pool:
            writing=pool.submit(self.writer.commit,'task-two');self.assertTrue(started.wait(1))
            deleting=pool.submit(self.deleter.request_delete,'task-two',actor='human',decision_id='delete-two',expected_hash=self.digest)
            time.sleep(.1);self.client.delete_checked.assert_not_called()
            release.set();self.assertEqual(writing.result()['status'],'committed')
            self.assertEqual(deleting.result()['status'],'deleted')
        self.assertEqual(self.store.get('role','task-two')['content'],'')

    def test_global_delete_decision_id_reuse_rolls_back_body_clear(self):
        self.delete()
        different='another verified fact';digest=hashlib.sha256(different.encode()).hexdigest()
        approval=MemoryApproval('task-two',digest,'approve-two')
        self.writer.approve('task-two',different,approval)
        self.client.write_approved.return_value={**self.client.write_approved.return_value,'taskId':'task-two','contentSha256':digest}
        self.writer.commit('task-two')
        with self.assertRaises(Conflict):self.store.reserve_delete('role','task-two','human','delete',digest)
        self.assertEqual(self.store.get('role','task-two')['content'],different)
        self.assertEqual(self.store.get('role','task-two')['status'],'committed')

    def test_untrusted_body_never_saved_in_deletion_receipt(self):
        self.client.delete_checked.return_value={**self.receipt,'content':'DO_NOT_STORE','nested':{'body':'DO_NOT_STORE'}}
        result=self.delete()
        self.assertNotIn('DO_NOT_STORE',str(result));self.assertNotIn('nested',result['receipt'])

    def test_block_evidence_is_metadata_only_and_never_inferred_from_missing_file(self):
        self.client.delete_checked.return_value={**self.receipt,'status':'deleting','indexSourceAbsent':False,'roleBlocked':True}
        self.assertIs(self.delete()['receipt']['roleBlocked'],True)
        self.client.observe_deletion.return_value={**self.receipt,'status':'deleting','indexSourceAbsent':False,'roleBlocked':1}
        self.assertIsNone(self.deleter.reconcile_delete('task')['receipt']['roleBlocked'])

    def test_uncommitted_memory_cannot_be_deleted(self):
        self.writer.approve('uncommitted',self.text,MemoryApproval('uncommitted',self.digest,'approve-pending'))
        with self.assertRaises(Conflict):self.deleter.request_delete('uncommitted',actor='human',decision_id='delete-pending',expected_hash=self.digest)
        self.client.delete_checked.assert_not_called()

    def test_failed_upgrade_transaction_preserves_v1(self):
        from unittest.mock import patch
        with self.store.connect() as db:
            db.execute('DROP TABLE memory_deletions');db.execute('UPDATE installation SET schema_version=1')
        with patch('agent_line.memory.DELETION_SCHEMA','INVALID SQL'):
            with self.assertRaises(sqlite3.DatabaseError):MemoryCommitStore.upgrade(self.path)
        reopened=MemoryCommitStore(self.path)
        self.assertEqual(reopened.schema_version,1);self.assertEqual(reopened.get('role','task')['content'],self.text)

    def test_native_needs_review_is_visible_in_commit_status(self):
        self.client.delete_checked.return_value={**self.receipt,'status':'needs_review','fileAbsent':False}
        self.assertEqual(self.delete()['status'],'needs_review')
        self.assertEqual(self.store.get('role','task')['status'],'needs_review')
        self.assertEqual(self.store.get('role','task')['content'],'')

    def test_new_task_same_deleted_hash_is_durable_blocked_without_put(self):
        self.delete();self.client.write_approved.reset_mock()
        approval=MemoryApproval('new-task',self.digest,'new-approval')
        blocked=self.writer.approve('new-task',self.text,approval)
        self.assertEqual(blocked['status'],'needs_review');self.assertEqual(blocked['content'],'')
        self.assertEqual(blocked['task_id'],'new-task')
        with self.store.connect() as db:count=db.execute('SELECT count(*) FROM memory_events').fetchone()[0]
        self.assertEqual(self.writer.approve('new-task',self.text,approval),blocked)
        self.assertEqual(self.writer.commit('new-task')['status'],'needs_review')
        self.client.write_approved.assert_not_called();self.client.reconcile.assert_not_called()
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM memory_events').fetchone()[0],count)

    def test_old_approved_intent_and_pending_queue_cannot_reintroduce_hash(self):
        for task,state in [('queued-approved','approved'),('queued-intent','intent'),('queued-pending','pending')]:
            self.writer.approve(task,self.text,MemoryApproval(task,self.digest,'approve-'+task))
            if state!='approved':
                _,row=self.store.claim('role',task)
                if state=='pending':self.store.record('role',task,row['version'],{'status':'pending'})
        self.delete();self.client.write_approved.reset_mock()
        for task in ('queued-approved','queued-intent','queued-pending'):
            row=self.writer.commit(task);self.assertEqual(row['status'],'needs_review');self.assertEqual(row['content'],'')
        self.client.write_approved.assert_not_called();self.client.reconcile.assert_not_called()

    def test_hash_tombstone_does_not_block_another_role(self):
        self.delete();other=Mock();other.role='other-role'
        other.write_approved.return_value={'roleAgentId':'other-role','taskId':'another','contentSha256':self.digest,'status':'committed'}
        writer=MemoryCoordinator(self.store,other)
        self.assertEqual(writer.approve('another',self.text,MemoryApproval('another',self.digest,'other-review'))['status'],'approved')
        self.assertEqual(writer.commit('another')['status'],'committed');other.write_approved.assert_called_once()

    def test_role_delete_waits_for_another_task_writer(self):
        started=threading.Event();release=threading.Event()
        self.writer.approve('other-task',self.text,MemoryApproval('other-task',self.digest,'other-approval'))
        def write(task,content,**kw):
            started.set();release.wait(2)
            return {'roleAgentId':'role','taskId':task,'contentSha256':self.digest,'status':'committed'}
        self.client.write_approved.side_effect=write
        with ThreadPoolExecutor(max_workers=2) as pool:
            write_future=pool.submit(self.writer.commit,'other-task');self.assertTrue(started.wait(1))
            delete_future=pool.submit(self.delete)
            time.sleep(.1);self.client.delete_checked.assert_not_called()
            release.set();write_future.result();self.assertEqual(delete_future.result()['status'],'deleted')

    def test_every_tombstone_phase_blocks_new_task_same_hash(self):
        self.store.reserve_delete('role','task','human','delete',self.digest)
        phases=[('intent',None),('deleting',{}),('needs_review',{**self.receipt,'status':'needs_review','fileAbsent':False}),('deleted',self.receipt)]
        for phase,receipt in phases:
            if receipt is not None:self.store.record_deletion('role','task',receipt)
            self.assertEqual(self.store.get_deletion('role','task')['status'],phase)
            task='new-'+phase
            row=self.writer.approve(task,self.text,MemoryApproval(task,self.digest,'approve-'+phase))
            self.assertEqual(row['status'],'needs_review');self.assertEqual(row['content'],'')
            self.assertEqual(self.writer.commit(task)['status'],'needs_review')
        self.client.write_approved.assert_called_once();self.client.reconcile.assert_not_called()
