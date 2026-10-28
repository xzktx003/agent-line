from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_line.memory_review import MemoryReviewLedger
from agent_line.store import Conflict


class MemoryReviewTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        self.path=self.root/'reviews.sqlite';self.now=1000
        self.ledger=MemoryReviewLedger.initialize(self.path,clock=lambda:self.now)
        self.args=dict(project_id='project',role_id='role',task_id='task',actor='zukang.xu',decision_id='decision',content_sha256='a'*64,source={'taskId':'task','executionIdentity':{'codexThreadId':'thread'},'version':1})
    def reserve(self,**changes):return self.ledger.reserve(**{**self.args,**changes})
    def test_explicit_init_and_missing_database_never_recreated(self):
        with self.assertRaises(FileExistsError):MemoryReviewLedger.initialize(self.path)
        missing=self.root/'missing.sqlite'
        with self.assertRaises(FileNotFoundError):MemoryReviewLedger(missing)
        self.path.unlink()
        with self.assertRaises(FileNotFoundError):self.reserve()
        self.assertFalse(self.path.exists());self.assertFalse(missing.exists())
    def test_private_permissions_owner_and_symlink_required(self):
        self.assertEqual(self.path.stat().st_mode&0o777,0o600)
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):MemoryReviewLedger(self.path)
        self.path.chmod(0o600)
        with patch('agent_line.memory_review.os.getuid',return_value=self.path.stat().st_uid+1):
            with self.assertRaises(ValueError):MemoryReviewLedger(self.path)
        link=self.root/'linked';link.symlink_to(self.path)
        with self.assertRaises(ValueError):MemoryReviewLedger(link)
    def test_identical_reservation_and_restart_keep_original_time(self):
        first=self.reserve();self.now=2000
        self.assertEqual(self.reserve(source={'version':1,'executionIdentity':{'codexThreadId':'thread'},'taskId':'task'}),first)
        restarted=MemoryReviewLedger(self.path,clock=lambda:3000)
        self.assertEqual(restarted.reserve(**self.args),first)
        self.assertEqual(first['created_at'],1000);self.assertEqual(first['content_hash'],'a'*64)
        self.assertEqual(first['source'],self.args['source']);self.assertNotIn('content',first)
    def test_actor_decision_content_source_and_project_are_immutable(self):
        self.reserve()
        for change in ({'actor':'other'},{'decision_id':'other'},{'content_sha256':'b'*64},{'source':{'taskId':'different'}},{'project_id':'other'}):
            with self.subTest(change=change),self.assertRaises(Conflict):self.reserve(**change)
    def test_decision_id_cannot_be_reused_on_other_tuple(self):
        self.reserve()
        with self.assertRaises(Conflict):self.reserve(task_id='other-task')
        with self.assertRaises(Conflict):self.reserve(role_id='other-role')
    def test_concurrent_identical_review_commits_one_record(self):
        with ThreadPoolExecutor(max_workers=8) as pool:results=list(pool.map(lambda _:self.reserve(),range(8)))
        self.assertTrue(all(row==results[0] for row in results))
        self.assertEqual(len(self.ledger.list({'project'})['reviews']),1)
    def test_competing_decisions_only_one_wins(self):
        def attempt(n):
            try:return self.reserve(decision_id='decision-'+str(n))
            except Conflict:return None
        with ThreadPoolExecutor(max_workers=6) as pool:results=list(pool.map(attempt,range(6)))
        self.assertEqual(sum(row is not None for row in results),1)
    def test_scope_filtered_stable_pagination(self):
        for role,task,project in [('a','one','hidden'),('a','two','project'),('b','one','project'),('b','two','hidden'),('c','one','project')]:
            self.reserve(role_id=role,task_id=task,project_id=project,decision_id=role+'-'+task)
        rows=[];cursor=None
        while True:
            page=self.ledger.list({'project'},after=cursor,limit=1);rows.extend(page['reviews']);cursor=page['next_cursor']
            if cursor is None:break
        self.assertEqual([(r['role_id'],r['task_id']) for r in rows],[('a','two'),('b','one'),('c','one')])
        self.assertEqual(self.ledger.list(set())['reviews'],[])
        with self.assertRaises(KeyError):self.ledger.get('missing','task')
    def test_strict_ids_hashes_source_and_finite_clock(self):
        bad=[{'role_id':'../escape'},{'task_id':''},{'actor':' '},{'decision_id':''},{'content_sha256':'bad'},
             {'source':[]},{'source':{'number':float('nan')}},{'source':{1:'nonstring'}},{'source':{'tuple':(1,2)}}]
        for change in bad:
            with self.subTest(change=change),self.assertRaises(ValueError):self.reserve(**change)
        self.now=float('inf')
        with self.assertRaises(ValueError):self.reserve()
        self.assertEqual(self.ledger.list({'project'})['reviews'],[])
    def test_bad_cursor_and_limits_rejected(self):
        for cursor in ('garbage','e30','WyJyb2xlIl0'):
            with self.assertRaises(ValueError):self.ledger.list({'project'},after=cursor)
        for limit in (0,501,True):
            with self.assertRaises(ValueError):self.ledger.list({'project'},limit=limit)

    def test_concurrent_global_decision_collision_has_single_owner(self):
        def attempt(index):
            try:return self.reserve(task_id='task-'+str(index))
            except Conflict:return None
        with ThreadPoolExecutor(max_workers=6) as pool:results=list(pool.map(attempt,range(6)))
        self.assertEqual(sum(result is not None for result in results),1)
        self.assertEqual(len(self.ledger.list({'project'})['reviews']),1)

    def test_permission_change_after_open_fails_closed(self):
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):self.reserve()
        self.path.chmod(0o600)
        self.assertEqual(self.ledger.list({'project'})['reviews'],[])
