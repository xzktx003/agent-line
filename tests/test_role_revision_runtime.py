"""Trusted revision pins and durable preflight blocks without native execution."""
import copy
import unittest

from fastapi.testclient import TestClient

from agent_line.service import create_app
from agent_line.store import Conflict
from tests import test_service as fixtures

REVISION = 'qwenpaw-role-v1:' + 'a' * 64


class RevisionRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ServiceTests(); self.f.setUp()
        self.addCleanup(self.f.doCleanups); self.addCleanup(self.f.client.close)
        self.f.config.bindings['developer']['roleRevision'] = REVISION

    def client(self):
        client = TestClient(create_app(self.f.store, self.f.config))
        self.addCleanup(client.close)
        return client

    def test_server_pin_is_frozen_and_not_taken_from_input_or_client_fields(self):
        client = self.client()
        self.f.body['input']['roleRevision'] = 'qwenpaw-role-v1:' + 'b' * 64
        response = client.post('/extension/v1/node-tasks', headers=self.f.headers, json=self.f.body)
        self.assertEqual(response.status_code, 202, response.text)
        task = self.f.store.get(response.json()['taskId'])
        self.assertEqual(task['payload']['roleRevision'], REVISION)
        self.f.config.bindings['developer']['roleRevision'] = 'qwenpaw-role-v1:' + 'c' * 64
        changed = self.client().post('/extension/v1/node-tasks', headers=self.f.headers, json=self.f.body)
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(self.f.store.get(task['id'])['payload']['roleRevision'], REVISION)
        forged = {**self.f.body, 'roleRevision': REVISION}
        self.assertEqual(client.post('/extension/v1/node-tasks', headers=self.f.headers, json=forged).status_code, 422)

    def test_malformed_server_or_persistent_payload_pin_is_rejected(self):
        for value in (None, True, 1, 'a' * 64, 'qwenpaw-role-v1:' + 'A' * 64, 'qwenpaw-role-v2:' + 'a' * 64):
            self.f.config.bindings['developer']['roleRevision'] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.client()
        self.f.config.bindings['developer']['roleRevision'] = REVISION
        client = self.client()
        task_id = client.post('/extension/v1/node-tasks', headers=self.f.headers, json=self.f.body).json()['taskId']
        payload = copy.deepcopy(self.f.store.get(task_id)['payload']); payload['roleRevision'] = 'bad'
        with self.assertRaises(ValueError): self.f.store.register('malformed', payload)
        payload['roleRevision'] = 'qwenpaw-role-v1:' + 'b' * 64
        with self.assertRaises(Conflict): self.f.store.register(self.f.body['idempotencyKey'], payload)

    def test_block_is_durable_scoped_visible_and_deduplicated_without_claim(self):
        client = self.client()
        task_id = client.post('/extension/v1/node-tasks', headers=self.f.headers, json=self.f.body).json()['taskId']
        store = self.f.store
        store.block_role_revision(task_id, 'role_revision_mismatch')
        blocked = store.get(task_id)
        self.assertEqual(blocked['executionStatus'], 'registered')
        self.assertEqual(blocked['submissionBlock'], 'role_revision_mismatch')
        events = store.events(task_id)
        store.block_role_revision(task_id, 'role_revision_mismatch')
        self.assertEqual(store.events(task_id), events)
        from agent_line.store import TaskStore
        reopened = TaskStore(store.path)
        self.assertEqual(reopened.get(task_id), blocked)
        with store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM workspace_leases').fetchone()[0], 0)
        view = client.get('/extension/v1/node-tasks/' + task_id, headers=self.f.headers).json()
        self.assertEqual(view['submissionBlock'], 'role_revision_mismatch')
        self.assertEqual(view['roleRevision'], REVISION)
        page = client.get('/extension/v1/node-tasks', headers=self.f.headers).json()
        self.assertEqual(page['tasks'][0]['submissionBlock'], 'role_revision_mismatch')
        self.assertEqual(client.get('/extension/v1/node-tasks/' + task_id,
            headers={'Authorization': 'Bearer test-other-token'}).status_code, 404)
        reopened.clear_role_revision_block(task_id)
        self.assertNotIn('submissionBlock', reopened.get(task_id))
        count = len(reopened.events(task_id)); reopened.clear_role_revision_block(task_id)
        self.assertEqual(len(reopened.events(task_id)), count)

    def test_unbounded_reason_or_block_after_claim_cannot_rewrite_execution(self):
        client = self.client()
        task_id = client.post('/extension/v1/node-tasks', headers=self.f.headers, json=self.f.body).json()['taskId']
        with self.assertRaises(ValueError): self.f.store.block_role_revision(task_id, 'raw prompt or credential')
        self.f.store.claim(task_id, str(self.f.root))
        old = self.f.store.get(task_id); events = self.f.store.events(task_id)
        self.f.store.block_role_revision(task_id, 'role_revision_mismatch')
        self.f.store.clear_role_revision_block(task_id)
        self.assertEqual(self.f.store.get(task_id), old)
        self.assertEqual(self.f.store.events(task_id), events)


if __name__ == '__main__':
    unittest.main()
