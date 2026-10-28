"""Project-scoped human responses to QwenPaw's native permission requests.

The official plugin owns request validity and durable delivery intents. This
boundary neither resolves approvals itself nor resumes a Sim workflow.
"""
from contextlib import nullcontext
import math
import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException
import httpx
from pydantic import BaseModel, ConfigDict, Field

from .adapters.sim import segment
from .store import AdmissionClosed, Conflict


class ExecutionIdentity(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    upstreamTaskId: str = Field(min_length=1, max_length=160)
    codexThreadId: str = Field(min_length=1, max_length=160)
    codexTurnId: str = Field(min_length=1, max_length=160)


class NativeDecisionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    decisionId: str = Field(min_length=1, max_length=160, pattern=r'^[A-Za-z0-9_-]+$')
    decision: Literal['approve', 'reject']
    expectedIdentity: ExecutionIdentity


class NativeApprovalAPI:
    def __init__(self, store, dispatcher, access, *, service_tokens=()):
        self.store, self.dispatcher, self.access = store, dispatcher, access
        self.service_tokens = tuple(service_tokens)

    def _authorized(self, actor):
        if not self.access.authorize(actor):
            raise HTTPException(403, '人工审批权限已失效')

    def _task(self, task_id, actor):
        try:
            task = self.store.get(task_id)
        except KeyError:
            raise HTTPException(404, '节点任务不存在') from None
        if task['payload']['projectId'] not in actor.project_scopes:
            raise HTTPException(404, '节点任务不存在')
        return task

    def _gateway(self, task):
        gateway = self.dispatcher._gateway(task)
        if gateway is None or any(not callable(getattr(type(gateway), name, None)) for name in (
                'execution_approvals', 'decide_execution_approval')):
            raise HTTPException(503, '此执行后端尚未接入原生审批查询')
        return gateway

    def _record(self, record, task, secret):
        if not isinstance(record, dict):
            raise ValueError('Invalid native approval record')
        request_id = segment(record.get('requestId'))
        if (record.get('taskId') != task['id'] or record.get('identity') != task['executionIdentity']
                or record.get('roleSessionId') != 'agent-line-' + task['id']
                or record.get('purpose') != 'execution_permission'
                or type(record.get('bound')) is not bool
                or record.get('status') not in ('waiting', 'expired', 'unbound', 'not_observed', 'decision_recorded')
                or record.get('deliveryStatus') not in ('not_submitted', 'intent', 'uncertain', 'accepted', 'unavailable')):
            raise ValueError('Native approval binding or state mismatch')
        segment(record.get('targetAgentId'))
        for key in ('createdAt', 'expiresAt'):
            value = record.get(key)
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value <= 0):
                raise ValueError('Invalid native approval time')
        if record['status'] == 'waiting' and record.get('expiresAt') is None:
            raise ValueError('Pending approval expiration required')
        method = record.get('method')
        methods = {'item/commandExecution/requestApproval', 'item/fileChange/requestApproval',
                   'item/permissions/requestApproval'}
        item = record.get('providerItemId')
        if ((method is not None and method not in methods)
                or (item is not None and (not isinstance(item, str) or not 1 <= len(item) <= 512))
                or record.get('severity') not in ('low', 'medium', 'high', 'critical', 'unknown')):
            raise ValueError('Invalid native approval display metadata')
        if record['bound'] and (method not in methods or not isinstance(item, str) or not 1 <= len(item) <= 512):
            raise ValueError('Native item identity required')
        decision = record.get('decision')
        if decision is not None:
            if (not isinstance(decision, dict) or set(decision) != {'decisionId', 'decision', 'decidedBy'}
                    or decision['decision'] not in ('approve', 'reject')
                    or not isinstance(decision['decidedBy'], str) or not 1 <= len(decision['decidedBy']) <= 256):
                raise ValueError('Invalid recorded native decision')
            segment(decision['decisionId'])
        summary = record.get('summary') or ''
        if not isinstance(summary, str) or len(summary) > 4000:
            raise ValueError('Invalid native approval summary')
        for token in (secret, *self.service_tokens):
            if token:
                summary = summary.replace(token, '[凭证已隐藏]')
        workflow = task['payload']['workflow']
        return {key: record.get(key) for key in (
            'purpose', 'taskId', 'roleSessionId', 'targetAgentId', 'identity', 'providerItemId',
            'method', 'createdAt', 'expiresAt', 'severity', 'status', 'bound', 'decision', 'deliveryStatus'
        )} | {'requestId': request_id, 'summary': summary, 'projectId': task['payload']['projectId'],
              'roleAgentId': task['payload']['roleAgentId'], 'workflowId': workflow.get('id'),
              'runId': workflow.get('runId'), 'nodeId': workflow.get('nodeId'),
              'nodeExecutionId': workflow.get('nodeExecutionId'), 'attemptId': workflow.get('attemptId'),
              'revision': workflow.get('revision')}

    def _listing(self, gateway, task, actor, secret):
        page = gateway.execution_approvals(task['id'])
        if (not isinstance(page, dict) or page.get('taskId') != task['id']
                or page.get('identity') != task['executionIdentity']
                or not isinstance(page.get('approvals'), list) or len(page['approvals']) > 256):
            raise ValueError('Invalid native approval envelope')
        records = [self._record(row, task, secret) for row in page['approvals']]
        if len({r['requestId'] for r in records}) != len(records):
            raise ValueError('Ambiguous native request identity')
        self._authorized(actor)
        for record in records:
            self._note_approval(lambda record=record: self.store.note_approval_created(
                task['id'], record['requestId'], status=record['status'],
                occurred_at=record.get('createdAt')))
            decision = record.get('decision')
            if isinstance(decision, dict):
                self._note_approval(lambda record=record, decision=decision: self.store.note_approval_decided(
                    task['id'], record['requestId'], decision.get('decisionId'),
                    decision.get('decision')))
        return {'taskId': task['id'], 'identity': task['executionIdentity'], 'approvals': records}

    @staticmethod
    def _note_approval(operation):
        try:
            operation()
        except (ValueError, OSError, sqlite3.Error):
            return

    @staticmethod
    def _perform(operation):
        try:
            return operation()
        except HTTPException:
            raise
        except AdmissionClosed:
            raise HTTPException(503, '维护或恢复核对中，审批决定尚未提交') from None
        except Conflict:
            raise HTTPException(409, '任务或原生审批状态冲突，请查询原记录') from None
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (404, 409):
                raise HTTPException(exc.response.status_code, '原生审批已失效或绑定不符，请查询原记录') from None
            raise HTTPException(503, '原生审批暂时无法核对，请保留原决定标识') from None
        except Exception:
            raise HTTPException(503, '原生审批状态未知，请查询原记录，不要重新投递') from None

    def router(self):
        router = APIRouter(prefix='/extension/v1/node-tasks/{task_id}/execution-approvals')

        def identity(authorization: str | None = Header(default=None)):
            actor = self.access.authenticate(authorization)
            return actor, (authorization or '').partition(' ')[2]

        @router.get('')
        def listing(task_id: str, authenticated=Depends(identity)):
            actor, secret = authenticated
            def query():
                task = self._task(task_id, actor)
                return self._listing(self._gateway(task), task, actor, secret)
            return self._perform(query)

        @router.post('/{request_id}/decision')
        def decide(task_id: str, request_id: str, body: NativeDecisionRequest,
                   authenticated=Depends(identity)):
            actor, secret = authenticated
            def submit():
                task = self._task(task_id, actor)
                if body.expectedIdentity.model_dump() != task['executionIdentity']:
                    raise Conflict('Stale native execution identity')
                gateway = self._gateway(task)
                page = self._listing(gateway, task, actor, secret)
                record = next((r for r in page['approvals'] if r['requestId'] == request_id), None)
                if record is None:
                    raise HTTPException(404, '原生审批未观察到；不能据此推断已批准')
                decision = {'decisionId': body.decisionId, 'decision': body.decision, 'decidedBy': actor.subject}
                if record['decision'] is not None:
                    if record['decision'] != decision:
                        raise Conflict('Native human decision is immutable')
                    return record  # Existing intent/unknown delivery never invokes POST again.
                workflow = task['payload']['workflow']
                control = self.dispatcher.run_control
                with self.store.workflow_admission(), (
                        control.admission(workflow['id'], workflow['runId']) if control else nullcontext()
                        ), self.store.verification_scope(task_id):
                    current = self._task(task_id, actor)
                    if (current['executionIdentity'] != task['executionIdentity']
                            or current['executionStatus'] != 'accepted' or current['cancellationRequested']
                            or record['bound'] is not True or record['status'] != 'waiting'
                            or record['expiresAt'] <= self.access.clock()):
                        raise Conflict('Native request is not currently actionable')
                    self._authorized(actor)
                    response = gateway.decide_execution_approval(task_id, request_id, {
                        **decision, 'expectedIdentity': current['executionIdentity']})
                    public = self._record(response, current, secret)
                    if public['requestId'] != request_id or public['decision'] != decision:
                        raise ValueError('Native decision receipt mismatch')
                    binding = None
                    if (decision['decision'] == 'reject' and public.get('deliveryStatus') == 'accepted'
                            and public.get('taskId') == current['id']
                            and public.get('identity') == current['executionIdentity']):
                        binding = {
                            'taskId': current['id'],
                            'executionIdentity': dict(current['executionIdentity']),
                            'deliveryStatus': 'accepted',
                        }
                    recorded = {}

                    def remember(binding=binding):
                        recorded['event'] = self.store.note_approval_decided(
                            task_id, request_id, decision['decisionId'], decision['decision'],
                            binding=binding)

                    self._note_approval(remember)
                    if binding is not None and recorded.get('event') is None:
                        self._note_approval(lambda: self.store.note_approval_decided(
                            task_id, request_id, decision['decisionId'], decision['decision']))
                    self._authorized(actor)
                    return public
            return self._perform(submit)

        return router
