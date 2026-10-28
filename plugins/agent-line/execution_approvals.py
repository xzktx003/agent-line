"""Task-scoped adapter to QwenPaw's existing native approval service."""
import asyncio
import json
import re
import sys
import time
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from qwenpaw.app.approvals import ApprovalActor, get_approval_service
from qwenpaw.security.tool_guard.approval import ApprovalDecision, ApprovalScope

METHODS=frozenset({'item/commandExecution/requestApproval','item/fileChange/requestApproval','item/permissions/requestApproval'})

class NativeIdentity(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    upstreamTaskId:str=Field(min_length=1,max_length=256)
    codexThreadId:str=Field(min_length=1,max_length=256)
    codexTurnId:str=Field(min_length=1,max_length=256)

class DecisionRequest(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    decisionId:str=Field(min_length=1,max_length=160,pattern=r'^[A-Za-z0-9_-]+$')
    decision:Literal['approve','reject']
    decidedBy:str=Field(min_length=1,max_length=256)
    expectedIdentity:NativeIdentity

class ExecutionApprovals:
    def __init__(self,bridge):
        self.bridge=bridge

    def _binding(self,task_id):
        payload,record=self.bridge.ledger.get(task_id)
        with self.bridge.ledger.connection() as db:
            row=db.execute('SELECT role_id,session_id FROM role_bindings WHERE task_id=?',(task_id,)).fetchone()
            revoked=db.execute('SELECT 1 FROM revoked_delegations WHERE task_id=?',(task_id,)).fetchone()
        if (row is None or row[0] not in self.bridge.roles or row[1]!='agent-line-'+task_id
                or payload.get('roleSessionId')!=row[1] or payload.get('targetAgentId')!=self.bridge.target
                or payload.get('workspace')!=str(self.bridge.workspace)):
            raise HTTPException(409,'执行审批任务绑定不符')
        return payload,record,bool(revoked)

    async def _active(self,task_id,expected=None):
        self._binding(task_id)
        record=await self.bridge.observe(task_id)
        payload,current,revoked=self._binding(task_id)
        identity=current.get('identity',{})
        try:NativeIdentity.model_validate(identity)
        except ValueError:raise HTTPException(409,'执行审批缺少完整原生身份') from None
        if (record.get('identity')!=identity or current.get('status')!='accepted'
                or current.get('terminalConfirmed') is not False or current.get('cancellationRequested') is True
                or current.get('executorInterrupted') is True or revoked
                or (expected is not None and expected!=identity)):
            raise HTTPException(409,'执行不再允许新的权限决定')
        return payload,current

    async def _control(self,identity):
        process=None
        control=getattr(self.bridge,'container_control',None)
        if not control:return None
        try:
            process=await asyncio.create_subprocess_exec(sys.executable,str(control),'approvals',
                '--thread',identity['codexThreadId'],'--turn',identity['codexTurnId'],
                '--workspace',str(self.bridge.workspace),stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
            async def read():
                parts=[];size=0
                while True:
                    chunk=await process.stdout.read(min(65536,262145-size))
                    if not chunk:break
                    parts.append(chunk);size+=len(chunk)
                    if size>262144:raise ValueError('Control output exceeds limit')
                data=b''.join(parts)
                await process.wait()
                if process.returncode!=0:return None
                return json.loads(data)
            return await asyncio.wait_for(read(),20)
        except (OSError,ValueError,asyncio.TimeoutError):
            return None
        finally:
            if process is not None and process.returncode is None:
                process.kill();await process.wait()

    def _item(self,pending,payload,identity,proof):
        if (pending.session_id!=payload['roleSessionId'] or pending.root_session_id!=payload['roleSessionId']
                or pending.owner_agent_id!=self.bridge.target or pending.agent_id!=self.bridge.target
                or pending.extra.get('source_type')!='codex' or pending.extra.get('provider')!='codex'
                or not isinstance(pending.extra.get('provider_item_id'),str) or not pending.extra['provider_item_id']
                or pending.future.done() or pending.status!='pending'):
            return None
        if (not isinstance(proof,dict) or type(proof.get('schemaVersion')) is not int or proof['schemaVersion']!=1
                or proof.get('codexThreadId')!=identity['codexThreadId'] or proof.get('codexTurnId')!=identity['codexTurnId']
                or not isinstance(proof.get('owner'),str) or re.fullmatch('[0-9a-f]{32}',proof['owner']) is None
                or not isinstance(proof.get('containerId'),str) or re.fullmatch('[0-9a-f]{64}',proof['containerId']) is None
                or not isinstance(proof.get('approvals'),list)):
            return None
        matches=[x for x in proof['approvals'] if isinstance(x,dict) and x.get('itemId')==pending.extra['provider_item_id']]
        if len(matches)!=1:return None
        item=matches[0]
        if (set(item)!={'requestId','method','threadId','turnId','itemId'}
                or item['method'] not in METHODS or item['threadId']!=identity['codexThreadId']
                or item['turnId']!=identity['codexTurnId'] or type(item['requestId']) not in (str,int)
                or isinstance(item['requestId'],str) and not item['requestId']):return None
        return item

    def _view(self,task_id,payload,identity,pending=None,history=None,proof=None):
        if history is not None:
            view=dict(history['view'])
            view.update(status='decision_recorded',decision=history['decision'],deliveryStatus=history['deliveryStatus'])
            return view
        item=self._item(pending,payload,identity,proof)
        expired=time.time()>=pending.created_at+pending.timeout_seconds
        return dict(requestId=pending.request_id,purpose='execution_permission',taskId=task_id,
            roleSessionId=payload['roleSessionId'],targetAgentId=self.bridge.target,identity=identity,
            providerItemId=pending.extra.get('provider_item_id') if isinstance(pending.extra.get('provider_item_id'),str) else None,
            method=item['method'] if item else None,createdAt=pending.created_at,expiresAt=pending.created_at+pending.timeout_seconds,
            summary=str(pending.result_summary)[:4000],severity=str(pending.severity)[:32],
            status='expired' if expired else 'waiting' if item else 'unbound',bound=item is not None and not expired,
            decision=None,deliveryStatus='not_submitted')

    async def list(self,task_id):
        payload,record,_=self._binding(task_id)
        identity=record.get('identity',{})
        active=True
        try:payload,record=await self._active(task_id)
        except HTTPException:active=False
        identity=record.get('identity',{})
        proof=await self._control(identity) if active else None
        pending=await get_approval_service().get_pending_by_root_session(payload['roleSessionId'])
        history=record.get('approvalDecisions',{})
        rows=[]
        for item in pending:
            # Do not expose another role's request from a colliding session.
            if item.agent_id!=self.bridge.target or item.owner_agent_id!=self.bridge.target:continue
            view=self._view(task_id,payload,identity,pending=item,history=history.get(item.request_id),proof=proof)
            if not active and item.request_id not in history:view.update(bound=False,status='unbound')
            rows.append(view)
        seen={x['requestId'] for x in rows}
        rows.extend(self._view(task_id,payload,identity,history=h) for key,h in history.items() if key not in seen)
        return dict(taskId=task_id,identity=identity,approvals=rows)

    def _history(self,task_id,request_id,decision):
        _,record,_=self._binding(task_id)
        history=record.get('approvalDecisions',{}).get(request_id)
        if history and history['intent']!=decision:raise HTTPException(409,'原审批决定不可变')
        return history

    def _claim(self,task_id,request_id,intent,view,item):
        with self.bridge.ledger.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT record FROM receipts WHERE task_id=?',(task_id,)).fetchone()
            if row is None:raise HTTPException(404,'执行登记缺失')
            record=json.loads(row[0]);mapping=record.setdefault('approvalDecisions',{})
            old=mapping.get(request_id)
            if old:
                if old['intent']!=intent:raise HTTPException(409,'原审批决定不可变')
                return old,False
            if (record.get('identity')!=intent['expectedIdentity'] or record.get('status')!='accepted'
                    or record.get('terminalConfirmed') is not False or record.get('cancellationRequested') or record.get('executorInterrupted')
                    or db.execute('SELECT 1 FROM revoked_delegations WHERE task_id=?',(task_id,)).fetchone()):
                raise HTTPException(409,'执行已取消或结束')
            value=dict(intent=intent,view=view,nativeBinding=item,
                decision={k:intent[k] for k in ('decisionId','decision','decidedBy')},deliveryStatus='intent')
            mapping[request_id]=value
            db.execute('UPDATE receipts SET record=? WHERE task_id=?',(json.dumps(record),task_id))
            return value,True

    def _delivered(self,task_id,request_id,status):
        with self.bridge.ledger.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT record FROM receipts WHERE task_id=?',(task_id,)).fetchone()
            record=json.loads(row[0]);value=record['approvalDecisions'][request_id];value['deliveryStatus']=status
            db.execute('UPDATE receipts SET record=? WHERE task_id=?',(json.dumps(record),task_id))
            return value

    async def decide(self,task_id,request_id,body):
        intent=body.model_dump();expected=intent['expectedIdentity']
        payload,record,_=self._binding(task_id)
        if body.decision=='approve' and payload.get('executionControls',{}).get('sandbox')=='read-only':
            raise HTTPException(409,'只读执行不允许批准扩大权限；可以拒绝或取消任务')
        old=self._history(task_id,request_id,intent)
        if old:return self._view(task_id,payload,expected,history=old)
        payload,record=await self._active(task_id,expected)
        svc=get_approval_service();pending=await svc.get_request(request_id)
        if pending is None:
            old=self._history(task_id,request_id,intent)
            if old:return self._view(task_id,payload,expected,history=old)
            raise HTTPException(409,'原生审批未观察到；不得推断已经批准')
        proof=await self._control(expected)
        item=self._item(pending,payload,expected,proof)
        if item is None or time.time()>=pending.created_at+pending.timeout_seconds:
            raise HTTPException(409,'原生审批身份未绑定或已过期')
        view=self._view(task_id,payload,expected,pending=pending,proof=proof)
        # Recheck task and service after asynchronous control observation.
        await self._active(task_id,expected)
        fresh_proof=await self._control(expected)
        if (self._item(pending,payload,expected,fresh_proof)!=item
                or any(fresh_proof.get(k)!=proof.get(k) for k in ('owner','containerId'))):
            old=self._history(task_id,request_id,intent)
            if old:return self._view(task_id,payload,expected,history=old)
            raise HTTPException(409,'原生审批关联证据已改变')
        if await svc.get_request(request_id) is not pending or pending.future.done():
            raise HTTPException(409,'原审批状态已改变')
        value,fresh=self._claim(task_id,request_id,intent,view,
            {**item,'owner':proof['owner'],'containerId':proof['containerId']})
        if not fresh:return self._view(task_id,payload,expected,history=value)
        try:
            result=await svc.resolve_request(request_id,
                ApprovalDecision.APPROVED if body.decision=='approve' else ApprovalDecision.DENIED,
                scope=ApprovalScope.EXACT,actor=ApprovalActor(session_id=pending.session_id,
                root_session_id=pending.root_session_id,user_id=body.decidedBy,channel='console',
                agent_id=self.bridge.target,is_admin=True))
            status='accepted' if result is not None else 'unavailable'
        except Exception:status='uncertain'
        value=self._delivered(task_id,request_id,status)
        return self._view(task_id,payload,expected,history=value)
