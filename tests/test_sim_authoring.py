import json
import subprocess
import unittest

import httpx
from agent_line.adapters.sim import SimClient
from agent_line.sim_authoring import (SimAuthoringClient, AuthoringError, AuthoringUncertain,
                                     UnsupportedDocument, compile_document, decode_document)


def document():
    def node(i,t,c): return {'id':i,'type':t,'name':'中文'+i,'position':{'x':12,'y':24},'config':c}
    return {'schemaVersion':1,'nodes':[node('start','start',{}),node('check','condition',{'rules':[{'id':'yes','operator':'eq','field':'input.answer','value':True}]}),node('approved','end',{'output':{'结果':'通过'}}),node('rejected','end',{'output':{'结果':'拒绝'}})],'edges':[{'id':'one','source':'start','target':'check'},{'id':'two','source':'check','target':'approved','branch':'yes'},{'id':'three','source':'check','target':'rejected','branch':'else'}],'variables':{}}


class CompilerTests(unittest.TestCase):
    def test_roundtrip_namespace_positions_and_chinese(self):
        doc=document(); state=compile_document(doc,'workflow-a')
        self.assertEqual(decode_document(state),doc)
        self.assertEqual(state,compile_document(doc,'workflow-a'))
        self.assertTrue(set(state['blocks']).isdisjoint(compile_document(doc,'workflow-b')['blocks']))
        native={**state,'loops':{},'parallels':{}}
        self.assertEqual(decode_document(native),doc)

    def test_condition_executes_strictly_and_missing_throws(self):
        state=compile_document(document(),'wf')
        condition=next(b for b in state['blocks'].values() if b['type']=='condition')
        expr=json.loads(condition['subBlocks']['conditions']['value'])[0]['value']
        import re
        for value,expected in [({'answer':True},'true'),({'answer':False},'false'),({},'ERROR'),({'answer':'true'},'ERROR'),(None,'ERROR')]:
            code=re.sub(r'<[^>]+\.input>',json.dumps(value),expr)
            result=subprocess.run(['node','-e',f'try {{ console.log({code}); }} catch(e) {{ console.log("ERROR"); }}'],capture_output=True,text=True,check=True)
            self.assertEqual(result.stdout.strip(),expected)

    def test_reject_bad_graphs_fields_unknown_types(self):
        mutations=[lambda d:d['nodes'][1]['config']['rules'][0].update(field='input.x);process.exit()'),lambda d:d['nodes'][1]['config']['rules'][0].update(field='input.__proto__.x'),lambda d:d['edges'].pop(),lambda d:d['edges'][1].update(branch='else'),lambda d:d['edges'][2].update(target='check'),lambda d:d['nodes'][0].update(type='agent'),lambda d:d.update(variables={'secret':'x'}),lambda d:d['nodes'][0]['position'].update(x=float('nan'))]
        for change in mutations:
            d=document();change(d)
            with self.assertRaises(AuthoringError):compile_document(d,'wf')

    def test_decode_refuses_modified_native_expression_or_unknown_data(self):
        state=compile_document(document(),'wf'); condition=next(b for b in state['blocks'].values() if b['type']=='condition')
        condition['subBlocks']['conditions']['value']=condition['subBlocks']['conditions']['value'].replace('return v === true','return true')
        with self.assertRaises(UnsupportedDocument):decode_document(state)
        state=compile_document(document(),'wf');next(iter(state['blocks'].values()))['data']={'parentId':'loop'}
        with self.assertRaises(UnsupportedDocument):decode_document(state)

    def test_human_uses_native_fields_not_approval_metadata(self):
        d=document();d['nodes'][1].update(type='human',config={'requiredBoolean':'approved'})
        d['nodes'].pop();d['edges'].pop();d['edges'][1].pop('branch')
        state=compile_document(d,'wf');self.assertEqual(decode_document(state),d)
        b=next(b for b in state['blocks'].values() if b['type']=='human_in_the_loop_v2')
        self.assertEqual(b['subBlocks']['inputFormat']['value'][0]['type'],'boolean')

    def test_template_value_cannot_inject_native_reference(self):
        d=document();d['nodes'][1]['config']['rules'][0]['value']='<secret.output>{{KEY}}'
        state=compile_document(d,'wf');b=next(b for b in state['blocks'].values() if b['type']=='condition')
        expr=json.loads(b['subBlocks']['conditions']['value'])[0]['value']
        self.assertNotIn('<secret.output>',expr);self.assertNotIn('{{KEY}}',expr)
        self.assertEqual(decode_document(state),d)

    def test_pinned_normalized_defaults_only(self):
        d=document();state=compile_document(d,'wf')
        state.update(loops={},parallels={},lastSaved=1792602028643)
        for block in state['blocks'].values():
            block.update(horizontalHandles=True,height=0,advancedMode=False,errorEnabled=False,triggerMode=False,data={},locked=False)
        for edge in state['edges']:edge.update(type='default',data={})
        self.assertEqual(decode_document(state),d)
        next(iter(state['blocks'].values()))['errorEnabled']=True
        with self.assertRaises(UnsupportedDocument):decode_document(state)

    def test_names_cannot_inject_reference_and_duplicate_names_reject(self):
        for name in ('开始>;<secret.x>', '中文check'):
            d=document();d['nodes'][0]['name']=name
            with self.assertRaises(AuthoringError):compile_document(d,'wf')


