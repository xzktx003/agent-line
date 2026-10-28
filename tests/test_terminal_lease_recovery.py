"""Trusted resource completion and its owned lease deletion are one transaction."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent_line.dispatcher import Dispatcher, DispatchLoop
from agent_line.store import Conflict, TaskStore
from agent_line.role_dispatch import UnstartedCancellationProof


class TerminalLeaseRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'tasks.sqlite'
        self.store = TaskStore.initialize(self.path)
        self.workspace = str(Path(self.temp.name) / 'work')
        self.identity = {'upstreamTaskId': 'native', 'codexThreadId': 'thread', 'codexTurnId': 'turn'}

    def register(self, key):
        task, _ = self.store.register(key, {'projectId': 'project', 'roleAgentId': 'role',
            'workspace': self.workspace + '/nested/..', 'workflow': {'runId': key,
            'nodeExecutionId': key, 'attemptId': '1'}, 'input': {}})
        return task['id']

    def prepared(self, mode):
        task = self.register(mode)
        self.store.claim(task, self.workspace)
        if mode == 'native':
            self.store.accept(task, self.identity)
            self.store.finish(task, 'succeeded', {}, [
                {'kind': 'execution_terminal', 'executionId': 'turn', 'confirmed': True}])
            receipt = {'source': 'docker-inspect-before-remove', 'owner': 'owner', 'containerId': 'container',
                'workspace': self.workspace + '/nested/..', 'identities': [self.identity], 'running': False,
                'pid': 0, 'status': 'exited', 'finishedAt': '2026-10-21T12:00:00Z'}
            complete = lambda: self.store.confirm_resources_released(task, 'turn', receipt)
            result = {'identity': self.identity, 'status': 'succeeded', 'output': {},
                      'terminalConfirmed': True, 'resourcesReleased': True, 'resourceExitReceipt': receipt}
        else:
            self.store.request_cancel(task)
            receipt = {'source': 'revoked_delegation_and_finished_parent', 'taskId': task,
                'roleAgentId': 'role', 'roleSessionId': 'agent-line-' + task, 'parentTaskId': 'parent',
                'parentFinished': True, 'delegationRevoked': True, 'executionClaimed': False}
            complete = lambda: self.store.resolve_unstarted_cancellation(task, receipt)
            result = None
        return task, complete, receipt, result

    def leases(self):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM workspace_leases')]

    def strand(self, task):
        with self.store.connect() as db:
            db.execute('INSERT OR REPLACE INTO workspace_leases VALUES(?,?,0)', (self.workspace, task))

    def test_completion_atomically_removes_canonical_owned_lease(self):
        for mode in ('native', 'unstarted'):
            with self.subTest(mode=mode):
                task, complete, _, _ = self.prepared(mode)
                self.assertTrue(complete())
                self.assertTrue(self.store.get(task)['resourcesReleased'])
                self.assertEqual(self.leases(), [])

    def test_delete_failure_rolls_back_result_evidence_events_and_lease(self):
        for mode in ('native', 'unstarted'):
            with self.subTest(mode=mode):
                task, complete, _, _ = self.prepared(mode)
                before = self.store.get(task); events = self.store.events(task)
                with self.store.connect() as db:
                    db.execute("CREATE TRIGGER fail_lease_delete BEFORE DELETE ON workspace_leases BEGIN SELECT RAISE(ABORT,'test interruption'); END")
                with self.assertRaises(sqlite3.IntegrityError): complete()
                self.assertEqual(self.store.get(task), before)
                self.assertEqual(self.store.events(task), events)
                self.assertEqual(self.leases()[0]['task_id'], task)
                with self.store.connect() as db: db.execute('DROP TRIGGER fail_lease_delete')
                complete()

    def test_reopen_replay_repairs_stranded_lease_and_preserves_next_owner(self):
        original_path = self.path
        for mode in ('native', 'unstarted'):
            with self.subTest(mode=mode):
                self.path = original_path.with_name('reopen-' + mode + '.sqlite')
                self.workspace = str(self.path.parent / ('work-' + mode))
                self.store = TaskStore.initialize(self.path)
                task, complete, _, _ = self.prepared(mode); complete(); self.strand(task)
                self.store = TaskStore(self.path)
                self.assertIn(task, [x['id'] for x in self.store.dispatch_candidates()])
                self.assertFalse(complete())
                self.assertEqual(self.leases(), [])
                next_task = self.register(mode + '-next')
                self.assertTrue(self.store.claim(next_task, self.workspace))
                self.assertFalse(complete())
                self.assertEqual(self.leases()[0]['task_id'], next_task)
                self.assertNotIn(task, [x['id'] for x in self.store.dispatch_candidates()])
                # Separate cases share one store, so remove only this fixture lease.
                with self.store.connect() as db: db.execute('DELETE FROM workspace_leases WHERE task_id=?', (next_task,))

    def test_quarantined_or_conflicting_replay_keeps_lease(self):
        for mode in ('native', 'unstarted'):
            with self.subTest(mode=mode):
                task, complete, receipt, _ = self.prepared(mode); complete(); self.strand(task)
                if mode == 'native': receipt['containerId'] = 'tampered'
                else: receipt['parentTaskId'] = 'tampered'
                with self.assertRaises(Conflict): complete()
                self.assertEqual(self.leases()[0]['task_id'], task)
                with self.store.connect() as db:
                    original = json.loads(db.execute('SELECT evidence FROM tasks WHERE id=?', (task,)).fetchone()[0])
                    if mode == 'native': receipt['containerId'] = original[-1]['receipt']['containerId']
                    else: receipt['parentTaskId'] = original[0]['proof']['parentTaskId']
                    self.store._event(db, task, 'resources_quarantined')
                with self.assertRaises(Conflict): complete()
                self.assertEqual(self.leases()[0]['task_id'], task)
                with self.store.connect() as db: db.execute('DELETE FROM workspace_leases WHERE task_id=?', (task,))

    def test_dispatch_loop_repairs_legacy_lease_without_submit_or_second_release(self):
        task, complete, _, result = self.prepared('native'); complete(); self.strand(task)
        gateway = Mock(); gateway.observe.return_value = result
        dispatcher = Dispatcher(self.store, {'project': gateway})
        self.store.release_workspace = Mock(side_effect=AssertionError('must be atomic'))
        rows = DispatchLoop(dispatcher, allow_submissions=False).tick()
        self.assertEqual(rows, [{'taskId': task, 'status': 'succeeded'}])
        gateway.submit.assert_not_called()
        self.store.release_workspace.assert_not_called()
        self.assertEqual(self.leases(), [])

    def test_unstarted_dispatch_replay_repairs_lease_without_secondary_release(self):
        task, complete, proof, _ = self.prepared('unstarted'); complete(); self.strand(task)
        gateway = Mock()
        gateway.observe.return_value = {'unstartedCancellationProof':
            UnstartedCancellationProof(task, 'role', proof['parentTaskId'])}
        self.store.release_workspace = Mock(side_effect=AssertionError('must be atomic'))
        result = Dispatcher(self.store, {'project': gateway}).reconcile(task)
        self.assertEqual(result['executionStatus'], 'canceled')
        self.assertTrue(result['resourcesReleased'])
        self.assertEqual(self.leases(), [])
        self.store.release_workspace.assert_not_called()

    def test_corrupted_stored_exit_proof_does_not_release_legacy_lease(self):
        task, complete, _, _ = self.prepared('native'); complete(); self.strand(task)
        with self.store.connect() as db:
            evidence = self.store.get(task)['evidence']
            evidence[-1]['executionIdentity'] = {'codexTurnId': 'other'}
            db.execute('UPDATE tasks SET evidence=? WHERE id=?', (json.dumps(evidence), task))
        with self.assertRaises(Conflict): self.store.confirm_resources_released(task, 'turn')
        self.assertEqual(self.leases()[0]['task_id'], task)

    def test_failure_after_lease_delete_rolls_back_entire_completion(self):
        for mode in ('native', 'unstarted'):
            with self.subTest(mode=mode):
                task, complete, _, _ = self.prepared(mode)
                before = self.store.get(task); events = self.store.events(task)
                with patch.object(self.store, '_event', side_effect=RuntimeError('event write failed')):
                    with self.assertRaises(RuntimeError): complete()
                self.assertEqual(TaskStore(self.path).get(task), before)
                self.assertEqual(self.store.events(task), events)
                self.assertEqual(self.leases()[0]['task_id'], task)
                complete()
