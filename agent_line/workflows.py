"""Authenticated workflow boundary; Sim remains the sole graph scheduler."""

import copy
import sqlite3
from contextlib import nullcontext
import uuid
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from .store import AdmissionClosed, Conflict, canonical
from .adapters.sim import UpstreamFailure, segment
from .sim_journal import RunInputTooLarge
from .sim_input import require_sim_input
from .workflow_archive import execution_scope as archive_execution_scope


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    idempotencyKey: str = Field(min_length=1, max_length=256)
    input: dict
    workItemId: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,128}$")


class ResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    contextId: str = Field(min_length=1, max_length=160)
    input: dict


class ControlRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    operationId: str = Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')
    action: str = Field(pattern=r'^(pause|continue|stop)$')
    expectedVersion: int = Field(ge=0, le=2147483647)
    activeExecution: str | None = Field(default=None, pattern=r'^(continue|interrupt)$')


class WorkflowGateway:
    def __init__(self, client, journal, bindings, *, installation_id):
        if not isinstance(installation_id, str) or not installation_id.strip():
            raise ValueError("Stable installation identity required")
        self.client, self.journal = client, journal
        self.bindings = copy.deepcopy(bindings)
        self.installation_id = installation_id
        self.approval_discovery = None
        self.publication_verifier = None
        self.task_flow = None
        self.catalog = None
        self.run_control = None
        self.dispatcher = None
        self.verifier = None
        self.work_items = None
        ownership = {}
        for binding in self.bindings.values():
            segment(binding["workflowId"])
            if (
                not isinstance(binding.get("projectId"), str)
                or not binding["projectId"]
            ):
                raise ValueError("Workflow project binding required")
            policy = binding.get("humanApproval")
            machine = binding.get('externalTasks')
            if machine is not None:
                if (not isinstance(machine, dict) or not {
                        'revision', 'deploymentVersionId', 'agentPolicies', 'ttlSeconds'} <= set(machine) <= {
                        'revision', 'deploymentVersionId', 'agentPolicies', 'ttlSeconds', 'loopPolicies', 'staticParallel',
                        'subworkflowDepthLimit'}
                        or not isinstance(machine['revision'], str) or not machine['revision'].isdigit()
                        or int(machine['revision']) < 1 or not isinstance(machine['agentPolicies'], dict)
                        or not machine['agentPolicies'] or type(machine['ttlSeconds']) is not int
                        or not 1 <= machine['ttlSeconds'] <= 31536000):
                    raise ValueError('Invalid trusted machine-task policy')
                if ('subworkflowDepthLimit' in machine
                        and (type(machine['subworkflowDepthLimit']) is not int
                             or machine['subworkflowDepthLimit'] != 1)):
                    raise ValueError('Invalid subworkflow depth limit')
                segment(machine['deploymentVersionId'])
                for block_id in machine['agentPolicies']: segment(block_id)
                if ('staticParallel' in machine and (machine['staticParallel'] is not True
                        or 'loopPolicies' in machine)):
                    raise ValueError('Invalid explicit static parallel policy')
                if 'loopPolicies' in machine:
                    from .approval_discovery import validate_loop_policies
                    validate_loop_policies(machine['loopPolicies'], machine['agentPolicies'])
            if policy is not None:
                from .approval_discovery import validate_human_policy
                validate_human_policy(policy, **({'revision': machine['revision'],
                    'deployment': machine['deploymentVersionId'], 'agent_policies': machine['agentPolicies']}
                    if machine is not None else {}))
            owner = ownership.setdefault(binding["workflowId"], binding["projectId"])
            if owner != binding["projectId"]:
                raise ValueError("A native workflow cannot belong to multiple projects")

    def available(self, scopes):
        bindings = {key: value for key, value in self.bindings.items() if value['projectId'] in scopes}
        if self.catalog is not None:
            try:
                published = self.catalog.bindings(scopes)
                for key, binding in published.items():
                    if key in self.bindings:
                        raise ValueError('Published binding collides with static configuration')
                    if any(other['workflowId'] == binding['workflowId'] and other['projectId'] != binding['projectId']
                           for other in self.bindings.values()):
                        raise ValueError('Published workflow belongs to another project')
                bindings.update(published)
            except (ValueError, TypeError, KeyError, sqlite3.Error):
                raise HTTPException(503, '已发布流程记录无法核实，请核对配置与操作日志') from None
        return bindings

    def binding_for(self, binding_id, scopes):
        binding = self.available(scopes).get(binding_id)
        if not binding or binding["projectId"] not in scopes:
            raise HTTPException(404, "工作流不存在")
        return binding

    @staticmethod
    def publication_snapshot(binding_id, binding):
        return {'bindingId': binding_id, **binding}

    @staticmethod
    def receipt(result):
        # Native response data can include internal details; expose identities
        # and submission state only. Query Sim to obtain execution state.
        return {
            key: result[key]
            for key in ("runId", "contextId", "status", "safeToResubmit")
        } | {"verifiedComplete": False}

    def run_id_for(self, binding_id, binding, idempotency_key):
        identity = canonical([self.installation_id, binding['projectId'], binding['workflowId'], idempotency_key]
                             + ([binding_id] if 'publication' in binding else []))
        return str(uuid.uuid5(uuid.NAMESPACE_URL, identity))

    def start(self, binding_id, body: StartRequest, scopes):
        """Submit through the same durable boundary for manual and trusted triggers."""
        binding = self.binding_for(binding_id, scopes)
        work_item = None
        if body.workItemId is not None:
            if self.work_items is None:
                raise HTTPException(503, '业务任务登记尚未配置，未启动流程')
            try:
                item = self.work_items.get(binding['projectId'], body.workItemId)
            except KeyError:
                raise HTTPException(404, '此项目的业务任务不存在') from None
            except Conflict:
                raise HTTPException(409, '业务任务定义暂时无法核对，未启动流程') from None
            from .work_items import task_context
            try:
                combined = canonical({'input': body.input, 'workItem': task_context(item)})
                if len(combined) > 16000:
                    raise ValueError
            except (ValueError, RecursionError):
                raise HTTPException(422, '业务目标与输入无效或超过上下文上限，请缩短后启动') from None
            work_item = {'projectId': binding['projectId'], 'bindingId': binding_id,
                         'workItemId': item['workItemId'], 'definitionSha256': item['definitionSha256']}
        run_id = self.run_id_for(binding_id, binding, body.idempotencyKey)
        try:
            try:
                self.journal.get(binding["workflowId"], run_id)
                existing = True
            except KeyError:
                existing = False
            with (self.journal.mutation_scope() if not existing else nullcontext()), (
                    (self.catalog.execution_scope() if self.catalog is not None else archive_execution_scope(self.journal)) if not existing else nullcontext()):
                if not existing:
                    # A same-key request may have completed while this request
                    # waited for the shared lifecycle lock. Replays remain reads.
                    try:
                        self.journal.get(binding['workflowId'], run_id)
                        existing = True
                    except KeyError:
                        pass
                if not existing:
                    require_sim_input(body.input)
                    from .workflow_archive import record as archive_record
                    if archive_record(self.journal, binding['projectId'], binding['workflowId']) is not None:
                        raise HTTPException(409, '流程已归档或归档结果待核对，未启动新运行')
                if 'publication' in binding and not existing:
                    # Another request may have completed while this one waited
                    # for our shared publisher lock. Observe its original intent.
                    try:
                        self.journal.get(binding['workflowId'], run_id)
                        existing = True
                    except KeyError:
                        pass
                if 'publication' in binding:
                    if existing:
                        try:
                            frozen_publication = self.journal.get_publication(binding['workflowId'], run_id)
                        except KeyError:
                            raise Conflict('Existing run has no frozen publication') from None
                        if frozen_publication != self.publication_snapshot(binding_id, binding):
                            raise Conflict('Existing run belongs to another publication')
                    else:
                        from .sim_authoring import AuthoringError
                        try:
                            self.catalog.check_active(binding)
                        except AuthoringError:
                            raise HTTPException(503, '此发布版本不再活动或无法核实，请选择当前版本；未启动流程') from None
                policy = binding.get("humanApproval")
                machine = binding.get('externalTasks')
                if policy is not None and machine is not None:
                    from .approval_discovery import validate_human_policy
                    validate_human_policy(policy, revision=machine['revision'],
                        deployment=machine['deploymentVersionId'], agent_policies=machine['agentPolicies'])
                    if (self.approval_discovery is None or self.task_flow is None
                            or self.approval_discovery.runs.path != self.task_flow.runs.path):
                        raise HTTPException(503, '混合流程需要共享运行记录的人工审批与任务运行时，未启动流程')
                if machine is not None:
                    if (not existing and self.verifier is None
                            and any('verificationPolicy' in policy for policy in machine['agentPolicies'].values())):
                        raise HTTPException(503, '冻结的自动验收策略尚未配置验证器，未启动流程')
                    if self.task_flow is None or self.publication_verifier is None:
                        raise HTTPException(503, '机器任务发现与回传尚未配置，未启动流程')
                    from .publication import PublicationUnverified
                    if not existing:
                        discovery = getattr(self.task_flow, 'discovery', None)
                        if discovery is None or getattr(discovery, 'allow_submissions', False) is not True:
                            raise HTTPException(503, '受控执行尚未准入，未启动机器任务流程')
                        readiness = getattr(discovery, 'execution_readiness', None)
                        if readiness is not None:
                            try:
                                readiness.assert_current()
                            except ValueError:
                                raise HTTPException(503, '执行验收证明已失效，未启动机器任务流程') from None
                    try:
                        self.publication_verifier.check_definition(
                            binding['workflowId'], machine['revision'], machine['deploymentVersionId'],
                            (sorted(set(machine['agentPolicies']) | set(policy['blockIds']))
                             if policy is not None else list(machine['agentPolicies'])), require_active=not existing,
                            **({'static_parallel': machine['staticParallel']} if 'staticParallel' in machine else {}),
                            **({'loop_policies': machine['loopPolicies']} if 'loopPolicies' in machine else {}),
                            **({'subworkflow_depth_limit': machine['subworkflowDepthLimit']}
                               if 'subworkflowDepthLimit' in machine else {}))
                    except PublicationUnverified:
                        raise HTTPException(503, '机器任务发布版本尚未核实，未启动流程') from None
                    from .run_budget import validate_run_budget
                    if not existing:
                        try:
                            frozen_run = self.task_flow.runs.get(binding['workflowId'], run_id)
                        except KeyError:
                            discovery = getattr(self.task_flow, 'discovery', None)
                            budget = validate_run_budget(
                                getattr(discovery, 'projects', {}).get(binding['projectId'], {}).get('runBudget'))
                            if 'subworkflowDepthLimit' in machine and budget is None:
                                raise HTTPException(503, '子流程必须配置父运行预算，未启动流程')
                        else:
                            if (frozen_run.get('projectId') != binding['projectId']
                                    or frozen_run.get('workflowId') != binding['workflowId']
                                    or frozen_run.get('runId') != run_id):
                                raise Conflict('Existing run has another budget owner')
                            budget = frozen_run.get('runBudget')
                            if 'subworkflowDepthLimit' in machine:
                                budget = validate_run_budget(budget)
                                if budget is None:
                                    raise Conflict('Existing subworkflow run has no frozen budget')
                        self.task_flow.runs.reserve_tasks(
                            project_id=binding['projectId'], workflow_id=binding['workflowId'], run_id=run_id,
                            revision=machine['revision'], deployment_version_id=machine['deploymentVersionId'],
                            agent_policies=machine['agentPolicies'], ttl_seconds=machine['ttlSeconds'],
                            **({'run_budget': budget} if budget is not None else {}),
                            **({'subworkflow_depth_limit': machine['subworkflowDepthLimit']}
                               if 'subworkflowDepthLimit' in machine else {}),
                            **({'static_parallel': machine['staticParallel']} if 'staticParallel' in machine else {}),
                            **({'human_approval': policy} if policy is not None else {}),
                            **({'loop_policies': machine['loopPolicies']} if 'loopPolicies' in machine else {}))
                    else:
                        try:
                            frozen = self.task_flow.runs.get(binding['workflowId'], run_id)
                        except KeyError:
                            raise Conflict('Existing run has no frozen machine-task policy') from None
                        frozen_budget = (validate_run_budget(frozen.get('runBudget'))
                                         if 'subworkflowDepthLimit' in machine else None)
                        if (frozen.get('purpose') != 'external_task_result' or frozen['revision'] != machine['revision']
                                or frozen['deploymentVersionId'] != machine['deploymentVersionId']
                                or frozen['ttlSeconds'] != machine['ttlSeconds']
                                or frozen.get('agentPolicies') != machine['agentPolicies']
                                or frozen.get('loopPolicies') != machine.get('loopPolicies')
                                or frozen.get('subworkflowDepthLimit') != machine.get('subworkflowDepthLimit')
                                or ('subworkflowDepthLimit' in machine
                                    and frozen_budget is None)
                                or frozen.get('staticParallel', False) != machine.get('staticParallel', False)
                                or frozen.get('approvalBlockIds') != (sorted(policy['blockIds']) if policy is not None else None)
                                or frozen.get('approvalTtlSeconds') != (policy['ttlSeconds'] if policy is not None else None)
                                or frozen.get('approvalTtlByBlock') != (policy.get('ttlByBlock') if policy is not None else None)
                                or frozen.get('approvalQuestions') != (policy.get('questionsByBlock') if policy is not None else None)):
                            raise Conflict('Existing run belongs to a different machine-task policy')
                if policy is not None and machine is None and not existing:
                    if self.approval_discovery is None or self.publication_verifier is None:
                        raise HTTPException(503, "人工审批发现尚未配置，未启动流程")
                    from .publication import PublicationUnverified
                    try:
                        self.publication_verifier.check_definition(
                            binding["workflowId"], policy["revision"],
                            policy["deploymentVersionId"], policy["blockIds"], require_active=True)
                    except PublicationUnverified:
                        raise HTTPException(503, "发布版本或人工节点尚未核实，未启动流程") from None
                    self.approval_discovery.runs.reserve(
                        project_id=binding["projectId"], workflow_id=binding["workflowId"],
                        run_id=run_id, revision=policy["revision"],
                        deployment_version_id=policy["deploymentVersionId"],
                        human_block_ids=policy["blockIds"], ttl_seconds=policy["ttlSeconds"],
                        **({'approval_ttls': policy['ttlByBlock']} if 'ttlByBlock' in policy else {}),
                        **({'approval_questions': policy['questionsByBlock']} if 'questionsByBlock' in policy else {}))
                if 'publication' in binding and not existing:
                    self.journal.reserve_publication(binding['workflowId'], run_id, self.publication_snapshot(binding_id, binding))
                result = self.journal.execute(self.client, binding["workflowId"], run_id, body.input,
                    **({"work_item": work_item} if work_item is not None else {}))
                if machine is not None:
                    self.task_flow.runs.record_submission(binding['workflowId'], run_id,
                        result['status'] if result['status'] in ('accepted', 'rejected') else 'uncertain')
                if self.approval_discovery is not None:
                    try:
                        self.approval_discovery.runs.get(binding["workflowId"], run_id)
                    except KeyError:
                        pass
                    else:
                        self.approval_discovery.runs.record_submission(
                            binding["workflowId"], run_id,
                            result["status"] if result["status"] in ('accepted', 'rejected') else 'uncertain')
            response = self.receipt(result)
            if work_item is not None:
                response['workItem'] = item
            return response
        except AdmissionClosed:
            raise HTTPException(503, "维护或恢复核对中，暂不提交新的工作流操作") from None
        except Conflict:
            raise HTTPException(409, "同一触发标识的输入、业务任务或发布关联已改变") from None
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise HTTPException(422, "输入无效：请使用有限安全数值、有效文本和不超过64层的JSON；大整数编号请改为文本") from None

    def router(self, authorize):
        router = APIRouter(prefix="/extension/v1/workflows")

        available = self.available
        binding_for = self.binding_for
        snapshot = self.publication_snapshot
        receipt = self.receipt

        def known_run(binding_id, binding, run_id):
            try:
                segment(run_id)
                if 'publication' in binding and self.journal.get_publication(binding['workflowId'], run_id) != snapshot(binding_id, binding):
                    raise KeyError(run_id)
                return self.journal.get(binding["workflowId"], run_id)
            except (KeyError, ValueError):
                raise HTTPException(404, "工作流运行不存在") from None

        def frozen_run(binding, run_id):
            for runtime in (self.task_flow, self.approval_discovery):
                if runtime is None:
                    continue
                try:
                    frozen = runtime.runs.get(binding['workflowId'], run_id)
                except KeyError:
                    continue
                if frozen['projectId'] != binding['projectId']:
                    raise HTTPException(404, '工作流运行不存在')
                return frozen
            return None

        def control_summary(binding, run_id):
            if self.run_control is None or frozen_run(binding, run_id) is None:
                return {'supported': False}
            tasks = self.run_control.run_tasks(binding['projectId'], binding['workflowId'], run_id)
            counts = {'total': len(tasks), 'queued': 0, 'active': 0, 'uncertain': 0,
                      'resourcesUnreleased': 0, 'verificationPending': 0,
                      'verificationCancelRequested': 0, 'verificationCanceled': 0}
            for task in tasks:
                status = task['executionStatus']
                if status == 'registered':
                    counts['queued'] += 1
                if status in ('accepted', 'cancel_requested', 'submitting', 'uncertain'):
                    counts['active'] += 1
                if status in ('submitting', 'uncertain'):
                    counts['uncertain'] += 1
                if status != 'registered' and task['resourcesReleased'] is not True:
                    counts['resourcesUnreleased'] += 1
                policy = task['payload'].get('verificationPolicy', {})
                if policy.get('schemaVersion') == 2 and policy.get('requireTests') is True:
                    if status == 'succeeded' and task['verificationStatus'] == 'pending':
                        counts['verificationPending'] += 1
                        if task.get('cancellationRequested'):
                            counts['verificationCancelRequested'] += 1
                    if task['verificationStatus'] == 'canceled':
                        counts['verificationCanceled'] += 1
            return {**self.run_control.status(binding['workflowId'], run_id), 'supported': True, 'taskCounts': counts}

        def can_continue(binding, run_id):
            local = self.journal.get(binding['workflowId'], run_id)
            if local['status'] != 'accepted':
                return False
            resume = self.journal.resume_summary(binding['workflowId'], run_id)
            if resume is not None and resume['latestAttempt']['status'] != 'accepted':
                return False
            frozen = frozen_run(binding, run_id)
            if frozen is None or self.publication_verifier is None:
                return False
            try:
                if self.publication_verifier.verify_run(frozen) is not True:
                    return False
                observed = self.client.observe(binding['workflowId'], run_id)
                data = observed.get('response', {}).get('data', {})
                pause = data.get('paused') or {}
                points, resumed = pause.get('pausePointCount'), pause.get('resumedCount', 0)
                if (data.get('workflowId') != binding['workflowId'] or data.get('runId') != run_id
                        or data.get('status') != 'paused' or pause.get('pauseKind') != 'human'
                        or type(points) is not int or type(resumed) is not int
                        or points < 1 or resumed < 0 or points <= resumed):
                    return False
                if frozen.get('staticParallel') is True:
                    from .adapters.sim_wait import SimWaitClient
                    details = SimWaitClient(self.client).read_pauses(binding['workflowId'], run_id)
                    if (details['totalPauseCount'] != points or details['resumedCount'] != resumed
                            or any(p['blockId'] not in frozen['humanBlockIds']
                                   or p['resumeStatus'] not in ('paused', 'resumed')
                                   or (p['resumeStatus'] == 'paused' and p['snapshotReady'] is not True)
                                   for p in details['pausePoints'])
                            or not any(p['resumeStatus'] == 'paused' for p in details['pausePoints'])):
                        return False
                elif (points-resumed != 1 or pause.get('blockedOnBlockId') not in frozen['humanBlockIds']
                        or not isinstance(pause.get('contextId'), str)):
                    return False
                tasks = self.run_control.run_tasks(binding['projectId'], binding['workflowId'], run_id)
                for task in tasks:
                    if self.run_control.tasks.recorded_approval_blocks_continue(task['id']):
                        return False
                    if (task.get('cancellationRequested') or task['executionStatus'] in ('submitting', 'uncertain', 'cancel_requested', 'canceled')):
                        return False
                    if task['executionStatus'] == 'registered':
                        continue
                    if task['executionStatus'] in ('succeeded', 'failed') and task['resourcesReleased'] is True:
                        continue
                    if self.dispatcher is None or self.dispatcher.verify_continue(task['id']) is not True:
                        return False
                return True
            except Exception:
                return False

        @router.post('/{binding_id}/runs/{run_id}/control')
        def control_run(binding_id: str, run_id: str, body: ControlRequest, scopes=Depends(authorize)):
            binding = binding_for(binding_id, scopes)
            known_run(binding_id, binding, run_id)
            if self.run_control is None or frozen_run(binding, run_id) is None:
                raise HTTPException(409, '此流程尚未接入受控等待，不能保证暂停后继准入')
            try:
                operation = self.run_control.apply(project_id=binding['projectId'], workflow_id=binding['workflowId'],
                    run_id=run_id, action=body.action, operation_id=body.operationId,
                    expected_version=body.expectedVersion, active_execution=body.activeExecution,
                    validate_continue=lambda: can_continue(binding, run_id))
                result = {'operation': operation}
                if body.action == 'stop':
                    try:
                        canceled = self.run_control.reconcile_stop(binding['projectId'], binding['workflowId'], run_id, self.client)
                        result['cancelSubmission'] = {'status': canceled['status'], 'verifiedStopped': False,
                            'safeToResubmit': False}
                    except AdmissionClosed:
                        result['cancelSubmission'] = {'status': 'guarded', 'verifiedStopped': False,
                            'safeToResubmit': False}
                result['control'] = control_summary(binding, run_id)
                return result
            except Conflict:
                raise HTTPException(409, '流程控制版本已变化、已申请停止或继续前核对未通过；请查询原运行') from None
            except ValueError:
                raise HTTPException(422, '流程控制请求无效') from None

        @router.get("")
        def list_bindings(scopes=Depends(authorize)):
            return {
                "workflows": [
                    {"bindingId": binding_id, "projectId": binding["projectId"],
                     "workflowId": binding["workflowId"],
                     **({'revision': binding['publication']['revision']} if 'publication' in binding else {})}
                    for binding_id, binding in sorted(available(scopes).items())
                    if binding["projectId"] in scopes
                ]
            }

        @router.post("/{binding_id}/runs", status_code=202)
        def start(binding_id: str, body: StartRequest, scopes=Depends(authorize)):
            return self.start(binding_id, body, scopes)

        @router.get('/{binding_id}/runs')
        def list_runs(binding_id: str, after: str | None = Query(default=None, max_length=160),
                      limit: int = Query(default=50, ge=1, le=100), scopes=Depends(authorize)):
            binding = binding_for(binding_id, scopes)
            try:
                page = self.journal.list_runs(binding['workflowId'], after=after, limit=limit,
                    publication=snapshot(binding_id, binding) if 'publication' in binding else None)
            except KeyError:
                raise HTTPException(404, '运行目录游标不存在或不属于当前绑定') from None
            except ValueError:
                raise HTTPException(422, '运行目录游标或分页参数无效') from None
            except Conflict:
                raise HTTPException(503, '运行目录记录暂时无法核实，请保留原运行标识') from None
            return {'bindingId': binding_id, 'projectId': binding['projectId'],
                    'workflowId': binding['workflowId'], 'source': 'local_submission_journal',
                    'executionStateIncluded': False, **page}

        @router.get('/{binding_id}/runs/{run_id}/input')
        def read_run_input(binding_id: str, run_id: str, scopes=Depends(authorize)):
            binding = binding_for(binding_id, scopes)
            try:
                known_run(binding_id, binding, run_id)
                input_value, input_hash = self.journal.read_run_input(binding['workflowId'], run_id)
            except HTTPException:
                raise
            except KeyError:
                raise HTTPException(404, '工作流运行不存在') from None
            except RunInputTooLarge:
                raise HTTPException(413, '原运行输入超过读取上限') from None
            except (sqlite3.Error, TypeError, ValueError, UnicodeError, RecursionError):
                raise HTTPException(503, '原运行输入暂时无法核实') from None

            try:
                current = binding_for(binding_id, scopes)
            except HTTPException as exc:
                if exc.status_code == 404:
                    raise HTTPException(409, '读取期间工作流绑定已变化，请重新读取') from None
                raise
            if current != binding:
                raise HTTPException(409, '读取期间工作流绑定已变化，请重新读取')
            return JSONResponse(
                content={'source': 'local_run_input', 'readOnly': True,
                         'executionSupported': False, 'bindingId': binding_id,
                         'projectId': binding['projectId'], 'workflowId': binding['workflowId'],
                         'runId': run_id, 'inputSha256': input_hash, 'input': input_value},
                headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'},
            )

        @router.get("/{binding_id}/runs/{run_id}")
        def observe(binding_id: str, run_id: str, scopes=Depends(authorize)):
            binding = binding_for(binding_id, scopes)
            local = known_run(binding_id, binding, run_id)
            try:
                native = self.client.observe(binding["workflowId"], run_id)
            except UpstreamFailure:
                raise HTTPException(502, "上游工作流查询失败") from None
            result = {"submission": receipt(local), "execution": native}
            from .work_items import run_item
            try:
                item = run_item(self.journal, binding['projectId'], binding['workflowId'], run_id, binding_id=binding_id)
            except (KeyError, Conflict):
                raise HTTPException(409, '业务任务与原运行的关联暂时无法核对') from None
            if item is not None:
                result['workItem'] = item
            if self.run_control is not None:
                result['usage'] = {'bindingId': binding_id, **self.run_control.tasks.run_usage(
                    binding['projectId'], binding['workflowId'], run_id)}
                result['budget'] = {'bindingId': binding_id, **self.run_control.tasks.run_budget_summary(
                    binding['projectId'], binding['workflowId'], run_id, frozen_run=frozen_run(binding, run_id))}
                result['health'] = {'bindingId': binding_id, **self.run_control.tasks.run_health(
                    binding['projectId'], binding['workflowId'], run_id)}
            try:
                result['control'] = control_summary(binding, run_id)
            except Conflict:
                raise HTTPException(503, '流程控制记录无法核对，请保留原运行标识') from None
            try:
                resumption = self.journal.resume_summary(binding['workflowId'], run_id)
            except (sqlite3.Error, ValueError, TypeError):
                raise HTTPException(503, '恢复执行记录不可用，请保留原运行标识核对') from None
            if resumption is not None:
                result['resumeSubmission'] = resumption['latestAttempt']
                accepted = resumption['latestAccepted']
                if accepted is not None:
                    child_id = accepted['runId']
                    child = {'runId': child_id, 'status': 'unknown', 'safeToResubmit': False}
                    if child_id is not None:
                        try:
                            child = self.client.observe(binding['workflowId'], child_id)
                        except UpstreamFailure:
                            # Keep the readable parent and accepted identity;
                            # a child read failure never authorizes resubmission.
                            pass
                    result['resumption'] = {'contextId': accepted['contextId'], 'runId': child_id,
                                            'submissionStatus': accepted['status'], 'execution': child}
            if self.approval_discovery is not None:
                try:
                    snapshot = self.approval_discovery.runs.get(binding["workflowId"], run_id)
                    result["approvalRegistration"] = {key: snapshot.get(key) for key in
                        ("registrationStatus", "registrationReason")}
                except KeyError:
                    pass
            return result

        @router.get("/{binding_id}/runs/{run_id}/health")
        def local_run_health(binding_id: str, run_id: str, scopes=Depends(authorize)):
            binding = binding_for(binding_id, scopes)
            known_run(binding_id, binding, run_id)
            if self.run_control is None:
                raise HTTPException(503, '本地运行健康摘要不可用')
            summary = self.run_control.tasks.run_health(
                binding['projectId'], binding['workflowId'], run_id)
            return JSONResponse(
                content={'source': 'local_run_health', 'readOnly': True,
                         'bindingId': binding_id, **summary},
                headers={'Cache-Control': 'no-store'},
            )

        @router.post("/{binding_id}/runs/{run_id}/resume", status_code=202)
        def resume(
            binding_id: str, run_id: str, body: ResumeRequest, scopes=Depends(authorize)
        ):
            binding = binding_for(binding_id, scopes)
            known_run(binding_id, binding, run_id)
            protected = binding.get("humanApproval") is not None or binding.get('externalTasks') is not None
            if self.task_flow is not None:
                try:
                    protected = protected or bool(self.task_flow.runs.get(binding['workflowId'], run_id)['humanBlockIds'])
                except KeyError:
                    pass
            if self.approval_discovery is not None:
                try:
                    snapshot = self.approval_discovery.runs.get(binding["workflowId"], run_id)
                    protected = protected or bool(snapshot["humanBlockIds"])
                except KeyError:
                    pass
            if protected:
                raise HTTPException(403, "此流程的暂停由专用审批或任务回传接口处理")
            try:
                return receipt(
                    self.journal.resume(
                        self.client,
                        binding["workflowId"],
                        run_id,
                        body.contextId,
                        body.input,
                    )
                )
            except AdmissionClosed:
                raise HTTPException(503, "维护或恢复核对中，暂不提交新的工作流操作") from None
            except Conflict:
                raise HTTPException(409, "同一恢复上下文的输入已改变") from None
            except (ValueError, TypeError, UnicodeError, RecursionError):
                raise HTTPException(422, "恢复上下文或输入无效；数值需在安全范围，大整数编号请改为文本") from None

        return router
