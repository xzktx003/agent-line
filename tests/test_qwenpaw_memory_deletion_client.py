import unittest
import httpx
from agent_line.adapters.qwenpaw_memory import QwenPawMemoryClient


class DeletionClientTests(unittest.TestCase):
    def client(self, handler, *, secret='m' * 32):
        transport = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(transport.close)
        return QwenPawMemoryClient('http://qwen.invalid', 'host-token',
                                  role_agent_id='role', client=transport, deletion_service_token=secret)

    def receipt(self, **fields):
        return {'roleAgentId': 'role', 'taskId': 'task', 'contentHash': 'a' * 64,
                'status': 'deleted', 'fileAbsent': True, 'indexSourceAbsent': True, **fields}

    def test_no_native_deletion_token_never_contacts_host(self):
        client = self.client(lambda r: self.fail('Not configured'), secret=None)
        with self.assertRaises(ValueError):
            client.delete_checked('task', 'a' * 64, 'decision', 'reviewer')

    def test_independent_header_fixed_path_and_read_only_observe(self):
        calls = []
        def handler(request):
            calls.append(request)
            self.assertEqual(request.headers['Authorization'], 'Bearer host-token')
            self.assertEqual(request.headers['X-Agent-Line-Memory-Token'], 'm' * 32)
            self.assertEqual(request.headers['X-Agent-Id'], 'role')
            return httpx.Response(200, json=self.receipt(secret='must-not-persist'))
        client = self.client(handler)
        deleted = client.delete_checked('task', 'a' * 64, 'decision', 'reviewer')
        observed = client.observe_deletion('task', 'a' * 64)
        self.assertEqual([r.method for r in calls], ['POST', 'GET'])
        self.assertEqual(calls[0].url.path, '/api/agent-line/memory/role/task/delete')
        self.assertEqual(calls[1].url.path, '/api/agent-line/memory/role/task/deletion')
        self.assertEqual(calls[1].url.params['expectedContentSha256'], 'a' * 64)
        self.assertEqual(deleted['status'], 'deleted')
        self.assertEqual(observed['status'], 'deleted')
        self.assertNotIn('secret', deleted)

    def test_double_absence_and_exact_identity_required(self):
        for change in ({'fileAbsent': 1}, {'indexSourceAbsent': 1}, {'fileAbsent': False},
                       {'taskId': 'other'}, {'roleAgentId': 'other'}, {'contentHash': 'b' * 64}):
            client = self.client(lambda r: httpx.Response(200, json=self.receipt(**change)))
            with self.subTest(change=change):
                self.assertEqual(client.observe_deletion('task', 'a' * 64)['status'], 'deleting')

    def test_timeout_and_redirect_do_not_retry(self):
        calls = []
        def handler(request):
            calls.append(request)
            raise httpx.ReadTimeout('private-data')
        result = self.client(handler).delete_checked('task', 'a' * 64, 'decision', 'reviewer')
        self.assertEqual(result['status'], 'deleting')
        self.assertEqual(len(calls), 1)
        calls.clear()
        def redirect(request):
            calls.append(request)
            return httpx.Response(307, headers={'Location': 'http://other.invalid/private'})
        result = self.client(redirect).delete_checked('task', 'a' * 64, 'decision', 'reviewer')
        self.assertEqual(result['status'], 'deleting')
        self.assertEqual(len(calls), 1)

    def test_invalid_owned_identity_never_contacts_host(self):
        client = self.client(lambda r: self.fail('Invalid identity'))
        for task in ('../x', 'a/b', ''):
            with self.assertRaises(ValueError):
                client.observe_deletion(task, 'a' * 64)
        with self.assertRaises(ValueError):
            client.observe_deletion('task', 'wrong')

    def test_native_needs_review_preserved_without_body(self):
        result = self.client(lambda r: httpx.Response(200, json=self.receipt(
            status='needs_review', fileAbsent=False, indexSourceAbsent=False, content='private text'))
        ).observe_deletion('task', 'a' * 64)
        self.assertEqual(result['status'], 'needs_review')
        self.assertNotIn('content', result)

    def test_role_block_receipt_requires_boolean_and_matching_identity(self):
        for value, expected in ((True, True), (False, False), (1, None), ('true', None), (None, None)):
            result = self.client(lambda r: httpx.Response(200, json=self.receipt(roleBlocked=value))
                                 ).observe_deletion('task', 'a' * 64)
            self.assertIs(result['roleBlocked'], expected)
        result = self.client(lambda r: httpx.Response(200, json=self.receipt(roleBlocked=True, taskId='other'))
                             ).observe_deletion('task', 'a' * 64)
        self.assertIsNone(result['roleBlocked'])
