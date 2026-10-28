import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parents[1]))
from agent_line.backup import create_backup, restore_backup
from agent_line.service import ServiceConfig, create_app
from agent_line.store import SCHEMA_V2, SCHEMA_VERSION, Conflict, TaskStore


def write_version_two(path):
    with path.open('xb'):
        pass
    path.chmod(0o600)
    database = sqlite3.connect(path)
    try:
        database.execute('PRAGMA journal_mode=WAL')
        database.executescript(SCHEMA_V2)
        database.execute(
            'INSERT INTO installation VALUES (?,?)', (uuid.uuid4().hex, 2))
        database.commit()
    finally:
        database.close()


class RecoveryRecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.payload = {
            'projectId': 'project',
            'roleAgentId': 'role',
            'workspace': '/workspace',
            'workflow': {
                'runId': 'run',
                'nodeExecutionId': 'activation',
                'attemptId': 'attempt',
            },
            'input': {'task': 'recover'},
        }

    def test_version_two_rejects_write_until_explicit_migration(self):
        path = self.root / 'legacy.sqlite'
        write_version_two(path)
        store = TaskStore(path)
        task, _reused = store.register('same', self.payload)
        with self.assertRaises(ValueError):
            store.record_recovery(
                task['id'], action='reconcile', finding='identity still unknown',
                decision='keep the original task', allow_new_attempt=False)
        with sqlite3.connect(path) as database:
            self.assertIsNone(database.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recovery_records'"
            ).fetchone())
            self.assertEqual(database.execute(
                'SELECT schema_version FROM installation').fetchone()[0], 2)
            self.assertFalse(any(
                row[0] == 'recovery_recorded'
                for row in database.execute('SELECT kind FROM events')))
        self.assertTrue(store.migrate_recovery_records())
        self.assertFalse(store.migrate_recovery_records())
        reopened = TaskStore(path)
        with sqlite3.connect(path) as database:
            self.assertEqual(database.execute(
                'SELECT schema_version FROM installation').fetchone()[0], 3)
        self.assertEqual(reopened.get(task['id'])['version'], task['version'])

    def test_saved_record_persists_without_creating_an_attempt(self):
        path = self.root / 'current.sqlite'
        store = TaskStore.initialize(path)
        task, _reused = store.register('same', self.payload)
        before = store.get(task['id'])
        saved = store.record_recovery(
            task['id'], action='reconcile', finding='original turn is still the only turn',
            decision='a later attempt may be considered by a person',
            allow_new_attempt=True)
        current = store.get(task['id'])
        self.assertEqual(current['version'], before['version'])
        self.assertEqual(current['executionStatus'], before['executionStatus'])
        self.assertEqual(current['upstreamSubmissionStatus'], before['upstreamSubmissionStatus'])
        self.assertEqual(current['payload'], before['payload'])
        self.assertFalse(saved['submissionAttempted'])
        self.assertTrue(saved['allowNewAttempt'])
        with sqlite3.connect(path) as database:
            self.assertEqual(database.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 1)
            self.assertEqual(database.execute(
                "SELECT json_extract(payload,'$.workflow.attemptId') FROM tasks"
            ).fetchone()[0], 'attempt')
        reopened = TaskStore(path)
        records = reopened.recovery_records(task['id'])
        self.assertEqual(records, [saved])
        self.assertIn('recovery_recorded', {event['kind'] for event in reopened.events(task['id'])})
        with self.assertRaises(Conflict):
            reopened.register('different', self.payload)

    def test_version_two_backup_restores_and_schema_one_is_rejected(self):
        path = self.root / 'legacy.sqlite'
        write_version_two(path)
        store = TaskStore(path)
        task, _reused = store.register('same', self.payload)
        bundle = self.root / 'backup'
        manifest = create_backup(path, bundle)
        self.assertEqual(manifest['schemaVersion'], 2)
        restored = self.root / 'restored.sqlite'
        restore_backup(bundle, restored)
        restored_store = TaskStore(restored)
        self.assertEqual(restored_store.get(task['id'])['payload'], self.payload)
        with self.assertRaises(ValueError):
            restored_store.record_recovery(
                task['id'], action='reconcile', finding='backup is still version 2',
                decision='do not start another attempt', allow_new_attempt=False)
        broken = self.root / 'broken-manifest.json'
        document = (bundle / 'manifest.json').read_text().replace(
            '"schemaVersion": 2', '"schemaVersion": 1', 1)
        broken.write_text(document)
        (bundle / 'manifest.json').write_text(document)
        with self.assertRaises(ValueError):
            restore_backup(bundle, self.root / 'rejected.sqlite')
        self.assertFalse((self.root / 'rejected.sqlite').exists())


if __name__ == '__main__':
    unittest.main()


class RecordingObserver:
    def __init__(self):
        self.calls = 0

    def reconcile(self, task_id, *, on_observation=None):
        self.calls += 1
        if on_observation is not None:
            on_observation({'outcome': 'unknown'})


class RecoveryApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = ServiceConfig(
            tokens={'test-owner-token': {'a'}},
            projects={'a': {'workspace': str(self.root)}},
            bindings={'developer': {'projectId': 'a', 'roleAgentId': 'stable-role'}},
            controlled_execution='confirmed',
        )
        self.headers = {'Authorization': 'Bearer test-owner-token'}
        self.body = {
            'schemaVersion': '1.0',
            'idempotencyKey': 'run:activation:attempt',
            'workflow': {
                'id': 'wf', 'revision': 'rev', 'runId': 'run', 'nodeId': 'dev',
                'nodeExecutionId': 'activation', 'attemptId': 'attempt',
            },
            'projectId': 'a', 'roleBindingId': 'developer',
            'input': {'requirement': 'recover the original task'},
            'memoryPolicyId': 'policy', 'outputSchemaId': 'task-result',
            'limits': {'maxExecutionSeconds': 30, 'maxBusinessAttempts': 1},
        }

    def open_client(self, store, observer):
        return TestClient(create_app(store, self.config, dispatcher=observer))

    def register(self, store):
        created = TestClient(create_app(store, self.config)).post(
            '/extension/v1/node-tasks', json=self.body, headers=self.headers)
        self.assertEqual(created.status_code, 202, created.text)
        task_id = created.json()['taskId']
        detail = TestClient(create_app(store, self.config)).get(
            f'/extension/v1/node-tasks/{task_id}', headers=self.headers)
        self.assertEqual(detail.status_code, 200, detail.text)
        return task_id, detail.json()['version']

    def test_schema_three_persists_ruling_without_new_attempt(self):
        store = TaskStore.initialize(self.root / 'current.sqlite')
        task_id, version = self.register(store)
        observer = RecordingObserver()
        client = self.open_client(store, observer)
        response = client.post(
            f'/extension/v1/recovery/{task_id}/reconcile', headers=self.headers,
            json={'expectedVersion': version, 'finding': 'original turn is still the only turn',
                  'decision': 'a later attempt may be considered by a person',
                  'allowNewAttempt': True})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(observer.calls, 1)
        self.assertTrue(body['recoveryRecorded'])
        self.assertFalse(body['submissionAttempted'])
        self.assertFalse(body['recoveryRecord']['submissionAttempted'])
        self.assertTrue(body['recoveryRecord']['allowNewAttempt'])
        self.assertEqual(body['task']['version'], version)
        self.assertEqual(body['task']['workflow']['attemptId'], 'attempt')
        listed = client.get(f'/extension/v1/recovery/{task_id}/records', headers=self.headers)
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json()['records'], [body['recoveryRecord']])
        self.assertEqual(store.get(task_id)['version'], version)
        with sqlite3.connect(store.path) as database:
            self.assertEqual(database.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 1)

    def test_observation_without_ruling_does_not_write_a_record(self):
        store = TaskStore.initialize(self.root / 'current.sqlite')
        task_id, version = self.register(store)
        observer = RecordingObserver()
        client = self.open_client(store, observer)
        response = client.post(
            f'/extension/v1/recovery/{task_id}/reconcile', headers=self.headers,
            json={'expectedVersion': version})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn('recoveryRecord', response.json())
        self.assertEqual(observer.calls, 1)
        self.assertEqual(store.recovery_records(task_id), [])

    def test_schema_two_refuses_ruling_before_observation_or_migration(self):
        path = self.root / 'legacy.sqlite'
        write_version_two(path)
        store = TaskStore(path)
        task, _reused = store.register('same', {
            'projectId': 'a', 'roleAgentId': 'stable-role', 'workspace': str(self.root),
            'workflow': {'runId': 'run', 'nodeExecutionId': 'activation', 'attemptId': 'attempt'},
            'input': {'task': 'recover'},
        })
        observer = RecordingObserver()
        client = self.open_client(store, observer)
        response = client.post(
            f"/extension/v1/recovery/{task['id']}/reconcile", headers=self.headers,
            json={'expectedVersion': task['version'], 'finding': 'identity still unknown',
                  'decision': 'keep the original task', 'allowNewAttempt': False})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn('版本 2', response.json()['detail'])
        self.assertEqual(observer.calls, 0)
        listed = client.get(f"/extension/v1/recovery/{task['id']}/records", headers=self.headers)
        self.assertEqual(listed.status_code, 409, listed.text)
        with sqlite3.connect(path) as database:
            self.assertIsNone(database.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recovery_records'"
            ).fetchone())
            self.assertEqual(database.execute(
                'SELECT schema_version FROM installation').fetchone()[0], 2)

    def test_partial_or_stale_ruling_does_not_call_the_observer(self):
        store = TaskStore.initialize(self.root / 'current.sqlite')
        task_id, version = self.register(store)
        observer = RecordingObserver()
        client = self.open_client(store, observer)
        partial = client.post(
            f'/extension/v1/recovery/{task_id}/reconcile', headers=self.headers,
            json={'expectedVersion': version, 'finding': 'only a finding'})
        self.assertEqual(partial.status_code, 422, partial.text)
        stale = client.post(
            f'/extension/v1/recovery/{task_id}/reconcile', headers=self.headers,
            json={'expectedVersion': version + 1,
                  'finding': 'original turn is still the only turn',
                  'decision': 'keep the original task', 'allowNewAttempt': False})
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(observer.calls, 0)
        self.assertEqual(store.recovery_records(task_id), [])
