"""Observation freshness is durable metadata, separate from execution outcome."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from agent_line.store import TaskStore


class ExecutionObservationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = TaskStore.initialize(self.root / 'tasks.sqlite')
        self.task = self.register('first')
        self.identity = {'upstreamTaskId': 'original-native', 'codexThreadId': 'thread', 'codexTurnId': 'turn'}
        self.store.claim(self.task['id'], str(self.root))
        self.store.accept(self.task['id'], self.identity)

    def register(self, name):
        task, _ = self.store.register(name, {
            'projectId': 'project', 'roleAgentId': 'role', 'workspace': str(self.root),
            'workflow': {'runId': 'run', 'nodeExecutionId': name, 'attemptId': 'one'}, 'input': {},
        })
        return task

    def observation_events(self, task_id=None):
        return [event for event in self.store.events(task_id or self.task['id'])
                if event['kind'] in ('execution_observed', 'execution_observation_unknown')]

    def terminal(self, status='succeeded'):
        self.store.finish(self.task['id'], status, {'text': 'preserved original output'}, [
            {'kind': 'execution_terminal', 'executionId': 'turn', 'confirmed': True}])

    def release(self):
        receipt = {'source': 'docker-inspect-before-remove', 'owner': 'a' * 32,
                   'containerId': 'b' * 64, 'workspace': str(self.root), 'running': False,
                   'pid': 0, 'status': 'exited', 'finishedAt': '2026-10-22T00:00:00Z',
                   'identities': [self.identity.copy()]}
        self.store.confirm_resources_released(self.task['id'], 'turn', receipt)

    def test_first_unknown_persists_without_changing_execution_or_lease(self):
        before = self.store.get(self.task['id'])
        self.assertIsNone(self.store.last_execution_observation(self.task['id']))
        with patch('agent_line.store.time.time', return_value=123.5):
            self.assertTrue(self.store.record_execution_observation(self.task['id'], 'unknown'))
        reopened = TaskStore(self.store.path)
        self.assertEqual(reopened.last_execution_observation(self.task['id']), {'status': 'unknown', 'updatedAt': 123.5})
        after = reopened.get(self.task['id'])
        self.assertEqual(after['version'], before['version'] + 1)
        self.assertEqual({k: v for k, v in after.items() if k != 'version'},
                         {k: v for k, v in before.items() if k != 'version'})
        self.assertEqual(len(self.observation_events()), 1)
        with reopened.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM workspace_leases WHERE task_id=?',
                                        (self.task['id'],)).fetchone()[0], 1)

    def test_first_observed_and_repeated_observation_do_not_churn(self):
        self.assertTrue(self.store.record_execution_observation(self.task['id'], 'observed'))
        before = self.store.get(self.task['id'])
        events = self.store.events(self.task['id'])
        observation = self.store.last_execution_observation(self.task['id'])
        for _ in range(3):
            self.assertFalse(TaskStore(self.store.path).record_execution_observation(self.task['id'], 'observed'))
        self.assertEqual(self.store.get(self.task['id']), before)
        self.assertEqual(self.store.events(self.task['id']), events)
        self.assertEqual(self.store.last_execution_observation(self.task['id']), observation)

    def test_unknown_then_observed_changes_only_once_per_transition(self):
        before = self.store.get(self.task['id'])
        with patch('agent_line.store.time.time', return_value=100.0):
            self.assertTrue(self.store.record_execution_observation(self.task['id'], 'unknown'))
        self.assertFalse(self.store.record_execution_observation(self.task['id'], 'unknown'))
        with patch('agent_line.store.time.time', return_value=101.0):
            self.assertTrue(self.store.record_execution_observation(self.task['id'], 'observed'))
        self.assertFalse(self.store.record_execution_observation(self.task['id'], 'observed'))
        self.assertEqual(self.store.last_execution_observation(self.task['id']), {'status': 'observed', 'updatedAt': 101.0})
        self.assertEqual([event['kind'] for event in self.observation_events()],
                         ['execution_observation_unknown', 'execution_observed'])
        self.assertEqual(self.store.get(self.task['id'])['version'], before['version'] + 2)

    def test_other_event_kinds_do_not_reset_observation_deduplication(self):
        self.store.record_execution_observation(self.task['id'], 'unknown')
        self.store.set_memory_status(self.task['id'], 'pending')
        before = self.store.get(self.task['id'])
        events = self.store.events(self.task['id'])
        self.assertFalse(self.store.record_execution_observation(self.task['id'], 'unknown'))
        self.assertEqual(self.store.get(self.task['id']), before)
        self.assertEqual(self.store.events(self.task['id']), events)

    def test_terminal_with_released_resources_is_never_modified(self):
        self.terminal()
        self.release()
        for status in ('succeeded', 'failed', 'canceled'):
            with self.subTest(status=status):
                # Independent fixture keeps each real terminal transition valid.
                task = self.register('terminal-' + status)
                self.assertTrue(self.store.claim(task['id'], str(self.root)))
                self.store.accept(task['id'], self.identity)
                self.store.finish(task['id'], status, None, [
                    {'kind': 'execution_terminal', 'executionId': 'turn', 'confirmed': True}])
                receipt = {'source': 'docker-inspect-before-remove', 'owner': 'a' * 32,
                           'containerId': 'b' * 64, 'workspace': str(self.root), 'running': False,
                           'pid': 0, 'status': 'exited', 'finishedAt': '2026-10-22T00:00:00Z',
                           'identities': [self.identity.copy()]}
                self.store.confirm_resources_released(task['id'], 'turn', receipt)
                before = self.store.get(task['id'])
                events = self.store.events(task['id'])
                for observation in ('unknown', 'observed'):
                    self.assertFalse(self.store.record_execution_observation(task['id'], observation))
                self.assertIsNone(self.store.last_execution_observation(task['id']))
                self.assertEqual(self.store.get(task['id']), before)
                self.assertEqual(self.store.events(task['id']), events)

    def test_terminal_release_preserves_preexisting_observation(self):
        self.store.record_execution_observation(self.task['id'], 'observed')
        observation = self.store.last_execution_observation(self.task['id'])
        self.terminal()
        self.release()
        before = self.store.get(self.task['id'])
        events = self.store.events(self.task['id'])
        self.assertFalse(self.store.record_execution_observation(self.task['id'], 'unknown'))
        self.assertEqual(self.store.last_execution_observation(self.task['id']), observation)
        self.assertEqual(self.store.get(self.task['id']), before)
        self.assertEqual(self.store.events(self.task['id']), events)

    def test_terminal_release_allows_one_existing_unknown_to_be_cleared(self):
        self.store.record_execution_observation(self.task['id'], 'unknown')
        self.terminal()
        self.release()
        before = self.store.get(self.task['id'])
        self.assertFalse(self.store.record_execution_observation(self.task['id'], 'unknown'))
        self.assertTrue(self.store.record_execution_observation(self.task['id'], 'observed'))
        after = self.store.get(self.task['id'])
        self.assertEqual(after['version'], before['version'] + 1)
        self.assertEqual({k: v for k, v in after.items() if k != 'version'},
                         {k: v for k, v in before.items() if k != 'version'})
        self.assertEqual(self.store.last_execution_observation(self.task['id'])['status'], 'observed')
        self.assertEqual([event['kind'] for event in self.observation_events()],
                         ['execution_observation_unknown', 'execution_observed'])
        events = self.store.events(self.task['id'])
        for status in ('observed', 'unknown', 'observed'):
            self.assertFalse(TaskStore(self.store.path).record_execution_observation(self.task['id'], status))
        self.assertEqual(self.store.get(self.task['id']), after)
        self.assertEqual(self.store.events(self.task['id']), events)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM workspace_leases WHERE task_id=?',
                                        (self.task['id'],)).fetchone()[0], 0)

    def test_terminal_without_resource_release_can_still_be_unknown(self):
        self.terminal()
        self.assertTrue(self.store.record_execution_observation(self.task['id'], 'unknown'))
        current = self.store.get(self.task['id'])
        self.assertEqual(current['executionStatus'], 'succeeded')
        self.assertFalse(current['resourcesReleased'])
        self.assertEqual(current['executionIdentity'], self.identity)
        self.assertEqual(self.store.last_execution_observation(self.task['id'])['status'], 'unknown')

    def test_invalid_status_is_rejected_without_writes(self):
        before = self.store.get(self.task['id'])
        events = self.store.events(self.task['id'])
        for value in ('accepted', 'running', '', 'unknown ', 'execution_observed', None, True, 1, [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.record_execution_observation(self.task['id'], value)
        self.assertEqual(self.store.get(self.task['id']), before)
        self.assertEqual(self.store.events(self.task['id']), events)

    def test_concurrent_same_status_records_one_event_per_transition(self):
        for status in ('unknown', 'observed'):
            barrier = threading.Barrier(8)
            def record(_):
                store = TaskStore(self.store.path)
                barrier.wait(timeout=5)
                return store.record_execution_observation(self.task['id'], status)
            with ThreadPoolExecutor(max_workers=8) as pool:
                outcomes = list(pool.map(record, range(8)))
            self.assertEqual(outcomes.count(True), 1)
            self.assertEqual(outcomes.count(False), 7)
        self.assertEqual(len(self.observation_events()), 2)

    def test_task_observations_are_isolated_and_missing_task_is_not_created(self):
        other = self.register('second')
        self.store.record_execution_observation(self.task['id'], 'unknown')
        self.assertIsNone(self.store.last_execution_observation(other['id']))
        self.store.record_execution_observation(other['id'], 'observed')
        self.assertEqual(self.store.last_execution_observation(self.task['id'])['status'], 'unknown')
        self.assertEqual(self.store.last_execution_observation(other['id'])['status'], 'observed')
        with self.store.connect() as db:
            counts = (db.execute('SELECT count(*) FROM tasks').fetchone()[0],
                      db.execute('SELECT count(*) FROM events').fetchone()[0])
        with self.assertRaises(KeyError):
            self.store.last_execution_observation('absent')
        with self.assertRaises(KeyError):
            self.store.record_execution_observation('absent', 'unknown')
        with self.store.connect() as db:
            self.assertEqual(counts, (db.execute('SELECT count(*) FROM tasks').fetchone()[0],
                                      db.execute('SELECT count(*) FROM events').fetchone()[0]))

    def test_missing_database_is_not_initialized(self):
        missing = self.root / 'missing.sqlite'
        with self.assertRaises(ValueError):
            TaskStore(missing)
        self.assertFalse(missing.exists())


if __name__ == '__main__':
    unittest.main()
