"""Authenticated node-task boundary; execution remains a trusted adapter concern.

Creating this app neither initializes storage nor starts executors. Capability
confirmation must come from deployment checks, never from a request parameter.
"""

import copy
import asyncio
import hashlib
import json
import re
from contextlib import asynccontextmanager, nullcontext
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .adapters.sim import host_role_shared_across_projects
from .store import AdmissionClosed, Conflict, TaskStore, canonical
from .usage import task_usage
from .output_contract import inspect_output, supported_output_schema

Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Workflow(StrictModel):
    id: Identifier
    revision: Identifier
    runId: Identifier
    nodeId: Identifier
    nodeExecutionId: Identifier
    attemptId: Identifier


class Limits(StrictModel):
    maxExecutionSeconds: int = Field(default=3600, ge=1, le=86400)
    maxBusinessAttempts: int = Field(default=3, ge=1, le=100)


class NodeTaskRequest(StrictModel):
    schemaVersion: Literal['1.0']
    idempotencyKey: Identifier
    workflow: Workflow
    projectId: Identifier
    roleBindingId: Identifier
    input: dict
    memoryPolicyId: Identifier
    outputSchemaId: Identifier
    outputFields: dict | None = None
    limits: Limits = Field(default_factory=Limits)


class ReconcileRequest(StrictModel):
    expectedVersion: int = Field(ge=0, le=9_007_199_254_740_991)


class RecoveryReconcileRequest(ReconcileRequest):
    finding: str | None = None
    decision: str | None = None
    allowNewAttempt: bool | None = None


class MemoryRetrievalEvaluationRequest(ReconcileRequest):
    expectedSnapshotSha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    expectedText: str = Field(min_length=4, max_length=256)


@dataclass(frozen=True)
class ServiceConfig:
    # Tokens are service credentials supplied by deployment, not stored in tasks.
    tokens: dict[str, set[str]]
    projects: dict[str, dict]
    bindings: dict[str, dict]
    controlled_execution: Literal['unknown', 'unsupported', 'confirmed'] = 'unknown'
    execution_block_reason: str | None = None


def public_event(row):
    """Expose the event contract without inventing fields an older schema never stored."""
    if not isinstance(row, dict):
        row = dict(row)
    event = {
        'seq': row.get('seq'),
        'task_id': row.get('task_id'),
        'kind': row.get('kind'),
        'created_at': row.get('created_at'),
    }
    for key in ('event_id', 'run_id', 'occurred_at', 'received_at',
                'source_identity', 'task_version', 'category'):
        event[key] = row[key] if key in row else None
    payload = row['payload'] if 'payload' in row else None
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            parsed = None
        payload = parsed if isinstance(parsed, dict) else None
    elif not isinstance(payload, dict):
        payload = None
    event['payload'] = payload
    return event


def _clean_identity_token(value):
    return isinstance(value, str) and 0 < len(value) <= 256 and value.strip() == value and '\0' not in value


_UNWRITTEN_EXECUTION_IDENTITY = {
    'roleSessionId': '适配版本不把 roleSessionId 写入执行身份。可核对任务上的角色 ID；上游任务号只在已记录时有效。不能补造会话号。',
    'executorAgentId': '适配版本不把 executorAgentId 写入执行身份。可核对任务上的角色 ID；上游任务号只在已记录时有效。不能把显示名当作执行证明。',
}


def native_execution_identity_capability():
    """Capability check: this adapter version does not store these native IDs."""
    return {
        'readOnly': True,
        'mutatesStoredIdentity': False,
        'roleSessionId': {
            'state': 'not_written',
            'reason': _UNWRITTEN_EXECUTION_IDENTITY['roleSessionId'],
        },
        'executorAgentId': {
            'state': 'not_written',
            'reason': _UNWRITTEN_EXECUTION_IDENTITY['executorAgentId'],
        },
        'verifiable': {
            'roleAgentId': 'task_field',
            'upstreamTaskId': 'recorded_execution_identity_only',
        },
    }


def execution_identity_availability(identity, role_agent_id):
    """Explain native IDs this version does not store. Never invent or rewrite them."""
    stored = identity if isinstance(identity, dict) else {}

    def field(key):
        if key not in stored:
            return {'state': 'not_written', 'reason': _UNWRITTEN_EXECUTION_IDENTITY[key]}
        if _clean_identity_token(stored.get(key)):
            return {'state': 'recorded', 'reason': '该值已在存储的执行身份中，这次读取没有补写或改写。'}
        return {'state': 'unavailable', 'reason': '存储值不能作为原生身份证明，这次读取没有改写。'}

    return {
        'readOnly': True,
        'mutatesStoredIdentity': False,
        'fields': {key: field(key) for key in _UNWRITTEN_EXECUTION_IDENTITY},
        'verifiable': {
            'roleAgentId': 'recorded_outside_execution_identity' if role_agent_id else 'unavailable',
            'upstreamTaskId': 'recorded' if _clean_identity_token(stored.get('upstreamTaskId')) else 'not_recorded',
        },
    }


