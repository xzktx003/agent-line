"""Official registration only. No monkey patches or host-global state access."""
import json
from fastapi import APIRouter, Depends, HTTPException, Request
from qwenpaw.plugins.api import PluginApi
from agentscope.middleware import MiddlewareBase
from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolChunk
from qwenpaw.app.agent_context import get_current_agent_id, get_current_session_id


def tool_result(text: str, state: ToolResultState) -> ToolChunk:
    return ToolChunk(content=[TextBlock(type='text', text=text)], state=state, is_last=True)


class UnavailableMemoryGovernance:
    """Keep admission guards installed when configured governance cannot load."""
    available = False

    def __init__(self, config, fallback_roles):
        self.roles = frozenset(fallback_roles)
        declared = config.get('managed_role_ids') if isinstance(config, dict) else None
        if isinstance(declared, list):
            self.roles |= frozenset(role for role in declared if isinstance(role, str) and role.strip())

    async def is_blocked(self, role):
        return True

    def router(self):
        router = APIRouter()

        @router.get('/memory/{role}/{task}/deletion')
        @router.post('/memory/{role}/{task}/delete')
        async def unavailable(role: str, task: str):
            raise HTTPException(503, '记忆治理状态不可用；受管角色新推理与委派已阻断，请恢复原状态后重新加载插件')

        return router


class UnavailableApplicability:
    """Configured ledger failed to open. Managed roles must not search blindly."""
    available = False

    def recall(self, **kwargs):
        raise PermissionError('适用性账本不可用，禁止检索')

    def exact_hidden(self, *args, **kwargs):
        return False

    def classify(self, **kwargs):
        raise PermissionError('适用性账本不可用')

    def get(self, *args, **kwargs):
        raise PermissionError('适用性账本不可用')


class UnavailableExecutionBridge:
    """A missing/mismatched ledger must never remove managed-role middleware."""
    available = False

    def __init__(self):
        self.ledger = self

    def assert_role_revision(self, *args):
        raise HTTPException(503, '执行绑定状态不可用，请显式迁移或恢复原库')

    bound_task = assert_role_revision

    async def submit_from_parent(self, *args):
        self.assert_role_revision()

    def router(self):
        router = APIRouter()

        @router.get('/execution-binding')
        @router.post('/delegations/bind')
        @router.post('/executions')
        @router.get('/executions/{task_id}')
        @router.post('/executions/{task_id}/cancel')
        @router.get('/executions/{task_id}/approvals')
        @router.post('/executions/{task_id}/approvals/{request_id}/decision')
        @router.post('/delegations/{task_id}/cancel')
        async def unavailable():
            self.assert_role_revision()

        return router


