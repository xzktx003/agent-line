"""Small project-scoped editor boundary; Sim owns all drafts and deployments.

The existing outbound journal records mutation intent, ownership and read-only
pre-archive audit evidence. Sim owns editable drafts/deployments. A native
workspace alone does not confer project ownership.
"""
import hashlib
import fcntl
import json
import os
import re
import sqlite3
import stat
from typing import Literal
from contextlib import contextmanager

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from .approval_api import ApprovalAccess
from .approvals import TrustedPrincipal
from .adapters.sim import host_role_shared_across_projects, segment
from .sim_authoring import AuthoringError, UnsupportedDocument, compile_document, decode_document, is_initial_state, native_id
from .store import AdmissionClosed, Conflict, canonical
from .team_view import CorruptTeamJournal
from .workflow_template import export_template, import_template
from .static_batch import build_static_batch


class AuthoringAccess(ApprovalAccess):
    def authorize(self, principal):
        with self._lock:
            return (isinstance(principal, TrustedPrincipal)
                    and principal.expires_at > self.clock()
                    and 'edit_workflows' in principal.capabilities
                    and principal in self._credentials.values())

    def authenticate(self, authorization):
        try:
            return super().authenticate(authorization)
        except HTTPException as exc:
            raise HTTPException(exc.status_code, '需要有效且未过期的流程编辑凭证', headers=exc.headers) from None


class CreateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    operationId: str = Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')
    name: str = Field(min_length=1, max_length=255)
    description: str = Field(default='', max_length=5000)


class PublishRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    operationId: str = Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')
    expectedStateSha256: str = Field(pattern=r'^[0-9a-f]{64}$')


class NativeAgentBinding(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    roleBindingId: str = Field(pattern=r'^[A-Za-z0-9_-]{1,160}$')
    instruction: str = Field(min_length=1, max_length=4000, pattern=r'\S')
    requireMemoryCommit: bool = False
    allowFailureBranch: bool = False


class NativePublishRequest(PublishRequest):
    nativeBindings: dict[str, NativeAgentBinding] = Field(max_length=200)
    humanBlockIds: list[str] = Field(default_factory=list, max_length=200)


class SaveRequest(PublishRequest):
    document: dict


class ExportTemplateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    document: dict


class ImportTemplateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    template: dict
    roleBindings: dict


class StaticBatchPreviewRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    recipeId: str = Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')
    roleBindingId: str = Field(pattern=r'^[A-Za-z0-9_-]{1,160}$')
    instruction: str = Field(min_length=1, max_length=2000)
    items: list[object] = Field(min_length=2, max_length=20)
    policy: Literal['all_success', 'allow_partial']


class SubworkflowReferenceRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    parentWorkflowId: str = Field(pattern=r'^[A-Za-z0-9_-]{1,160}$')
    reference: dict


class SubworkflowExpansionRequest(SubworkflowReferenceRequest):
    expectedParentStateSha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    callId: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_]{0,63}$')
    edgeId: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_]{0,63}$')
    inputFrom: dict
    inputMapping: dict | None = None
    outputMapping: dict | None = None


