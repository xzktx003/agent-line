import multiprocessing
import tempfile
import threading
import unittest
from pathlib import Path

from agent_line.sim_journal import SimJournal
from agent_line.store import TaskStore, Conflict, canonical
from agent_line.run_control import RunControl, RunControlClosed


def _child_pause(journal_path, task_path, ready, done):
    control = RunControl(SimJournal(journal_path), TaskStore(task_path))
    ready.set()
    control.apply(project_id='p', workflow_id='w', run_id='r', action='pause',
                  operation_id='child', expected_version=0, active_execution='continue')
    done.set()


class RunControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.journal = SimJournal.initialize(self.path / 'sim.sqlite')
        self.tasks = TaskStore.initialize(self.path / 'tasks.sqlite')
        self.control = RunControl(self.journal, self.tasks)

    def apply(self, action='pause', op='one', version=0, **kw):
        if action == 'pause' and 'active_execution' not in kw:
            kw['active_execution'] = 'continue'
        return self.control.apply(project_id='p', workflow_id='w', run_id='r',
                                  action=action, operation_id=op, expected_version=version, **kw)

    def task(self, name, project='p', workflow='w', run='r'):
        workspace = self.path / name
        workspace.mkdir()
        task, _ = self.tasks.register(name, {'projectId': project, 'roleAgentId': 'role-' + name,
            'workspace': str(workspace), 'workflow': {'id': workflow, 'runId': run,
            'nodeExecutionId': name, 'attemptId': 'one'}, 'input': {}})
        return task['id'], workspace

    def test_replay_cas_stop_and_reopen(self):
        first = self.apply()
        self.assertEqual(first['state'], 'paused')
        with self.assertRaises(Conflict):
            self.apply(action='stop')
        with self.assertRaises(Conflict):
            self.apply(op='two')
        self.apply('continue', 'two', 1, validate_continue=lambda: True)
        self.assertEqual(self.apply(), first)
        self.assertEqual(self.control.status('w', 'r')['version'], 2)
        self.apply('stop', 'three', 2)
        reopened = RunControl(SimJournal(self.journal.path), self.tasks)
        self.assertEqual(reopened.status('w', 'r')['state'], 'stop_requested')
        with self.assertRaises(Conflict):
            self.apply('continue', 'four', 3, validate_continue=lambda: True)
        self.assertEqual(reopened.status('w', 'other')['version'], 0)

    def test_continue_is_explicit_and_outside_transaction(self):
        self.apply()
        for answer in (None, False, 1):
            with self.assertRaises(Conflict):
                self.apply('continue', 'two', 1, validate_continue=lambda: answer)
        def validate():
            with self.journal.connect() as db:
                db.execute('BEGIN IMMEDIATE')
            return True
        self.apply('continue', 'two', 1, validate_continue=validate)

    def test_shared_gate_drains_before_pause_and_is_reentrant(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def protected():
            with self.control.admission('w', 'r'):
                with RunControl(self.journal, self.tasks).admission('w', 'r'):
                    entered.set()
                    release.wait(3)
        t = threading.Thread(target=protected)
        t.start()
        self.assertTrue(entered.wait(2))
        changer = threading.Thread(target=lambda: (self.apply(), finished.set()))
        changer.start()
        self.assertFalse(finished.wait(.1))
        release.set()
        t.join(3)
        changer.join(3)
        self.assertTrue(finished.is_set())
        with self.assertRaises(RunControlClosed) as raised:
            with self.control.admission('w', 'r'):
                self.fail('closed')
        self.assertEqual(raised.exception.state, 'paused')

    def test_apply_inside_admission_rejected(self):
        with self.control.admission('w', 'r'):
            with self.assertRaises(Conflict):
                self.apply()

    def test_corrupt_record_and_private_lock_fail_closed(self):
        self.apply()
        lock = next(self.path.glob('*.control.lock'))
        self.assertEqual(lock.stat().st_mode & 0o777, 0o600)
        lock.chmod(0o644)
        with self.assertRaises(Conflict):
            self.control.status('w', 'r')
        lock.chmod(0o600)
        with self.journal.connect() as db:
            db.execute("UPDATE operations SET receipt='{}'")
        with self.assertRaises(Conflict):
            self.control.status('w', 'r')

    def test_task_scope_and_cancel_requests_only(self):
        ids = []
        for index, (project, workflow, run) in enumerate((('p','w','r'), ('q','w','r'), ('p','x','r'), ('p','w','s'))):
            task, _ = self.tasks.register(str(index), {'projectId': project, 'roleAgentId': 'role',
                'workspace': str(self.path), 'workflow': {'id': workflow, 'runId': run,
                'nodeExecutionId': str(index), 'attemptId': 'one'}, 'input': {}})
            ids.append(task['id'])
        self.tasks.claim(ids[0], str(self.path))
        self.assertEqual([t['id'] for t in self.control.run_tasks('p','w','r')], ids[:1])
        self.control.cancel_tasks('p','w','r')
        self.control.cancel_tasks('p','w','r')
        task = self.tasks.get(ids[0])
        self.assertTrue(task['cancellationRequested'])
        self.assertFalse(task['resourcesReleased'])
        self.assertFalse(self.tasks.get(ids[1])['cancellationRequested'])

    def test_unknown_context_ignored_missing_database_rejected(self):
        self.journal.reserve_publication('w', 'r', {'state': 'stop_requested'})
        self.assertEqual(self.control.status('w', 'r')['state'], 'active')
        self.journal.path.unlink()
        with self.assertRaises(Conflict):
            self.control.status('w', 'r')

    def test_cross_process_gate(self):
        ctx = multiprocessing.get_context('spawn')
        ready, done = ctx.Event(), ctx.Event()
        child = ctx.Process(target=_child_pause,
                            args=(self.journal.path, self.tasks.path, ready, done))
        with self.control.admission('w', 'r'):
            child.start()
            self.assertTrue(ready.wait(5))
            self.assertFalse(done.wait(.15))
        child.join(5)
        self.assertEqual(child.exitcode, 0)
        self.assertTrue(done.is_set())
        self.assertEqual(self.control.status('w', 'r')['state'], 'paused')

    def test_concurrent_version_cas_only_one_command(self):
        barrier = threading.Barrier(2)
        accepted, conflicts = [], []
        def apply(op):
            barrier.wait()
            try:
                accepted.append(self.apply(op=op))
            except Conflict:
                conflicts.append(op)
        threads = [threading.Thread(target=apply, args=(op,)) for op in ('a', 'b')]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
        self.assertEqual((len(accepted), len(conflicts)), (1, 1))
        self.assertEqual(self.control.status('w', 'r')['version'], 1)

    def test_record_gap_invalid_status_and_journal_mode_rejected(self):
        self.apply()
        self.apply(op='two', version=1)
        with self.journal.connect() as db:
            db.execute("DELETE FROM operations WHERE context='run-control-one'")
        with self.assertRaises(Conflict):
            self.control.status('w', 'r')
        with self.journal.connect() as db:
            db.execute('DELETE FROM operations')
        self.journal.path.chmod(0o644)
        with self.assertRaises(Conflict):
            self.control.status('w', 'r')

    def test_wrong_project_and_boolean_version_rejected(self):
        self.apply()
        with self.assertRaises(Conflict):
            self.control.apply(project_id='other', workflow_id='w', run_id='r',
                               action='stop', operation_id='two', expected_version=1)
        with self.assertRaises(ValueError):
            self.apply(version=True)

    def test_new_pause_requires_explicit_active_execution_choice(self):
        with self.assertRaises(ValueError):
            self.control.apply(project_id='p', workflow_id='w', run_id='r', action='pause',
                               operation_id='missing', expected_version=0)
        for action in ('continue', 'stop'):
            with self.assertRaises(ValueError):
                self.control.apply(project_id='p', workflow_id='w', run_id='r', action=action,
                                   operation_id=action, expected_version=0, active_execution='continue')
        self.assertEqual(self.control.status('w', 'r')['version'], 0)

    def test_continue_choice_does_not_cancel_and_interrupt_only_targets_live_submissions(self):
        kept, kept_workspace = self.task('kept')
        self.tasks.claim(kept, kept_workspace)
        self.tasks.accept(kept, {'upstreamTaskId': 'kept'})
        self.apply(active_execution='continue')
        self.assertEqual(self.tasks.get(kept)['executionStatus'], 'accepted')
        self.assertFalse(self.tasks.get(kept)['cancellationRequested'])
        self.assertEqual(self.control.status('w', 'r')['activeExecution'], 'continue')
        self.apply('continue', 'resume', 1, validate_continue=lambda: True)
        self.assertNotIn('activeExecution', self.control.status('w', 'r'))

        queued, queued_workspace = self.task('queued')
        submitting, submitting_workspace = self.task('submitting')
        uncertain, uncertain_workspace = self.task('uncertain')
        accepted, accepted_workspace = self.task('accepted')
        outsider, outsider_workspace = self.task('outsider', run='other')
        self.tasks.claim(submitting, submitting_workspace)
        self.tasks.claim(uncertain, uncertain_workspace)
        self.tasks.mark_submission_uncertain(uncertain)
        self.tasks.claim(accepted, accepted_workspace)
        self.tasks.accept(accepted, {'upstreamTaskId': 'native'})
        self.tasks.claim(outsider, outsider_workspace)
        self.tasks.accept(outsider, {'upstreamTaskId': 'other'})
        receipt = self.apply('pause', 'interrupt', 2, active_execution='interrupt')
        self.assertEqual(receipt['state'], 'paused')
        self.assertEqual(self.control.status('w', 'r')['activeExecution'], 'interrupt')
        self.assertEqual(self.tasks.get(queued)['executionStatus'], 'registered')
        for task_id in (submitting, uncertain, accepted):
            task = self.tasks.get(task_id)
            self.assertEqual(task['executionStatus'], 'cancel_requested')
            self.assertFalse(task['resourcesReleased'])
        self.assertEqual(self.tasks.get(outsider)['executionStatus'], 'accepted')
        self.assertEqual(self.apply('pause', 'interrupt', 2, active_execution='interrupt'), receipt)
        self.tasks.claim(queued, queued_workspace)
        self.tasks.accept(queued, {'upstreamTaskId': 'later'})
        self.assertEqual(self.apply('pause', 'interrupt', 2, active_execution='interrupt'), receipt)
        self.assertEqual(self.tasks.get(queued)['executionStatus'], 'cancel_requested')
        with self.journal.connect() as db:
            contexts = [row['context'] for row in db.execute('SELECT context FROM operations')]
        self.assertFalse(any(context == 'run-cancel' or context.startswith('run-cancel') for context in contexts))

    def test_legacy_pause_stays_unrecorded_and_exact_replay_does_not_choose(self):
        payload = {'projectId': 'p', 'action': 'pause', 'expectedVersion': 0}
        receipt = {'state': 'paused', 'version': 1, 'operationId': 'legacy'}
        with self.journal.connect() as db:
            db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?)',
                       ('w', 'r', 'run-control-legacy', canonical(payload), 'accepted', canonical(receipt), 1))
        status = self.control.status('w', 'r')
        self.assertEqual(status['state'], 'paused')
        self.assertEqual(status['activeExecution'], 'unrecorded')
        queued, queued_workspace = self.task('queued')
        self.tasks.claim(queued, queued_workspace)
        self.tasks.accept(queued, {'upstreamTaskId': 'native'})
        self.assertEqual(self.control.apply(project_id='p', workflow_id='w', run_id='r', action='pause',
                                            operation_id='legacy', expected_version=0), receipt)
        self.assertEqual(self.tasks.get(queued)['executionStatus'], 'accepted')
        with self.assertRaises(Conflict):
            self.control.apply(project_id='p', workflow_id='w', run_id='r', action='pause',
                               operation_id='legacy', expected_version=0, active_execution='interrupt')
        self.assertEqual(self.tasks.get(queued)['executionStatus'], 'accepted')

    def test_pause_choice_on_other_actions_or_unknown_value_invalidates_history(self):
        payload = {'projectId': 'p', 'action': 'stop', 'expectedVersion': 0, 'activeExecution': 'continue'}
        receipt = {'state': 'stop_requested', 'version': 1, 'operationId': 'bad'}
        with self.journal.connect() as db:
            db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?)',
                       ('w', 'r', 'run-control-bad', canonical(payload), 'accepted', canonical(receipt), 1))
        with self.assertRaises(Conflict):
            self.control.status('w', 'r')

