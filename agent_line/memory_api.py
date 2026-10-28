"""Authenticated human review of task-derived memory; QwenPaw owns active content."""
import copy
import hashlib
import json
import re
from contextlib import nullcontext

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from .adapters.qwenpaw_memory import MemoryApproval, content_hash
from .adapters.sim import segment
from .memory_review import (MemoryReviewLedger, require_completed_content,
                           require_unexpired_content, content_deadline)
from .skill_candidate import SkillCandidateRequest, build_skill_candidate
from .store import AdmissionClosed, Conflict, TERMINAL, canonical


class MemoryReviewRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    decisionId: str = Field(min_length=1, max_length=160)
    content: str = Field(min_length=1, max_length=128000)
    expectedEvidenceSha256: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')


class MemoryDeleteRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    decisionId: str = Field(min_length=1, max_length=160)
    expectedContentSha256: str = Field(pattern=r'^[a-f0-9]{64}$')


class MemoryReconcileRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class MemoryRetryRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expectedVersion: int = Field(ge=1)


class MemoryAPI:
    def __init__(self, access, coordinators, reviews, tasks, *, bindings, admission=None, deletions=None):
        if set(coordinators) != set(bindings):
            raise ValueError('Memory coordinators must match trusted bindings')
        self.access, self.coordinators, self.reviews, self.tasks = access, coordinators, reviews, tasks
        self.bindings = {key: dict(value) for key, value in bindings.items()}
        self.admission = admission or nullcontext
        self.deletions = dict(deletions or {})
        if not set(self.deletions) <= set(bindings):
            raise ValueError('Deletion coordinators require trusted memory bindings')
        for key, deletion in self.deletions.items():
            if (deletion.role != coordinators[key].role
                    or deletion.store.path != coordinators[key].store.path):
                raise ValueError('Deletion and write must share role and authoritative ledger')
        owners = {}
        for key, binding in self.bindings.items():
            for value in (key, binding['projectId'], binding['roleBindingId'], binding['roleAgentId']):
                segment(value)
            if coordinators[key].role != binding['roleAgentId']:
                raise ValueError('Memory coordinator role mismatch')
            owner = owners.setdefault(binding['roleAgentId'], binding['projectId'])
            if owner != binding['projectId']:
                raise ValueError('A memory role cannot belong to multiple projects')
        self.by_role = {}
        for key, binding in self.bindings.items():
            if binding['roleAgentId'] in self.by_role:
                raise ValueError('A memory role requires one canonical binding')
            self.by_role[binding['roleAgentId']] = key

    def close(self):
        seen = set()
        for coordinator in self.coordinators.values():
            if id(coordinator.client) not in seen:
                coordinator.client.close()
                seen.add(id(coordinator.client))

    def _binding(self, binding_id, actor):
        binding = self.bindings.get(binding_id)
        if binding is None or binding['projectId'] not in actor.project_scopes:
            raise HTTPException(404, '记忆角色不存在')
        return binding

    def _task(self, binding, task_id):
        try:
            segment(task_id)
            task = self.tasks.get(task_id)
        except (KeyError, ValueError):
            raise HTTPException(404, '来源任务不存在') from None
        payload = task['payload']
        if any(payload.get(key) != binding[key] for key in ('projectId', 'roleBindingId', 'roleAgentId')):
            raise HTTPException(404, '来源任务不存在')
        return task

    @staticmethod
    def _source(task):
        # Freeze the reviewed evidence; memory-status updates are independent task events.
        return {'workflow': task['payload']['workflow'],
                'executionStatus': task['executionStatus'],
                'verificationStatus': task['verificationStatus'],
                'evidenceSha256': hashlib.sha256(canonical(task['evidence']).encode()).hexdigest()}

    def _public(self, binding_id, record, *, detail=False):
        coordinator = self.coordinators[binding_id]
        try:
            commit = coordinator.store.get(record['role_id'], record['task_id'])
        except KeyError:
            commit = {'status': 'review_recorded', 'reason': 'approved_content_not_registered'}
        if (commit.get('content_hash') is not None
                and (commit['content_hash'] != record['content_hash']
                     or commit.get('decision_id') != record['decision_id'])):
            commit = {'status': 'needs_review', 'reason': 'existing_commit_does_not_match_review'}
        result = {'bindingId': binding_id, 'projectId': record['project_id'],
                  'roleAgentId': record['role_id'], 'taskId': record['task_id'],
                  'actor': record['actor'], 'decisionId': record['decision_id'],
                  'contentSha256': record['content_hash'], 'source': record['source'],
                  'createdAt': record['created_at'], 'status': commit['status'],
                  'reason': commit['reason'], 'commitVersion': commit.get('version')}
        if coordinator.store.schema_version >= 2:
            try:
                result['deletion'] = coordinator.store.get_deletion(record['role_id'], record['task_id'])
            except KeyError:
                pass
        if detail:
            result['content'] = commit.get('content')
            result['candidateDeadline'] = content_deadline(commit.get('content'))
            with coordinator.store.connect() as db:
                rows = db.execute(
                    'SELECT seq,kind,created_at FROM memory_events WHERE role_id=? AND task_id=? ORDER BY seq',
                    (record['role_id'], record['task_id'])).fetchall()
            result['events'] = [{'seq': row['seq'], 'kind': row['kind'],
                                 'createdAt': row['created_at']} for row in rows]
        return result

    def router(self):
        router = APIRouter(prefix='/extension/v1/memories')

        def principal(authorization: str | None = Header(default=None)):
            actor = self.access.authenticate(authorization)
            if 'review_memory' not in actor.capabilities:
                raise HTTPException(403, '缺少记忆审核权限')
            return actor

        def still_authorized(actor):
            if not self.access.authorize(actor):
                raise HTTPException(403, '记忆审核权限已失效')

        def validate_submission(task, record, row):
            source = record['source']
            # Preserve the original review snapshot when verification later
            # adds task evidence. Retry must use precisely that same identity.
            if (not isinstance(source, dict) or source.get('workflow') != task['payload']['workflow']
                    or source.get('executionStatus') not in TERMINAL
                    or source.get('verificationStatus') not in ('pending', 'passed', 'failed', 'canceled')
                    or not isinstance(source.get('evidenceSha256'), str)
                    or re.fullmatch(r'[0-9a-f]{64}', source['evidenceSha256']) is None
                    or row['content_hash'] != record['content_hash']
                    or row['decision_id'] != record['decision_id']):
                raise Conflict('Stored memory submission does not match its original review')
            if row['status'] in ('approved', 'intent', 'pending', 'committed', 'failed'):
                try:
                    matches = content_hash(row['content']) == record['content_hash']
                except ValueError:
                    matches = False
                if not matches:
                    raise Conflict('Stored approved content does not match its original review')

        @router.get('')
        def listing(after: str | None = Query(default=None, max_length=1024),
                    limit: int = Query(default=50, ge=1, le=500), actor=Depends(principal)):
            try:
                page = self.reviews.list(actor.project_scopes, after=after, limit=limit)
                result = []
                for record in page['reviews']:
                    binding_id = self.by_role.get(record['role_id'])
                    if binding_id is not None and self.bindings[binding_id]['projectId'] == record['project_id']:
                        result.append(self._public(binding_id, record))
                still_authorized(actor)
                return {'memories': result, 'nextCursor': page['next_cursor']}
            except ValueError:
                raise HTTPException(422, '记忆分页参数无效') from None

        @router.get('/bindings')
        def binding_catalog(actor=Depends(principal)):
            result = [{'bindingId': key, **{name: binding[name] for name in
                       ('projectId', 'roleBindingId', 'roleAgentId')},
                       'candidateWriteDeadline': 'utc-header-v1',
                       'memoryDeletion': {'configured': key in self.deletions,
                           'canRequest': key in self.deletions and 'delete_memory' in actor.capabilities}}
                      for key, binding in sorted(self.bindings.items())
                      if binding['projectId'] in actor.project_scopes]
            still_authorized(actor)
            return {'bindings': result}

        @router.get('/sources')
        def sources(bindingId: str = Query(min_length=1, max_length=160),
                    after: str = Query(default='', max_length=128),
                    limit: int = Query(default=20, ge=1, le=100), actor=Depends(principal)):
            binding = self._binding(bindingId, actor)
            page = self.tasks.list_tasks(frozenset({binding['projectId']}), after=after, limit=limit)
            rows = []
            for task in page['tasks']:
                payload = task['payload']
                if (payload.get('roleBindingId') == binding['roleBindingId']
                        and payload.get('roleAgentId') == binding['roleAgentId']):
                    rows.append({'taskId': task['id'], 'executionStatus': task['executionStatus'],
                                 'verificationStatus': task['verificationStatus'],
                                 'resourcesReleased': task['resourcesReleased'],
                                 'memoryStatus': task['memoryStatus'],
                                 'workflowRunId': payload.get('workflow', {}).get('runId')})
            still_authorized(actor)
            return {'tasks': rows, 'nextCursor': page['nextCursor']}

        @router.get('/sources/{binding_id}/{task_id}')
        def source_detail(binding_id: str, task_id: str, actor=Depends(principal)):
            binding = self._binding(binding_id, actor)
            task = self._task(binding, task_id)
            evidence = copy.deepcopy(task['evidence'])
            for item in evidence:
                if isinstance(item, dict) and item.get('kind') == 'resource_exit':
                    receipt = item.get('receipt')
                    if isinstance(receipt, dict):
                        receipt.pop('workspace', None)
            output = json.dumps(task['output'], ensure_ascii=False, indent=2)
            encoded_evidence = json.dumps(evidence, ensure_ascii=False, indent=2)
            result = {'taskId': task['id'], 'projectId': binding['projectId'],
                      'roleAgentId': binding['roleAgentId'], 'workflow': task['payload']['workflow'],
                      'executionStatus': task['executionStatus'],
                      'verificationStatus': task['verificationStatus'],
                      'resourcesReleased': task['resourcesReleased'], 'memoryStatus': task['memoryStatus'],
                      'evidenceSha256': self._source(task)['evidenceSha256'],
                      'outputPreview': output[:16000], 'outputTruncated': len(output) > 16000,
                      'evidencePreview': encoded_evidence[:64000],
                      'evidenceTruncated': len(encoded_evidence) > 64000}
            still_authorized(actor)
            return result

        @router.get('/{binding_id}/{task_id}')
        def detail(binding_id: str, task_id: str, actor=Depends(principal)):
            binding = self._binding(binding_id, actor)
            try:
                record = self.reviews.get(binding['roleAgentId'], task_id)
                if record['project_id'] != binding['projectId']:
                    raise KeyError()
                result = self._public(binding_id, record, detail=True)
                still_authorized(actor)
                return result
            except KeyError:
                raise HTTPException(404, '记忆审核记录不存在') from None

        @router.post('/{binding_id}/{task_id}/reconcile')
        def reconcile(binding_id: str, task_id: str, body: MemoryReconcileRequest, actor=Depends(principal)):
            binding = self._binding(binding_id, actor)
            task = self._task(binding, task_id)
            coordinator = self.coordinators[binding_id]
            try:
                with self.admission():
                    still_authorized(actor)
                    with coordinator.store.memory_scope(binding['roleAgentId'], task_id):
                        record = self.reviews.get(binding['roleAgentId'], task_id)
                        if record['project_id'] != binding['projectId']:
                            raise KeyError()
                        row = coordinator.store.get(binding['roleAgentId'], task_id)
                        validate_submission(task, record, row)
                        still_authorized(actor)
                        coordinator.reconcile(task_id)
                        def latest_status():
                            current = coordinator.store.get(binding['roleAgentId'], task_id)
                            return (current['status'] if current['status'] in
                                    ('committed', 'failed', 'needs_review', 'deleting', 'deleted') else 'pending')
                        self.tasks.sync_memory_status(task_id, latest_status)
                        result = self._public(binding_id, record, detail=True)
                        still_authorized(actor)
                        return result
            except KeyError:
                raise HTTPException(404, '记忆审核或原提交记录不存在') from None
            except AdmissionClosed:
                raise HTTPException(503, '维护或恢复核对中，暂不核对记忆写回') from None
            except Conflict:
                raise HTTPException(409, '原记忆提交与审核身份、正文或来源不符') from None

        @router.post('/{binding_id}/{task_id}/retry')
        def retry(binding_id: str, task_id: str, body: MemoryRetryRequest, actor=Depends(principal)):
            binding = self._binding(binding_id, actor)
            task = self._task(binding, task_id)
            if task['executionStatus'] not in TERMINAL or task['resourcesReleased'] is not True:
                raise HTTPException(409, '来源任务尚未结束或执行资源尚未确认退出')
            coordinator = self.coordinators[binding_id]
            try:
                with self.admission():
                    still_authorized(actor)
                    with coordinator.store.memory_scope(binding['roleAgentId'], task_id):
                        record = self.reviews.get(binding['roleAgentId'], task_id)
                        if record['project_id'] != binding['projectId']:
                            raise KeyError()
                        row = coordinator.store.get(binding['roleAgentId'], task_id)
                        validate_submission(task, record, row)
                        still_authorized(actor)
                        coordinator.retry(task_id, expected_version=body.expectedVersion)

                        def latest_status():
                            current = coordinator.store.get(binding['roleAgentId'], task_id)
                            return (current['status'] if current['status'] in
                                    ('committed', 'failed', 'needs_review', 'deleting', 'deleted') else 'pending')

                        self.tasks.sync_memory_status(task_id, latest_status)
                        result = self._public(binding_id, record, detail=True)
                        still_authorized(actor)
                        return result
            except KeyError:
                raise HTTPException(404, '记忆审核或原提交记录不存在') from None
            except AdmissionClosed:
                raise HTTPException(503, '维护或恢复核对中，暂不重试记忆写回') from None
            except Conflict:
                raise HTTPException(409, '原记忆提交版本、审核身份、正文或来源已变化') from None
            except ValueError:
                raise HTTPException(422, '原审核正文未完成、写入期限已过或格式无效，未重新写入；请核对原记录') from None

        @router.post('/{binding_id}/{task_id}/review', status_code=202)
        def review(binding_id: str, task_id: str, body: MemoryReviewRequest,
                   actor=Depends(principal), authorization: str | None = Header(default=None)):
            binding = self._binding(binding_id, actor)
            task = self._task(binding, task_id)
            if task['executionStatus'] not in TERMINAL or task['resourcesReleased'] is not True:
                raise HTTPException(409, '来源任务尚未结束或执行资源尚未确认退出')
            coordinator = self.coordinators[binding_id]
            try:
                require_completed_content(body.content)
                credential = (authorization or '').partition(' ')[2]
                if credential and credential in body.content:
                    raise ValueError('memory_review_credential_in_content')
                segment(body.decisionId)
                digest = content_hash(body.content)
                with self.admission():
                    still_authorized(actor)
                    # Serialize source capture and reservation with verification
                    # and cancellation. Release before native memory network I/O.
                    with self.tasks.verification_scope(task_id):
                        task = self._task(binding, task_id)
                        if task['executionStatus'] not in TERMINAL or task['resourcesReleased'] is not True:
                            raise HTTPException(409, '来源任务尚未结束或执行资源尚未确认退出')
                        # Replays retain the original human-reviewed snapshot.
                        try:
                            old = self.reviews.get(binding['roleAgentId'], task_id)
                            source = old['source']
                        except KeyError:
                            require_unexpired_content(body.content)
                            source = self._source(task)
                        if (body.expectedEvidenceSha256 is not None
                                and (not isinstance(source, dict)
                                     or source.get('evidenceSha256') != body.expectedEvidenceSha256)):
                            raise Conflict('Reviewed source evidence changed')
                        record = self.reviews.reserve(
                            project_id=binding['projectId'], role_id=binding['roleAgentId'],
                            task_id=task_id, actor=actor.subject, decision_id=body.decisionId,
                            content_sha256=digest, source=source)
                    approved = coordinator.approve(
                        task_id, body.content, MemoryApproval(task_id, digest, body.decisionId))
                    if approved['status'] != 'needs_review':
                        coordinator.commit(task_id)
                    def latest_status():
                        row = coordinator.store.get(binding['roleAgentId'], task_id)
                        if (row['content_hash'] != digest or row['decision_id'] != body.decisionId):
                            return 'needs_review'
                        return row['status'] if row['status'] in ('committed', 'failed', 'needs_review', 'deleting', 'deleted') else 'pending'
                    self.tasks.sync_memory_status(task_id, latest_status)
                    result = self._public(binding_id, record, detail=True)
                    still_authorized(actor)
                    return result
            except AdmissionClosed:
                raise HTTPException(503, '维护或恢复核对中，暂不提交记忆审核') from None
            except Conflict:
                raise HTTPException(409, '来源证据已变化，或正文、操作者、审核标识与原记录冲突；请重新读取核对') from None
            except ValueError:
                raise HTTPException(422, '记忆正文、写入期限或审核标识无效；请核对期限、补全占位，并移除无效字符或当前审核凭证') from None

        @router.post('/{binding_id}/{task_id}/skill-candidate')
        def skill_candidate(binding_id: str, task_id: str, body: SkillCandidateRequest,
                            actor=Depends(principal)):
            binding = self._binding(binding_id, actor)
            task = self._task(binding, task_id)
            coordinator = self.coordinators[binding_id]
            try:
                with self.admission():
                    still_authorized(actor)
                    with coordinator.store.memory_scope(binding['roleAgentId'], task_id):
                        record = self.reviews.get(binding['roleAgentId'], task_id)
                        if record['project_id'] != binding['projectId']:
                            raise KeyError()
                        row = coordinator.store.get(binding['roleAgentId'], task_id)
                        validate_submission(task, record, row)
                        if (row['status'] != 'committed'
                                or record['content_hash'] != body.expectedContentSha256):
                            raise Conflict('Only matching committed experience can be exported')
                        if coordinator.store.schema_version >= 2:
                            try:
                                coordinator.store.get_deletion(binding['roleAgentId'], task_id)
                            except KeyError:
                                pass
                            else:
                                raise Conflict('Deleted or deletion-pending experience cannot become a skill')
                        result = build_skill_candidate(body, binding, record, row['content'])
                        still_authorized(actor)
                        return result
            except KeyError:
                raise HTTPException(404, '记忆审核或原提交记录不存在') from None
            except AdmissionClosed:
                raise HTTPException(503, '维护或恢复核对中，暂不导出技能候选') from None
            except Conflict:
                raise HTTPException(409, '经验来源、正文或写回状态已变化，或已有删除记录；请重新核对') from None
            except ValueError:
                raise HTTPException(422, '请补全技能说明、版本、适用条件、失效条件、操作边界和人工审核步骤') from None

        @router.post('/{binding_id}/{task_id}/skill-status')
        def skill_status(binding_id: str, task_id: str, body: SkillCandidateRequest,
                         actor=Depends(principal)):
            # Rebuild from the same approved source; no client-chosen digest can
            # turn another role's skill into evidence for this experience.
            candidate = skill_candidate(binding_id, task_id, body, actor)
            result = self.coordinators[binding_id].client.observe_skill_candidate(
                body.name, candidate['skillContentSha256'])
            # Native GET has no local transaction or deletion lock held. Check
            # source authorization/tombstones again before releasing its result.
            skill_candidate(binding_id, task_id, body, actor)
            return {'projectId': candidate['projectId'], 'taskId': task_id,
                    'sourceContentSha256': candidate['sourceContentSha256'],
                    **result}

        def deletion_binding(binding_id, task_id, actor):
            binding = self._binding(binding_id, actor)
            if binding_id not in self.deletions:
                raise HTTPException(503, '此角色尚未配置受控记忆删除')
            try:
                reviewed = self.reviews.get(binding['roleAgentId'], task_id)
            except KeyError:
                raise HTTPException(404, '记忆审核记录不存在') from None
            if reviewed['project_id'] != binding['projectId']:
                raise HTTPException(404, '记忆审核记录不存在')
            self._task(binding, task_id)
            return binding, reviewed, self.deletions[binding_id]

        def sync_deletion(binding, task_id):
            store = self.coordinators[self.by_role[binding['roleAgentId']]].store
            self.tasks.sync_memory_status(task_id, lambda: store.get(binding['roleAgentId'], task_id)['status'])

        @router.post('/{binding_id}/{task_id}/delete', status_code=202)
        def delete(binding_id: str, task_id: str, body: MemoryDeleteRequest, actor=Depends(principal)):
            if 'delete_memory' not in actor.capabilities:
                raise HTTPException(403, '缺少记忆删除权限')
            binding, reviewed, deletion = deletion_binding(binding_id, task_id, actor)
            if reviewed['content_hash'] != body.expectedContentSha256:
                raise HTTPException(409, '记忆正文版本已改变，请核对原记录')
            try:
                segment(body.decisionId)
                with self.admission():
                    still_authorized(actor)
                    deletion.request_delete(task_id, actor=actor.subject, decision_id=body.decisionId,
                                            expected_hash=body.expectedContentSha256)
                    sync_deletion(binding, task_id)
                    result = self._public(binding_id, reviewed, detail=True)
                    still_authorized(actor)
                    return result
            except AdmissionClosed:
                raise HTTPException(503, '维护或恢复核对中，暂不提交记忆删除') from None
            except Conflict:
                raise HTTPException(409, '删除决定或正文版本与原记录冲突') from None
            except ValueError:
                raise HTTPException(422, '记忆删除参数或版本不受支持') from None

        @router.get('/{binding_id}/{task_id}/deletion')
        def deletion_status(binding_id: str, task_id: str, actor=Depends(principal)):
            binding, reviewed, deletion = deletion_binding(binding_id, task_id, actor)
            try:
                with self.admission():
                    still_authorized(actor)
                    deletion.reconcile_delete(task_id)
                    sync_deletion(binding, task_id)
                    result = self._public(binding_id, reviewed, detail=True)
                    still_authorized(actor)
                    return result
            except KeyError:
                raise HTTPException(404, '尚无记忆删除请求') from None
            except AdmissionClosed:
                raise HTTPException(503, '维护或恢复核对中，暂不更新记忆删除状态') from None

        return router