class TransportTests(unittest.TestCase):
    def setup_client(self,handler):
        client=httpx.Client(transport=httpx.MockTransport(handler),follow_redirects=True)
        self.addCleanup(client.close)
        return SimAuthoringClient(SimClient('http://sim.test','private-key',client=client),'workspace')

    def test_workspace_mismatch_prevents_write(self):
        calls=[]
        def handler(r):
            calls.append(r.method)
            return httpx.Response(200,json={'data':{'id':'wf','workspaceId':'other','name':'x'}})
        api=self.setup_client(handler)
        with self.assertRaises(AuthoringError):api.save_state('wf',compile_document(document(),'wf'))
        self.assertEqual(calls,['GET'])

    def test_save_fixed_contract_and_no_extra_fields(self):
        calls=[]
        def handler(r):
            calls.append(r)
            self.assertEqual(r.headers['x-api-key'],'private-key')
            if r.method=='GET':return httpx.Response(200,json={'data':{'id':'wf','workspaceId':'workspace','name':'测试'}})
            body=json.loads(r.content);self.assertEqual(set(body),{'blocks','edges','variables'})
            self.assertEqual(r.url.params['dryRun'],'true')
            return httpx.Response(200,json={'data':{'id':'wf','dryRun':True,'needsRedeployment':False,'warnings':[],'lint':{}}})
        api=self.setup_client(handler);result=api.save_state('wf',compile_document(document(),'wf'),dry_run=True)
        self.assertTrue(result['dryRun']);self.assertEqual([r.method for r in calls],['GET','PUT'])

    def test_uncertain_write_no_retry_no_redirect_or_secret_error(self):
        for status,body in [(302,{}),(500,{'secret':'private-key'}),(201,{'data':{'id':'x'}})]:
            calls=[]
            def handler(r):
                calls.append(r);return httpx.Response(status,json=body,headers={'location':'http://evil.test'})
            api=self.setup_client(handler)
            with self.assertRaises(AuthoringUncertain) as ctx:api.create('测试','描述')
            self.assertEqual(len(calls),1);self.assertNotIn('private-key',str(ctx.exception))

    def test_timeout_write_unknown_and_read_failure(self):
        def handler(r):raise httpx.ReadTimeout('secret')
        api=self.setup_client(handler)
        with self.assertRaises(AuthoringUncertain):api.create('x')
        with self.assertRaises(AuthoringError) as ctx:api.get_detail('wf')
        self.assertNotIsInstance(ctx.exception,AuthoringUncertain)

    def test_list_fixed_workspace_cursor_and_path_rejected(self):
        def handler(r):
            self.assertEqual(r.url.params['workspaceId'],'workspace')
            self.assertEqual(r.url.params['cursor'],'opaque')
            return httpx.Response(200,json={'data':[{'id':'wf','workspaceId':'workspace','name':'中文'}],'nextCursor':None})
        api=self.setup_client(handler);self.assertEqual(len(api.list_workflows('opaque',1)['data']),1)
        with self.assertRaises(AuthoringError):api.get_detail('../secret')

    def test_known_rejection_not_uncertain(self):
        api=self.setup_client(lambda r:httpx.Response(403,json={'secret':'credential'}))
        with self.assertRaises(AuthoringError) as ctx:api.create('x')
        self.assertNotIsInstance(ctx.exception,AuthoringUncertain)
        self.assertEqual(ctx.exception.code,'upstream_rejected')

    def test_versions_and_deploy_public_contract(self):
        state=compile_document(document(),'wf');calls=[]
        def handler(r):
            calls.append((r.method,r.url.path))
            if r.url.path.endswith('/state'):return httpx.Response(200,json={'data':{**state,'loops':{},'parallels':{}}})
            if r.url.path.endswith('/versions'):return httpx.Response(200,json={'data':[{'id':'version-id','version':1,'isActive':True}],'nextCursor':None})
            if r.url.path.endswith('/deploy'):return httpx.Response(200,json={'data':{'id':'wf','isDeployed':True,'version':1,'activeDeployment':{'deploymentVersionId':'version-id'}}})
            return httpx.Response(200,json={'data':{'id':'wf','workspaceId':'workspace','name':'x'}})
        api=self.setup_client(handler)
        self.assertEqual(api.versions('wf')['data'][0]['version'],1)
        self.assertEqual(api.deploy('wf')['version'],1)
        self.assertEqual(sum(method=='POST' for method,path in calls),1)

    def test_get_version_scope_and_revision(self):
        def handler(r):
            if r.url.path.endswith('/versions/2'):
                return httpx.Response(200,json={'data':{'id':'published-id','version':2,'isActive':False,'state':{}}})
            return httpx.Response(200,json={'data':{'id':'wf','workspaceId':'workspace','name':'x'}})
        api=self.setup_client(handler)
        self.assertEqual(api.get_version('wf','2')['id'],'published-id')
        with self.assertRaises(AuthoringError):api.get_version('wf','../active')


