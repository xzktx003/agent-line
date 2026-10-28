import hashlib
import unittest
import httpx
from agent_line.adapters.qwenpaw_memory import MemoryApproval, QwenPawMemoryClient


class MemoryClientTests(unittest.TestCase):
    def adapter(self, handler):
        self.client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(self.client.close)
        return QwenPawMemoryClient('http://qwenpaw.invalid', 'test-token', role_agent_id='fixed-role', client=self.client)

    def approval(self, text='verified fact'):
        return MemoryApproval('task-1', hashlib.sha256(text.strip().encode()).hexdigest(), 'review-1')

    def test_unapproved_content_never_calls_upstream(self):
        adapter = self.adapter(lambda r: self.fail('Unapproved content must not access upstream'))
        self.assertEqual(adapter.write_approved('task-1', 'verified fact')['status'], 'needs_review')
        self.assertEqual(adapter.write_approved('task-1', 'changed', approval=self.approval())['status'], 'needs_review')

    def test_fixed_role_namespace_write_then_readback(self):
        calls = []
        def handler(request):
            calls.append(request.method)
            self.assertEqual(request.headers['X-Agent-Id'], 'fixed-role')
            self.assertEqual(request.url.path, '/api/workspace/memory/agent-line-task-task-1.md')
            self.assertEqual(request.url.params['section'], 'digest')
            if len(calls) == 1:return httpx.Response(404)
            if request.method == 'PUT':return httpx.Response(200, json={'written':True})
            return httpx.Response(200, json={'content':'verified fact'})
        result = self.adapter(handler).write_approved('task-1','verified fact',approval=self.approval())
        self.assertEqual(result['status'],'committed')
        self.assertEqual(calls,['GET','PUT','GET'])

    def test_existing_different_content_never_overwritten(self):
        calls=[]
        def handler(request):
            calls.append(request.method)
            return httpx.Response(200,json={'content':'other existing fact'})
        result=self.adapter(handler).write_approved('task-1','verified fact',approval=self.approval())
        self.assertEqual(result['status'],'needs_review')
        self.assertEqual(calls,['GET'])

    def test_lost_receipt_reads_back_without_second_put(self):
        calls=[]
        def handler(request):
            calls.append(request.method)
            if len(calls)==1:return httpx.Response(404)
            if request.method=='PUT':raise httpx.ReadTimeout('lost receipt',request=request)
            return httpx.Response(200,json={'content':'verified fact'})
        result=self.adapter(handler).write_approved('task-1','verified fact',approval=self.approval())
        self.assertEqual(result['status'],'committed')
        self.assertEqual(calls,['GET','PUT','GET'])

    def test_unknown_is_pending_and_reconcile_never_writes(self):
        calls=[]
        def handler(request):
            calls.append(request.method)
            if len(calls)==1:return httpx.Response(404)
            raise httpx.ReadTimeout('unknown',request=request)
        adapter=self.adapter(handler)
        result=adapter.write_approved('task-1','verified fact',approval=self.approval())
        self.assertEqual(result['status'],'pending')
        self.assertEqual(result['nextAction'],'reconcile_only')
        adapter.reconcile('task-1','verified fact')
        self.assertEqual(calls.count('PUT'),1)

    def test_readback_not_found_or_mismatch_is_not_success(self):
        for last in [httpx.Response(404),httpx.Response(200,json={'content':'different'})]:
            calls=[]
            def handler(request):
                calls.append(request.method)
                if len(calls)==1:return httpx.Response(404)
                if request.method=='PUT':return httpx.Response(200,json={'written':True})
                return last
            result=self.adapter(handler).write_approved('task-1','verified fact',approval=self.approval())
            self.assertNotEqual(result['status'],'committed')

    def test_identical_existing_content_does_not_write(self):
        calls=[]
        def handler(request):
            calls.append(request.method)
            return httpx.Response(200,json={'content':'verified fact'})
        result=self.adapter(handler).write_approved('task-1','verified fact',approval=self.approval())
        self.assertEqual(result['status'],'committed')
        self.assertEqual(calls,['GET'])

    def test_invalid_task_path_is_rejected_without_request(self):
        adapter=self.adapter(lambda r:self.fail('No request'))
        for task in ['../outside','a/b','', 'role?x=bad']:
            with self.assertRaises(ValueError):adapter.write_approved(task,'verified fact',approval=self.approval())

    def test_direct_native_prewrite_rejection_and_missing_readback_are_failed(self):
        for status in (401, 403, 404, 422):
            with self.subTest(status=status):
                calls=[]
                def handler(request):
                    calls.append(request.method)
                    return httpx.Response(status if request.method=='PUT' else 404,json={'detail':'rejected before write'})
                http=httpx.Client(transport=httpx.MockTransport(handler))
                self.addCleanup(http.close)
                adapter=QwenPawMemoryClient('http://127.0.0.1:52403','test-token',
                    role_agent_id='fixed-role',client=http,native_write_rejections=True)
                result=adapter.write_approved('task-1','verified fact',approval=self.approval())
                self.assertEqual(result['status'],'failed')
                self.assertEqual(result['reason'],'official_write_rejected')
                self.assertEqual(calls,['GET','PUT','GET'])

    def test_ambiguous_or_remote_rejections_never_authorize_retry(self):
        for origin,status,last in [('http://qwenpaw.invalid',403,404),
                ('http://127.0.0.1:52403',500,404),('http://127.0.0.1:52403',403,503),
                ('http://127.0.0.1:52403',200,404),('http://127.0.0.1:52403',400,404)]:
            with self.subTest(origin=origin,status=status,last=last):
                calls=[]
                def handler(request):
                    calls.append(request.method)
                    return httpx.Response(status if request.method=='PUT' else (404 if len(calls)==1 else last),json={'detail':'fixture rejection'})
                http=httpx.Client(transport=httpx.MockTransport(handler));self.addCleanup(http.close)
                adapter=QwenPawMemoryClient(origin,'test-token',role_agent_id='fixed-role',client=http,native_write_rejections=True)
                result=adapter.write_approved('task-1','verified fact',approval=self.approval())
                self.assertEqual(result['status'],'pending')
                self.assertEqual(result['nextAction'],'reconcile_only')

    def test_untrusted_or_malformed_rejections_remain_pending(self):
        cases = [(False, {'detail':'denied'}, True),
                 (True, {'detail':''}, True), (True, {'detail':[]}, True),
                 (True, {'detail':False}, True), (True, {'message':'denied'}, True),
                 (True, {'detail':'denied'}, False)]
        for enabled,body,json_type in cases:
            with self.subTest(enabled=enabled,body=body,json_type=json_type):
                calls=[]
                def handler(request):
                    calls.append(request.method)
                    if request.method=='PUT':
                        if json_type:return httpx.Response(403,json=body)
                        return httpx.Response(403,text='{"detail":"denied"}')
                    return httpx.Response(404)
                with httpx.Client(transport=httpx.MockTransport(handler)) as http:
                    adapter=QwenPawMemoryClient('http://127.0.0.1:52403','test-token',
                        role_agent_id='fixed-role',client=http,native_write_rejections=enabled)
                    result=adapter.write_approved('task-1','verified fact',approval=self.approval())
                self.assertEqual(result['status'],'pending')
                self.assertEqual(calls,['GET','PUT','GET'])