def create_app(store: TaskStore, config: ServiceConfig, *, workflows=None, dispatcher=None, approvals=None, memories=None, authoring=None, task_flow=None, verifier=None, triggers=None, execution_readiness=None) -> FastAPI:
    config = copy.deepcopy(config)
    if not config.tokens or any(not token.strip() for token in config.tokens):
        raise ValueError('Service authentication must be configured')
    if config.controlled_execution not in ('unknown', 'unsupported', 'confirmed'):
        raise ValueError('Invalid controlled execution capability')
    if config.execution_block_reason is not None:
        from .execution_runtime import ExecutionCompatibilityError
        if (config.execution_block_reason not in ExecutionCompatibilityError.REASONS.values()
                or config.controlled_execution == 'confirmed' or dispatcher is not None
                or task_flow is not None):
            raise ValueError('Invalid degraded execution configuration')
    projects = copy.deepcopy(config.projects)
    for project in projects.values():
        workspace = Path(project['workspace']).resolve(strict=True)
        if not workspace.is_dir():
            raise ValueError('Registered workspace must be a directory')
        project['workspace'] = str(workspace)
        if 'kanbanReadOnly' in project:
            from .kanban_readonly import validate_settings
            project['kanbanReadOnly'] = validate_settings(project['kanbanReadOnly'])
        if 'maxConcurrentTasks' in project:
            from .project_concurrency import validate_project_limit
            validate_project_limit(project['maxConcurrentTasks'])
        if 'runBudget' in project:
            from .run_budget import validate_run_budget
            if validate_run_budget(project['runBudget']) is None:
                raise ValueError('An explicit run budget must be configured')
    if verifier is not None:
        from .automatic_verification import AutomaticVerifier
        if not isinstance(verifier, AutomaticVerifier) or verifier.store.path != store.path:
            raise ValueError('Automatic verifier must use this existing task store')
        from .verification_runtime import check_evidence_location
        check_evidence_location(verifier.directory, projects)
        if dispatcher is None and config.execution_block_reason is None:
            from .dispatcher import Dispatcher
            dispatcher = Dispatcher(store, {}, verifier=verifier)
        elif dispatcher is not None and dispatcher.store.path != store.path:
            raise ValueError('Verification dispatcher must use the same task store')
        if dispatcher is not None:
            dispatcher.verifier = verifier
    for binding in config.bindings.values():
        if 'roleRevision' in binding:
            from .role_revision import validate_role_revision
            validate_role_revision(binding['roleRevision'])
        if 'verificationPolicy' in binding:
            from .verification_policy import validate_policy
            binding['verificationPolicy'] = validate_policy(binding['verificationPolicy'])
            if verifier is None:
                raise ValueError('Configured verification policy requires an automatic verifier')
            verifier.validate_policy(binding['verificationPolicy'])
    from .worker import ExecutionWorker
    worker = ExecutionWorker(dispatcher, allow_submissions=config.controlled_execution == 'confirmed') if dispatcher is not None else None

    @asynccontextmanager
    async def lifespan(application):
        started = []
        try:
            for owned in ([worker] if worker is not None else []) + (list(task_flow.workers) if task_flow is not None else []):
                owned.start()
                started.append(owned)
            yield
        finally:
            # Signal every owner before joining any transport in flight.
            for owned in started:
                owned.stop_event.set()
            failed = False
            for owned in reversed(started):
                try:
                    await asyncio.to_thread(owned.stop)
                except RuntimeError:
                    failed = True
            if failed:
                raise RuntimeError('Owned workflow workers have not drained')

    app = FastAPI(title='Agent Line 任务适配服务', docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.execution_worker = worker
    app.state.task_flow = task_flow
    app.state.verifier = verifier
    app.state.execution_readiness = execution_readiness
    if task_flow is not None:
        task_flow.discovery.allow_submissions = config.controlled_execution == 'confirmed'
        task_flow.discovery.execution_readiness = execution_readiness

    def execution_state():
        if execution_readiness is None:
            return config.controlled_execution, None
        if not execution_readiness.confirmed:
            return 'unknown', execution_readiness.reason
        try:
            execution_readiness.assert_current()
        except ValueError:
            return 'unknown', '执行验收证据已失效，已停止新任务；请重新核对当前部署。'
        return config.controlled_execution, None

    def authorize(authorization: str | None = Header(default=None)) -> frozenset[str]:
        scheme, _, value = (authorization or '').partition(' ')
        supplied = value.encode('utf-8') if scheme.lower() == 'bearer' else b''
        allowed = None
        # Compare every configured token; do not use direct string equality.
        for token, scopes in config.tokens.items():
            if secrets.compare_digest(supplied, token.encode('utf-8')):
                allowed = frozenset(scopes)
        if allowed is None:
            raise HTTPException(401, '需要有效的服务凭证', headers={'WWW-Authenticate': 'Bearer'})
        return allowed

    def accessible(task_id: str, scopes: frozenset[str]):
        try:
            task = store.get(task_id)
        except KeyError:
            raise HTTPException(404, '任务不存在') from None
        if task['payload']['projectId'] not in scopes:
            raise HTTPException(404, '任务不存在')
        return task

    if config.execution_block_reason is not None:
        @app.middleware('http')
        async def reject_degraded_mutations(request, call_next):
            path = request.url.path
            parts = path.rstrip('/').split('/')
            # This status GET reconciles native deletion and writes both ledgers.
            deletion_reconciliation = (request.method == 'GET' and len(parts) == 7
                and parts[:4] == ['', 'extension', 'v1', 'memories']
                and parts[-1] == 'deletion')
            if (path.startswith('/extension/v1/')
                    and (request.method not in ('GET', 'HEAD', 'OPTIONS')
                         or deletion_reconciliation)):
                # Preserve each existing credential boundary before reporting the
                # bounded compatibility reason. No route body or journal is touched.
                authority = None
                for prefix, api in (('/extension/v1/authoring/', authoring),
                                    ('/extension/v1/approvals/', approvals),
                                    ('/extension/v1/memories/', memories)):
                    if path.startswith(prefix) and api is not None:
                        authority = api.access
                        break
                if '/execution-approvals/' in path and approvals is not None:
                    authority = approvals.access
                try:
                    authorization = request.headers.get('authorization')
                    if authority is None:
                        authorize(authorization)
                    else:
                        authority.authenticate(authorization)
                except HTTPException as error:
                    return JSONResponse(status_code=error.status_code,
                        content={'detail': error.detail}, headers=error.headers)
                return JSONResponse(status_code=503,
                    content={'detail': config.execution_block_reason})
            return await call_next(request)

    def public_task(task, *, include_output_validation=True):
        # Do not return resolved workspace paths or other internal configuration.
        tests = task['payload'].get('verificationPolicy', {}).get('requireTests') is True
        if tests and 'cancellationRequested' not in task:
            task = store.get(task['id'])
        validation, structured = inspect_output(task) if include_output_validation else (None, None)
        evidence = copy.deepcopy(task['evidence'])
        def redact_exit(value):
            if isinstance(value, dict):
                if value.get('source') == 'docker-inspect-before-remove':
                    value.pop('workspace', None)
                for child in value.values():
                    redact_exit(child)
            elif isinstance(value, list):
                for child in value:
                    redact_exit(child)
        redact_exit(evidence)
        observation = store.last_execution_observation(task['id'])
        try:
            approval_decisions = store.approval_decisions(task['id'])
        except ValueError:
            approval_decisions = None
        queue = (task['executionQueue'] if 'executionQueue' in task else store.execution_queue(task['id'])) if task['executionStatus'] == 'registered' else None
        try:
            limits = store.execution_limits(task['id'])
        except ValueError:
            limits = {'state': 'unknown'}
        try:
            run_budget = store.run_budget(task['id'])
        except ValueError:
            run_budget = {'state': 'unknown'}
        try:
            capacity = store.project_concurrency(task['id'])
        except (ValueError, TypeError, KeyError):
            capacity = {'state': 'unknown'}
        batch_item = None
        if 'batchItem' in task['payload']:
            from .batch_item import task_batch_item, batch_summary
            try:
                item = task_batch_item(task['payload'])
                batch_item = item if include_output_validation else batch_summary(item)
            except ValueError:
                batch_item = {'state': 'unknown'}
        role_agent = task['payload'].get('roleAgentId')
        if (not isinstance(role_agent, str) or not role_agent.strip() or len(role_agent) > 256
                or role_agent.strip() != role_agent or '\0' in role_agent):
            role_agent = None
        return {
            'taskId': task['id'], 'projectId': task['payload']['projectId'],
            **({'activity': task['activity']} if 'activity' in task else {}),
            'workflow': task['payload']['workflow'],
            **({'workItem': task['payload']['workItem']} if 'workItem' in task['payload'] else {}),
            **({'outputFields': task['payload']['outputFields']} if 'outputFields' in task['payload'] else {}),
            **({'batchItem': batch_item} if batch_item is not None else {}),
            'roleBindingId': task['payload']['roleBindingId'],
            'roleAgentId': role_agent,
            **{key: task[key] for key in (
                'executionStatus', 'upstreamSubmissionStatus', 'version',
                'executionIdentity', 'output', 'verificationStatus', 'memoryStatus', 'resourcesReleased',
            )},
            # Keep this beside the stored identity. Approval, verification and
            # cancellation compare that object exactly, so missing native IDs
            # must not be filled in here.
            'executionIdentityAvailability': execution_identity_availability(
                task.get('executionIdentity'), role_agent),
            'evidence': evidence,
            **({'approvalDecisions': approval_decisions} if approval_decisions else {}),
            **({'artifactAccess': {'configured': verifier is not None, 'maxBytes': 1_048_576}}
               if include_output_validation else {}),
            'usage': task_usage(task),
            **({'outputValidation': validation} if include_output_validation else {}),
            **({'structuredOutput': structured} if structured is not None else {}),
            **({'executionObservation': observation} if observation is not None else {}),
            **({'executionQueue': queue} if queue is not None else {}),
            **({'executionLimits': limits} if limits is not None else {}),
            **({'runBudget': run_budget} if run_budget is not None else {}),
            **({'projectConcurrency': capacity} if capacity is not None else {}),
            **({'canCancelVerification': task['executionStatus'] == 'succeeded'
                and task['verificationStatus'] == 'pending'
                and not task.get('cancellationRequested', False),
                'verificationCancellationRequested': task.get('verificationCancellationRequested', False)} if tests else {}),
            **({'roleRevision': task['payload']['roleRevision']} if 'roleRevision' in task['payload'] else {}),
            **({'submissionBlock': task['submissionBlock']} if 'submissionBlock' in task else {}),
        }

    @app.exception_handler(sqlite3.Error)
    async def storage_failure(_request, _exc):
        return JSONResponse(status_code=503, content={'detail': '任务存储不可用，请核对数据目录；未确认登记成功'})

    @app.get('/extension/v1/capabilities')
    def capabilities(_scopes: frozenset[str] = Depends(authorize)):
        # Report only this service's bounded contract/configuration surface.
        # Module presence grants no access through its independent credential.
        capability, _reason = execution_state()
        with store.connect() as db:
            guarded = db.execute('SELECT 1 FROM admission_guard LIMIT 1').fetchone() is not None
        if config.execution_block_reason is not None or capability == 'unsupported':
            block_reason = 'compatibility_blocked'
        elif guarded:
            block_reason = 'reconciliation_required'
        elif capability != 'confirmed':
            block_reason = ('readiness_stale' if execution_readiness is not None
                            and execution_readiness.confirmed and _reason is not None
                            else 'readiness_unconfirmed')
        else:
            block_reason = 'none'
        modules = (('workflows', workflows), ('authoring', authoring),
                   ('approvals', approvals), ('memories', memories), ('triggers', triggers))
        return JSONResponse(content={
            'schemaVersion': '1.0', 'source': 'authorized_service_capabilities',
            'readOnly': True, 'observedAt': time.time(), 'apiContractVersion': '1.0',
            'execution': {'controlledExecution': capability,
                          'readyForExecution': capability == 'confirmed' and not guarded,
                          'reconciliationRequired': guarded, 'blockReason': block_reason},
            'services': [{'id': name, 'state': 'configured' if module is not None else 'not_configured'}
                         for name, module in modules],
            # A checkout pin is not a runtime-version observation. Neither the
            # current service contract nor readiness evidence supplies versions.
            'versions': {'qwenpaw': None, 'sim': None}, 'versionSource': 'not_observed',
            # This version does not expose roleSessionId or executorAgentId.
            # Say so here; do not invent them on a task to satisfy the check.
            'nativeExecutionIdentity': native_execution_identity_capability(),
        }, headers={'Cache-Control': 'no-store'})

    @app.get('/extension/v1/health')
    def health(scopes: frozenset[str] = Depends(authorize)):
        with store.connect() as db:
            # SQLite mode=rw can still open a filesystem-readonly database.
            # Verify write admission without changing any row or committing data.
            db.execute('UPDATE installation SET schema_version=schema_version WHERE 0')
            db.rollback()
            db.execute('SELECT schema_version FROM installation').fetchone()
            guarded = db.execute('SELECT 1 FROM admission_guard LIMIT 1').fetchone() is not None
        capability, readiness_reason = execution_state()
        return {'status': 'ok', 'storage': 'available',
                'controlledExecution': capability,
                'executionObserver': worker.health_snapshot(project_scopes=scopes) if worker is not None else {'status': 'not_configured'},
                'reconciliationRequired': guarded,
                'readyForExecution': capability == 'confirmed' and not guarded,
                **({'executionReadinessReason': readiness_reason} if readiness_reason else {}),
                **({'executionBlockReason': config.execution_block_reason}
                   if config.execution_block_reason is not None else {})}

    @app.get('/extension/v1/maintenance/plugin-removal')
    def plugin_removal(_scopes: frozenset[str] = Depends(authorize)):
        return JSONResponse(content=store.plugin_removal_decision(),
                            headers={'Cache-Control': 'no-store'})

    @app.post('/extension/v1/maintenance/plugin-removal')
    def refuse_plugin_removal(_scopes: frozenset[str] = Depends(authorize)):
        # This route never unloads a plugin and never releases a write lease.
        decision = store.plugin_removal_decision()
        status = 200 if decision['coldRemovalAllowed'] else 409
        return JSONResponse(status_code=status, content=decision,
                            headers={'Cache-Control': 'no-store'})

    @app.post('/extension/v1/node-tasks', status_code=202)
    def register(body: NodeTaskRequest, scopes: frozenset[str] = Depends(authorize)):
        if body.projectId not in scopes or body.projectId not in projects:
            raise HTTPException(403, '无权使用此项目')
        binding = config.bindings.get(body.roleBindingId)
        if not binding or binding.get('projectId') != body.projectId or binding.get('enabled', True) is not True:
            raise HTTPException(403, '角色绑定不可用或不属于此项目')
        if not isinstance(binding.get('roleAgentId'), str) or not binding['roleAgentId'].strip():
            raise HTTPException(503, '正式角色配置不完整')
        # Native recall follows the host role. Reject a new task before readiness so a
        # stale execution proof cannot hide a cross-project binding.
        if host_role_shared_across_projects(config.bindings, binding['roleAgentId'], body.projectId):
            with store.connect() as db:
                existing = db.execute(
                    'SELECT 1 FROM tasks WHERE idempotency_key=?', (body.idempotencyKey,),
                ).fetchone() is not None
            if not existing:
                raise HTTPException(422, '原生角色不能跨项目共用记忆')
        capability, readiness_reason = execution_state()
        if capability != 'confirmed':
            raise HTTPException(503, readiness_reason or '受控执行能力尚未确认，禁止新任务准入')
        payload = body.model_dump()
        if 'outputFields' not in body.model_fields_set:
            payload.pop('outputFields', None)
        key = payload.pop('idempotencyKey')
        payload['roleAgentId'] = binding['roleAgentId']
        payload['workspace'] = projects[body.projectId]['workspace']
        if 'verificationPolicy' in binding:
            payload['verificationPolicy'] = copy.deepcopy(binding['verificationPolicy'])
        if 'roleRevision' in binding:
            payload['roleRevision'] = binding['roleRevision']
        try:
            if workflows is not None:
                from .work_items import run_item, task_context
                item = run_item(workflows.journal, body.projectId, body.workflow.id, body.workflow.runId)
                if item is not None:
                    context = task_context(item)
                    if 'workItem' in payload['input'] and payload['input']['workItem'] != context:
                        raise Conflict('Task business context differs from its workflow run')
                    payload['workItem'] = context
                    payload['input'] = {**payload['input'], 'workItem': context}
            if 'workItem' in payload['input'] and 'workItem' not in payload:
                raise Conflict('Business context requires a server-bound workflow run')
            budget = projects[body.projectId].get('runBudget')
            frozen_budget = False
            flow = getattr(workflows, 'task_flow', None)
            if flow is not None:
                try:
                    run = flow.runs.get(body.workflow.id, body.workflow.runId)
                except KeyError:
                    pass  # Direct node submissions may have no Sim run ledger entry.
                else:
                    if (run.get('projectId') != body.projectId
                            or run.get('workflowId') != body.workflow.id
                            or run.get('runId') != body.workflow.runId):
                        raise Conflict('Workflow run budget belongs to another scope')
                    node_policy = run.get('agentPolicies', {}).get(body.workflow.nodeId, {})
                    if 'batchItem' in node_policy:
                        raise HTTPException(403, '批量节点由固定流程登记，请从原工作流继续处理')
                    budget = run.get('runBudget')
                    frozen_budget = True
            payload = store.bind_project_concurrency(key, payload, projects[body.projectId].get('maxConcurrentTasks'))
            payload = store.bind_run_budget(payload, budget)
            if frozen_budget and payload.get('runBudget') != budget:
                raise Conflict('Task budget differs from its frozen workflow run')
            # Exact existing registration remains readable during a pause. New
            # registration holds the same run lease as the background observer.
            with store.connect() as db:
                exists = db.execute('SELECT 1 FROM tasks WHERE idempotency_key=?', (key,)).fetchone() is not None
            if not exists and dispatcher is not None:
                try:
                    gateway = dispatcher._gateway({'payload': payload})
                except Conflict:
                    gateway = None
                if gateway is None:
                    raise HTTPException(503, '此角色尚未接通可用的执行服务，暂不接受新任务') from None
            with (workflows.run_control.admission(body.workflow.id, body.workflow.runId)
                    if workflows is not None and workflows.run_control is not None and not exists else nullcontext()):
                task, reused = store.register(key, payload,
                    replay_only=not (supported_output_schema(body.outputSchemaId, body.outputFields)
                        and not ('outputFields' in body.model_fields_set and body.outputFields is None)))
        except KeyError:
            raise HTTPException(404, '业务任务与此运行的关联不可用') from None
        except AdmissionClosed:
            raise HTTPException(503, '维护或恢复核对中，暂不接受新任务') from None
        except Conflict:
            raise HTTPException(409, '幂等键或节点尝试与已有登记冲突') from None
        except ValueError:
            raise HTTPException(422, '任务输入或结果结构不受支持，请核对当前可选格式、配置及原任务') from None
        return {
            'taskId': task['id'], 'registrationStatus': 'registered',
            'upstreamSubmissionStatus': task['upstreamSubmissionStatus'],
            'executionStatus': task['executionStatus'], 'reusedExistingTask': reused,
        }

    @app.get('/extension/v1/overview')
    def overview(after: str = Query(default='', max_length=128),
                 limit: int = Query(default=25, ge=1, le=50),
                 projectId: str | None = Query(default=None, min_length=1, max_length=256),
                 status: Literal['registered', 'submitting', 'uncertain', 'accepted', 'cancel_requested', 'succeeded', 'failed', 'canceled'] | None = None):
        # The owner explicitly permits direct LAN viewing of instance summaries.
        # Keep this projection separate from private task details and all actions.
        try:
            page = store.list_tasks(frozenset(projects), after=after, limit=limit,
                                    project_id=projectId, status=status, include_queue=True)
        except KeyError:
            raise HTTPException(404, '项目或分页记录不可用，请刷新列表') from None
        except ValueError:
            raise HTTPException(422, '筛选条件无效，请重新选择项目') from None

        def identifier(value):
            return value if isinstance(value, str) and 0 < len(value) <= 256 else None

        summaries = []
        for task in page['tasks']:
            payload = task['payload']
            workflow = payload.get('workflow')
            role_agent = payload.get('roleAgentId')
            if (not isinstance(role_agent, str) or not role_agent.strip() or len(role_agent) > 256
                    or role_agent.strip() != role_agent or '\0' in role_agent):
                role_agent = None
            summaries.append({
                'taskId': task['id'], 'projectId': identifier(payload.get('projectId')),
                'roleBindingId': identifier(payload.get('roleBindingId')),
                'roleAgentId': role_agent,
                'activity': task['activity'],
                **({'workItemId': identifier(payload['workItem'].get('workItemId'))}
                   if isinstance(payload.get('workItem'), dict) else {}),
                **({'verificationCancellationRequested': task['verificationCancellationRequested']}
                   if payload.get('verificationPolicy', {}).get('requireTests') is True else {}),
                **({'executionQueue': task['executionQueue']} if 'executionQueue' in task else {}),
                **({'submissionBlock': task['submissionBlock']} if 'submissionBlock' in task else {}),
                'workflow': {key: workflow[key] for key in
                             ('id', 'revision', 'runId', 'nodeId', 'nodeExecutionId', 'attemptId')
                             if isinstance(workflow, dict) and identifier(workflow.get(key)) is not None},
                **{key: task[key] for key in ('executionStatus', 'verificationStatus',
                                              'memoryStatus', 'resourcesReleased')},
            })
        services = [{'id': 'adapter', 'name': '任务服务', 'state': 'available',
                     'description': '本服务正在响应，提供本机任务记录与状态摘要。'}]
        if workflows is not None:
            services.append({'id': 'sim', 'name': 'Sim 工作流服务', 'state': 'configured',
                             'description': '负责流程编排；已配置，不代表原生运行状态已核对。'})
        if config.bindings:
            services.append({'id': 'agents', 'name': 'QwenPaw 角色服务', 'state': 'configured',
                             'count': len(config.bindings),
                             'description': '复用已建立的角色绑定；可用性以实际任务记录为准。'})
        if memories is not None:
            services.append({'id': 'memory', 'name': '记忆审核服务', 'state': 'configured',
                             'description': '审核后写入原角色记忆，写回结论独立记录。'})
        return JSONResponse(content={
            'source': 'local_instance_summary', 'readOnly': True,
            'projects': [{'projectId': key, 'name': identifier(value.get('name')) or key}
                         for key, value in sorted(projects.items())],
            'services': services, 'tasks': summaries, 'nextCursor': page['nextCursor'],
            'filters': {'projectId': projectId, 'status': status},
        }, headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

    @app.get('/extension/v1/node-tasks')
    def list_tasks(after: str = Query(default='', max_length=128),
                   limit: int = Query(default=50, ge=1, le=500),
                   status: Literal['registered', 'submitting', 'uncertain', 'accepted', 'cancel_requested', 'succeeded', 'failed', 'canceled'] | None = None,
                   projectId: str | None = Query(default=None, min_length=1, max_length=256),
                   workflowId: str | None = Query(default=None, min_length=1, max_length=256),
                   runId: str | None = Query(default=None, min_length=1, max_length=256),
                   nodeId: str | None = Query(default=None, min_length=1, max_length=256),
                   workItemId: str | None = Query(default=None, min_length=1, max_length=128,
                                                  pattern=r'^[A-Za-z0-9_-]+$'),
                   scopes: frozenset[str] = Depends(authorize)):
        try:
            page = store.list_tasks(scopes, after=after, limit=limit, status=status,
                                    project_id=projectId, workflow_id=workflowId, run_id=runId, include_queue=True,
                                    **({'node_id': nodeId} if nodeId is not None else {}),
                                    **({'work_item_id': workItemId} if workItemId is not None else {}))
        except KeyError:
            raise HTTPException(404, '任务范围或分页游标不可用，请核对范围或刷新列表') from None
        except ValueError:
            raise HTTPException(422, '筛选标识无效；业务任务须提供项目，按运行或节点筛选须同时提供项目、工作流和运行标识') from None
        # Catalog omits potentially large artifact content and internal payloads.
        summaries = []
        for task in page['tasks']:
            visible = public_task(task, include_output_validation=False)
            visible.pop('output', None)
            visible.pop('evidence', None)
            summaries.append(visible)
        return {'tasks': summaries, 'nextCursor': page['nextCursor'],
                'filters': {'projectId': projectId, 'workflowId': workflowId, 'runId': runId,
                            **({'nodeId': nodeId} if nodeId is not None else {}),
                            **({'workItemId': workItemId} if workItemId is not None else {})}}

    @app.get('/extension/v1/node-tasks/{task_id}/artifacts/{check_index}')
    def artifact_content(task_id: str, check_index: int, scopes: frozenset[str] = Depends(authorize)):
        task = accessible(task_id, scopes)
        if verifier is None:
            raise HTTPException(503, '当前未配置受控产物读取')
        from .artifact_access import read_artifact, ArtifactUnavailable, ArtifactTooLarge
        try:
            result = read_artifact(verifier, task, check_index, projects[task['payload']['projectId']]['workspace'])
            accessible(task_id, scopes)
            return JSONResponse(result, headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
        except ArtifactTooLarge:
            raise HTTPException(413, '产物超过本入口1MiB读取上限，请保留原证据核对') from None
        except (ArtifactUnavailable, ValueError, TypeError, KeyError, OSError, RecursionError):
            raise HTTPException(409, '当前产物与原验收证据无法核对，未返回文件内容') from None

    @app.get('/extension/v1/node-tasks/{task_id}')
    def get_task(task_id: str, scopes: frozenset[str] = Depends(authorize)):
        return public_task(accessible(task_id, scopes))

    @app.post('/extension/v1/node-tasks/{task_id}/workflow-draft')
    def workflow_draft(task_id: str, body: ReconcileRequest,
                       scopes: frozenset[str] = Depends(authorize)):
        task = accessible(task_id, scopes)
        if task['version'] != body.expectedVersion:
            raise HTTPException(409, '任务记录已变化，请重新查询原任务后核对草稿')
        if (task['executionStatus'] != 'succeeded'
                or task['resourcesReleased'] is not True
                or task['payload'].get('outputSchemaId') != 'workflow-draft-v1'):
            raise HTTPException(409, '当前任务尚无可核对的已结束流程草稿')
        # Parse the original output through the same bounded template compiler.
        # This is structural review only; do not execute or approve the draft.
        validation, template = inspect_output(task)
        if validation['state'] != 'valid' or not isinstance(template, dict):
            raise HTTPException(409, '模型草稿不符合可移植模板契约，未返回草稿')
        current = accessible(task_id, scopes)
        if (current['version'] != task['version']
                or current.get('output') != task.get('output')
                or current['verificationStatus'] != task['verificationStatus']
                or current['resourcesReleased'] is not True
                or current['executionStatus'] != 'succeeded'):
            raise HTTPException(409, '核对期间任务记录已变化，请重新查询原任务')
        return JSONResponse(content={
            'source': 'validated_model_workflow_draft', 'readOnly': True,
            'executionSupported': False, 'saved': False, 'published': False,
            'projectId': task['payload']['projectId'], 'taskId': task_id,
            'taskVersion': task['version'],
            'templateSha256': hashlib.sha256(canonical(template).encode('utf-8')).hexdigest(),
            'nodeCount': len(template['document']['nodes']),
            'edgeCount': len(template['document']['edges']),
            'roleSlotCount': len(template['roleSlots']),
            'verificationStatus': task['verificationStatus'], 'template': template,
        }, headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

    @app.get('/extension/v1/recovery/{task_id}/records')
    def recovery_records(task_id: str, scopes: frozenset[str] = Depends(authorize)):
        accessible(task_id, scopes)
        if not store.recovery_records_available():
            raise HTTPException(409, '任务库仍是版本 2，没有恢复记录可读。本次不会自动迁移。')
        return JSONResponse(content={
            'source': 'recovery_records', 'taskId': task_id,
            'submissionAttempted': False, 'records': store.recovery_records(task_id),
        }, headers={'Cache-Control': 'no-store'})

    @app.post('/extension/v1/recovery/{task_id}/reconcile')
    def reconcile_task(task_id: str, body: RecoveryReconcileRequest,
                       scopes: frozenset[str] = Depends(authorize)):
        supplied = (body.finding, body.decision, body.allowNewAttempt)
        decision_requested = any(value is not None for value in supplied)
        if decision_requested and (None in supplied or type(body.allowNewAttempt) is not bool):
            raise HTTPException(422, '人工裁决须同时提供发现、裁决，以及是否允许新尝试')
        if decision_requested:
            try:
                TaskStore._recovery_line(body.finding, 'finding', 500)
                TaskStore._recovery_line(body.decision, 'decision', 500)
            except ValueError:
                raise HTTPException(422, '发现和裁决必须是不含控制字符的单行文本') from None
        task = accessible(task_id, scopes)
        if task['version'] != body.expectedVersion:
            raise HTTPException(409, '任务记录已变化，请先查询原任务，再决定是否核对')
        if decision_requested and not store.recovery_records_available():
            raise HTTPException(
                409, '任务库仍是版本 2，恢复裁决未写入，也未核对执行。请先显式迁移，不要登记新尝试。')
        if dispatcher is None:
            raise HTTPException(503, '当前未配置原执行核对，请保留任务标识等待恢复')
        observations = []
        try:
            # Reuse the original observer only. It may honor an existing stop
            # or deadline, but never submits work or advances Sim successors.
            dispatcher.reconcile(task_id, on_observation=observations.append)
        except (Conflict, ValueError, KeyError):
            raise HTTPException(409, '原执行身份或状态无法核对，请保留原任务记录；未重新提交执行') from None
        outcome = observations[-1]['outcome'] if observations else 'not_queried'
        recovery = None
        if decision_requested:
            try:
                recovery = store.record_recovery(
                    task_id, action='reconcile', finding=body.finding,
                    decision=body.decision, allow_new_attempt=body.allowNewAttempt)
            except ValueError:
                raise HTTPException(
                    409, '原执行已核对，但恢复裁决没能写入。任务未因裁决改版本，也没有登记新尝试。') from None
        content = {
            'source': 'original_execution_reconciliation', 'taskId': task_id,
            'submissionAttempted': False, 'outcome': outcome,
            'task': public_task(accessible(task_id, scopes)),
        }
        if recovery is not None:
            content['recoveryRecorded'] = True
            content['recoveryRecord'] = recovery
        return JSONResponse(content=content, headers={'Cache-Control': 'no-store'})

    @app.get('/extension/v1/node-tasks/{task_id}/memory-trace')
    def memory_trace(task_id: str, scopes: frozenset[str] = Depends(authorize)):
        task = accessible(task_id, scopes)
        if dispatcher is None:
            raise HTTPException(503, '此实例暂时无法读取原生角色快照')
        try:
            gateway = dispatcher._gateway(task)
            if gateway is None or not callable(getattr(type(gateway), 'memory_trace', None)):
                raise HTTPException(503, '此执行后端尚未提供原生角色快照追踪')
            result = gateway.memory_trace(task['id'], task['executionIdentity'])
            if (not isinstance(result, dict) or result.get('taskId') != task['id']
                    or result.get('roleAgentId') != task['payload']['roleAgentId']
                    or result.get('roleSessionId') != 'agent-line-' + task['id']
                    or result.get('source') != 'cached_native_memory_trace'
                    or result.get('readOnly') is not True):
                raise ValueError('Memory trace ownership differs')
        except (Conflict, KeyError, ValueError, TypeError, sqlite3.Error):
            raise HTTPException(503, '原生角色快照暂时无法对应，请保留原任务标识核对') from None
        try:
            store.note_cached_trace(task['id'], result)
        except (ValueError, OSError, sqlite3.Error):
            pass  # Ownership already matched. A missing category note must not hide the trace.
        return JSONResponse(result, headers={'Cache-Control': 'no-store'})

    @app.post('/extension/v1/node-tasks/{task_id}/memory-retrieval-evaluation')
    def memory_retrieval_evaluation(task_id: str, body: MemoryRetrievalEvaluationRequest,
                                    scopes: frozenset[str] = Depends(authorize),
                                    authorization: str | None = Header(default=None)):
        task = accessible(task_id, scopes)
        if task['version'] != body.expectedVersion:
            raise HTTPException(409, '任务记录已变化，请重新读取原任务和记忆快照')
        credential = (authorization or '').partition(' ')[2]
        if (body.expectedText != body.expectedText.strip()
                or any(ord(char) < 32 or ord(char) == 127 for char in body.expectedText)
                or credential and credential in body.expectedText):
            raise HTTPException(422, '预期文本须为不含控制字符或当前凭证的4至256字符单行文本')
        if dispatcher is None:
            raise HTTPException(503, '此实例尚未提供原生召回证据核对')
        try:
            gateway = dispatcher._gateway(task)
            if gateway is None or not callable(getattr(type(gateway), 'memory_retrieval_evaluation', None)):
                raise HTTPException(503, '此执行后端尚未提供原生召回证据核对')
            result = gateway.memory_retrieval_evaluation(task_id, task['executionIdentity'], body.expectedText)
            if (not isinstance(result, dict) or result.get('source') != 'cached_memory_retrieval_evaluation'
                    or result.get('readOnly') is not True or result.get('taskId') != task_id
                    or result.get('roleAgentId') != task['payload']['roleAgentId']
                    or result.get('roleSessionId') != 'agent-line-' + task_id):
                raise ValueError('Retrieval ownership differs')
        except (Conflict, KeyError, ValueError, TypeError, sqlite3.Error, RecursionError, UnicodeError):
            raise HTTPException(503, '本地原生快照尚无法核对，未发起新的记忆检索') from None
        if result.get('snapshotSha256') != body.expectedSnapshotSha256:
            raise HTTPException(409, '原生快照已变化，请重新读取快照再核对')
        current = accessible(task_id, scopes)
        if (current['version'] != task['version']
                or current['executionIdentity'] != task['executionIdentity']):
            raise HTTPException(409, '任务执行身份或版本已变化，请重新读取原任务')
        fields = ('source', 'readOnly', 'taskId', 'roleAgentId', 'roleSessionId',
                  'parentTaskId', 'snapshotSha256', 'readAt', 'snapshotCapturedAt',
                  'coverage', 'criterion', 'criterionSha256', 'successfulSearches',
                  'textMatchedSearches', 'matchedSearchesBeforeDelegation',
                  'matchedDelegations', 'semanticRelevance', 'codexInputInjection')
        return JSONResponse(content={**{key: result[key] for key in fields},
                                     'projectId': task['payload']['projectId'], 'taskVersion': task['version']},
                            headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

    @app.get('/extension/v1/node-tasks/{task_id}/kanban')
    async def task_kanban(task_id: str, expectedVersion: int = Query(ge=0, le=9_007_199_254_740_991),
                    scopes: frozenset[str] = Depends(authorize),
                    authorization: str | None = Header(default=None)):
        task = await asyncio.to_thread(accessible, task_id, scopes)
        if task['version'] != expectedVersion:
            raise HTTPException(409, '任务记录已变化，请重新查询原任务后读取Kanban摘要')
        identity = task.get('executionIdentity') or {}

        def native_id(value):
            return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,256}', value) else None

        thread = native_id(identity.get('codexThreadId'))
        turn = native_id(identity.get('codexTurnId'))
        project = projects[task['payload']['projectId']]
        settings = project.get('kanbanReadOnly')
        result = {'state': 'not_configured', 'directoryGeneratedAt': None, 'session': None}
        if settings is not None:
            if thread is None:
                result['state'] = 'identity_unavailable'
            else:
                from .kanban_readonly import read_session_summary, KanbanReadUnavailable
                credential = (authorization or '').partition(' ')[2]
                try:
                    result = await read_session_summary(settings, workspace=project['workspace'],
                        codex_thread_id=thread, redact_tokens=(credential,) if credential else ())
                except KanbanReadUnavailable:
                    raise HTTPException(502, 'Kanban目录暂时无法只读核对，未创建、更新或控制会话') from None
        current = await asyncio.to_thread(accessible, task_id, scopes)
        if current['version'] != task['version'] or current.get('executionIdentity') != task.get('executionIdentity'):
            raise HTTPException(409, '读取期间原任务或执行身份已变化，请重新查询原任务')
        return JSONResponse(content={
            'source': 'coding_kanban_readonly', 'readOnly': True, 'executionSupported': False,
            'taskId': task_id, 'taskVersion': task['version'], 'projectId': task['payload']['projectId'],
            'codexThreadId': thread, 'codexTurnId': turn, 'readAt': time.time(),
            'coverage': 'session_directory_snapshot', 'taskTurnMatched': None,
            'resultAuthority': 'unverified_session_summary', **result,
        }, headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

    @app.get('/extension/v1/node-tasks/{task_id}/events')
    def events(task_id: str, after: int = Query(default=0, ge=0),
               limit: int = Query(default=100, ge=1, le=500),
               scopes: frozenset[str] = Depends(authorize)):
        accessible(task_id, scopes)
        page = [public_event(row) for row in store.events(task_id, after=after, limit=limit)]
        return {'events': page, 'nextCursor': page[-1]['seq'] if page else after}

    @app.post('/extension/v1/node-tasks/{task_id}/cancel')
    def cancel(task_id: str, scopes: frozenset[str] = Depends(authorize)):
        accessible(task_id, scopes)
        changed = store.request_cancel(task_id)
        return {**public_task(accessible(task_id, scopes)), 'changed': changed}

    if memories is not None:
        memories.access.reject_service_tokens(config.tokens)
        app.include_router(memories.router())
    if workflows is not None:
        workflows.journal.mutation_guard = store.workflow_admission
        from .run_control import RunControl
        control = RunControl(workflows.journal, store)
        workflows.run_control = workflows.journal.run_control = control
        workflows.dispatcher = dispatcher
        workflows.verifier = verifier
        if dispatcher is not None:
            dispatcher.run_control = control
        if task_flow is not None:
            task_flow.discovery.run_control = control
        if approvals is not None:
            approvals.coordinator.run_control = control
            approvals.coordinator.journal.run_control = control
            if approvals.discovery is not None:
                approvals.discovery.run_control = control
        app.state.run_control = control
    if task_flow is not None:
        if workflows is None or workflows.task_flow is not task_flow:
            raise ValueError('Machine tasks require the same workflow gateway and runtime')
    if approvals is not None:
        approvals.coordinator.journal.mutation_guard = store.workflow_admission
        approvals.access.reject_service_tokens(config.tokens)
        app.include_router(approvals.router())
        if dispatcher is not None:
            from .native_approval_api import NativeApprovalAPI
            app.include_router(NativeApprovalAPI(store, dispatcher, approvals.access,
                                                service_tokens=config.tokens).router())
    if workflows is not None:
        from .work_items import WorkItems
        workflows.work_items = WorkItems(workflows.journal, projects)
        app.include_router(workflows.work_items.router(authorize))
        if authoring is not None:
            if (authoring.journal.path != workflows.journal.path
                    or any(client.sim is not workflows.client for client in authoring.clients.values())):
                raise ValueError('Published workflows require the same Sim transport and journal')
            from .published_workflows import PublishedWorkflowCatalog
            workflows.catalog = PublishedWorkflowCatalog(authoring)
        app.include_router(workflows.router(authorize))

        @app.get('/extension/v1/workflows/{binding_id}/runs/{run_id}/view')
        def fixed_run_view(binding_id: str, run_id: str, scopes: frozenset[str] = Depends(authorize)):
            from .run_view import ViewUnavailable, read_view
            try:
                return JSONResponse(content=read_view(workflows, store, binding_id, run_id, scopes),
                                    headers={'Cache-Control': 'no-store'})
            except KeyError:
                raise HTTPException(404, '固定发布版本或运行记录不可用') from None
            except (ViewUnavailable, Conflict, ValueError, TypeError, RecursionError):
                raise HTTPException(409, '无法核对固定版本运行图；请保留原运行标识，仍可查询任务目录') from None

        @app.get('/extension/v1/workflows/{binding_id}/runs/{run_id}/view/state')
        def fixed_run_state(binding_id: str, run_id: str,
                            graphSha256: str = Query(min_length=64, max_length=64, pattern=r'^[0-9a-f]{64}$'),
                            scopes: frozenset[str] = Depends(authorize)):
            from .run_view import ViewUnavailable, read_state
            try:
                return JSONResponse(content=read_state(workflows, store, binding_id, run_id, scopes, graphSha256),
                                    headers={'Cache-Control': 'no-store'})
            except KeyError:
                raise HTTPException(404, '固定运行的本地状态记录不可用') from None
            except (ViewUnavailable, Conflict, ValueError, TypeError, RecursionError):
                raise HTTPException(409, '无法核对当前图的本地任务状态；请保留原运行标识并手动核对') from None

        @app.get('/extension/v1/workflows/{binding_id}/runs/{run_id}/view/native')
        def native_run_summary(binding_id: str, run_id: str,
                               graphSha256: str = Query(min_length=64, max_length=64, pattern=r'^[0-9a-f]{64}$'),
                               scopes: frozenset[str] = Depends(authorize)):
            from .run_view import ViewUnavailable, read_native_summary
            from .adapters.sim import UpstreamFailure
            try:
                return JSONResponse(content=read_native_summary(workflows, store, binding_id, run_id, scopes, graphSha256),
                                    headers={'Cache-Control': 'no-store'})
            except KeyError:
                raise HTTPException(404, '固定运行记录不可用') from None
            except UpstreamFailure:
                raise HTTPException(502, '原生流程状态暂时无法核对；不代表运行未执行或已经停止') from None
            except (ViewUnavailable, Conflict, ValueError, TypeError, RecursionError):
                raise HTTPException(409, '无法将原生摘要对应到当前固定运行，请保留原标识核对') from None
        @app.get('/extension/v1/workflows/{binding_id}/runs/{run_id}/view/decisions')
        def condition_decisions(binding_id: str, run_id: str,
                                graphSha256: str = Query(min_length=64, max_length=64, pattern=r'^[0-9a-f]{64}$'),
                                after: str | None = Query(default=None, min_length=1, max_length=64,
                                                          pattern=r'^[A-Za-z][A-Za-z0-9_]*$'),
                                limit: int = Query(default=20, ge=1, le=20),
                                scopes: frozenset[str] = Depends(authorize)):
            from .run_view import ViewUnavailable, read_condition_decisions
            from .adapters.sim import UpstreamFailure
            try:
                return JSONResponse(content=read_condition_decisions(workflows, binding_id, run_id, scopes,
                                    graphSha256, after=after, limit=limit), headers={'Cache-Control': 'no-store'})
            except KeyError:
                raise HTTPException(404, '固定运行或条件分页记录不可用') from None
            except UpstreamFailure:
                raise HTTPException(502, '原生条件记录暂时不可核对，不代表节点未执行') from None
            except (ViewUnavailable, Conflict, ValueError, TypeError, RecursionError):
                raise HTTPException(409, '无法将条件记录对应到固定运行，请保留原标识核对') from None
    if triggers is not None:
        if workflows is None or triggers.workflows is not workflows:
            raise ValueError('Triggers require the application workflow gateway')
        app.include_router(triggers.router(authorize))
    app.state.trigger_api = triggers
    if authoring is not None:
        # Capability follows the assembled runtime, never a caller-supplied flag.
        from .approval_discovery import ApprovalDiscovery
        from .completion_worker import CompletionLoop
        from .publication import PublicationVerifier
        from .task_discovery import ExternalTaskDiscovery
        from .verification_policy import validate_policy

        def check_publication(project_id, policies):
            if (execution_state()[0] != 'confirmed' or dispatcher is None
                    or dispatcher.store.path != store.path or task_flow is None
                    or workflows is None or workflows.task_flow is not task_flow):
                return 'execution_required'
            discovery = task_flow.discovery
            if (not isinstance(discovery, ExternalTaskDiscovery)
                    or discovery.tasks.path != store.path
                    or discovery.runs.path != task_flow.runs.path
                    or discovery.pauses.path != task_flow.pauses.path
                    or discovery.client is not workflows.client
                    or discovery.wait_client.sim is not workflows.client
                    or not isinstance(discovery.publication_verifier, PublicationVerifier)
                    or discovery.publication_verifier.sim is not workflows.client
                    or discovery.journal is None or discovery.journal.path != workflows.journal.path
                    or not any(owned.loop is discovery for owned in task_flow.workers)
                    or not any(isinstance(owned.loop, CompletionLoop)
                        and owned.loop.store.path == store.path
                        and owned.loop.bindings.path == task_flow.pauses.path
                        and owned.loop.relay.client is workflows.client
                        and owned.loop.relay.journal is workflows.journal
                        for owned in task_flow.workers)):
                return 'execution_required'
            if verifier is None or dispatcher.verifier is not verifier:
                return 'verification_required'
            checked = set()
            for policy in policies.values():
                try:
                    verification = verifier.validate_policy(policy['verificationPolicy'])
                except (ValueError, TypeError, KeyError):
                    return 'verification_required'
                binding_id = policy['roleBindingId']
                from .adapters.sim import host_role_shared_across_projects
                if host_role_shared_across_projects(config.bindings, policy.get('roleAgentId'), project_id):
                    return 'role_binding_required'
                role = config.bindings.get(binding_id)
                discovered = discovery.role_bindings.get(binding_id)
                project = discovery.projects.get(project_id)
                if (not isinstance(role, dict) or not isinstance(discovered, dict)
                        or not isinstance(project, dict) or project.get('enabled', True) is not True
                        or project_id not in projects
                        or Path(project['workspace']).resolve() != Path(projects[project_id]['workspace'])
                        or any(candidate.get('enabled', True) is not True
                            or candidate.get('projectId') != project_id
                            or candidate.get('roleAgentId') != policy['roleAgentId']
                            or candidate.get('roleRevision') != policy.get('roleRevision')
                            for candidate in (role, discovered))
                        or not policy.get('roleRevision')):
                    return 'role_binding_required'
                try:
                    matching_policy = all(validate_policy(candidate.get('verificationPolicy')) == verification
                                          for candidate in (role, discovered))
                except (ValueError, TypeError):
                    matching_policy = False
                if not matching_policy:
                    return 'verification_required'
                try:
                    gateway = dispatcher._gateway({'payload': {'projectId': project_id,
                        'roleBindingId': binding_id, 'roleAgentId': policy['roleAgentId'],
                        'workspace': projects[project_id]['workspace']}})
                except Conflict:
                    return 'role_binding_required'
                if gateway is None or not callable(getattr(type(gateway), 'preflight', None)):
                    return 'execution_required'
                native_origin = getattr(getattr(gateway, '_gateway', gateway), 'base', '').removesuffix('/api')
                if policy['requireMemoryCommit'] and (memories is None
                        or memories.tasks.path != store.path or not native_origin
                        or not any(binding.get('projectId') == project_id
                            and binding.get('roleBindingId') == binding_id
                            and binding.get('roleAgentId') == policy['roleAgentId']
                            and memories.coordinators[key].role == policy['roleAgentId']
                            and memories.coordinators[key].client.origin == native_origin
                            for key, binding in memories.bindings.items())):
                    return 'memory_commit_required'
                if binding_id not in checked:
                    # No task registration/lease/delegation. An impossible task-ID
                    # prefix forces a current role GET instead of historical replay.
                    import hashlib
                    preview_id = 'publication-check-' + hashlib.sha256(
                        (project_id + ':' + binding_id + ':' + policy['roleRevision']).encode()).hexdigest()
                    try:
                        gateway.preflight(task_id=preview_id, role_revision=policy['roleRevision'])
                    except ValueError:
                        return 'role_binding_required'
                    checked.add(binding_id)
            return None

        authoring.publication_check = check_publication
        authoring.approval_enabled = False
        authoring.verification_enabled = verifier is not None
        if approvals is not None and workflows is not None:
            discovery = approvals.discovery
            if (isinstance(discovery, ApprovalDiscovery)
                    and workflows.approval_discovery is discovery
                    and discovery.client is workflows.client
                    and approvals.coordinator.client is workflows.client
                    and approvals.coordinator.journal.path == workflows.journal.path
                    and discovery.approvals.path == approvals.coordinator.ledger.path
                    and (task_flow is None or discovery.runs.path == task_flow.runs.path)):
                authoring.approval_enabled = True
        authoring.journal.mutation_guard = store.workflow_admission
        authoring.task_store = store
        # Presentation only. Never used to grant execution or publication rights.
        from .execution_runtime import BoundRoleGateway
        authoring.role_execution = {}
        if dispatcher is not None:
            for key, gateway in dispatcher.gateways.items():
                if not isinstance(gateway, BoundRoleGateway) or not isinstance(key, tuple) or len(key) != 2:
                    continue
                project_id, binding_id = key
                binding = config.bindings.get(binding_id, {})
                presented = authoring.role_bindings.get(binding_id, {})
                if (gateway.project_id != project_id or gateway.role_binding_id != binding_id
                        or binding.get('projectId') != project_id
                        or binding.get('roleAgentId') != gateway.role_agent_id
                        or presented.get('projectId') != project_id
                        or presented.get('roleAgentId') != gateway.role_agent_id
                        or presented.get('roleRevision') != gateway.role_revision
                        or binding.get('roleRevision') != gateway.role_revision
                        or project_id not in projects or project_id not in authoring.clients
                        or gateway.workspace != projects[project_id]['workspace']):
                    continue
                authoring.role_execution[key] = {
                    'source': 'native_binding_descriptor_at_startup',
                    'executorAgentId': gateway.executor_agent_id,
                    'sandbox': 'read-only' if gateway.execution_controls == ('read-only', 'never') else None,
                    'approvalPolicy': 'never' if gateway.execution_controls == ('read-only', 'never') else None,
                }
        authoring.access.reject_service_tokens(set(config.tokens) | {client.sim._key for client in authoring.clients.values()})
        app.include_router(authoring.router())
    app.state.authoring_api = authoring
    return app