class InitialStateTests(unittest.TestCase):
    def initial(self):
        bid='f2ca410a-97cf-4190-95fd-720a0fcf66a7'
        return {'blocks':{bid:{'id':bid,'type':'start_trigger','name':'Start','position':{'x':0,'y':0},'subBlocks':{'inputFormat':{'id':'inputFormat','type':'input-format','value':[{'id':'f414d801-6a0c-4a7f-a711-4c2ff2b7c1cc','name':'','type':'string','value':'','collapsed':False}]},'runMetadata':{'id':'runMetadata','type':'switch','value':False}},'outputs':{'files':{'type':'file[]','description':'User uploaded files'},'input':{'type':'string','description':'Primary user input or message'},'conversationId':{'type':'string','description':'Conversation thread identifier'}},'enabled':True,'horizontalHandles':True,'height':0,'advancedMode':False,'errorEnabled':False,'triggerMode':False,'data':{},'locked':False}},'edges':[],'loops':{},'parallels':{},'variables':{}}

    def test_pinned_initial_and_genuinely_empty(self):
        from agent_line.sim_authoring import is_initial_state
        self.assertTrue(is_initial_state(self.initial()))
        self.assertTrue(is_initial_state({'blocks':{},'edges':[],'variables':{}}))
        self.assertFalse(is_initial_state({}))

    def test_modified_or_unknown_initial_refused(self):
        from agent_line.sim_authoring import is_initial_state
        mutations=[lambda s,b:s.update(unknown=True),lambda s,b:b.update(unknown=True),lambda s,b:b.update(name='用户流程'),lambda s,b:b['position'].update(x=1),lambda s,b:b['outputs']['input'].update(description='changed'),lambda s,b:b['subBlocks']['inputFormat']['value'][0].update(value='private'),lambda s,b:b['subBlocks']['inputFormat']['value'][0].update(name='field'),lambda s,b:s['blocks'].update(other=b),lambda s,b:s['variables'].update(x={}),lambda s,b:b.update(enabled=1),lambda s,b:b['subBlocks']['runMetadata'].update(value=True)]
        for mutate in mutations:
            state=self.initial();block=next(iter(state['blocks'].values()));mutate(state,block)
            self.assertFalse(is_initial_state(state))


