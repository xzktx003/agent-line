"""Human-only approval credentials and HTTP boundary; no binding creation API."""
import math
import secrets
import sqlite3
import threading
import time
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from .approvals import TrustedPrincipal
from .store import AdmissionClosed, Conflict
from .run_control import RunControlClosed


class ApprovalAccess:
    def __init__(self, credentials, *, clock=time.time):
        self.clock = clock
        self._lock = threading.RLock()
        self._service_tokens = frozenset()
        self.replace_credentials(credentials)

    def replace_credentials(self, credentials):
        checked = dict(credentials)
        subjects = set()
        for token, principal in checked.items():
            if (not isinstance(token, str) or not token.strip()
                    or not isinstance(principal, TrustedPrincipal)
                    or not isinstance(principal.subject, str) or not principal.subject.strip()
                    or len(principal.subject) > 256 or principal.subject in subjects
                    or not isinstance(principal.project_scopes, frozenset)
                    or not isinstance(principal.capabilities, frozenset)
                    or any(not isinstance(x, str) or not x.strip() for x in principal.project_scopes | principal.capabilities)
                    or type(principal.expires_at) not in (int, float)
                    or not math.isfinite(principal.expires_at)):
                raise ValueError('Invalid independent approval credential registry')
            subjects.add(principal.subject)
        with self._lock:
            if any(secrets.compare_digest(h.encode(), s.encode())
                   for h in checked for s in self._service_tokens):
                raise ValueError('Human and service credentials must be distinct')
            self._credentials = checked

    def reject_service_tokens(self, tokens):
        with self._lock:
            if any(secrets.compare_digest(h.encode(), s.encode())
                   for h in self._credentials for s in tokens):
                raise ValueError('Human and service credentials must be distinct')
            self._service_tokens = frozenset(tokens)

    def revoke(self, token):
        with self._lock:
            self._credentials.pop(token, None)

    def authorize(self, principal):
        with self._lock:
            return (isinstance(principal, TrustedPrincipal)
                    and principal.expires_at > self.clock()
                    and 'approve' in principal.capabilities
                    and principal in self._credentials.values())

    def authenticate(self, authorization):
        scheme, _, token = (authorization or '').partition(' ')
        supplied = token.encode() if scheme.lower() == 'bearer' else b''
        principal = None
        with self._lock:
            for credential, candidate in self._credentials.items():
                if secrets.compare_digest(supplied, credential.encode()):
                    principal = candidate
        if principal is None:
            raise HTTPException(401, '需要有效的人工审批凭证', headers={'WWW-Authenticate': 'Bearer'})
        if not self.authorize(principal):
            raise HTTPException(403, '人工审批权限已失效或不足')
        return principal


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    decisionId: str = Field(min_length=1, max_length=256)
    decision: Literal['approve', 'reject']


