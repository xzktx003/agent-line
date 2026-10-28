import hashlib
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import httpx

from agent_line.adapters.qwenpaw_memory import MemoryApproval, QwenPawMemoryClient
from agent_line.memory import MemoryCommitStore, MemoryCoordinator
from agent_line.store import Conflict


class MemoryCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'memory.db'
        self.store = MemoryCommitStore.initialize(self.path)
        self.content = 'Approved verified project fact'
        self.approval = MemoryApproval('task-1', hashlib.sha256(self.content.encode()).hexdigest(), 'review-1')
        self.remote = None
        self.puts = 0
        self.rejection_status = None
        self.lock = threading.Lock()
        def handler(request):
            with self.lock:
                if request.method == 'PUT':
                    self.puts += 1
                    if self.rejection_status is not None:
                        return httpx.Response(self.rejection_status,json={'detail':'fixture prewrite rejection'})
                    self.remote = self.content
                    return httpx.Response(200, json={'written':True})
                return httpx.Response(404) if self.remote is None else httpx.Response(200,json={'content':self.remote})
        self.http = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(self.http.close)
        self.client = QwenPawMemoryClient('http://127.0.0.1:52403','test',role_agent_id='role-1',client=self.http,native_write_rejections=True)
        self.coordinator = MemoryCoordinator(self.store,self.client)

    def approve(self):
        return self.coordinator.approve('task-1',self.content,self.approval)

    def failed_write(self):
        self.approve();self.rejection_status=403
        row=self.coordinator.commit('task-1')
        self.assertEqual(row['status'],'failed')
        return row

    def test_failed_write_survives_restart_and_reconcile_never_retries_put(self):
        row=self.failed_write()
        restart=MemoryCoordinator(MemoryCommitStore(self.path),self.client)
        self.assertEqual(restart.commit('task-1'),row)
        self.assertEqual(restart.reconcile('task-1'),row)
        self.assertEqual(self.puts,1)

    def test_explicit_retry_consumes_only_the_failed_version(self):
        row=self.failed_write();self.rejection_status=None
        self.assertEqual(self.coordinator.retry('task-1',expected_version=row['version'])['status'],'committed')
        with self.assertRaises(Conflict):self.coordinator.retry('task-1',expected_version=row['version'])
        self.assertEqual(self.puts,2)

    def test_concurrent_retry_has_one_new_put_and_does_not_reuse_old_version(self):
        row=self.failed_write();self.rejection_status=None
        def retry(_):
            try:
                MemoryCoordinator(MemoryCommitStore(self.path),self.client).retry('task-1',expected_version=row['version'])
                return True
            except Conflict:return False
        with ThreadPoolExecutor(max_workers=6) as pool:results=list(pool.map(retry,range(8)))
        self.assertEqual(sum(results),1);self.assertEqual(self.puts,2)

    def test_crash_after_retry_intent_does_not_submit_again(self):
        row=self.failed_write();self.rejection_status=None
        self.store.retry_claim('role-1','task-1',row['version'])
        restart=MemoryCoordinator(MemoryCommitStore(self.path),self.client)
        self.assertEqual(restart.commit('task-1')['status'],'pending')
        with self.assertRaises(Conflict):restart.retry('task-1',expected_version=row['version'])
        self.assertEqual(self.puts,1)

    def test_retry_rejects_invalid_versions_and_deleted_content(self):
        row=self.failed_write()
        for version in (True,0,-1,'3'):
            with self.subTest(version=version),self.assertRaises(ValueError):
                self.coordinator.retry('task-1',expected_version=version)
        with self.store.connect() as db:
            db.execute("INSERT INTO memory_deletions VALUES(?,?,?,?,?,?,?,?,?,?)",
                ('role-1','other-task','reviewer','delete-other',row['content_hash'],'intent','{}',1,1,1))
        with self.assertRaises(Conflict):self.coordinator.retry('task-1',expected_version=row['version'])
        self.assertEqual(self.puts,1)

    def test_failed_readonly_reconcile_can_accept_matching_content(self):
        self.failed_write();self.remote=self.content
        self.assertEqual(self.coordinator.reconcile('task-1')['status'],'committed')
        self.assertEqual(self.puts,1)

    def test_failed_readonly_conflict_blocks_retry_without_writing(self):
        row=self.failed_write();self.remote="different existing memory"
        self.assertEqual(self.coordinator.reconcile("task-1")["status"],"needs_review")
        with self.assertRaises(Conflict):
            self.coordinator.retry("task-1",expected_version=row["version"])
        self.assertEqual(self.puts,1)

    def test_explicit_initialization_and_no_implicit_approval(self):
        with self.assertRaises(ValueError):MemoryCommitStore(self.path.parent/'missing.db')
        with self.assertRaises(FileExistsError):MemoryCommitStore.initialize(self.path)
        self.assertEqual(self.coordinator.approve('task-1',self.content,None)['status'],'needs_review')
        with self.assertRaises(KeyError):self.coordinator.commit('task-1')
        self.assertEqual(self.puts,0)

    def test_approved_write_uses_real_adapter_then_restart_reuses(self):
        self.assertEqual(self.approve()['status'],'approved')
        self.assertEqual(self.coordinator.commit('task-1')['status'],'committed')
        reboot=MemoryCoordinator(MemoryCommitStore(self.path),self.client)
        self.assertEqual(reboot.commit('task-1')['status'],'committed')
        self.assertEqual(self.puts,1)

    def test_concurrent_calls_only_one_put(self):
        self.approve()
        def commit(_):
            return MemoryCoordinator(MemoryCommitStore(self.path),self.client).commit('task-1')
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(commit,range(16)))
        self.assertEqual(self.coordinator.commit('task-1')['status'],'committed')
        self.assertEqual(self.puts,1)

    def test_crash_after_intent_before_network_never_resubmits(self):
        self.approve()
        mode,row=self.store.claim('role-1','task-1')
        self.assertEqual(mode,'write')
        reboot=MemoryCoordinator(MemoryCommitStore(self.path),self.client)
        result=reboot.commit('task-1')
        self.assertEqual(result['status'],'pending')
        self.assertEqual(self.puts,0)
        self.assertEqual(reboot.commit('task-1')['status'],'pending')
        self.assertEqual(self.puts,0)

    def test_lost_receipt_exception_then_restart_only_reconciles(self):
        self.approve()
        original=self.client.write_approved
        def lost(*args,**kwargs):
            original(*args,**kwargs)
            raise RuntimeError('receipt lost')
        self.client.write_approved=lost
        self.assertEqual(self.coordinator.commit('task-1')['status'],'pending')
        self.client.write_approved=lambda *a,**kw:self.fail('Must only reconcile after intent')
        reboot=MemoryCoordinator(MemoryCommitStore(self.path),self.client)
        self.assertEqual(reboot.commit('task-1')['status'],'committed')
        self.assertEqual(self.puts,1)

    def test_changed_content_or_decision_does_not_replace_approval(self):
        self.approve()
        changed=MemoryApproval('task-1',hashlib.sha256(b'changed').hexdigest(),'review-2')
        self.assertEqual(self.coordinator.approve('task-1','changed',changed)['status'],'needs_review')
        other=MemoryApproval('task-1',self.approval.content_sha256,'other-review')
        self.assertEqual(self.coordinator.approve('task-1',self.content,other)['status'],'needs_review')
        self.assertEqual(self.store.get('role-1','task-1')['content'],self.content)
        self.assertEqual(self.puts,0)

    def test_late_pending_cannot_regress_committed(self):
        self.approve()
        _,row=self.store.claim('role-1','task-1')
        self.assertTrue(self.store.record('role-1','task-1',row['version'],{'status':'committed','reason':'verified'}))
        self.assertFalse(self.store.record('role-1','task-1',row['version'],{'status':'pending','reason':'late'}))
        self.assertEqual(self.store.get('role-1','task-1')['status'],'committed')

    def test_missing_database_after_open_is_not_recreated(self):
        self.path.unlink()
        with self.assertRaises(Exception):self.approve()
        self.assertFalse(self.path.exists())

    def test_database_receipt_failure_keeps_intent_and_reconciles(self):
        self.approve()
        original=self.store.record
        self.store.record=lambda *a,**k:(_ for _ in ()).throw(RuntimeError('database unavailable'))
        with self.assertRaises(RuntimeError):self.coordinator.commit('task-1')
        self.store.record=original
        self.assertEqual(self.store.get('role-1','task-1')['status'],'intent')
        reboot=MemoryCoordinator(MemoryCommitStore(self.path),self.client)
        self.assertEqual(reboot.commit('task-1')['status'],'committed')
        self.assertEqual(self.puts,1)

    def test_wrong_role_receipt_does_not_commit(self):
        self.approve()
        self.client.write_approved=lambda *a,**kw:{'status':'committed','taskId':'task-1',
            'roleAgentId':'other-role','contentSha256':self.approval.content_sha256}
        self.assertEqual(self.coordinator.commit('task-1')['status'],'pending')

    def test_other_role_has_no_access_to_approval(self):
        self.approve()
        other=QwenPawMemoryClient('http://qwenpaw.invalid','test',role_agent_id='role-2',client=self.http)
        with self.assertRaises(KeyError):MemoryCoordinator(self.store,other).commit('task-1')
        self.assertEqual(self.puts,0)

    def test_readonly_reconcile_never_submits_approved_memory(self):
        approved=self.approve()
        self.client.write_approved=Mock(side_effect=AssertionError('No first PUT'))
        self.client.reconcile=Mock(side_effect=AssertionError('No GET before submission intent'))
        self.assertEqual(self.coordinator.reconcile('task-1'),approved)
        self.client.write_approved.assert_not_called();self.client.reconcile.assert_not_called()

    def test_readonly_reconcile_intent_uses_official_get_and_committed_replay_does_not_change_version(self):
        self.approve();self.store.claim('role-1','task-1')
        self.remote=self.content
        restarted=MemoryCoordinator(MemoryCommitStore(self.path),self.client)
        first=restarted.reconcile('task-1')
        self.assertEqual(first['status'],'committed')
        self.assertEqual(restarted.reconcile('task-1'),first)
        self.assertEqual(self.puts,0)

    def test_readonly_reconcile_missing_or_other_role_never_creates_approval(self):
        with self.assertRaises(KeyError):self.coordinator.reconcile('absent')
        self.approve()
        other=QwenPawMemoryClient('http://qwenpaw.invalid','test',role_agent_id='role-2',client=self.http)
        with self.assertRaises(KeyError):MemoryCoordinator(self.store,other).reconcile('task-1')
        self.assertEqual(self.puts,0)

    def test_late_readonly_pending_cannot_regress_concurrent_committed_memory(self):
        self.approve();self.store.claim('role-1','task-1')
        entered=threading.Event();release=threading.Event()
        first=MemoryCoordinator(MemoryCommitStore(self.path),self.client)
        other_client=Mock();other_client.role='role-1'
        other=MemoryCoordinator(MemoryCommitStore(self.path),other_client)
        def slow(task,content):
            entered.set()
            if not release.wait(3):raise TimeoutError()
            return {'taskId':task,'roleAgentId':'role-1','contentSha256':self.approval.content_sha256,
                    'status':'pending','reason':'late_readback'}
        self.client.reconcile=slow
        other_client.reconcile.return_value={'taskId':'task-1','roleAgentId':'role-1',
            'contentSha256':self.approval.content_sha256,'status':'committed','reason':'official_readback_matches'}
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending=pool.submit(first.reconcile,'task-1')
            self.assertTrue(entered.wait(3))
            try:self.assertEqual(other.reconcile('task-1')['status'],'committed')
            finally:release.set()
            self.assertEqual(pending.result(timeout=3)['status'],'committed')
        other_client.write_approved.assert_not_called()
