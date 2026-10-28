"""Cross-layer native approvals: real ledgers/services, ASGI transport only.

Native execution identities are fixtures, never claims of Codex execution.
The native Future is created/resolved only on the plugin TestClient portal.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import importlib
from pathlib import Path
import sys
import types
import uuid
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from qwenpaw.app.approvals import ApprovalService, ApprovalRequestSummary
from qwenpaw.security.tool_guard.approval import ApprovalDecision

from agent_line.dispatcher import Dispatcher
from agent_line.role_dispatch import RoleDispatchGateway, RoleDispatchLedger
from agent_line.service import create_app
from tests import test_approval_api as outer_fixture


class NativeApprovalIntegrationTests(unittest.TestCase):
    def setUp(self):
        # Discover may also import test modules by their bare names. Each fixture
        # owns one package, so router relative imports resolve to the exact
        # module whose native service is patched below, regardless of discovery.
        package_name='_native_approval_integration_'+uuid.uuid4().hex
        package=types.ModuleType(package_name)
        package.__path__=[str(Path(__file__).resolve().parents[1]/'plugins/agent-line')]
        sys.modules[package_name]=package
        def unload():
            for key in list(sys.modules):
                if key==package_name or key.startswith(package_name+'.'):
                    sys.modules.pop(key,None)
        self.addCleanup(unload)
        self.plugin_fixture=types.SimpleNamespace(
            bridge_mod=importlib.import_module(package_name+'.execution_bridge'),
            mod=importlib.import_module(package_name+'.execution_approvals'))
        self.f=outer_fixture.ApprovalAPITests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.identity=dict(upstreamTaskId='fixture-native-task',codexThreadId='fixture-thread',codexTurnId='fixture-turn')
        task,_=self.f.store.register('native-approval-integration',dict(projectId='project',roleAgentId='parent',
            roleBindingId='parent',workspace=str(self.f.root),workflow=dict(id='workflow',runId='run',nodeId='node',nodeExecutionId='node',attemptId='1')))
        self.task=task['id'];self.session='agent-line-'+self.task
        self.f.store.claim(self.task,self.f.root);self.f.store.accept(self.task,self.identity)
        path=self.f.root/'plugin.sqlite';self.plugin_fixture.bridge_mod.ReceiptLedger.initialize(path)
        self.bridge=self.plugin_fixture.bridge_mod.ExecutionBridge.__new__(self.plugin_fixture.bridge_mod.ExecutionBridge)
        self.bridge.ledger=self.plugin_fixture.bridge_mod.ReceiptLedger(path)
        self.bridge.target='codex';self.bridge.roles={'parent'};self.bridge.workspace=self.f.root;self.bridge.token='plugin-service-secret'
        self.bridge.ledger.bind(self.task,'parent',self.session,'qwenpaw-role-v1:'+'a'*64)
        self.bridge.ledger.claim(self.task,dict(roleSessionId=self.session,targetAgentId='codex',workspace=str(self.f.root)))
        self.bridge.ledger.patch(self.task,status='accepted',identity=self.identity)
        async def observe(task_id):return self.bridge.ledger.get(task_id)[1]
        self.bridge.observe=observe
        self.service=ApprovalService()
        self.resolve_calls=[];original=self.service.resolve_request
        async def counted(*args,**kwargs):
            self.resolve_calls.append((args,kwargs));return await original(*args,**kwargs)
        self.service.resolve_request=counted
        self.service_patch=patch.object(self.plugin_fixture.mod,'get_approval_service',return_value=self.service)
        self.service_patch.start();self.addCleanup(self.service_patch.stop)
        async def proof(_api,identity):
            return dict(schemaVersion=1,codexThreadId=identity['codexThreadId'],codexTurnId=identity['codexTurnId'],
                owner='a'*32,containerId='b'*64,approvals=[dict(requestId=17,method='item/commandExecution/requestApproval',
                    threadId=identity['codexThreadId'],turnId=identity['codexTurnId'],itemId='fixture-item')])
        self.control_patch=patch.object(self.plugin_fixture.mod.ExecutionApprovals,'_control',proof)
        self.control_patch.start();self.addCleanup(self.control_patch.stop)
        self.plugin_client=None;self.start_plugin();self.addCleanup(self.stop_plugin)
        self.pending=self.plugin_client.portal.call(self.make_pending,'fixture-item')
        self.other=self.plugin_client.portal.call(self.make_pending,'other-item')
        self.roles=RoleDispatchLedger.initialize(self.f.root/'roles.sqlite')
        self.roles.claim(self.task,dict(roleAgentId='parent',roleSessionId=self.session,deploymentId='fixture-installation'))
        self.transport_calls=[];self.lose_response=False
        def transport(request):
            self.transport_calls.append((request.method,request.url.path))
            response=self.plugin_client.request(request.method,request.url.path,headers=dict(request.headers),content=request.content)
            if self.lose_response and request.method=='POST':
                self.lose_response=False
                raise httpx.ReadTimeout('fixture lost acknowledgement',request=request)
            return httpx.Response(response.status_code,content=response.content,headers={'content-type':'application/json'})
        self.http=httpx.Client(transport=httpx.MockTransport(transport));self.addCleanup(self.http.close)
        self.gateway=RoleDispatchGateway('http://fixture','host-secret','plugin-service-secret',role_agent_id='parent',
            deployment_id='fixture-installation',ledger=self.roles,memory_query='fixture memory query',client=self.http)
        self.outer=None;self.start_outer();self.addCleanup(lambda:self.outer.close())
        self.url='/extension/v1/node-tasks/'+self.task+'/execution-approvals'

    async def make_pending(self,item):
        return await self.service.create_pending_summary(session_id=self.session,root_session_id=self.session,
            owner_agent_id='codex',agent_id='codex',user_id='fixture',channel='console',
            summary=ApprovalRequestSummary(source_type='codex',name='Codex command',severity='high',result_summary='Run bounded fixture action',
                payload=dict(provider='codex',provider_item_id=item)))

    def start_plugin(self):
        app=FastAPI()
        @app.middleware('http')
        async def fixture_host_auth(request,next_handler):
            if request.headers.get('Authorization')!='Bearer host-secret':return JSONResponse({'detail':'host authentication required'},status_code=401)
            return await next_handler(request)
        app.include_router(self.bridge.router(),prefix='/api/agent-line')
        self.plugin_client=TestClient(app);self.plugin_client.__enter__()

    def stop_plugin(self):
        if self.plugin_client:self.plugin_client.__exit__(None,None,None);self.plugin_client=None

    def start_outer(self):
        if self.outer:self.outer.close()
        dispatcher=Dispatcher(self.f.store,{'project':self.gateway})
        # No lifespan worker starts: this tests only authenticated API delivery.
        self.outer=TestClient(create_app(self.f.store,self.f.config,approvals=self.f.api,dispatcher=dispatcher))

    def get(self,token='human-token'):
        return self.outer.get(self.url,headers={'Authorization':'Bearer '+token})

    def decide(self,decision='approve',token='human-token'):
        return self.outer.post(self.url+'/'+self.pending.request_id+'/decision',headers={'Authorization':'Bearer '+token},
            json=dict(decisionId='fixture-decision',decision=decision,expectedIdentity=self.identity))

    def future(self,pending):
        async def read():return pending.future.result() if pending.future.done() else None
        return self.plugin_client.portal.call(read)

    def test_approve_resolves_only_original_future_once_and_survives_app_rebuild(self):
        response=self.decide();self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['deliveryStatus'],'accepted')
        self.assertEqual(self.future(self.pending),ApprovalDecision.APPROVED);self.assertIsNone(self.future(self.other))
        self.assertEqual(self.decide().status_code,200);self.assertEqual(len(self.resolve_calls),1)
        self.stop_plugin();self.start_plugin();self.start_outer()
        listing=self.get();self.assertEqual(listing.status_code,200,listing.text)
        row=next(x for x in listing.json()['approvals'] if x['requestId']==self.pending.request_id)
        self.assertEqual(row['decision']['decidedBy'],'human');self.assertEqual(row['deliveryStatus'],'accepted')
        self.assertEqual(self.decide().status_code,200);self.assertEqual(len(self.resolve_calls),1)
        task=self.f.store.get(self.task);self.assertEqual(task['executionStatus'],'accepted')
        self.assertEqual(task['verificationStatus'],'pending');self.assertFalse(self.f.sim.posts)

    def test_reject_reaches_real_native_future(self):
        response=self.decide('reject');self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.future(self.pending),ApprovalDecision.DENIED)
        self.assertIsNone(self.future(self.other));self.assertEqual(len(self.resolve_calls),1)

    def test_lost_transport_response_replay_observes_receipt_without_second_post(self):
        self.lose_response=True;response=self.decide();self.assertEqual(response.status_code,503,response.text)
        before=len([x for x in self.transport_calls if x[0]=='POST'])
        response=self.decide();self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['deliveryStatus'],'accepted')
        self.assertEqual(len([x for x in self.transport_calls if x[0]=='POST']),before)
        self.assertEqual(len(self.resolve_calls),1)

    def test_persisted_intent_after_uncertain_native_reply_is_not_replayed(self):
        async def fail(*args,**kwargs):raise RuntimeError('fixture native ambiguity')
        self.service.resolve_request=fail
        response=self.decide();self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['deliveryStatus'],'uncertain')
        self.assertIsNone(self.future(self.pending))
        self.stop_plugin();self.start_plugin();self.start_outer()
        before=len([x for x in self.transport_calls if x[0]=='POST'])
        self.assertEqual(self.decide().json()['deliveryStatus'],'uncertain')
        self.assertEqual(len([x for x in self.transport_calls if x[0]=='POST']),before)

    def test_concurrent_same_decision_only_one_native_resolution(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses=list(pool.map(lambda _:self.decide(),range(2)))
        self.assertTrue(all(r.status_code in (200,409) for r in responses),[r.text for r in responses])
        self.assertEqual(len(self.resolve_calls),1)

    def test_two_auth_boundaries_and_project_scope_are_independent(self):
        for token,code in [('service-token',401),('other-token',404),('bad',401)]:
            self.assertEqual(self.get(token).status_code,code)
        self.assertFalse(self.transport_calls)
        plugin_path='/api/agent-line/executions/'+self.task+'/approvals'
        self.assertEqual(self.plugin_client.get(plugin_path,headers={'X-Agent-Line-Service-Token':'plugin-service-secret'}).status_code,401)
        self.assertEqual(self.plugin_client.get(plugin_path,headers={'Authorization':'Bearer host-secret'}).status_code,403)
        self.assertIsNone(self.future(self.pending));self.assertFalse(self.resolve_calls)

    def test_interrupted_plugin_intent_survives_outer_rebuild_without_delivery(self):
        async def interrupted(*args,**kwargs):raise asyncio.CancelledError()
        self.service.resolve_request=interrupted
        api=self.plugin_fixture.mod.ExecutionApprovals(self.bridge)
        body=self.plugin_fixture.mod.DecisionRequest(decisionId='fixture-decision',decision='approve',
            decidedBy='human',expectedIdentity=self.identity)
        from concurrent.futures import CancelledError
        with self.assertRaises(CancelledError):
            self.plugin_client.portal.call(api.decide,self.task,self.pending.request_id,body)
        history=self.bridge.ledger.get(self.task)[1]['approvalDecisions'][self.pending.request_id]
        self.assertEqual(history['deliveryStatus'],'intent')
        self.start_outer();before=len(self.transport_calls)
        response=self.decide();self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['deliveryStatus'],'intent')
        self.assertFalse(any(method=='POST' for method,_ in self.transport_calls[before:]))
        self.assertIsNone(self.future(self.pending))

    def test_unknown_plugin_receipt_never_calls_native_resolution(self):
        with self.bridge.ledger.connection() as db:
            db.execute('DELETE FROM receipts WHERE task_id=?',(self.task,))
        self.assertEqual(self.get().status_code,404)
        self.assertEqual(self.decide().status_code,404)
        self.assertFalse(self.resolve_calls);self.assertIsNone(self.future(self.pending))