class TeamEdge(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    sourceAgentId: str = Field(pattern=r'^[A-Za-z0-9_-]{1,160}$')
    targetAgentId: str = Field(pattern=r'^[A-Za-z0-9_-]{1,160}$')


class TeamUpdateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    operationId: str = Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')
    expectedRevision: int = Field(ge=0)
    edges: list[TeamEdge] = Field(max_length=200)


def state_hash(state):
    return hashlib.sha256(canonical(state).encode()).hexdigest()


def namespace(project):
    return 'authoring-' + hashlib.sha256(project.encode()).hexdigest()


def project_native_waits(state):
    """Expose native human-wait identity only. Block values stay in Sim."""
    if not isinstance(state, dict) or not isinstance(state.get('blocks'), dict):
        return None
    waits = []
    for key, block in state['blocks'].items():
        if not isinstance(block, dict) or block.get('type') != 'human_in_the_loop_v2':
            continue
        try:
            segment(key)
        except ValueError:
            return None
        if len(waits) >= 200:
            return None
        name = block.get('name')
        if (not isinstance(name, str) or not name.strip() or len(name) > 120
                or '\0' in name or any(ord(char) < 32 for char in name)):
            name = key
        task_input = None
        subblocks = block.get('subBlocks')
        form = subblocks.get('inputFormat') if isinstance(subblocks, dict) else None
        fields = form.get('value') if isinstance(form, dict) else None
        if isinstance(fields, list):
            matches = [field for field in fields if isinstance(field, dict)
                       and isinstance(field.get('name'), str) and field['name'].strip() == 'taskInput']
            if len(matches) == 1 and matches[0].get('type') in ('object', 'string'):
                task_input = matches[0]['type']
        waits.append({'id': key, 'name': name, 'taskInput': task_input})
    waits.sort(key=lambda item: item['id'])
    return waits


def require_portable_document(document):
    if (isinstance(document, dict) and isinstance(document.get('nodes'), list)
            and any(isinstance(node, dict) and node.get('type') in ('subflow_input', 'subflow_output')
                    for node in document['nodes'])):
        raise AuthoringError('subworkflow_portable_template_unsupported')


def subworkflow_admission(authoring, project, workflow, document):
    from .subworkflow_admission import validate_admission
    try:
        return validate_admission(authoring, project, workflow, document), None
    except AuthoringError as error:
        reasons = {'subworkflow_budget_required', 'subworkflow_agent_required',
                   'subworkflow_source_unpublished', 'subworkflow_source_archived',
                   'subworkflow_role_changed', 'subworkflow_source_changed',
                   'subworkflow_reference_invalid', 'subworkflow_version_mismatch'}
        return None, error.code if error.code in reasons else 'subworkflow_source_invalid'


class AuthoringAPI:
    def __init__(self, access, journal, clients, *, projects, role_bindings=None, approval_enabled=False, verification_enabled=False,
                 publication_check=None, task_store=None):
        if type(approval_enabled) is not bool:
            raise ValueError('Approval capability must be an explicit boolean')
        if type(verification_enabled) is not bool:
            raise ValueError('Verification capability must be an explicit boolean')
        self.verification_enabled = verification_enabled
        self.approval_enabled = approval_enabled
        # Injected only by the server's assembled execution context, never HTTP.
        self.publication_check = publication_check
        self.access, self.journal = access, journal
        self.clients = dict(clients)
        self.projects = {key: dict(projects[key]) for key in clients}
        self.role_bindings = dict(role_bindings or {})
        self.task_store = task_store
        self.role_execution = {}  # Display-only startup metadata; never authority.
        for key in clients:
            segment(key)
            if self.projects[key].get('enabled', True) is not True:
                raise ValueError('Authoring requires enabled projects')

    def _role_summary(self, project, binding_id, role):
        def text(value):
            return value if isinstance(value, str) and value.strip() and len(value) <= 255 and '\0' not in value else None

        revision = role.get('roleRevision')
        from .role_revision import validate_role_revision
        try:
            revision = validate_role_revision(revision)
        except ValueError:
            revision = None
        verification = {'state': 'unavailable', 'policyId': None, 'requireTests': None,
                        'artifactCount': None, 'testCount': None}
        if 'verificationPolicy' in role:
            from .verification_policy import validate_policy
            try:
                policy = validate_policy(role['verificationPolicy'])
            except (ValueError, TypeError, KeyError):
                verification['state'] = 'invalid'
            else:
                verification = {'state': 'configured', 'policyId': policy['policyId'],
                                'requireTests': policy['requireTests'],
                                'artifactCount': len(policy['artifacts']),
                                'testCount': len(policy.get('tests', []))}
        execution = {'source': 'unavailable', 'backend': None, 'executorAgentId': None,
                     'sandbox': None, 'approvalPolicy': None, 'modelId': None,
                     'workspace': 'project_registered'}
        snapshot = self.role_execution.get((project, binding_id))
        if isinstance(snapshot, dict) and snapshot.get('source') == 'native_binding_descriptor_at_startup':
            execution.update(source='native_binding_descriptor_at_startup', backend='codex',
                             executorAgentId=text(snapshot.get('executorAgentId')))
            if snapshot.get('sandbox') == 'read-only' and snapshot.get('approvalPolicy') == 'never':
                execution.update(sandbox='read-only', approvalPolicy='never')
        return {'roleBindingId': binding_id, 'roleAgentId': role['roleAgentId'],
                'memoryPolicyId': 'native-role', 'outputSchemaId': 'task-result',
                'details': {'source': 'configured_role_summary', 'displayName': text(role.get('name')) or binding_id,
                            'taskRole': role.get('taskRole') if role.get('taskRole') in ('planner', 'developer', 'reviewer') else None,
                            'access': role.get('executionAccess') if role.get('executionAccess') in ('readonly', 'delegate') else None,
                            'revision': revision, 'memory': 'qwenpaw_native_role',
                            'execution': execution, 'verification': verification}}

    def _agent_policies(self, project, workflow, document, *, native_ids=False):
        policies = {}
        for node in document['nodes']:
            if node['type'] != 'agent':
                continue
            config = node['config']
            role = self.role_bindings.get(config['roleBindingId'])
            if (not isinstance(role, dict) or role.get('projectId') != project
                    or role.get('enabled', True) is not True
                    or not isinstance(role.get('roleAgentId'), str)
                    or config['memoryPolicyId'] != 'native-role'):
                return None
            from .output_contract import supported_output_schema
            if not supported_output_schema(config['outputSchemaId'], config.get('outputFields')):
                return None
            segment(role['roleAgentId'])
            if host_role_shared_across_projects(self.role_bindings, role['roleAgentId'], project):
                return None
            if self.verification_enabled is not True or 'verificationPolicy' not in role:
                raise AuthoringError('verification_required')
            from .verification_policy import validate_policy
            try:
                verification = validate_policy(role['verificationPolicy'])
            except (ValueError, TypeError):
                raise AuthoringError('verification_required') from None
            node_id = node['id'] if native_ids else native_id(workflow, node['id'])
            policies[node_id] = {
                key: value for key, value in config.items() if key != 'inputFrom'} | {'roleAgentId': role['roleAgentId']}
            if 'roleRevision' in role:
                from .role_revision import validate_role_revision
                try:
                    revision = validate_role_revision(role['roleRevision'])
                except (ValueError, TypeError):
                    raise AuthoringError('role_binding_unavailable') from None
                policies[node_id]['roleRevision'] = revision
            policies[node_id]['verificationPolicy'] = verification
        return policies

    @staticmethod
    def _native_agent_nodes(state, body):
        from .publication import PublicationVerifier, PublicationUnverified
        agents, humans = set(body.nativeBindings), set(body.humanBlockIds)
        for node_id in agents | humans:
            segment(node_id)
        if (len(humans) != len(body.humanBlockIds) or agents & humans
                or not isinstance(state, dict) or not isinstance(state.get('blocks'), dict)):
            raise AuthoringError('native_binding_invalid')
        waits = {key for key, block in state['blocks'].items()
                 if isinstance(block, dict) and block.get('type') == 'human_in_the_loop_v2'}
        if waits != agents | humans:
            raise AuthoringError('native_binding_invalid')
        try:
            PublicationVerifier._graph(state, sorted(waits))
        except PublicationUnverified:
            raise AuthoringError('native_binding_invalid') from None
        for node_id in agents:
            subblocks = state['blocks'][node_id].get('subBlocks')
            form = subblocks.get('inputFormat') if isinstance(subblocks, dict) else None
            fields = form.get('value') if isinstance(form, dict) else None
            if not isinstance(fields, list):
                raise AuthoringError('native_task_input_required')
            inputs = [field for field in fields if isinstance(field, dict)
                      and isinstance(field.get('name'), str) and field['name'].strip() == 'taskInput']
            if len(inputs) != 1 or inputs[0].get('type') not in ('object', 'string'):
                raise AuthoringError('native_task_input_required')
        return [{'id': key, 'type': 'agent', 'config': {
            **binding.model_dump(), 'memoryPolicyId': 'native-role', 'outputSchemaId': 'task-result'}}
            for key, binding in body.nativeBindings.items()]

    def _owned(self, project, workflow):
        segment(workflow)
        with self.journal.connect() as db:
            row = db.execute(
                "SELECT 1 FROM operations WHERE workflow=? AND context='author-create' "
                "AND status='accepted' AND json_extract(receipt,'$.workflowId')=? LIMIT 1",
                (namespace(project), workflow),
            ).fetchone()
        if row is None:
            raise HTTPException(404, '流程不存在或不属于此项目')

    @staticmethod
    def _operation(record, project, kind):
        return {'projectId': project, 'operationId': record['runId'], 'kind': kind,
                'status': record['status'], 'safeToResubmit': False, 'receipt': record['receipt']}

    def _mutate(self, project, actor, kind, body, operation):
        payload = {'projectId': project, 'subject': actor.subject, 'request': body}
        def serialized():
            with self._edit_lock():
                if kind in ('save', 'publish'):
                    from .workflow_archive import record as archive_record
                    if archive_record(self.journal, project, body['workflowId']) is not None:
                        return {'workflowId': body['workflowId'], 'outcome': 'not_submitted',
                                'reason': 'workflow_archived_or_pending'}
                return operation()
        record = self.journal.mutate(namespace(project), body['operationId'], kind, payload, serialized)
        return self._operation(record, project, kind)

    @contextmanager
    def _edit_lock(self):
        # Serialize our own processes without holding a DB transaction during IO.
        # Sim's separate editor does not participate; readback checks remain vital.
        path = self.journal.path.with_name(self.journal.path.name + '.authoring.lock')
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1):
                raise ValueError('Invalid authoring lock')
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _state(self, project, workflow):
        self._owned(project, workflow)
        native = self.clients[project].get_state(workflow)
        return native, state_hash(native)

    def router(self):
        router = APIRouter(prefix='/extension/v1/authoring')

        def principal(authorization: str | None = Header(default=None)):
            return self.access.authenticate(authorization)

        def scoped(project, actor, publish=False):
            if not self.access.authorize(actor):
                raise HTTPException(403, '流程编辑权限已失效')
            if project not in self.clients or project not in actor.project_scopes:
                raise HTTPException(404, '项目不存在或未授权')
            if publish and 'publish_workflows' not in actor.capabilities:
                raise HTTPException(403, '需要流程发布权限')

        def perform(operation):
            try:
                return operation()
            except HTTPException:
                raise
            except AdmissionClosed:
                raise HTTPException(503, '当前处于维护或核对状态，未准入新的修改',
                                    headers={'X-Agent-Line-Admission': 'closed'}) from None
            except Conflict:
                raise HTTPException(409, '操作编号已用于不同请求，请核对原操作') from None
            except sqlite3.Error:
                raise HTTPException(503, '流程操作日志不可用，未确认修改结果') from None
            except CorruptTeamJournal:
                raise HTTPException(503, '团队关系日志不可用，未确认当前状态') from None
            except UnsupportedDocument:
                raise HTTPException(422, '此流程包含当前界面尚不支持的内容，请使用 Sim 原生编辑器') from None
            except AuthoringError as error:
                if error.code == 'subworkflow_portable_template_unsupported':
                    raise HTTPException(422, '固定子流程暂不支持跨安装模板导入或导出') from None
                raise HTTPException(422, '流程内容无效或 Sim 返回的数据无法确认') from None
            except (ValueError, TypeError, KeyError, RecursionError):
                raise HTTPException(422, '流程配置或标识无效') from None

        @router.get('/projects')
        def projects(actor=Depends(principal)):
            return {'projects': [{'id': key, 'name': self.projects[key].get('name', key)}
                                 for key in sorted(self.clients) if key in actor.project_scopes],
                    'nodeTypes': ['start', 'condition', 'human', 'agent', 'end', 'loop', 'parallel', 'join'],
                    'canPublish': 'publish_workflows' in actor.capabilities}

        @router.get('/projects/{project}/roles')
        def roles(project: str, actor=Depends(principal)):
            scoped(project, actor)
            from .output_contract import OUTPUT_SCHEMAS
            from .sim_authoring import MAX_STATIC_PARALLEL_GROUPS
            return {'roles': [self._role_summary(project, key, role)
                              for key, role in sorted(self.role_bindings.items())
                              if role.get('projectId') == project and role.get('enabled', True) is True
                              and isinstance(role.get('roleAgentId'), str)],
                    'source': 'configured_existing_roles', 'nativeAvailabilityVerified': False,
                    'outputSchemaIds': list(OUTPUT_SCHEMAS), 'humanApprovalTtlSupported': True,
                    'humanApprovalQuestionSupported': True,
                    'maxStaticParallelGroups': MAX_STATIC_PARALLEL_GROUPS,
                    'subworkflowAuthoring': {'maxDepth': 1, 'requiresRunBudget': True, 'editorSupported': True},
                    'nodeLimitsSupported': True}

        @router.post('/projects/{project}/templates/export')
        def export_workflow_template(project: str, body: ExportTemplateRequest,
                                     response: Response, actor=Depends(principal)):
            scoped(project, actor)
            response.headers['Cache-Control'] = 'no-store'
            def export():
                require_portable_document(body.document)
                return export_template(body.document)
            return perform(export)

        @router.post('/projects/{project}/subworkflows/resolve')
        def resolve_subworkflow(project: str, body: SubworkflowReferenceRequest,
                                response: Response, actor=Depends(principal)):
            scoped(project, actor)
            response.headers['Cache-Control'] = 'no-store'
            def read():
                from .subworkflow_reference import read_reference, validate_reference
                reference = validate_reference(body.reference, project, body.parentWorkflowId)
                self._owned(project, body.parentWorkflowId)
                self._owned(project, reference['workflowId'])
                result = read_reference(self.clients[project], reference, project=project,
                                        parent_workflow=body.parentWorkflowId)
                scoped(project, actor)
                return result
            return perform(read)

        @router.post('/projects/{project}/subworkflows/preview')
        def preview_subworkflow(project: str, body: SubworkflowExpansionRequest,
                                response: Response, actor=Depends(principal)):
            scoped(project, actor)
            response.headers['Cache-Control'] = 'no-store'
            def read():
                from .subworkflow_reference import read_reference, validate_reference
                from .subworkflow_expansion import build_preview
                reference = validate_reference(body.reference, project, body.parentWorkflowId)
                self._owned(project, body.parentWorkflowId)
                self._owned(project, reference['workflowId'])
                native, digest = self._state(project, body.parentWorkflowId)
                if digest != body.expectedParentStateSha256:
                    raise HTTPException(409, '父流程草稿已变化，请重新读取后再预览')
                parent = decode_document(native)
                snapshot = read_reference(self.clients[project], reference, project=project,
                                          parent_workflow=body.parentWorkflowId)
                document = build_preview(parent, reference, snapshot['document'],
                                         parent_workflow=body.parentWorkflowId, call_id=body.callId,
                                         edge_id=body.edgeId, input_from=body.inputFrom,
                                         input_mapping=body.inputMapping, output_mapping=body.outputMapping)
                compiled = compile_document(document, body.parentWorkflowId)
                if len(canonical(compiled).encode()) > 1_000_000:
                    raise AuthoringError('subworkflow_expansion_too_large')
                # Verify marker decoding against the same exact generated graph.
                if canonical(compile_document(decode_document(compiled), body.parentWorkflowId)) != canonical(compiled):
                    raise AuthoringError('subworkflow_expansion_unreadable')
                _, current_digest = self._state(project, body.parentWorkflowId)
                if current_digest != digest:
                    raise HTTPException(409, '预览期间父流程草稿已变化，请重新读取')
                scoped(project, actor)
                return {'source': 'validated_subworkflow_expansion_preview', 'readOnly': True,
                        'executionSupported': False, 'reason': 'preview_only',
                        'projectId': project, 'parentWorkflowId': body.parentWorkflowId,
                        'parentStateSha256': digest, 'reference': reference, 'callId': body.callId,
                        'inputFrom': body.inputFrom, 'document': document,
                        **({'inputMapping': body.inputMapping} if body.inputMapping is not None else {}),
                        **({'outputMapping': body.outputMapping} if body.outputMapping is not None else {}),
                        'graphSha256': hashlib.sha256(canonical(compiled).encode()).hexdigest(),
                        'nodeCount': len(document['nodes']), 'edgeCount': len(document['edges'])}
            return perform(read)

        @router.post('/projects/{project}/templates/import')
        def import_workflow_template(project: str, body: ImportTemplateRequest,
                                     response: Response, actor=Depends(principal)):
            scoped(project, actor)
            response.headers['Cache-Control'] = 'no-store'
            def read():
                require_portable_document(body.template.get('document'))
                return import_template(body.template, body.roleBindings, project=project,
                                       configured_role_bindings=self.role_bindings)
            return perform(read)

        @router.post('/projects/{project}/batches/preview')
        def preview_static_batch(project: str, body: StaticBatchPreviewRequest,
                                  response: Response, actor=Depends(principal)):
            scoped(project, actor)
            response.headers['Cache-Control'] = 'no-store'
            return perform(lambda: build_static_batch(
                project, body.model_dump(), self.role_bindings))

        @router.get('/projects/{project}/team')
        def team(project: str, actor=Depends(principal)):
            scoped(project, actor)
            from .team_view import TeamView
            def read():
                result = TeamView(self.journal, self.projects, self.role_bindings,
                                  self.task_store).get(project)
                scoped(project, actor)
                return result
            return perform(read)

        @router.post('/projects/{project}/team')
        def update_team(project: str, body: TeamUpdateRequest, actor=Depends(principal)):
            scoped(project, actor)
            from .team_view import TeamView
            view = TeamView(self.journal, self.projects, self.role_bindings, self.task_store)

            def recheck():
                scoped(project, actor)

            def submit():
                try:
                    receipt = view.replace(project, actor, body.operationId, body.expectedRevision,
                                           [edge.model_dump() for edge in body.edges], recheck)
                except AdmissionClosed:
                    raise
                except Conflict as exc:
                    raise HTTPException(409, '团队图版本冲突或操作编号已用于其他请求') from exc
                return receipt

            return perform(submit)

        @router.get('/projects/{project}/team/operations/{operation_id}')
        def team_operation(project: str, operation_id: str, actor=Depends(principal)):
            scoped(project, actor)
            from .team_view import TeamView

            def lookup():
                receipt = TeamView(self.journal, self.projects, self.role_bindings,
                                   self.task_store).operation(project, operation_id)
                scoped(project, actor)
                if receipt is None:
                    raise HTTPException(404, '团队操作不存在')
                return receipt

            return perform(lookup)

        @router.get('/projects/{project}/workflows')
        def workflows(project: str, after: str = Query(default='', max_length=128),
                      limit: int = Query(default=30, ge=1, le=100),
                      search: str = Query(default='', max_length=255), actor=Depends(principal)):
            scoped(project, actor)
            def read():
                if after and not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', after):
                    raise ValueError('Invalid cursor')
                normalized_search = search.strip()
                folded_search = normalized_search.casefold()
                with self.journal.connect() as db:
                    rows = db.execute(
                        "SELECT run,receipt FROM operations WHERE workflow=? AND context='author-create' "
                        "AND status='accepted' AND run>? ORDER BY run LIMIT ?",
                        (namespace(project), after, limit),
                    ).fetchall()
                items = []
                for row in rows:
                    receipt = json.loads(row['receipt'])
                    from .workflow_archive import record as archive_record
                    archived = archive_record(self.journal, project, receipt['workflowId'])
                    if archived is not None and archived['status'] == 'accepted':
                        continue
                    detail = (archived['payload']['snapshot']['detail'] if archived is not None
                              else self.clients[project].get_detail(receipt['workflowId']))
                    name = detail['name']
                    description = detail.get('description') or ''
                    if folded_search and not any(
                            folded_search in value.casefold()
                            for value in (name, description) if isinstance(value, str)):
                        continue
                    # Native deployment state is authoritative after rollback or
                    # undeploy; local publication receipts are only history.
                    deployment_fields = {'deploymentStatus': 'unknown', 'currentVersion': None,
                                         'needsRedeployment': None}
                    deployed = detail.get('isDeployed') is True
                    try:
                        deployment = self.clients[project].get_deployment(detail['id'])
                    except AuthoringError:
                        pass  # A missing capability must not invent a version.
                    else:
                        deployed = deployment['isDeployed']
                        active = deployment['activeDeployment']
                        deployment_fields = {
                            'deploymentStatus': 'deployed' if deployed else 'draft',
                            'currentVersion': active['version'] if active else None,
                            'needsRedeployment': deployment['needsRedeployment']}
                    last_run_at = detail.get('lastRunAt')
                    run_count = detail.get('runCount')
                    items.append({'id': detail['id'], 'name': detail['name'],
                                  'description': description,
                                  'isDeployed': deployed, **deployment_fields,
                                  'archivePending': archived is not None,
                                  'lastRunAt': last_run_at if isinstance(last_run_at, str) else None,
                                  'runCount': run_count if type(run_count) is int and run_count >= 0 else None})
                identities = [item['id'] for item in items]
                for workflow_id in identities:
                    self._owned(project, workflow_id)
                summaries = self.journal.summarize_runs(identities)
                for item in items:
                    item['localSubmissions'] = summaries[item['id']]
                scoped(project, actor)
                return {'workflows': items, 'nextCursor': rows[-1]['run'] if len(rows) == limit else None,
                        'search': normalized_search, 'scannedCount': len(rows)}
            return perform(read)

        @router.get('/projects/{project}/archives')
        def archives(project: str, after: str = Query(default='', max_length=128),
                     limit: int = Query(default=30, ge=1, le=100), actor=Depends(principal)):
            scoped(project, actor)
            def read():
                if after and not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', after):
                    raise ValueError('Invalid cursor')
                with self.journal.connect() as db:
                    rows = db.execute("SELECT run,status,input FROM operations WHERE workflow=? "
                                      "AND context='author-archive' AND run>? ORDER BY run LIMIT ?",
                                      (namespace(project), after, limit)).fetchall()
                result = []
                for row in rows:
                    snapshot = json.loads(row['input'])['snapshot']
                    self._owned(project, snapshot['workflowId'])
                    result.append({'operationId': row['run'], 'workflowId': snapshot['workflowId'],
                                   'name': snapshot['detail']['name'], 'status': row['status'],
                                   'capturedAt': snapshot['capturedAt'], 'readOnly': True})
                return {'archives': result, 'nextCursor': rows[-1]['run'] if len(rows) == limit else None}
            return perform(read)

        @router.post('/projects/{project}/workflows', status_code=202)
        def create(project: str, body: CreateRequest, actor=Depends(principal)):
            scoped(project, actor)
            def submit():
                if not body.name.strip():
                    raise ValueError('Empty workflow name')
                def operation():
                    scoped(project, actor)
                    detail = self.clients[project].create(body.name, body.description)
                    return {'workflowId': detail['id'], 'name': detail['name'], 'outcome': 'created'}
                return self._mutate(project, actor, 'create', body.model_dump(), operation)
            return perform(submit)

        @router.get('/projects/{project}/workflows/{workflow}')
        def workflow(project: str, workflow: str, actor=Depends(principal)):
            scoped(project, actor)
            def read():
                native, digest = self._state(project, workflow)
                detail = self.clients[project].get_detail(workflow)
                empty = is_initial_state(native)
                from .published_workflows import PublishedWorkflowCatalog
                published = [{'bindingId': key, 'revision': binding['publication']['revision']}
                             for key, binding in PublishedWorkflowCatalog(self).bindings({project}).items()
                             if binding['workflowId'] == workflow]
                document, editable = None, True
                if not empty:
                    try:
                        document = decode_document(native)
                    except UnsupportedDocument:
                        editable = False
                return {'workflowId': workflow, 'name': detail['name'],
                        'description': detail.get('description') or '',
                        'document': document, 'draftEmpty': empty, 'editable': editable,
                        'nativeWaits': None if empty or document is not None else project_native_waits(native),
                        'stateSha256': digest, 'isDeployed': detail.get('isDeployed') is True,
                        'publishedBindings': sorted(published, key=lambda item: item['revision'], reverse=True)}
            return perform(read)

        @router.get('/projects/{project}/workflows/{workflow}/versions')
        def versions(project: str, workflow: str, after: str | None = Query(default=None, max_length=1024),
                     limit: int = Query(default=20, ge=1, le=50), actor=Depends(principal)):
            scoped(project, actor)
            def read():
                self._owned(project, workflow)
                page = self.clients[project].versions(workflow, cursor=after, limit=limit)
                rows, cursor = page['data'], page.get('nextCursor')
                if (not isinstance(rows, list) or len(rows) > limit
                        or not (cursor is None or isinstance(cursor, str) and 0 < len(cursor) <= 1024)
                        or cursor is not None and (not rows or cursor == after)):
                    raise AuthoringError('version_page_invalid')
                items, revisions, deployments = [], set(), set()
                for row in rows:
                    deployment, revision = row['id'], row['version']
                    segment(deployment)
                    if (type(revision) is not int or not 1 <= revision <= 9007199254740991
                            or type(row['isActive']) is not bool
                            or revision in revisions or deployment in deployments):
                        raise AuthoringError('version_page_invalid')
                    revisions.add(revision)
                    deployments.add(deployment)
                    items.append({'revision': revision, 'deploymentVersionId': deployment,
                                  'isActive': row['isActive']})
                if sum(item['isActive'] for item in items) > 1:
                    raise AuthoringError('version_page_invalid')
                scoped(project, actor)
                return {'source': 'native_workflow_versions', 'readOnly': True,
                        'projectId': project, 'workflowId': workflow,
                        'versions': items, 'nextCursor': cursor}
            return perform(read)

        @router.get('/projects/{project}/workflows/{workflow}/versions/{revision}')
        def version_snapshot(project: str, workflow: str, revision: str, actor=Depends(principal)):
            scoped(project, actor)
            def read():
                self._owned(project, workflow)
                if not re.fullmatch(r'[1-9][0-9]{0,15}', revision) or int(revision) > 9007199254740991:
                    raise AuthoringError('revision_invalid')
                version = self.clients[project].get_version(workflow, int(revision))
                deployment = version['id']
                segment(deployment)
                if (type(version['version']) is not int or version['version'] != int(revision)
                        or type(version['isActive']) is not bool):
                    raise AuthoringError('version_snapshot_invalid')
                document = decode_document(version['state'])
                compiled = compile_document(document, workflow)
                digest = hashlib.sha256(canonical(compiled).encode()).hexdigest()
                scoped(project, actor)
                return {'source': 'native_workflow_version_snapshot', 'readOnly': True,
                        'projectId': project, 'workflowId': workflow,
                        'revision': int(revision), 'deploymentVersionId': deployment,
                        'isActive': version['isActive'], 'graphSha256': digest, 'document': document}
            return perform(read)

        @router.post('/projects/{project}/workflows/{workflow}/archive', status_code=202)
        def archive(project: str, workflow: str, body: PublishRequest, actor=Depends(principal)):
            scoped(project, actor)
            def submit():
                from .workflow_archive import capture, ensure_mutable, snapshot_digest, MAX_BYTES
                self._owned(project, workflow)
                request = {**body.model_dump(), 'workflowId': workflow}
                stable = {'projectId': project, 'subject': actor.subject, 'request': request}

                def old_operation():
                    with self.journal.connect() as db:
                        row = db.execute("SELECT * FROM operations WHERE workflow=? AND run=? AND context='author-archive'",
                                         (namespace(project), body.operationId)).fetchone()
                    if row is None:
                        return None
                    payload = json.loads(row['input'])
                    if {key: value for key, value in payload.items() if key not in ('snapshot', 'snapshotSha256')} != stable:
                        raise Conflict('Archive identity reused')
                    return self._operation(self.journal._public(row), project, 'archive')
                old = old_operation()
                if old is not None:
                    return old
                with self.journal.mutation_scope(), self._edit_lock():
                    old = old_operation()
                    if old is not None:
                        return old
                    scoped(project, actor)
                    ensure_mutable(self.journal, project, workflow)
                    snapshot = capture(self.clients[project], self.journal, workflow, body.expectedStateSha256)
                    snapshot['projectId'] = project
                    payload = {**stable, 'snapshot': snapshot, 'snapshotSha256': snapshot_digest(snapshot)}
                    if len(canonical(payload).encode()) > MAX_BYTES:
                        raise AuthoringError('archive_history_too_large')
                    def operation():
                        scoped(project, actor)
                        self.clients[project].archive(workflow)
                        return {'workflowId': workflow, 'outcome': 'archived',
                                'auditSha256': snapshot_digest(snapshot)}
                    record = self.journal.mutate(namespace(project), body.operationId, 'archive', payload, operation)
                    return self._operation(record, project, 'archive')
            return perform(submit)

        @router.get('/projects/{project}/archives/{operation_id}')
        def archive_audit(project: str, operation_id: str, actor=Depends(principal)):
            scoped(project, actor)
            def read():
                from .workflow_archive import snapshot_digest
                segment(operation_id)
                with self.journal.connect() as db:
                    row = db.execute("SELECT * FROM operations WHERE workflow=? AND run=? AND context='author-archive'",
                                     (namespace(project), operation_id)).fetchone()
                if row is None:
                    raise HTTPException(404, '归档审计不存在')
                payload = json.loads(row['input'])
                snapshot = payload.get('snapshot')
                if (not isinstance(snapshot, dict) or snapshot.get('projectId') != project
                        or snapshot.get('workflowId') != payload.get('request', {}).get('workflowId')
                        or snapshot_digest(snapshot) != payload.get('snapshotSha256')):
                    raise AuthoringError('archive_audit_invalid')
                self._owned(project, snapshot['workflowId'])
                receipt = json.loads(row['receipt']) if row['receipt'] else None
                if receipt is not None and receipt.get('auditSha256') != snapshot_digest(snapshot):
                    raise AuthoringError('archive_audit_invalid')
                return {'projectId': project, 'operationId': operation_id, 'status': row['status'],
                        'readOnly': True, 'source': 'pre_archive_native_api_snapshot',
                        'historyScope': 'api_visible_at_capture', 'auditSha256': snapshot_digest(snapshot),
                        'snapshot': snapshot}
            return perform(read)

        @router.post('/projects/{project}/archives/{operation_id}/reconcile')
        def reconcile_archive(project: str, operation_id: str, actor=Depends(principal)):
            scoped(project, actor)
            def reconcile():
                from .workflow_archive import pages
                import time
                with self.journal.mutation_scope(), self._edit_lock():
                    audit = archive_audit(project, operation_id, actor)
                    if audit['status'] != 'accepted':
                        workflow = audit['snapshot']['workflowId']
                        items = pages(lambda cursor: self.clients[project].list_archived(cursor, 100))
                        if any(item['id'] == workflow for item in items):
                            scoped(project, actor)
                            receipt = {'workflowId': workflow, 'outcome': 'archived',
                                       'auditSha256': audit['auditSha256'], 'reconciledFrom': 'native_archived_scope'}
                            with self.journal.connect() as db:
                                db.execute("UPDATE operations SET status='accepted',receipt=?,updated=? "
                                           "WHERE workflow=? AND run=? AND context='author-archive' "
                                           "AND status IN ('intent','uncertain')",
                                           (canonical(receipt), time.time(), namespace(project), operation_id))
                    return self._operation(self.journal.get(namespace(project), operation_id, 'author-archive'),
                                           project, 'archive')
            return perform(reconcile)

        @router.post('/projects/{project}/workflows/{workflow}/save', status_code=202)
        def save(project: str, workflow: str, body: SaveRequest, actor=Depends(principal)):
            scoped(project, actor)
            def submit():
                self._owned(project, workflow)
                compiled = compile_document(body.document, workflow)
                if len(canonical(compiled).encode()) > 1_000_000:
                    raise ValueError('Document too large')
                def operation():
                    scoped(project, actor)
                    old, digest = self._state(project, workflow)
                    if digest != body.expectedStateSha256:
                        return {'workflowId': workflow, 'outcome': 'conflict', 'stateSha256': digest}
                    _, refusal = subworkflow_admission(self, project, workflow, body.document)
                    if refusal is not None:
                        return {'workflowId': workflow, 'outcome': 'subworkflow_not_admitted', 'reason': refusal}
                    if not is_initial_state(old):
                        decode_document(old)  # Do not overwrite unsupported native content.
                    scoped(project, actor)
                    result = self.clients[project].save_state(workflow, compiled)
                    current, current_hash = self._state(project, workflow)
                    doc = decode_document(current)
                    if canonical(compile_document(doc, workflow)) != canonical(compiled):
                        return {'workflowId': workflow, 'outcome': 'readback_mismatch', 'stateSha256': current_hash}
                    return {'workflowId': workflow, 'outcome': 'saved', 'stateSha256': current_hash,
                            'needsRedeployment': result['needsRedeployment']}
                request = {**body.model_dump(), 'workflowId': workflow}
                return self._mutate(project, actor, 'save', request, operation)
            return perform(submit)

        @router.post('/projects/{project}/workflows/{workflow}/publish', status_code=202)
        def publish(project: str, workflow: str, body: PublishRequest | NativePublishRequest, actor=Depends(principal)):
            scoped(project, actor, publish=True)
            def submit():
                self._owned(project, workflow)
                def operation():
                    scoped(project, actor, publish=True)
                    native, digest = self._state(project, workflow)
                    if digest != body.expectedStateSha256:
                        return {'workflowId': workflow, 'outcome': 'conflict', 'stateSha256': digest}
                    use_native = isinstance(body, NativePublishRequest)
                    if use_native:
                        from .published_workflows import graph_hash
                        try:
                            doc = {'nodes': self._native_agent_nodes(native, body)}
                            native_hash = graph_hash(native, workflow, native=True)
                        except ValueError:
                            # No native write occurred: retain a known refusal,
                            # rather than reporting an ambiguous deployment.
                            return {'workflowId': workflow, 'outcome': 'native_binding_required'}
                        child_policy = None
                        human_blocks = sorted(body.humanBlockIds)
                    else:
                        doc = decode_document(native)
                        child_policy, refusal = subworkflow_admission(self, project, workflow, doc)
                        if refusal is not None:
                            return {'workflowId': workflow, 'outcome': 'subworkflow_not_admitted', 'reason': refusal}
                        human_blocks = sorted(native_id(workflow, node['id']) for node in doc['nodes'] if node['type'] == 'human')
                    if human_blocks and self.approval_enabled is not True:
                        return {'workflowId': workflow, 'outcome': 'approval_binding_required'}
                    try:
                        agent_policies = self._agent_policies(project, workflow, doc, native_ids=use_native)
                    except AuthoringError as error:
                        if error.code != 'verification_required':
                            raise
                        return {'workflowId': workflow, 'outcome': 'verification_required'}
                    if agent_policies is None:
                        return {'workflowId': workflow, 'outcome': 'role_binding_required'}
                    if human_blocks and set(human_blocks) & set(agent_policies):
                        return {'workflowId': workflow, 'outcome': 'approval_binding_required'}
                    if agent_policies:
                        try:
                            outcome = (self.publication_check(project, agent_policies)
                                       if callable(self.publication_check) else 'execution_required')
                        except Exception:
                            outcome = 'execution_required'
                        if outcome is not None:
                            allowed = {'execution_required', 'verification_required',
                                       'memory_commit_required', 'role_binding_required'}
                            return {'workflowId': workflow, 'outcome': outcome
                                    if type(outcome) is str and outcome in allowed else 'execution_required'}
                    # A deployment response alone does not prove which graph was published.
                    from .published_workflows import binding_id, graph_hash
                    expected_graph_hash = native_hash if use_native else graph_hash(native, workflow)
                    scoped(project, actor, publish=True)
                    result = self.clients[project].deploy(workflow)
                    revision = result['version']
                    deployment_id = result['activeDeployment']['deploymentVersionId']
                    version = self.clients[project].get_version(workflow, revision)
                    if (version['id'] != deployment_id or version['isActive'] is not True
                            or graph_hash(version['state'], workflow, native=use_native) != expected_graph_hash):
                        return {'workflowId': workflow, 'outcome': 'publication_mismatch', 'revision': revision}
                    from .sim_authoring import loop_policies, human_ttls, human_questions
                    loops = {} if use_native else loop_policies(doc, workflow)
                    approval_ttls = {} if use_native else human_ttls(doc, workflow)
                    approval_questions = {} if use_native else human_questions(doc, workflow)
                    return {'workflowId': workflow, 'outcome': 'published', 'revision': revision,
                            'deploymentVersionId': deployment_id, 'stateSha256': digest,
                            'bindingId': binding_id(project, workflow, deployment_id),
                            'graphSha256': expected_graph_hash,
                            **({'graphFormat': 'sim-native-v1'} if use_native else {}),
                            **({'externalTasks': {'revision': str(revision), 'deploymentVersionId': deployment_id,
                                                 'agentPolicies': agent_policies, 'ttlSeconds': 604800,
                                                 **({'subworkflowDepthLimit': child_policy['depthLimit']} if child_policy else {}),
                                                 **({'staticParallel': True} if any(n['type'] == 'parallel' for n in doc['nodes']) else {}),
                                                 **({'loopPolicies': loops} if loops else {})}}
                               if agent_policies else {}),
                            **({'humanApproval': {'revision': str(revision), 'deploymentVersionId': deployment_id,
                                                 'blockIds': human_blocks, 'ttlSeconds': 604800,
                                                 **({'ttlByBlock': approval_ttls} if approval_ttls else {}),
                                                 **({'questionsByBlock': approval_questions} if approval_questions else {})}}
                               if human_blocks else {})}
                request = {**body.model_dump(), 'workflowId': workflow}
                return self._mutate(project, actor, 'publish', request, operation)
            return perform(submit)

        @router.get('/projects/{project}/operations/{operation_id}')
        def operation(project: str, operation_id: str, kind: str = Query(pattern='^(create|save|publish|archive)$'), actor=Depends(principal)):
            scoped(project, actor)
            def read():
                segment(operation_id)
                try:
                    record = self.journal.get(namespace(project), operation_id, 'author-' + kind)
                except KeyError:
                    raise HTTPException(404, '操作记录不存在') from None
                return self._operation(record, project, kind)
            return perform(read)

        return router
