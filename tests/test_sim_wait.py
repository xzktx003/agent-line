import unittest
import httpx
from agent_line.adapters.sim import SimClient
from agent_line.adapters.sim_wait import SimWaitClient,WaitInputUnavailable


class SimWaitTests(unittest.TestCase):
    def payload(self):
        return {'data':{'workflowId':'wf','runId':'run','status':'paused','paused':{'blockedOnBlockId':'wait','contextId':'wait','pauseKind':'human','pausePointCount':1,'resumedCount':0},'blockOutputs':{'wait.taskInput':{'nested':{'values':[1,True,None,'中文']}}}}}

    def client(self,handler):
        http=httpx.Client(transport=httpx.MockTransport(handler),follow_redirects=True);self.addCleanup(http.close)
        return SimWaitClient(SimClient('http://sim.test','private-key',client=http))

    def test_exact_plain_input_and_fixed_selected_output(self):
        def handler(r):
            self.assertEqual(r.method,'GET');self.assertEqual(r.url.path,'/api/v2/workflows/wf/runs/run')
            self.assertEqual(dict(r.url.params),{'selectedOutputs':'wait.taskInput'})
            return httpx.Response(200,json=self.payload())
        result=self.client(handler).read_input('wf','run','wait')
        self.assertEqual(result['input'],self.payload()['data']['blockOutputs']['wait.taskInput'])
        self.assertEqual(result['run']['contextId'],'wait')

    def test_identity_pause_and_hidden_rejections(self):
        mutations=[lambda d:d.update(workflowId='other'),lambda d:d.update(runId='other'),lambda d:d.update(status='completed'),lambda d:d.update(paused=None),lambda d:d['paused'].update(contextId='other'),lambda d:d['paused'].update(blockedOnBlockId='other'),lambda d:d['paused'].update(pausePointCount=2),lambda d:d['paused'].update(pausePointCount=True),lambda d:d['paused'].update(resumedCount=-1),lambda d:d.update(blockOutputs=None),lambda d:d.update(blockOutputs={}),lambda d:d['blockOutputs'].update({'wait.taskInput':'<开始.input>'}),lambda d:d.update(executionDataTruncated=True)]
        for mutation in mutations:
            payload=self.payload();mutation(payload['data'])
            with self.assertRaises(WaitInputUnavailable):self.client(lambda r:httpx.Response(200,json=payload)).read_input('wf','run','wait')

    def test_nested_redactions_secret_templates_and_large_refs_rejected(self):
        for value in ('[REDACTED_SECRET]','{{TOKEN}}',{'__largeValueRef':True},{'__simLargeValueRef':True}):
            payload=self.payload();payload['data']['blockOutputs']['wait.taskInput']={'nested':[value]}
            with self.assertRaises(WaitInputUnavailable):self.client(lambda r:httpx.Response(200,json=payload)).read_input('wf','run','wait')

    def test_literal_source_code_and_business_fields_remain_task_data(self):
        payload=self.payload()
        value={'html':'<div class="panel">正文</div>','code':'if (a < b && b > c) return x;',
               '__doc__':'describe this method','truncated':False,'redacted':True}
        payload['data']['blockOutputs']['wait.taskInput']=value
        self.assertEqual(self.client(lambda r:httpx.Response(200,json=payload)).read_input('wf','run','wait')['input'],value)

    def test_no_redirect_retry_or_error_body_disclosure(self):
        for code in (302,401,403,404,500):
            calls=[]
            def handler(r):calls.append(r);return httpx.Response(code,json={'secret':'private-key'},headers={'location':'http://evil.test'})
            with self.assertRaises(WaitInputUnavailable) as ctx:self.client(handler).read_input('wf','run','wait')
            self.assertEqual(len(calls),1);self.assertNotIn('private-key',str(ctx.exception))

    def test_bad_identifier_never_requests(self):
        def handler(r):self.fail('must not request')
        with self.assertRaises(WaitInputUnavailable):self.client(handler).read_input('wf','../run','wait')

    def test_transport_failure_is_bounded(self):
        def handler(r):raise httpx.ReadTimeout('private-key')
        with self.assertRaises(WaitInputUnavailable) as ctx:self.client(handler).read_input('wf','run','wait')
        self.assertEqual(str(ctx.exception),'upstream_transport_failed')

    def test_json_object_string_is_explicit_and_duplicate_keys_reject(self):
        payload=self.payload();payload['data']['blockOutputs']['wait.taskInput']='{"nested":{"x":1}}'
        payload['data']['paused']['resumedCount']=2
        payload['data']['paused']['pausePointCount']=3
        result=self.client(lambda r:httpx.Response(200,json=payload)).read_input('wf','run','wait')
        self.assertEqual(result['input'],{'nested':{'x':1}})
        self.assertEqual(result['encoding'],'json-object-string')
        self.assertEqual(result['run']['resumedCount'],2)
        for bad in ('{"a":1,"a":2}','{"a":NaN}','{"a":"[RUNTIME_BINDING]"}','{"a":"{{KEY}}"}','[]'):
            payload['data']['blockOutputs']['wait.taskInput']=bad
            with self.assertRaises(WaitInputUnavailable):self.client(lambda r:httpx.Response(200,json=payload)).read_input('wf','run','wait')
