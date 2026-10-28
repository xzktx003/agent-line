import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from agent_line.sim_journal import SimJournal
from agent_line.store import AdmissionClosed, TaskStore


class WorkflowMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.store = TaskStore.initialize(root / 'tasks.sqlite')
        self.journal = SimJournal.initialize(root / 'sim.sqlite')
        self.client = Mock()
        self.client.execute.return_value = {'transportStatus': 'accepted'}
        self.client.resume.return_value = {'transportStatus': 'accepted'}

    def attach(self):
        self.journal.mutation_guard = self.store.workflow_admission

    def test_guard_blocks_new_intent_and_native_submission(self):
        self.attach()
        self.store.enter_maintenance()
        with self.assertRaises(AdmissionClosed):
            self.journal.execute(self.client, 'workflow', 'new-run', {})
        with self.assertRaises(KeyError):
            self.journal.get('workflow', 'new-run')
        self.client.execute.assert_not_called()

    def test_previous_receipts_remain_readable_without_new_post(self):
        self.attach()
        original = self.journal.execute(self.client, 'workflow', 'run', {})
        self.journal.resume(self.client, 'workflow', 'run', 'context', {})
        self.store.enter_maintenance()
        reopened = SimJournal(self.journal.path)
        reopened.mutation_guard = TaskStore(self.store.path).workflow_admission
        self.assertEqual(reopened.execute(self.client, 'workflow', 'run', {}), original)
        reopened.resume(self.client, 'workflow', 'run', 'context', {})
        with self.assertRaises(AdmissionClosed):
            reopened.resume(self.client, 'workflow', 'run', 'new-context', {})
        self.assertEqual(self.client.execute.call_count, 1)
        self.assertEqual(self.client.resume.call_count, 1)


def maintenance_in_other_process(path, ready, acquired, result):
    ready.set()
    owner = TaskStore(Path(path)).enter_maintenance()
    result.put(owner)
    acquired.set()


def hold_submission_in_other_process(path, held, release):
    with TaskStore(Path(path)).workflow_admission():
        held.set()
        release.wait(10)


class WorkflowMaintenanceConcurrencyTests(unittest.TestCase):
    def setUp(self):
        import multiprocessing
        self.context = multiprocessing.get_context('spawn')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = TaskStore.initialize(Path(self.temp.name) / 'tasks.sqlite')

    def start(self, target, *args):
        process = self.context.Process(target=target, args=(str(self.store.path), *args))
        process.start()
        def cleanup():
            if process.is_alive():
                process.terminate()
            process.join(5)
        self.addCleanup(cleanup)
        return process

    def test_maintenance_waits_for_other_process_submission_and_persists(self):
        ready, acquired = self.context.Event(), self.context.Event()
        result = self.context.Queue()
        with self.store.workflow_admission():
            process = self.start(maintenance_in_other_process, ready, acquired, result)
            self.assertTrue(ready.wait(5))
            self.assertFalse(acquired.wait(.15))
            self.assertFalse(self.store.maintenance_status()['guarded'])
        self.assertTrue(acquired.wait(5))
        process.join(5)
        self.assertEqual(process.exitcode, 0)
        reopened = TaskStore(self.store.path)
        with self.assertRaises(AdmissionClosed):
            with reopened.workflow_admission():
                self.fail('guarded callback entered')
        reopened.leave_maintenance(result.get(timeout=5))

    def test_process_death_releases_submission_lock(self):
        import threading
        held, release = self.context.Event(), self.context.Event()
        process = self.start(hold_submission_in_other_process, held, release)
        self.assertTrue(held.wait(5))
        outcome = []
        done = threading.Event()
        def maintain():
            outcome.append(self.store.enter_maintenance())
            done.set()
        thread = threading.Thread(target=maintain, daemon=True)
        thread.start()
        self.assertFalse(done.wait(.15))
        process.terminate()
        process.join(5)
        self.assertTrue(done.wait(5))
        thread.join(5)
        self.store.leave_maintenance(outcome[0])

    def test_invalid_lock_never_allows_submission(self):
        lock = self.store.path.with_name(self.store.path.name + '.admission.lock')
        target = Path(self.temp.name) / 'unrelated'
        target.write_text('preserve')
        lock.symlink_to(target)
        with self.assertRaises(AdmissionClosed):
            with self.store.workflow_admission():
                self.fail('symlink lock entered')
        self.assertEqual(target.read_text(), 'preserve')
        lock.unlink()
        lock.write_text('')
        lock.chmod(0o644)
        with self.assertRaises(AdmissionClosed):
            with self.store.workflow_admission():
                self.fail('public lock entered')
