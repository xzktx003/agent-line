"""Real native ApprovalService; temporary plugin receipts, no model execution."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import AsyncMock, patch
from fastapi import HTTPException
from pydantic import ValidationError
from qwenpaw.app.approvals import ApprovalService, ApprovalRequestSummary
from qwenpaw.security.tool_guard.approval import ApprovalDecision,ApprovalScope

ROOT=Path(__file__).resolve().parents[1]/'plugins/agent-line'
pkg=types.ModuleType('native_approval_fixture');pkg.__path__=[str(ROOT)];sys.modules[pkg.__name__]=pkg

def load(name):
    spec=importlib.util.spec_from_file_location(pkg.__name__+'.'+name,ROOT/(name+'.py'))
    mod=importlib.util.module_from_spec(spec);sys.modules[spec.name]=mod;spec.loader.exec_module(mod);return mod

bridge_mod=load('execution_bridge');mod=load('execution_approvals')

class NativeApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        db=self.root/'ledger.sqlite';bridge_mod.ReceiptLedger.initialize(db)
        self.bridge=bridge_mod.ExecutionBridge.__new__(bridge_mod.ExecutionBridge)
        self.bridge.ledger=bridge_mod.ReceiptLedger(db);self.bridge.workspace=self.root
        self.bridge.roles={'parent'};self.bridge.target='codex'
        self.bridge.ledger.bind('task','parent','agent-line-task','qwenpaw-role-v1:'+'a'*64)
        self.bridge.ledger.claim('task',{'roleSessionId':'agent-line-task','targetAgentId':'codex','workspace':str(self.root)})
        self.identity=dict(upstreamTaskId='native-task',codexThreadId='thread',codexTurnId='turn')
        self.bridge.ledger.patch('task',identity=self.identity,status='accepted')
        async def observe(task):return self.bridge.ledger.get(task)[1]
        self.bridge.observe=observe
        self.svc=ApprovalService();self.patch=patch.object(mod,'get_approval_service',return_value=self.svc)
        self.patch.start();self.addCleanup(self.patch.stop)
        self.api=mod.ExecutionApprovals(self.bridge)
        self.proof=dict(schemaVersion=1,codexThreadId='thread',codexTurnId='turn',owner='b'*32,containerId='c'*64,
            approvals=[dict(requestId=7,method='item/commandExecution/requestApproval',threadId='thread',turnId='turn',itemId='item')])
        self.api._control=AsyncMock(return_value=self.proof)
        self.pending=await self.svc.create_pending_summary(session_id='agent-line-task',root_session_id='agent-line-task',
            owner_agent_id='codex',agent_id='codex',user_id='service',channel='console',
            summary=ApprovalRequestSummary(source_type='codex',name='Codex command',severity='high',result_summary='bounded command summary',
                payload=dict(provider='codex',provider_item_id='item',command='secret-command')))
        self.body=mod.DecisionRequest(decisionId='decision',decision='approve',decidedBy='reviewer',expectedIdentity=self.identity)

    async def decide(self):return await self.api.decide('task',self.pending.request_id,self.body)

    async def test_approve_exact_once_and_list_history(self):
        listing=await self.api.list('task');self.assertTrue(listing['approvals'][0]['bound'])
        self.assertNotIn('secret-command',json.dumps(listing))
        with patch.object(self.svc,'resolve_request',wraps=self.svc.resolve_request) as resolve:
            result=await self.decide();replay=await self.decide()
            self.assertEqual(result,replay);self.assertEqual(resolve.await_count,1)
            self.assertEqual(resolve.call_args.kwargs['scope'],ApprovalScope.EXACT)
            self.assertTrue(resolve.call_args.kwargs['actor'].is_admin)
            self.assertEqual(resolve.call_args.kwargs['actor'].user_id,'reviewer')
        self.assertEqual(self.pending.future.result(),ApprovalDecision.APPROVED)
        self.assertEqual(result['deliveryStatus'],'accepted')
        self.assertEqual((await self.api.list('task'))['approvals'],[result])

    async def test_reject(self):
        self.body.decision='reject';await self.decide()
        self.assertEqual(self.pending.future.result(),ApprovalDecision.DENIED)

    async def test_wrong_binding_variants_never_resolve(self):
        cases=[('owner_agent_id','other'),('agent_id','other'),('session_id','other'),('root_session_id','other')]
        for key,value in cases:
            old=getattr(self.pending,key);setattr(self.pending,key,value)
            with self.assertRaises(HTTPException):await self.decide()
            setattr(self.pending,key,old)
        for key,value in [('provider','other'),('source_type','other'),('provider_item_id','missing')]:
            old=self.pending.extra[key];self.pending.extra[key]=value
            with self.assertRaises(HTTPException):await self.decide()
            self.pending.extra[key]=old
        self.assertFalse(self.pending.future.done())

    async def test_ambiguous_item_and_wrong_turn(self):
        original=list(self.proof['approvals']);self.proof['approvals']*=2
        with self.assertRaises(HTTPException):await self.decide()
        self.proof['approvals']=original;original[0]['turnId']='other'
        with self.assertRaises(HTTPException):await self.decide()
        self.assertFalse(self.pending.future.done())

    async def test_expired_and_at_resolve_time_edge(self):
        self.pending.created_at=time.time()-1000
        with self.assertRaises(HTTPException):await self.decide()
        self.pending.created_at=time.time()
        original=self.svc.resolve_request
        async def expire(*args,**kwargs):
            self.pending.created_at=time.time()-1000
            return await original(*args,**kwargs)
        with patch.object(self.svc,'resolve_request',side_effect=expire):result=await self.decide()
        self.assertEqual(result['deliveryStatus'],'unavailable')
        self.assertEqual(self.pending.future.result(),ApprovalDecision.TIMEOUT)

    async def test_concurrent_requests_only_one_resolve(self):
        with patch.object(self.svc,'resolve_request',wraps=self.svc.resolve_request) as resolve:
            results=await asyncio.gather(self.decide(),self.decide(),return_exceptions=True)
        self.assertEqual(resolve.await_count,1)
        self.assertTrue(any(isinstance(x,dict) and x['deliveryStatus']=='accepted' for x in results))

    async def test_response_lost_never_resubmits_and_conflicting_actor_rejected(self):
        original=self.svc.resolve_request
        async def lost(*args,**kwargs):await original(*args,**kwargs);raise OSError('sensitive detail')
        with patch.object(self.svc,'resolve_request',side_effect=lost) as resolve:
            result=await self.decide();self.assertEqual(result['deliveryStatus'],'uncertain')
            self.assertEqual(await self.decide(),result);self.assertEqual(resolve.await_count,1)
        self.body.decidedBy='other'
        with self.assertRaises(HTTPException):await self.decide()
        self.assertNotIn('sensitive detail',json.dumps(result))

    async def test_process_cancellation_leaves_intent_and_reopen_does_not_resolve(self):
        with patch.object(self.svc,'resolve_request',side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):await self.decide()
        self.bridge.ledger=bridge_mod.ReceiptLedger(self.bridge.ledger.path)
        api=mod.ExecutionApprovals(self.bridge)
        with patch.object(self.svc,'resolve_request',side_effect=AssertionError('must not repeat')):
            result=await api.decide('task',self.pending.request_id,self.body)
        self.assertEqual(result['deliveryStatus'],'intent')

    async def test_cancel_terminal_binding_mismatch_refuse(self):
        for changes in [dict(cancellationRequested=True),dict(terminalConfirmed=True),dict(status='succeeded')]:
            original=self.bridge.ledger.get('task')[1];self.bridge.ledger.patch('task',**changes)
            with self.assertRaises(HTTPException):await self.decide()
            self.bridge.ledger.patch('task',**original)
        self.bridge.ledger.revoke_delegation('task')
        with self.assertRaises(HTTPException):await self.decide()
        self.assertFalse(self.pending.future.done())

    async def test_unknown_after_restart_not_invented_approved(self):
        with patch.object(mod,'get_approval_service',return_value=ApprovalService()):
            self.assertEqual((await self.api.list('task'))['approvals'],[])
            with self.assertRaises(HTTPException):await self.decide()

    async def test_extra_body_and_incomplete_identity_refused(self):
        with self.assertRaises(ValidationError):mod.DecisionRequest(**{**self.body.model_dump(),'session_id':'arbitrary'})
        with self.assertRaises(ValidationError):mod.DecisionRequest(**{**self.body.model_dump(),'expectedIdentity':{'upstreamTaskId':'native'}})

    async def test_second_control_read_changed_blocks_decision(self):
        changed={**self.proof,'approvals':[]}
        self.api._control=AsyncMock(side_effect=[self.proof,changed])
        with self.assertRaises(HTTPException):await self.decide()
        self.assertFalse(self.pending.future.done())
        self.assertNotIn('approvalDecisions',self.bridge.ledger.get('task')[1])

    async def test_wrong_expected_identity_and_original_role_binding(self):
        self.body.expectedIdentity.codexTurnId='wrong'
        with self.assertRaises(HTTPException):await self.decide()
        self.body.expectedIdentity.codexTurnId='turn'
        with self.bridge.ledger.connection() as db:
            db.execute("UPDATE role_bindings SET session_id='other' WHERE task_id='task'")
        with self.assertRaises(HTTPException):await self.decide()
        self.assertFalse(self.pending.future.done())

    async def test_inflight_intent_replay_is_nonblocking_and_does_not_resolve_again(self):
        entered=asyncio.Event();release=asyncio.Event();original=self.svc.resolve_request
        async def slow(*args,**kwargs):
            entered.set();await release.wait();return await original(*args,**kwargs)
        with patch.object(self.svc,'resolve_request',side_effect=slow) as resolve:
            first=asyncio.create_task(self.decide());await asyncio.wait_for(entered.wait(),2)
            replay=await self.decide();self.assertEqual(replay['deliveryStatus'],'intent')
            release.set();result=await first;self.assertEqual(result['deliveryStatus'],'accepted')
            self.assertEqual(resolve.await_count,1)

    async def test_routes_inherit_service_token_auth_and_strict_body(self):
        import httpx
        from fastapi import FastAPI
        self.bridge.token='service-secret'
        app=FastAPI();app.include_router(self.bridge.router(),prefix='/api/agent-line')
        path='/api/agent-line/executions/task/approvals/'+self.pending.request_id+'/decision'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://fixture') as client:
            response=await client.post(path,json=self.body.model_dump());self.assertEqual(response.status_code,403)
            response=await client.post(path,headers={'X-Agent-Line-Service-Token':'service-secret'},
                json={**self.body.model_dump(),'targetAgentId':'other'})
            self.assertEqual(response.status_code,422)
        self.assertFalse(self.pending.future.done())

    async def test_other_role_hidden_and_unbound_pending_not_approved(self):
        self.pending.agent_id='other'
        self.assertEqual((await self.api.list('task'))['approvals'],[])
        self.pending.agent_id='codex';self.proof['approvals']=[]
        row=(await self.api.list('task'))['approvals'][0]
        self.assertEqual(row['status'],'unbound');self.assertFalse(row['bound'])
        self.assertIsNone(row['decision'])

    async def test_finished_task_keeps_history_without_new_control_or_decision(self):
        result=await self.decide()
        self.bridge.ledger.patch('task',status='succeeded',terminalConfirmed=True)
        with patch.object(self.api,'_control',side_effect=AssertionError('terminal control')):
            self.assertEqual((await self.api.list('task'))['approvals'],[result])
            self.assertEqual(await self.decide(),result)

    async def test_container_owner_changed_between_observations_refused(self):
        self.api._control=AsyncMock(side_effect=[self.proof,{**self.proof,'owner':'d'*32}])
        with self.assertRaises(HTTPException):await self.decide()
        self.assertFalse(self.pending.future.done())