class AgentCompilerTests(unittest.TestCase):
    def config(self,source='start',field='input'):
        return {'roleBindingId':'developer','instruction':'请实现 <代码>，不要展开 {{TOKEN}}。','inputFrom':{'nodeId':source,'field':field},'memoryPolicyId':'native-memory','outputSchemaId':'result-v1','requireMemoryCommit':False,'allowFailureBranch':True}

    def doc(self):
        def node(i,t,c):return {'id':i,'type':t,'name':{'start':'开始','first':'实现','second':'检查','end':'结束'}[i],'position':{'x':0,'y':0},'config':c}
        return {'schemaVersion':1,'nodes':[node('start','start',{}),node('first','agent',self.config()),node('second','agent',self.config('first','output')),node('end','end',{'output':{'ok':True}})],'edges':[{'id':'a','source':'start','target':'first'},{'id':'b','source':'first','target':'second'},{'id':'c','source':'second','target':'end'}],'variables':{}}

    def test_agent_roundtrip_and_native_id(self):
        from agent_line.sim_authoring import native_id
        doc=self.doc();state=compile_document(doc,'workflow-id')
        self.assertEqual(decode_document(state),doc)
        first=state['blocks'][native_id('workflow-id','first')]
        second=state['blocks'][native_id('workflow-id','second')]
        self.assertEqual(first['type'],'human_in_the_loop_v2')
        fields=first['subBlocks']['inputFormat']['value']
        self.assertEqual(fields[0]['value'],'<开始.input>')
        self.assertEqual(second['subBlocks']['inputFormat']['value'][0]['value'],'<实现.resumeInput.output>')
        self.assertTrue(fields[1]['value'].startswith('AL_AGENT_V1:'))
        self.assertNotIn('<',fields[1]['value']);self.assertNotIn('{{',fields[1]['value'])
        self.assertEqual(first['subBlocks']['notification']['value'],[])
        self.assertEqual(first['subBlocks']['builderData']['value'],[])
        self.assertNotEqual(native_id('workflow-id','first'),native_id('different','first'))
        for namespace,node in [('', 'first'),('x','../role'),(None,'first')]:
            with self.assertRaises(AuthoringError):native_id(namespace,node)

    def test_bad_config_roles_booleans_references_rejected(self):
        edits=[lambda c:c.update(roleBindingId='/tmp/role'),lambda c:c.update(memoryPolicyId='../policy'),lambda c:c.update(outputSchemaId='{{TOKEN}}'),lambda c:c.update(roleAgentId='hidden'),lambda c:c.update(requireMemoryCommit=1),lambda c:c.update(allowFailureBranch='false'),lambda c:c.update(instruction=' '),lambda c:c.update(instruction='a'*4001),lambda c:c['inputFrom'].update(field='resumeInput.output'),lambda c:c['inputFrom'].update(nodeId='unknown'),lambda c:c['inputFrom'].update(extra=True),lambda c:c.update(inputFrom={'nodeId':'first','field':'output'})]
        for edit in edits:
            doc=self.doc();edit(doc['nodes'][1]['config'])
            with self.assertRaises(AuthoringError):compile_document(doc,'wf')

    def test_future_source_is_not_dominator(self):
        doc=self.doc();doc['nodes'][1]['config']['inputFrom']={'nodeId':'second','field':'output'}
        with self.assertRaises(AuthoringError) as ctx:compile_document(doc,'wf')
        self.assertEqual(ctx.exception.code,'agent_source_not_dominator')

    def test_branch_local_source_cannot_feed_merge(self):
        doc=self.doc()
        doc['nodes'].insert(1,{'id':'gate','type':'condition','name':'条件','position':{'x':0,'y':0},'config':{'rules':[{'id':'yes','operator':'eq','field':'input.yes','value':True}]}})
        doc['edges']=[{'id':'a','source':'start','target':'gate'},{'id':'b','source':'gate','target':'first','branch':'yes'},{'id':'c','source':'gate','target':'second','branch':'else'},{'id':'d','source':'first','target':'second'},{'id':'e','source':'second','target':'end'}]
        with self.assertRaises(AuthoringError) as ctx:compile_document(doc,'wf')
        self.assertEqual(ctx.exception.code,'agent_source_not_dominator')
        # Start remains mandatory on both paths and is a valid source.
        doc['nodes'][3]['config']['inputFrom']={'nodeId':'start','field':'input'}
        self.assertEqual(decode_document(compile_document(doc,'wf')),doc)

    def test_agent_recognition_requires_exact_native_shape(self):
        from agent_line.sim_authoring import native_id
        changes=[lambda b:b['subBlocks']['inputFormat']['value'][0].update(value='<其他.input>'),lambda b:b['subBlocks']['inputFormat']['value'][1].update(type='object'),lambda b:b['subBlocks']['inputFormat']['value'][1].update(value='AL_AGENT_V1:invalid'),lambda b:b['subBlocks'].update(extra={'id':'extra','type':'short-input','value':'bad'}),lambda b:b['subBlocks']['notification'].update(value=[{'tool':'send'}])]
        for change in changes:
            state=compile_document(self.doc(),'wf');change(state['blocks'][native_id('wf','first')])
            with self.assertRaises(UnsupportedDocument):decode_document(state)

    def test_node_list_order_does_not_replace_graph_order(self):
        doc=self.doc();doc['nodes'].reverse()
        self.assertEqual(decode_document(compile_document(doc,'wf')),doc)


