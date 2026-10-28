import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_line.backup import create_backup, restore_backup
from agent_line.store import SCHEMA_VERSION, TaskStore
from tests.test_recovery_records import write_version_two


class EventContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.payload = {
            'projectId': 'project',
            'roleAgentId': 'role',
            'workspace': '/workspace',
            'workflow': {
                'runId': 'run-1',
                'nodeExecutionId': 'activation',
                'attemptId': 'attempt',
            },
            'input': {'task': 'audit'},
        }

    def test_new_store_records_run_source_and_payload(self):
        store = TaskStore.initialize(self.root / 'current.sqlite')
        task, _reused = store.register('same', self.payload)
        event = store.events(task['id'])[0]
        self.assertEqual(event['kind'], 'registered')
        self.assertEqual(event['category'], 'task_registered')
        self.assertEqual(event['run_id'], 'run-1')
        self.assertEqual(event['source_identity'], 'task-store')
        self.assertEqual(event['task_version'], task['version'])
        self.assertEqual(event['payload'], '{}')
        self.assertTrue(event['event_id'])
        self.assertEqual(event['occurred_at'], event['received_at'])
        self.assertEqual(event['received_at'], event['created_at'])
        with sqlite3.connect(store.path) as database:
            self.assertEqual(database.execute(
                'SELECT schema_version FROM installation').fetchone()[0], SCHEMA_VERSION)

    def test_version_three_must_migrate_before_sourced_events(self):
        path = self.root / 'legacy.sqlite'
        write_version_two(path)
        store = TaskStore(path)
        task, _reused = store.register('same', self.payload)
        self.assertTrue(store.migrate_recovery_records())
        with self.assertRaises(ValueError):
            store.record_sourced_event(
                task['id'], kind='recovery_checked', source_identity='operator',
                source_event_id='event-1', payload={'note': 'keep the original'})
        with sqlite3.connect(path) as database:
            self.assertEqual(database.execute(
                'SELECT schema_version FROM installation').fetchone()[0], 3)
            self.assertNotIn(
                'event_id',
                {row[1] for row in database.execute('PRAGMA table_info(events)')})
        with self.assertRaises(ValueError):
            store.migrate_event_categories()
        self.assertTrue(store.migrate_event_contract())
        self.assertFalse(store.migrate_event_contract())
        saved = store.record_sourced_event(
            task['id'], kind='recovery_checked', source_identity='operator',
            source_event_id='event-1', payload={'executionStatus': 'accepted'},
            occurred_at=10)
        repeated = store.record_sourced_event(
            task['id'], kind='recovery_checked', source_identity='operator',
            source_event_id='event-1', payload={'executionStatus': 'failed'})
        current = store.get(task['id'])
        self.assertEqual(current['version'], task['version'])
        self.assertEqual(current['executionStatus'], task['executionStatus'])
        self.assertEqual(saved['runId'], 'run-1')
        self.assertFalse(saved['duplicate'])
        self.assertFalse(saved['submissionAttempted'])
        self.assertTrue(repeated['duplicate'])
        self.assertEqual(repeated['seq'], saved['seq'])
        self.assertEqual(repeated['payload'], {'executionStatus': 'accepted'})
        with sqlite3.connect(path) as database:
            self.assertEqual(database.execute(
                'SELECT schema_version FROM installation').fetchone()[0], 4)
            self.assertEqual(database.execute(
                'SELECT COUNT(*) FROM events WHERE event_id=?', ('event-1',)
            ).fetchone()[0], 1)
            self.assertEqual(database.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 1)

    def test_version_two_refuses_event_migration(self):
        path = self.root / 'legacy.sqlite'
        write_version_two(path)
        store = TaskStore(path)
        with self.assertRaises(ValueError):
            store.migrate_event_contract()
        with self.assertRaises(ValueError):
            store.migrate_event_categories()
        with sqlite3.connect(path) as database:
            self.assertEqual(database.execute(
                'SELECT schema_version FROM installation').fetchone()[0], 2)
            self.assertIsNone(database.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recovery_records'"
            ).fetchone())

    def version_four(self, name):
        path = self.root / name
        write_version_two(path)
        store = TaskStore(path)
        task, _reused = store.register('same', self.payload)
        self.assertTrue(store.migrate_recovery_records())
        self.assertTrue(store.migrate_event_contract())
        return store, task

    def test_version_four_backup_keeps_the_event_contract(self):
        store, task = self.version_four('current.sqlite')
        path = store.path
        store.record_sourced_event(
            task['id'], kind='recovery_checked', source_identity='operator',
            source_event_id='event-1', payload={'note': 'original'})
        bundle = self.root / 'backup'
        manifest = create_backup(path, bundle)
        self.assertEqual(manifest['schemaVersion'], 4)
        restored = self.root / 'restored.sqlite'
        restore_backup(bundle, restored)
        reopened = TaskStore(restored)
        repeated = reopened.record_sourced_event(
            task['id'], kind='recovery_checked', source_identity='operator',
            source_event_id='event-1', payload={'note': 'changed'})
        self.assertTrue(repeated['duplicate'])
        self.assertEqual(repeated['payload'], {'note': 'original'})
        self.assertEqual(reopened.get(task['id'])['executionStatus'], 'registered')



    def test_version_four_gains_categories_without_changing_the_task(self):
        store, task = self.version_four('categories.sqlite')
        self.assertIsNone(store.note_category(task['id'], 'recovery_checked', 'before-category'))
        before = store.get(task['id'])
        self.assertTrue(store.migrate_event_categories())
        self.assertFalse(store.migrate_event_categories())
        current = store.get(task['id'])
        self.assertEqual(current['version'], before['version'])
        self.assertEqual(current['executionStatus'], before['executionStatus'])
        registered = store.events(task['id'])[0]
        self.assertEqual(registered['category'], 'task_registered')
        noted = store.note_category(
            task['id'], 'recovery_checked', 'checked-1', payload={'note': 'kept'})
        self.assertEqual(noted['category'], 'recovery_checked')
        self.assertEqual(noted['sourceIdentity'], 'operator')
        self.assertFalse(noted['duplicate'])
        self.assertFalse(noted['submissionAttempted'])
        self.assertEqual(store.get(task['id'])['executionStatus'], 'registered')
        with self.assertRaises(ValueError):
            store.note_category(task['id'], 'not-a-category', 'checked-2')

    def test_older_terminal_does_not_replace_a_newer_one(self):
        store = TaskStore.initialize(self.root / 'terminal.sqlite')
        task, _reused = store.register('same', self.payload)
        newer = store.record_sourced_event(
            task['id'], kind='succeeded', source_identity='codex-executor',
            source_event_id='done-new', occurred_at=200)
        older = store.record_sourced_event(
            task['id'], kind='failed', source_identity='codex-executor',
            source_event_id='done-old', occurred_at=100)
        latest = store.latest_terminal_event(task['id'])
        self.assertEqual(newer['category'], 'terminal')
        self.assertFalse(newer['staleTerminal'])
        self.assertTrue(older['staleTerminal'])
        self.assertEqual(latest['eventId'], 'done-new')
        self.assertEqual(latest['occurredAt'], 200)
        self.assertEqual(store.get(task['id'])['executionStatus'], 'registered')
        repeated = store.record_sourced_event(
            task['id'], kind='failed', source_identity='codex-executor',
            source_event_id='done-old', occurred_at=300, payload={'executionStatus': 'succeeded'})
        self.assertTrue(repeated['duplicate'])
        self.assertTrue(repeated['staleTerminal'])
        self.assertEqual(store.latest_terminal_event(task['id'])['eventId'], 'done-new')

    def test_observed_flow_categories_are_idempotent_and_do_not_change_state(self):
        store = TaskStore.initialize(self.root / 'flow.sqlite')
        task, _reused = store.register('same', self.payload)
        before = store.get(task['id'])
        trace = {
            'state': 'observed',
            'searches': [{'callId': 'search-1', 'resultState': 'success'},
                         {'callId': 'search-2', 'resultState': 'error'}],
            'delegations': [{'callId': 'deleg-1'}],
        }
        noted = store.note_cached_trace(task['id'], trace)
        created = store.note_approval_created(
            task['id'], 'request-1', status='waiting', occurred_at=20)
        decided = store.note_approval_decided(task['id'], 'request-1', 'decision-1', 'approve')
        repeated = store.note_cached_trace(task['id'], trace)
        again = store.note_approval_decided(task['id'], 'request-1', 'decision-1', 'reject')
        current = store.get(task['id'])
        kinds = [event['kind'] for event in store.events(task['id'])]
        self.assertEqual(kinds.count('role_started'), 1)
        self.assertEqual(kinds.count('memory_recalled'), 1)
        self.assertEqual(kinds.count('delegation_started'), 1)
        self.assertEqual(kinds.count('approval_created'), 1)
        self.assertEqual(kinds.count('approval_decided'), 1)
        self.assertEqual(created['category'], 'approval_created')
        self.assertEqual(created['sourceIdentity'], 'approval-store')
        self.assertEqual(created['payload'], {'status': 'waiting', 'requestId': 'request-1'})
        self.assertEqual(decided['payload'], {'decision': 'approve', 'requestId': 'request-1'})
        self.assertTrue(again['duplicate'])
        self.assertEqual(again['payload'], {'decision': 'approve', 'requestId': 'request-1'})
        self.assertTrue(all(item['duplicate'] for item in repeated))
        self.assertFalse(any(item['submissionAttempted'] for item in noted))
        self.assertEqual(current['version'], before['version'])
        self.assertEqual(current['executionStatus'], before['executionStatus'])
        self.assertEqual(store.note_cached_trace(task['id'], {'state': 'unknown'}), [])
        self.assertIsNone(store.note_approval_created(task['id'], 'request-2', status='not_observed'))

    def test_recorded_waiting_approval_blocks_continue_until_the_same_request_is_decided(self):
        store = TaskStore.initialize(self.root / 'approvals.sqlite')
        task, _reused = store.register('same', self.payload)
        self.assertFalse(store.recorded_approval_blocks_continue(task['id']))
        for status in ('expired', 'unbound', 'decision_recorded'):
            payload = {**self.payload, 'workflow': {**self.payload['workflow'], 'nodeExecutionId': status}}
            other, _reused = store.register(status, payload)
            store.note_approval_created(other['id'], 'request-' + status, status=status)
            self.assertFalse(store.recorded_approval_blocks_continue(other['id']))
        store.note_approval_created(task['id'], 'request-1', status='waiting')
        self.assertTrue(store.recorded_approval_blocks_continue(task['id']))
        store.note_approval_decided(task['id'], 'request-1', 'decision-1', 'approve')
        self.assertFalse(store.recorded_approval_blocks_continue(task['id']))
        bound, _reused = store.register('bound', {
            **self.payload, 'workflow': {**self.payload['workflow'], 'nodeExecutionId': 'bound'}})
        store.note_approval_created(bound['id'], 'request-bound', status='waiting')
        store.note_approval_decided(bound['id'], 'request-bound', 'decision-bound', 'reject', binding={
            'taskId': bound['id'],
            'executionIdentity': {'codexThreadId': 'thread', 'codexTurnId': 'turn'},
            'deliveryStatus': 'accepted',
        })
        self.assertFalse(store.recorded_approval_blocks_continue(bound['id']))
        legacy, _reused = store.register('legacy', {
            **self.payload, 'workflow': {**self.payload['workflow'], 'nodeExecutionId': 'legacy'}})
        store.record_sourced_event(
            legacy['id'], kind='approval_created', source_identity='approval-store',
            source_event_id='legacy-waiting', payload={'status': 'waiting'})
        with self.assertRaises(ValueError):
            store.recorded_approval_blocks_continue(legacy['id'])
        broken, _reused = store.register('broken', {
            **self.payload, 'workflow': {**self.payload['workflow'], 'nodeExecutionId': 'broken'}})
        store.record_sourced_event(
            broken['id'], kind='approval_created', source_identity='approval-store',
            source_event_id='broken-created', payload={'status': 'waiting', 'requestId': 'request-1', 'extra': True})
        with self.assertRaises(ValueError):
            store.recorded_approval_blocks_continue(broken['id'])

    def test_version_two_does_not_record_flow_categories(self):
        path = self.root / 'legacy.sqlite'
        write_version_two(path)
        store = TaskStore(path)
        task, _reused = store.register('same', self.payload)
        self.assertIsNone(store.note_role_started(task['id']))
        self.assertIsNone(store.note_memory_recalled(task['id'], 'search-1'))
        self.assertIsNone(store.note_delegation_started(task['id'], 'deleg-1'))
        self.assertIsNone(store.note_approval_created(task['id'], 'request-1', status='waiting'))
        self.assertIsNone(store.note_approval_decided(task['id'], 'request-1', 'decision-1', 'approve'))
        self.assertEqual([event['kind'] for event in store.events(task['id'])], ['registered'])

    def test_activity_start_uses_earliest_role_started_only(self):
        store = TaskStore.initialize(self.root / 'start.sqlite')
        task, _reused = store.register('same', self.payload)
        before = store.get(task['id'])
        self.assertIsNone(before['activity']['startedAt'])
        self.assertIsNotNone(before['activity']['registeredAt'])
        delegated = store.note_delegation_started(task['id'], 'deleg-1')
        self.assertEqual(delegated['kind'], 'delegation_started')
        store.note_memory_recalled(task['id'], 'search-1')
        self.assertIsNone(store.get(task['id'])['activity']['startedAt'])
        early = store.record_sourced_event(
            task['id'], kind='role_started', source_identity='role-binding',
            source_event_id='role-early', occurred_at=100)
        late = store.note_role_started(task['id'])
        self.assertFalse(early['duplicate'])
        self.assertFalse(late['duplicate'])
        self.assertGreater(late['occurredAt'], 100)
        count = len(store.events(task['id']))
        current = store.get(task['id'])
        self.assertEqual(len(store.events(task['id'])), count)
        self.assertEqual(current['version'], before['version'])
        self.assertEqual(current['executionStatus'], before['executionStatus'])
        self.assertEqual(current['activity']['startedAt'], 100)
        self.assertNotEqual(current['activity']['startedAt'], current['activity']['registeredAt'])
        self.assertEqual(current['activity']['lastEvent']['kind'], 'role_started')
        self.assertNotEqual(current['activity']['startedAt'], current['activity']['lastEvent']['recordedAt'])
        listed = store.list_tasks(['project'])['tasks']
        self.assertEqual(listed[0]['activity']['startedAt'], 100)

    def test_version_two_start_time_reads_role_started_created_at(self):
        path = self.root / 'legacy-start.sqlite'
        write_version_two(path)
        store = TaskStore(path)
        task, _reused = store.register('same', self.payload)
        self.assertIsNone(store.get(task['id'])['activity']['startedAt'])
        with sqlite3.connect(path) as database:
            database.execute(
                'INSERT INTO events(task_id,kind,created_at) VALUES (?,?,?)',
                (task['id'], 'delegation_started', 10))
            database.execute(
                'INSERT INTO events(task_id,kind,created_at) VALUES (?,?,?)',
                (task['id'], 'role_started', 50))
        activity = store.get(task['id'])['activity']
        self.assertEqual(activity['startedAt'], 50)
        self.assertNotEqual(activity['startedAt'], activity['registeredAt'])
        self.assertEqual(store.list_tasks(['project'])['tasks'][0]['activity']['startedAt'], 50)

    def test_current_backup_keeps_the_event_category(self):
        path = self.root / 'current.sqlite'
        store = TaskStore.initialize(path)
        task, _reused = store.register('same', self.payload)
        bundle = self.root / 'backup'
        manifest = create_backup(path, bundle)
        self.assertEqual(manifest['schemaVersion'], SCHEMA_VERSION)
        restored = self.root / 'restored.sqlite'
        restore_backup(bundle, restored)
        reopened = TaskStore(restored)
        self.assertEqual(reopened.events(task['id'])[0]['category'], 'task_registered')
        self.assertFalse(reopened.migrate_event_categories())


if __name__ == '__main__':
    unittest.main()
