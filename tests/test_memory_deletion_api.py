"""HTTP boundary regressions using real SQLite stores and mocked host HTTP."""
import unittest
from dataclasses import replace
import json

import httpx
from fastapi.testclient import TestClient

from agent_line.adapters.qwenpaw_memory import QwenPawMemoryClient
from agent_line.memory import MemoryDeletionCoordinator
from agent_line.memory_api import MemoryAPI
from agent_line.service import create_app
from tests import test_memory_api as fixtures


class MemoryDeletionAPITests(unittest.TestCase):
    make_task = fixtures.MemoryAPITests.make_task
    request = fixtures.MemoryAPITests.request
    review = fixtures.MemoryAPITests.review
    audit_count = fixtures.MemoryAPITests.audit_count

    def setUp(self):
        fixtures.MemoryAPITests.setUp(self)
        self.assertEqual(self.review().status_code, 202)
        self.original_task = self.tasks.get(self.task['id'])
        self.digest = self.commits.get('role', self.task['id'])['content_hash']
        self.access.replace_credentials({
            'human-token': replace(self.principal, capabilities=frozenset({'approve', 'review_memory', 'delete_memory'})),
            'review-only': replace(self.principal, subject='reader'),
            'other-token': replace(self.principal, subject='other', project_scopes=frozenset({'other'}),
                                   capabilities=frozenset({'approve', 'review_memory', 'delete_memory'})),
        })
        self.native_calls = []
        self.native_receipt = {'roleAgentId': 'role', 'taskId': self.task['id'],
                               'contentHash': self.digest, 'status': 'deleted',
                               'fileAbsent': True, 'indexSourceAbsent': True}
        self.timeout = False

        def transport(request):
            self.native_calls.append(request)
            if request.method == 'POST':
                # Durable intent must precede the destructive host request.
                self.assertIsNotNone(self.commits.get_deletion('role', self.task['id']))
                if self.timeout:
                    raise httpx.ReadTimeout('private credential detail')
            return httpx.Response(200, json=self.native_receipt)

        self.deletion_http = httpx.Client(transport=httpx.MockTransport(transport))
        self.addCleanup(self.deletion_http.close)
        self.coordinator.client = QwenPawMemoryClient(
            'http://qwen.invalid', 'private-host-token', role_agent_id='role',
            client=self.deletion_http, deletion_service_token='test-deletion-token-' + 'x' * 32)
        self.deletion = MemoryDeletionCoordinator(self.commits, self.coordinator.client)
        self.api = MemoryAPI(self.access, {'binding': self.coordinator}, self.reviews,
                             self.tasks, bindings=self.bindings, admission=self.api.admission,
                             deletions={'binding': self.deletion})
        self.client.close()
        self.client = TestClient(create_app(self.tasks, self.config, memories=self.api))
        self.addCleanup(self.client.close)

    def delete(self, *, token='human-token', body=None):
        return self.request('post', '/binding/' + self.task['id'] + '/delete', token=token,
                            json=body or {'decisionId': 'delete-decision', 'expectedContentSha256': self.digest})

    def deletion_count(self):
        with self.commits.connect() as db:
            return db.execute('SELECT count(*) FROM memory_deletions').fetchone()[0]

    def test_distinct_delete_permission_and_project_scope(self):
        for token, code in [('review-only', 403), ('other-token', 404), ('service-token', 401)]:
            self.assertEqual(self.delete(token=token).status_code, code)
        self.assertEqual(self.deletion_count(), 0)
        self.assertEqual(self.native_calls, [])

    def test_extra_actor_and_wrong_hash_never_create_tombstone(self):
        body = {'decisionId': 'delete-decision', 'expectedContentSha256': self.digest, 'actor': 'forged'}
        self.assertEqual(self.delete(body=body).status_code, 422)
        self.assertEqual(self.delete(body={'decisionId': 'delete-decision', 'expectedContentSha256': 'a' * 64}).status_code, 409)
        self.assertEqual(self.deletion_count(), 0)
        self.assertEqual(self.native_calls, [])

    def test_maintenance_prevents_intent_and_native_call(self):
        self.guarded = True
        self.assertEqual(self.delete().status_code, 503)
        self.assertEqual(self.deletion_count(), 0)
        self.assertEqual(self.native_calls, [])

    def test_deleted_requires_double_absence_and_hides_content_everywhere(self):
        response = self.delete()
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()['status'], 'deleted')
        self.assertEqual(self.commits.get('role', self.task['id'])['content'], '')
        for response in (self.request(path='/binding/' + self.task['id']), self.request()):
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('经人工核实的项目事实', response.text)
        task = self.tasks.get(self.task['id'])
        self.assertEqual(task['memoryStatus'], 'deleted')
        self.assertEqual(task['executionStatus'], self.original_task['executionStatus'])
        self.assertEqual(task['evidence'], self.original_task['evidence'])
        self.assertEqual(self.commits.get_deletion('role', self.task['id'])['actor'], 'reviewer')
        native = self.native_calls[0]
        self.assertEqual(native.url.path, '/api/agent-line/memory/role/' + self.task['id'] + '/delete')
        self.assertEqual(json.loads(native.content), {'expectedContentSha256': self.digest,
                                                     'decisionId': 'delete-decision', 'actor': 'reviewer'})

    def test_missing_index_absence_never_claims_deleted(self):
        self.native_receipt['indexSourceAbsent'] = False
        response = self.delete()
        self.assertEqual(response.status_code, 202, response.text)
        self.assertNotEqual(response.json()['status'], 'deleted')
        self.assertNotEqual(self.tasks.get(self.task['id'])['memoryStatus'], 'deleted')

    def test_repeated_delete_uses_only_observation_after_first_post(self):
        self.native_receipt.update(status='deleting', fileAbsent=False, indexSourceAbsent=False)
        self.assertEqual(self.delete().status_code, 202)
        self.assertEqual(self.delete().status_code, 202)
        self.native_receipt.update(status='deleted', fileAbsent=True, indexSourceAbsent=True)
        response = self.request(path='/binding/' + self.task['id'] + '/deletion', token='review-only')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['status'], 'deleted')
        self.assertEqual([r.method for r in self.native_calls].count('POST'), 1)
        self.assertTrue(all(r.method in ('GET', 'POST') for r in self.native_calls))

    def test_timeout_then_repeat_never_reissues_native_delete(self):
        self.timeout = True
        first = self.delete()
        self.assertEqual(first.status_code, 202, first.text)
        self.assertNotEqual(first.json()['status'], 'deleted')
        self.timeout = False
        second = self.delete()
        self.assertEqual(second.status_code, 202, second.text)
        self.assertEqual([r.method for r in self.native_calls].count('POST'), 1)
        self.assertNotIn('private credential', first.text)

    def test_changed_deletion_decision_is_conflict_without_new_native_call(self):
        self.assertEqual(self.delete().status_code, 202)
        count = len(self.native_calls)
        self.assertEqual(self.delete(body={'decisionId': 'different', 'expectedContentSha256': self.digest}).status_code, 409)
        self.assertEqual(len(self.native_calls), count)

    def test_unconfigured_deletion_binding_is_unavailable_without_intent(self):
        api = MemoryAPI(self.access, {'binding': self.coordinator}, self.reviews,
                        self.tasks, bindings=self.bindings, admission=self.api.admission)
        with TestClient(create_app(self.tasks, self.config, memories=api)) as client:
            response = client.post('/extension/v1/memories/binding/' + self.task['id'] + '/delete',
                                   headers={'Authorization': 'Bearer human-token'},
                                   json={'decisionId': 'delete-decision', 'expectedContentSha256': self.digest})
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(self.deletion_count(), 0)
        self.assertEqual(self.native_calls, [])

    def test_wrong_native_identity_and_nonboolean_absence_never_confirm_deletion(self):
        for key, value in [('roleAgentId', 'other'), ('taskId', 'other'),
                           ('contentHash', 'b' * 64), ('fileAbsent', 1), ('indexSourceAbsent', None)]:
            old = self.native_receipt[key]
            self.native_receipt[key] = value
            response = self.delete()
            self.assertEqual(response.status_code, 202, response.text)
            self.assertNotEqual(response.json()['status'], 'deleted')
            self.assertNotEqual(self.tasks.get(self.task['id'])['memoryStatus'], 'deleted')
            self.native_receipt[key] = old
        self.assertEqual([r.method for r in self.native_calls].count('POST'), 1)

    def test_deletion_observation_cannot_create_intent(self):
        response = self.request(path='/binding/' + self.task['id'] + '/deletion', token='review-only')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.deletion_count(), 0)
        self.assertEqual(self.native_calls, [])

    def test_pending_deletion_hides_content_and_review_replay_cannot_restore_it(self):
        self.native_receipt.update(status='deleting', fileAbsent=True, indexSourceAbsent=False)
        self.assertEqual(self.delete().status_code, 202)
        before = len(self.native_calls)
        replay = self.review()
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(replay.json()['status'], 'deleting')
        self.assertNotIn('经人工核实的项目事实', replay.text)
        self.assertEqual(self.commits.get('role', self.task['id'])['content'], '')
        self.assertEqual(len(self.native_calls), before)

    def test_native_content_conflict_is_needs_review_without_redelete_or_content(self):
        self.native_receipt.update(status='needs_review', fileAbsent=False, indexSourceAbsent=False)
        response = self.delete()
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()['status'], 'needs_review')
        self.assertEqual(response.json()['deletion']['status'], 'needs_review')
        self.assertEqual(self.tasks.get(self.task['id'])['memoryStatus'], 'needs_review')
        self.assertEqual(self.commits.get('role', self.task['id'])['content'], '')
        self.assertEqual(self.delete().status_code, 202)
        detail = self.request(path='/binding/' + self.task['id'])
        self.assertEqual(detail.json()['status'], 'needs_review')
        self.assertNotIn('经人工核实的项目事实', detail.text)
        self.assertEqual([r.method for r in self.native_calls].count('POST'), 1)
        self.assertEqual(self.tasks.get(self.task['id'])['evidence'], self.original_task['evidence'])


if __name__ == '__main__':
    unittest.main()