class AgentResultCompilerTests(unittest.TestCase):
    def doc(self):
        base=AgentCompilerTests().doc()
        base['nodes']=[base['nodes'][0],base['nodes'][1],{'id':'gate','type':'condition','name':'验收条件','position':{'x':400,'y':0},'config':{'rules':[{'id':'passed','operator':'eq','source':'first','field':'result.verificationStatus','value':'passed'}]}}, {'id':'yes','type':'end','name':'输出结果','position':{'x':600,'y':0},'config':{'resultFrom':'first'}},{'id':'no','type':'end','name':'未通过','position':{'x':600,'y':200},'config':{'output':{'status':'not_passed'}}}]
        base['edges']=[{'id':'a','source':'start','target':'first'},{'id':'b','source':'first','target':'gate'},{'id':'c','source':'gate','target':'yes','branch':'passed'},{'id':'d','source':'gate','target':'no','branch':'else'}]
        return base

    def test_result_condition_and_dynamic_end_lossless(self):
        from agent_line.sim_authoring import native_id
        d=self.doc();state=compile_document(d,'wf')
        self.assertEqual(decode_document(state),d)
        value=state['blocks'][native_id('wf','yes')]['subBlocks']['data']['value']
        self.assertEqual(value,'{"agentLineResult": <实现.resumeInput>}')
        expr=json.loads(state['blocks'][native_id('wf','gate')]['subBlocks']['conditions']['value'])[0]['value']
        self.assertIn('let v = <实现.resumeInput>',expr)
        self.assertNotIn('<开始.input>',expr)

    def test_resolved_result_condition_true_false_missing_and_wrong_type(self):
        from agent_line.sim_authoring import native_id
        for field,expected_value,samples in [
            ('result.verificationStatus','passed',[({'verificationStatus':'passed'},'true'),({'verificationStatus':'failed'},'false'),({},'ERROR'),({'verificationStatus':True},'ERROR')]),
            ('result.output.review.score',7,[({'output':{'review':{'score':7}}},'true'),({'output':{'review':{'score':6}}},'false'),({'output':{}},'ERROR'),({'output':{'review':{'score':'7'}}},'ERROR')])]:
            d=self.doc();d['nodes'][2]['config']['rules'][0].update(field=field,value=expected_value)
            state=compile_document(d,'wf');expr=json.loads(state['blocks'][native_id('wf','gate')]['subBlocks']['conditions']['value'])[0]['value']
            for result,expected in samples:
                code=expr.replace('<实现.resumeInput>',json.dumps(result))
                output=subprocess.run(['node','-e',f'try {{ console.log({code}); }} catch(e) {{ console.log("ERROR"); }}'],text=True,capture_output=True,check=True)
                self.assertEqual(output.stdout.strip(),expected)
            self.assertEqual(decode_document(state),d)

    def test_source_and_field_strict_allowlist(self):
        updates=[{'source':'start'},{'source':'missing'},{'source':'../first'}, {'field':'result.output.__proto__.x'}, {'field':'result.executionStatus'}, {'field':'result.output'}, {'field':'input.x'}, {'field':'result.output.x);evil()'}, {'extra':'value'}]
        for update in updates:
            d=self.doc();d['nodes'][2]['config']['rules'][0].update(update)
            with self.assertRaises(AuthoringError):compile_document(d,'wf')
        d=self.doc();d['nodes'][2]['config']['rules'][0].pop('source')
        with self.assertRaises(AuthoringError):compile_document(d,'wf')

    def test_end_mutual_exclusion_and_non_agent_source(self):
        for config in ({'output':{},'resultFrom':'first'},{'resultFrom':'start'},{'resultFrom':'missing'},{'resultFrom':'first','extra':True}):
            d=self.doc();d['nodes'][3]['config']=config
            with self.assertRaises(AuthoringError):compile_document(d,'wf')

    def test_future_and_branch_local_result_sources_refused(self):
        d=self.doc()
        # Gate executes before first Agent; one branch reaches first, the other never does.
        d['edges']=[{'id':'a','source':'start','target':'gate'},{'id':'b','source':'gate','target':'first','branch':'passed'},{'id':'c','source':'first','target':'yes'},{'id':'d','source':'gate','target':'no','branch':'else'}]
        with self.assertRaises(AuthoringError) as ctx:compile_document(d,'wf')
        self.assertEqual(ctx.exception.code,'result_source_not_dominator')
        d['nodes'][2]['config']['rules'][0]={'id':'passed','operator':'eq','field':'input.flag','value':True}
        self.assertEqual(decode_document(compile_document(d,'wf')),d)
        d['nodes'][4]['config']={'resultFrom':'first'}
        with self.assertRaises(AuthoringError) as ctx:compile_document(d,'wf')
        self.assertEqual(ctx.exception.code,'result_source_not_dominator')

    def test_native_result_templates_cannot_be_modified_or_quoted(self):
        from agent_line.sim_authoring import native_id
        for value in ('{"agentLineResult": "<实现.resumeInput>"}', '{"other": <实现.resumeInput>}', '{"agentLineResult": <开始.resumeInput>}'):
            state=compile_document(self.doc(),'wf');state['blocks'][native_id('wf','yes')]['subBlocks']['data']['value']=value
            with self.assertRaises(UnsupportedDocument):decode_document(state)