def _memory_directory_boundary():
    """Load the pure path check without requiring the plugin package parent."""
    import importlib.util
    module = getattr(_memory_directory_boundary, 'module', None)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(
        'agent_line_memory_directory_boundary',
        __import__('pathlib').Path(__file__).with_name('role_revision.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _memory_directory_boundary.module = module
    return module


def refuse_escaped_memory_directories(role):
    """Reject memory_search when a present role config leaves its workspace.

    A missing role or agent.json is not an escape: official defaults are
    relative names, and creating a fallback profile would change host state.
    """
    import json
    from pathlib import Path
    if not isinstance(role, str) or not role.strip():
        return
    try:
        from qwenpaw.config.utils import load_config
        reference = load_config().agents.profiles.get(role)
    except Exception:
        return
    if reference is None:
        return
    workspace = Path(reference.workspace_dir).expanduser()
    path = workspace / 'agent.json'
    try:
        if not path.is_file():
            return
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
        raise PermissionError('记忆目录配置无法确认仍在角色工作区内，禁止检索') from None
    if not isinstance(data, dict):
        raise PermissionError('记忆目录配置无法确认仍在角色工作区内，禁止检索')
    running = data.get('running')
    if not isinstance(running, dict) or 'reme_light_memory_config' not in running:
        return
    boundary = _memory_directory_boundary()
    try:
        directories = boundary.memory_directory_values(running['reme_light_memory_config'])
    except ValueError:
        raise PermissionError('记忆目录超出角色工作区，禁止检索') from None
    if directories is not None and not boundary.memory_directories_within_workspace(workspace, directories):
        raise PermissionError('记忆目录超出角色工作区，禁止检索')


class ManagedRoleToolGuard(MiddlewareBase):
    # A fixed allowlist: unknown tools and indirect delegation are denied too.
    ALLOWED = frozenset({'memory_search', 'agent_line_delegate', 'agent_line_execution_status'})

    def __init__(self, governance=None, role=None, bridge=None, applicability=None, project_id=None):
        self.governance, self.role, self.bridge = governance, role, bridge
        self.applicability, self.project_id = applicability, project_id

    @staticmethod
    def _message_text(message):
        if isinstance(message, str):
            return message
        getter = getattr(message, 'get_text_content', None)
        if callable(getter):
            try:
                value = getter()
            except Exception:
                return None
            if isinstance(value, str):
                return value
        content = getattr(message, 'content', None)
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return None
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get('text'), str):
                parts.append(block['text'])
            elif isinstance(getattr(block, 'text', None), str):
                parts.append(block.text)
            else:
                return None
        return '\n'.join(parts)

    def _without_forbidden_exact(self, messages):
        """Drop only a message whose whole text is a confirmed deleted body.

        Fragments and paraphrases are not recognized. The tombstone stores a
        hash, not the body, so this cannot search inside a larger message.
        """
        if not self.role or not isinstance(messages, list):
            return messages
        if self.governance is None and self.applicability is None:
            return messages
        kept = []
        for message in messages:
            self._rewrite_search_payload(message)
            text = self._message_text(message)
            deleted = (isinstance(text, str) and text.strip() and self.governance is not None
                       and self.governance.forbids_content(self.role, text))
            classified = (isinstance(text, str) and text.strip() and self.applicability is not None
                          and self.project_id
                          and self.applicability.exact_hidden(self.role, self.project_id, text))
            if deleted or classified:
                continue
            kept.append(message)
        return kept

    def _rewrite_search_payload(self, message):
        """Rewrite native memory_search text. Fragments are left unchanged."""
        if self.applicability is None or not self.role or not self.project_id:
            return
        if getattr(self.applicability, 'available', True) is False:
            return
        from agent_line.memory_applicability import filter_search_text

        def rewrite(holder):
            current = holder.get('text') if isinstance(holder, dict) else getattr(holder, 'text', None)
            if not isinstance(current, str) or not current.strip():
                return
            updated = filter_search_text(self.applicability, self.role, self.project_id, current)
            if updated == current:
                return
            if isinstance(holder, dict):
                holder['text'] = updated
            else:
                holder.text = updated

        content = message.get('content') if isinstance(message, dict) else getattr(message, 'content', None)
        name = message.get('name') if isinstance(message, dict) else getattr(message, 'name', None)
        if isinstance(content, str) and name == 'memory_search':
            updated = filter_search_text(self.applicability, self.role, self.project_id, content)
            if updated != content:
                if isinstance(message, dict):
                    message['content'] = updated
                else:
                    message.content = updated
            return
        if not isinstance(content, list):
            return
        for block in content:
            block_name = block.get('name') if isinstance(block, dict) else getattr(block, 'name', None)
            if block_name != 'memory_search' and name != 'memory_search':
                continue
            output = block.get('output') if isinstance(block, dict) else getattr(block, 'output', None)
            if isinstance(output, list):
                for part in output:
                    rewrite(part)
            elif isinstance(output, str) and block_name == 'memory_search':
                updated = filter_search_text(self.applicability, self.role, self.project_id, output)
                if updated != output:
                    if isinstance(block, dict):
                        block['output'] = updated
                    else:
                        block.output = updated
            rewrite(block)

    def check_role_revision(self):
        if self.bridge is not None:
            try:
                self.bridge.assert_role_revision(self.role, get_current_session_id())
            except Exception:
                raise PermissionError('角色修订未固定或已改变，禁止新推理或委派') from None

    async def check_memory_admission(self):
        self.check_role_revision()
        if self.governance is not None and (not self.role or await self.governance.is_blocked(self.role)):
            raise PermissionError('记忆删除尚未确认索引清除，禁止新的模型推理')

    async def on_reply(self, agent, input_kwargs, next_handler):
        await self.check_memory_admission()
        async for item in next_handler(**input_kwargs):
            yield item
        # Native auto-memory snapshots the context after this inner hook returns.
        context = getattr(getattr(agent, 'state', None), 'context', None)
        if isinstance(context, list):
            context[:] = self._without_forbidden_exact(context)

    async def on_reasoning(self, agent, input_kwargs, next_handler):
        # QwenPaw installs native memory model-call hooks before plugin hooks.
        # This earlier lifecycle phase prevents entering that injection chain.
        await self.check_memory_admission()
        async for item in next_handler(**input_kwargs):
            yield item

    async def on_model_call(self, agent, input_kwargs, next_handler):
        # Also refuse provider submission if deletion began during this turn.
        await self.check_memory_admission()
        if (self.applicability is not None and getattr(self.applicability, 'available', True) is False
                and self.role):
            raise PermissionError('适用性账本不可用，禁止新的模型推理')
        messages = input_kwargs.get('messages') if isinstance(input_kwargs, dict) else None
        if isinstance(messages, list):
            input_kwargs['messages'] = self._without_forbidden_exact(messages)
        return await next_handler(**input_kwargs)

    async def on_acting(self, agent, input_kwargs, next_handler):
        tool_call = input_kwargs.get('tool_call')
        name = tool_call.get('name') if isinstance(tool_call, dict) else getattr(tool_call, 'name', None)
        if name not in self.ALLOWED:
            raise PermissionError('工作流角色只能查询记忆或通过受控工具委派执行')
        if name == 'memory_search':
            refuse_escaped_memory_directories(self.role)
        if name in {'memory_search', 'agent_line_delegate'}:
            self.check_role_revision()
        if self.governance is not None and name in {'memory_search', 'agent_line_delegate'}:
            if not self.role or await self.governance.is_blocked(self.role):
                raise PermissionError('记忆删除尚未确认索引清除，禁止检索或新委派')
        if (name == 'memory_search' and self.applicability is not None
                and getattr(self.applicability, 'available', True) is False and self.role):
            raise PermissionError('适用性账本不可用，禁止检索')
        async for item in next_handler():
            if name == 'memory_search' and self.applicability is not None and self.project_id:
                item = self._filter_search_item(item)
            yield item

    def _filter_search_item(self, item):
        from agent_line.memory_applicability import filter_search_text
        if isinstance(item, str):
            return filter_search_text(self.applicability, self.role, self.project_id, item)
        content = getattr(item, 'content', None)
        if not isinstance(content, list):
            return item
        for block in content:
            current = block.get('text') if isinstance(block, dict) else getattr(block, 'text', None)
            if not isinstance(current, str):
                continue
            updated = filter_search_text(self.applicability, self.role, self.project_id, current)
            if updated == current:
                continue
            if isinstance(block, dict):
                block['text'] = updated
            else:
                block.text = updated
        return item


class DelegationContract:
    """Configured identity boundary. No unsafe fallback to native in-memory tasks.

    The durable dispatcher integration is intentionally absent until its receipt,
    backend verification and crash-reconciliation contract has been implemented.
    """
    def __init__(self, config):
        roles = config.get('managed_role_ids', [])
        if not isinstance(roles, list) or any(not isinstance(role, str) or not role.strip() for role in roles):
            raise ValueError('managed_role_ids 必须为非空角色标识列表')
        self.roles = frozenset(roles)
        target = config.get('codex_target_agent_id')
        if target is not None and (not isinstance(target, str) or not target.strip()):
            raise ValueError('codex_target_agent_id 必须为正式角色标识')
        if target in self.roles:
            raise ValueError('Codex 执行角色不能同时作为受限协调角色')
        self.target = target
        self.bridge = None
        self.governance = None
        self.applicability = None
        self.role_projects = {}

    async def submit(self, instruction: str) -> ToolChunk:
        role = get_current_agent_id()
        session = get_current_session_id()
        if role not in self.roles or not session:
            return tool_result('拒绝委派：缺少受管角色与可信任务会话', ToolResultState.DENIED)
        if self.governance is not None and await self.governance.is_blocked(role):
            return tool_result('记忆索引删除尚未核验，拒绝新委派', ToolResultState.DENIED)
        if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 32000:
            return tool_result('执行说明必须为 1 至 32000 字符', ToolResultState.ERROR)
        if not self.target:
            return tool_result('尚未配置服务端固定 Codex 角色', ToolResultState.ERROR)
        if self.bridge is not None:
            try:
                result = await self.bridge.submit_from_parent(role, session, instruction)
                return tool_result(json.dumps(result, ensure_ascii=False), ToolResultState.SUCCESS)
            except HTTPException as exc:
                return tool_result(str(exc.detail), ToolResultState.ERROR)
        return tool_result(
            '尚未配置并验证持久委派接口；没有启动 Codex。'
            '需由适配服务关联当前角色会话与已登记任务、验证固定目标的 Codex 后端并完成幂等登记。',
            ToolResultState.ERROR,
        )

    async def execution_status(self):
        role, session = get_current_agent_id(), get_current_session_id()
        if role not in self.roles or not session:
            return tool_result('缺少受管角色与可信任务会话', ToolResultState.DENIED)
        if self.bridge is None:
            return tool_result('尚未配置持久委派接口', ToolResultState.ERROR)
        try:
            task_id = self.bridge.ledger.bound_task(role, session)
            result = await self.bridge.observe(task_id)
            return tool_result(json.dumps(result, ensure_ascii=False), ToolResultState.SUCCESS)
        except HTTPException as exc:
            return tool_result(str(exc.detail), ToolResultState.ERROR)

    def middleware(self, ctx, agent_config):
        # Do not raise in the factory: QwenPaw logs and skips failing factories.
        role = getattr(ctx, 'agent_id', None)
        guarded_roles = self.roles | (self.governance.roles if self.governance is not None else frozenset())
        if role in guarded_roles or (not role and (guarded_roles or self.governance is not None)):
            applicability = getattr(self, 'applicability', None)
            projects = getattr(self, 'role_projects', {}) or {}
            project_id = projects.get(role) if isinstance(role, str) else None
            broken = applicability is not None and getattr(applicability, 'available', True) is False
            if broken and role:
                selected, selected_project = applicability, None
            elif applicability is not None and project_id:
                selected, selected_project = applicability, project_id
            else:
                selected, selected_project = None, None
            return ManagedRoleToolGuard(
                self.governance, role, self.bridge, selected, selected_project)
        return None


def require_identity(request: Request) -> str:
    """Fail closed even when host auth is disabled or bypassed for localhost.

    The host AuthMiddleware supplies a verified username in request.state.user.
    Authorization headers alone are not accepted as evidence of identity.
    """
    identity = getattr(request.state, 'user', None)
    if not isinstance(identity, str) or not identity.strip():
        raise HTTPException(status_code=401, detail='请先登录 QwenPaw 后访问工作台')
    return identity


def build_router(governance=None, boundary=None) -> APIRouter:
    router = APIRouter(dependencies=[Depends(require_identity)])

    @router.get('/health')
    def health():
        return {'status': 'degraded' if (governance is not None and not governance.available) or (boundary is not None and getattr(boundary.bridge, 'available', True) is False) else 'ok',
                'executionBinding': 'unavailable' if boundary is not None and getattr(boundary.bridge, 'available', True) is False else 'available' if boundary is not None and boundary.bridge is not None else 'disabled',
                'scope': 'plugin-registration-only',
                'memoryGovernance': 'disabled' if governance is None else 'available' if governance.available else 'unavailable',
                'message': '记忆治理不可用，新推理与委派已阻断' if governance is not None and not governance.available else '插件接口已注册，执行链路尚待验证'}

    @router.get('/capabilities')
    def capabilities():
        return {'capabilities': {
            'plugin_http_registration': 'available',
            'memory_governance': 'disabled' if governance is None else 'available' if governance.available else 'unavailable',
            'frontend_browser_mount': 'unknown',
            'agent_reuse_after_restart': 'unknown',
            'codex_execution': 'unknown',
            'codex_approval_roundtrip': 'unknown',
            'memory_roundtrip': 'unknown',
            'durable_execution_recovery': 'unknown',
            'sim_workflow_execution': 'unknown',
        }, 'message': '未知表示尚未通过真实端到端验收，不表示已支持'}

    return router


class AgentLinePlugin:
    def register(self, api: PluginApi):
        boundary = DelegationContract(api.config)
        governance_config = api.config.get('memory_governance')
        if 'memory_governance' in api.config:
            try:
                from .memory_governance import MemoryGovernance
                boundary.governance = MemoryGovernance(governance_config)
            except Exception:
                # Official loader otherwise skips registration, removing guards.
                # Never create or replace state and never expose raw diagnostics.
                boundary.governance = UnavailableMemoryGovernance(governance_config, boundary.roles)
        if 'memory_applicability' in api.config:
            raw = api.config.get('memory_applicability')
            try:
                from agent_line.memory_applicability import MemoryApplicability, validate_role_projects
                if not isinstance(raw, dict) or set(raw) - {'database', 'roles'}:
                    raise ValueError('applicability_config_invalid')
                boundary.role_projects = validate_role_projects(raw.get('roles'))
                boundary.applicability = MemoryApplicability(raw.get('database'))
            except Exception:
                boundary.applicability = UnavailableApplicability()
                boundary.role_projects = {}
        async def agent_line_delegate(instruction: str) -> ToolChunk:
            """委派当前工作流节点的执行说明；目标与任务身份由服务端决定。"""
            return await boundary.submit(instruction)

        api.register_tool(
            tool_name='agent_line_delegate', tool_func=agent_line_delegate,
            description='将当前节点交给固定 Codex 执行角色；持久委派未验证时拒绝执行',
            enabled=False, tool_type='internal',
        )
        async def agent_line_execution_status() -> ToolChunk:
            """查询当前已绑定节点的真实执行状态、身份和证据，不会启动新执行。"""
            return await boundary.execution_status()
        api.register_tool(tool_name='agent_line_execution_status', tool_func=agent_line_execution_status,
            description='只读查询当前任务原生执行状态', enabled=False, tool_type='internal')
        api.register_middleware(boundary.middleware, priority=1)
        router = build_router(boundary.governance, boundary)
        bridge_config = api.config.get('execution_bridge')
        if bridge_config:
            from .execution_bridge import ExecutionBridge
            try:
                bridge = ExecutionBridge({**bridge_config, 'managed_role_ids':list(boundary.roles)})
                if boundary.target != bridge.target:
                    raise ValueError('协调工具与执行桥接的固定目标必须一致')
                bridge.governance = boundary.governance
            except Exception:
                bridge = UnavailableExecutionBridge()
            boundary.bridge = bridge
            router.include_router(bridge.router())
        if boundary.governance is not None:
            router.include_router(boundary.governance.router())
        try:
            from agent_line.memory_applicability import build_applicability_router
            broken = boundary.applicability is not None and getattr(boundary.applicability, 'available', True) is False
            router.include_router(build_applicability_router(
                None if broken else boundary.applicability, boundary.role_projects, broken=broken))
        except Exception:
            # A missing ledger package must not remove the existing guards.
            pass
        api.register_http_router(router, prefix='/agent-line', tags=['Agent Line'])


plugin = AgentLinePlugin()
