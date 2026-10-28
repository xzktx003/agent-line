"""Durable serial admission uses registration order, not dispatcher scan order."""
import copy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from agent_line.store import TaskStore


class ExecutionQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'tasks.sqlite'
        self.store=TaskStore.initialize(self.path)

    def register(self, key, *, role='role', workspace='/queue/project', project='project'):
        payload={'projectId':project,'roleAgentId':role,'roleBindingId':'binding-'+role,
                 'workspace':workspace,'input':{'task':key},
                 'workflow':{'id':'workflow','runId':key,'nodeExecutionId':'node','attemptId':'1'}}
        return self.store.register(key,payload)[0]

    def test_later_task_cannot_overtake_older_registration_even_with_clock_reversal(self):
        first=self.register('first');later=self.register('later')
        with self.store.connect() as db:db.execute('UPDATE tasks SET created_at=-100 WHERE id=?',(later['id'],))
        self.assertFalse(self.store.claim(later['id'],'/queue/project'))
        self.assertEqual(self.store.execution_queue(later['id'])['reason'],'earlier_registered_task')
        self.assertTrue(self.store.claim(first['id'],'/queue/project'))

    def test_independent_role_and_workspace_can_claim_without_waiting(self):
        self.register('first')
        independent=self.register('independent',role='other',workspace='/queue/independent')
        self.assertTrue(self.store.claim(independent['id'],'/queue/independent'))

    def test_same_role_is_serial_even_when_workspaces_do_not_overlap(self):
        first=self.register('first');later=self.register('later',workspace='/queue/other')
        self.assertTrue(self.store.claim(first['id'],'/queue/project'))
        self.assertFalse(self.store.claim(later['id'],'/queue/other'))
        self.assertEqual(self.store.execution_queue(later['id'])['reason'],'role_in_use')

    def test_overlapping_workspaces_wait_across_distinct_roles_and_projects(self):
        first=self.register('first');later=self.register('later',role='other',project='private',workspace='/queue/project/child')
        projection=self.store.execution_queue(later['id'])
        self.assertEqual(projection,{'policy':'serial_fifo','state':'waiting','reason':'earlier_registered_task'})
        self.assertNotIn(first['id'],str(projection))
        self.assertTrue(self.store.claim(first['id'],'/queue/project'))
        self.assertEqual(self.store.execution_queue(later['id'])['reason'],'workspace_in_use')

    def test_restart_and_uncertain_or_cancel_requested_owner_keep_queue_closed(self):
        first=self.register('first');later=self.register('later')
        self.assertTrue(self.store.claim(first['id'],'/queue/project'))
        self.store.mark_submission_uncertain(first['id'])
        restarted=TaskStore(self.path)
        self.assertFalse(restarted.claim(later['id'],'/queue/project'))
        restarted.request_cancel(first['id'])
        self.assertFalse(restarted.claim(later['id'],'/queue/project'))
        self.assertEqual(restarted.get(first['id'])['executionStatus'],'cancel_requested')

    def test_cancel_unstarted_head_releases_position_but_preserves_record(self):
        first=self.register('first');later=self.register('later')
        self.assertTrue(self.store.request_cancel(first['id']))
        self.assertTrue(self.store.claim(later['id'],'/queue/project'))
        self.assertEqual(self.store.get(first['id'])['executionStatus'],'canceled')

    def test_competing_later_claims_never_bypass_head(self):
        first=self.register('first');later=[self.register(str(n)) for n in range(8)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda row:TaskStore(self.path).claim(row['id'],'/queue/project'),reversed(later)))
        self.assertFalse(any(results))
        self.assertTrue(self.store.claim(first['id'],'/queue/project'))

    def test_queue_reads_are_side_effect_free_and_maintenance_is_visible(self):
        task=self.register('first')
        before=copy.deepcopy(self.store.get(task['id']))
        with self.store.connect() as db:events=[tuple(row) for row in db.execute('SELECT * FROM events')]
        self.assertEqual(self.store.execution_queue(task['id'])['reason'],'awaiting_checks')
        owner=self.store.enter_maintenance()
        try:self.assertEqual(self.store.execution_queue(task['id'])['reason'],'admission_closed')
        finally:self.store.leave_maintenance(owner)
        self.assertEqual(self.store.get(task['id']),before)
        with self.store.connect() as db:self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM events')],events)

    def test_terminal_owner_waits_for_confirmed_resource_exit(self):
        first=self.register('first');later=self.register('later',workspace='/queue/other')
        self.store.claim(first['id'],'/queue/project')
        self.store.accept(first['id'],{'upstreamTaskId':'native-first'})
        self.store.finish(first['id'],'succeeded',{'artifact':'retained'},[
            {'kind':'execution_terminal','executionId':'native-first','confirmed':True}])
        self.assertFalse(self.store.claim(later['id'],'/queue/other'))
        self.store.confirm_resources_released(first['id'],'native-first')
        self.assertTrue(self.store.claim(later['id'],'/queue/other'))
        self.assertEqual(self.store.get(first['id'])['output'],{'artifact':'retained'})

    def test_blocked_head_keeps_its_position_and_unrelated_lane_progresses(self):
        first=self.register('first');later=self.register('later')
        self.store.block_role_revision(first['id'],'role_revision_mismatch')
        self.assertFalse(self.store.claim(later['id'],'/queue/project'))
        independent=self.register('independent',role='other',workspace='/elsewhere')
        self.assertTrue(self.store.claim(independent['id'],'/elsewhere'))

    def test_missing_or_duplicate_registration_identity_never_authorizes_claim(self):
        task=self.register('first')
        with self.store.connect() as db:db.execute("DELETE FROM events WHERE task_id=?",(task['id'],))
        self.assertEqual(self.store.execution_queue(task['id'])['reason'],'state_unknown')
        self.assertFalse(self.store.claim(task['id'],'/queue/project'))

    def test_same_native_role_is_serial_even_across_project_labels(self):
        first=self.register('first')
        later=self.register('later',project='other-project',workspace='/different-workspace')
        self.assertFalse(self.store.claim(later['id'],'/different-workspace'))
        self.assertTrue(self.store.claim(first['id'],'/queue/project'))
        self.assertEqual(self.store.execution_queue(later['id'])['reason'],'role_in_use')