class FiniteLoopCompilerTests(unittest.TestCase):
    @staticmethod
    def doc():
        d=AgentCompilerTests().doc()
        start,body,_,end=d['nodes']
        body['config']['allowFailureBranch']=True
        loop={'id':'repair','type':'loop','name':'修复循环','position':{'x':250,'y':0},'config':{'body':'first','maxIterations':3}}
        end['config']={'resultFrom':'first'}
        d['nodes']=[start,loop,body,end]
        d['edges']=[{'id':'a','source':'start','target':'repair'}, {'id':'b','source':'repair','target':'first','branch':'body'}, {'id':'c','source':'repair','target':'end','branch':'done'}]
        return d

    def test_single_native_loop_roundtrip_and_policy(self):
        from agent_line.sim_authoring import native_id,loop_policies,_derived_loops
        d=self.doc();s=compile_document(d,'wf');body=native_id('wf','first');loop=native_id('wf','repair')
        self.assertEqual(loop_policies(d,'wf'),{body:{'loopId':loop,'maxIterations':3}})
        self.assertEqual(s['blocks'][body]['data'],{'parentId':loop,'extent':'parent'})
        self.assertEqual(s['blocks'][body]['subBlocks']['inputFormat']['value'][0]['value'],'{"input": <开始.input>, "iteration": <loop.index>}')
        self.assertEqual(decode_document(s),d)
        s.update(loops=_derived_loops(s['blocks']),parallels={})
        s['blocks'][loop]['data'].update(collection='',whileCondition='')
        self.assertEqual(decode_document(s),d)
        s['blocks'][loop]['data']['collection']='unexpected'
        with self.assertRaises(UnsupportedDocument):decode_document(s)
        self.assertEqual(len([b for b in s['blocks'].values() if b['type']=='human_in_the_loop_v2']),1)
        self.assertEqual([e['sourceHandle'] for e in s['edges'][1:3]],['loop-start-source','loop-end-source'])

    def test_guard_strict_results_and_bounds_in_node(self):
        from agent_line.sim_authoring import native_id
        for bound in (1,2,3):
            d=self.doc();d['nodes'][1]['config']['maxIterations']=bound
            graph=compile_document(d,'wf');loop=graph['blocks'][native_id('wf','repair')]
            guard=graph['blocks'][native_id('wf','repair')+'--guard']
            expr=loop['data']['doWhileCondition'];code=guard['subBlocks']['code']['value']
            samples=[({'executionStatus':'succeeded','verificationStatus':'passed'},False),({'executionStatus':'succeeded','verificationStatus':'failed'},True),({'executionStatus':'failed'},True)]
            for result,retry in samples:
                source=code.replace('<实现.resumeInput>',json.dumps(result))
                proc=subprocess.run(['node','-e','console.log(JSON.stringify((()=>{'+source+'})()))'],text=True,capture_output=True,check=True)
                self.assertEqual(json.loads(proc.stdout),{'result':result,'needsRepair':retry})
                for index in (0,1,2,3):
                    condition=expr.replace('<loop.index>',str(index)).replace('<'+guard['name']+'.result.needsRepair>',str(retry).lower())
                    proc=subprocess.run(['node','-e',f'console.log({condition})'],text=True,capture_output=True,check=True)
                    self.assertEqual(proc.stdout.strip(),str(retry and index<bound).lower())
            for result in (None,[],True,{}, {'executionStatus':'cancelled'}, {'executionStatus':True}, {'executionStatus':'succeeded'}, {'executionStatus':'succeeded','verificationStatus':True}, {'executionStatus':'succeeded','verificationStatus':'pending'}):
                source=code.replace('<实现.resumeInput>',json.dumps(result))
                proc=subprocess.run(['node','-e','try { (()=>{'+source+'})(); console.log("BAD"); } catch(e) { console.log("ERROR"); }'],text=True,capture_output=True,check=True)
                self.assertEqual(proc.stdout.strip(),'ERROR')

    def test_reject_shape_bound_policy_and_body_edges(self):
        changes=[lambda d:d['nodes'][1]['config'].update(maxIterations=0),lambda d:d['nodes'][1]['config'].update(maxIterations=4),lambda d:d['nodes'][1]['config'].update(maxIterations=True),lambda d:d['nodes'][1]['config'].update(maxIterations='3'),lambda d:d['nodes'][1]['config'].update(extra=True),lambda d:d['nodes'][1]['config'].update(body='start'),lambda d:d['nodes'][2]['config'].update(allowFailureBranch=False),lambda d:d['edges'].append({'id':'extra','source':'first','target':'end'}),lambda d:d['edges'][1].update(branch='done'),lambda d:d['edges'][0].update(target='first')]
        for change in changes:
            d=self.doc();change(d)
            with self.assertRaises(AuthoringError):compile_document(d,'wf')

    def test_dominance_allows_body_after_completion_but_not_bypass(self):
        d=self.doc();d['nodes'].insert(1,{'id':'gate','type':'condition','name':'入口','position':{'x':10,'y':0},'config':{'rules':[{'id':'go','operator':'eq','field':'input.go','value':True}]}})
        d['edges'][0]['target']='gate'
        d['edges'] += [{'id':'go','source':'gate','target':'repair','branch':'go'},{'id':'skip','source':'gate','target':'end','branch':'else'}]
        with self.assertRaises(AuthoringError) as ctx:compile_document(d,'wf')
        self.assertEqual(ctx.exception.code,'result_source_not_dominator')
        d['nodes'][-1]['config']={'output':{}}
        self.assertEqual(decode_document(compile_document(d,'wf')),d)
        d['nodes'][1]['config']['rules'][0].update(source='first',field='result.verificationStatus',value='passed')
        with self.assertRaises(AuthoringError):compile_document(d,'wf')

    def test_loop_input_requires_external_dominator(self):
        d=self.doc();d['nodes'][2]['config']['inputFrom']={'nodeId':'first','field':'output'}
        with self.assertRaises(AuthoringError):compile_document(d,'wf')
        later=AgentCompilerTests().doc()['nodes'][2]
        d=self.doc();d['nodes'].insert(-1,later)
        d['edges'][-1]['target']='second';d['edges'].append({'id':'later','source':'second','target':'end'})
        d['nodes'][2]['config']['inputFrom']={'nodeId':'second','field':'output'}
        with self.assertRaises(AuthoringError) as ctx:compile_document(d,'wf')
        self.assertEqual(ctx.exception.code,'agent_source_not_dominator')

    def test_unknown_native_loop_or_iteration_template_readonly(self):
        from agent_line.sim_authoring import native_id,_derived_loops
        loop=native_id('wf','repair');body=native_id('wf','first')
        changes=[lambda s:s['blocks'][loop]['data'].update(doWhileCondition='true'),lambda s:s['blocks'][loop]['data'].update(width=501),lambda s:s['blocks'][body]['data'].update(parentId='other'),lambda s:s['blocks'][body]['subBlocks']['inputFormat']['value'][0].update(value='<开始.input>'),lambda s:s['loops'][loop].update(iterations=2),lambda s:s['loops'][loop].update(nodes=[]),lambda s:s['loops'].clear(),lambda s:s.update(parallels={'unknown':{}}),lambda s:s['blocks'][loop+'--guard']['subBlocks']['code'].update(value='return true'),lambda s:s['blocks'][loop+'--guard'].update(type='agent'),lambda s:s['edges'].pop(),lambda s:s['blocks'].pop(loop+'--guard')]
        for change in changes:
            s=compile_document(self.doc(),'wf');s['loops']=_derived_loops(s['blocks']);change(s)
            with self.assertRaises(UnsupportedDocument):decode_document(s)

    def test_loop_reference_matches_observed_native_function_envelope(self):
        from agent_line.sim_authoring import native_id
        graph=compile_document(self.doc(),'wf');loop=graph['blocks'][native_id('wf','repair')]
        guard=graph['blocks'][native_id('wf','repair')+'--guard']
        # Public selectedOutputs returns Function result wrapped by its native handler.
        native={'result':{'result':{'executionStatus':'succeeded','verificationStatus':'failed'},'needsRepair':True},'stdout':'','files':[]}
        expression=loop['data']['doWhileCondition'].replace('<loop.index>','1').replace('<'+guard['name']+'.result.needsRepair>','native.result.needsRepair')
        proc=subprocess.run(['node','-e','const native='+json.dumps(native)+';console.log('+expression+');'],text=True,capture_output=True,check=True)
        self.assertEqual(proc.stdout.strip(),'true')