class ApprovalAPI:
    def __init__(self, access, coordinator, *, discovery=None, publication_verifier=None):
        if coordinator.authorize != access.authorize:
            raise ValueError('Coordinator must use this live approval credential registry')
        self.access, self.coordinator = access, coordinator
        self.discovery = discovery
        self.publication_verifier = publication_verifier

    def _confirmation(self, binding):
        if self.discovery is None:
            return {'state': 'unavailable'}
        try:
            run = self.discovery.runs.get(binding.workflow_id, binding.run_id)
            if (run['projectId'] != binding.project_id or run['workflowId'] != binding.workflow_id
                    or run['runId'] != binding.run_id or run['revision'] != binding.revision
                    or binding.purpose != 'human_approval'):
                raise ValueError('Confirmation scope mismatch')
            allowed = run['approvalBlockIds'] if run.get('purpose') == 'external_task_result' else run['humanBlockIds']
            if binding.block_id not in allowed:
                raise ValueError('Confirmation node mismatch')
            if 'approvalQuestions' not in run:
                return {'state': 'unconfigured'}
            from .approval_discovery import validate_approval_questions
            questions = validate_approval_questions(run['approvalQuestions'], allowed)
            text = questions.get(binding.block_id)
            return {'state': 'configured', 'text': text} if text is not None else {'state': 'unconfigured'}
        except (KeyError, ValueError, TypeError, AttributeError, sqlite3.Error):
            return {'state': 'unknown'}

    def _public(self, record, *, include_confirmation=False):
        b = record['binding']
        return {'approvalId': b.approval_id, 'projectId': b.project_id,
                'workflowId': b.workflow_id, 'runId': b.run_id, 'blockId': b.block_id,
                'contextId': b.context_id, 'revision': b.revision,
                'activationId': b.activation_id, 'attemptId': b.attempt_id,
                'purpose': b.purpose, 'expiresAt': b.expires_at,
                'status': 'decision_recorded' if record['decision'] else (
                    'expired' if b.expires_at <= self.coordinator.clock() else 'waiting'),
                'decision': record['decision'], 'decidedAt': record['decidedAt'],
                **({'confirmation': self._confirmation(b)} if include_confirmation else {})}

    def router(self):
        router = APIRouter(prefix='/extension/v1/approvals')

        def principal(authorization: str | None = Header(default=None)):
            return self.access.authenticate(authorization)

        def accessible(approval_id, actor):
            try:
                record = self.coordinator.ledger.get(approval_id)
            except KeyError:
                raise HTTPException(404, '审批不存在') from None
            if record['binding'].project_id not in actor.project_scopes:
                raise HTTPException(404, '审批不存在')
            return record

        def perform(approval_id, actor, operation):
            accessible(approval_id, actor)
            try:
                result = operation()
                # Do not expose raw upstream responses or journal request/receipt.
                public = self._public(self.coordinator.ledger.get(approval_id), include_confirmation=True)
                public.update({key: result[key] for key in (
                    'status', 'decision', 'runStatus', 'boundPauseObserved', 'deliveryStatus'
                ) if key in result})
                if not self.access.authorize(actor):
                    raise PermissionError()
                return public
            except PermissionError:
                raise HTTPException(403, '人工审批权限或请求已失效') from None
            except AdmissionClosed:
                raise HTTPException(503, "维护或恢复核对中，审批决定尚未提交") from None
            except RunControlClosed:
                raise HTTPException(409, '此流程已暂停后继准入或申请停止，决定未提交；请查询原流程') from None
            except Conflict:
                raise HTTPException(409, '审批决定或恢复记录冲突') from None
            except ValueError:
                raise HTTPException(422, '审批请求格式无效') from None
            except Exception:
                raise HTTPException(503, '审批状态暂时无法确认，请查询原审批，不要重新投递') from None

        @router.get('')
        def listing(after: str = Query(default='', max_length=256),
                    limit: int = Query(default=50, ge=1, le=500),
                    actor: TrustedPrincipal = Depends(principal)):
            try:
                page = self.coordinator.ledger.list_approvals(actor.project_scopes, after=after, limit=limit)
                if not self.access.authorize(actor):
                    raise HTTPException(403, '人工审批权限已失效')
                return {'approvals': [self._public(record) for record in page['approvals']],
                        'nextCursor': page['nextCursor']}
            except HTTPException:
                raise
            except Exception:
                raise HTTPException(503, '审批列表暂时不可用') from None

        @router.get('/{approval_id}')
        def detail(approval_id: str, actor: TrustedPrincipal = Depends(principal)):
            return perform(approval_id, actor, lambda: self.coordinator.reconcile(approval_id, principal=actor))

        @router.post('/{approval_id}/decision')
        def decide(approval_id: str, body: DecisionRequest, actor: TrustedPrincipal = Depends(principal)):
            return perform(approval_id, actor, lambda: self.coordinator.decide(
                approval_id, decision_id=body.decisionId, decision=body.decision, principal=actor))

        return router
