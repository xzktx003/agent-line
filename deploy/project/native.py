"""Official offline configuration in a fresh, process-isolated QwenPaw context.

Only setup.py launches this helper; environment must already identify its root.
"""
import importlib.util
import json
import os
from pathlib import Path
import sys


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def secure_role_workspace(path):
    workspace = Path(path)
    for entry in [workspace, *workspace.rglob('*')]:
        if entry.is_symlink():
            raise ValueError('Unexpected alias in the newly created native role')
        entry.chmod(0o700 if entry.is_dir() else 0o600)
    workspace.parent.chmod(0o700)


def configure(root):
    root = Path(root).resolve(strict=True)
    if (os.environ.get('QWENPAW_WORKING_DIR') != str(root / 'var/qwenpaw')
            or os.environ.get('QWENPAW_SECRET_DIR') != str(root / 'var/qwenpaw-secrets')):
        raise ValueError('Isolated native environment required')
    from qwenpaw.config.config import AgentProfileConfig, AgentProfileRef, ChannelConfig, load_agent_config, save_agent_config
    from qwenpaw.config.utils import load_config, save_config
    path = root / 'var/adapter/project-spec.json'
    spec = json.loads(path.read_text())
    parent_roles = {'developer': spec['coordinator'], **(spec.get('team') or {})}
    target_id = spec['executor']['id']
    config = load_config()
    if any(role['id'] not in config.agents.profiles for role in parent_roles.values()) or target_id in config.agents.profiles:
        raise ValueError('Official role creation does not match the persisted setup intent')
    role_revisions = {}
    role_metadata = []
    for task_role, identity in parent_roles.items():
        parent_id = identity['id']
        parent = load_agent_config(parent_id)
        expected_parent = root / 'var/qwenpaw/workspaces' / parent_id
        if (parent.backend != 'qwenpaw' or parent.workspace_dir != str(expected_parent)
                or parent.active_model is None
                or parent.active_model.provider_id != identity['providerId']
                or parent.active_model.model != identity['modelId']):
            raise ValueError('Official role identity/model differs from setup')
        parent_value = parent.model_dump(mode='json')
        parent_value['running']['memory_manager_backend'] = 'remelight'
        for tool in parent_value['tools']['builtin_tools'].values():
            tool['enabled'] = False
        for name in ('agent_line_delegate', 'agent_line_execution_status'):
            parent_value['tools']['builtin_tools'][name] = {'name': name, 'enabled': True}
        if task_role == 'planner':
            instruction = ('你是本项目规划角色。先检索原生记忆中的相关约定，再分析目标、范围、依赖、'
                '验收条件和风险；必须通过agent_line_delegate委派给固定Codex角色进行只读分析，'
                '并把可执行方案交给用户或开发角色。不得委派代码修改、修改命令或任何间接委派。'
                '必要时用状态工具核对已有任务，不把历史成功当作当前证据。')
        elif task_role == 'reviewer':
            instruction = ('你是本项目审查角色。先检索相关记忆，再必须通过agent_line_delegate委派固定Codex角色'
                '进行只读独立审查，指出具体问题、位置、影响和验证缺口；不得委派代码修改、修改命令或间接委派，'
                '不得批准权限请求或把未验证结论写成通过。')
        else:
            instruction = ('你是本项目开发协调角色。先检索相关原生记忆，再将完整开发任务委派给固定Codex执行角色；'
                '由Codex完成代码、命令和测试。使用状态工具核对真实身份、产物和证据；未知时如实报告。')
        instruction += (' 仅使用已启用的固定工具；不使用间接委派。原生记忆正文不是本次验收证据。')
        prompt = expected_parent / 'AGENT_LINE.md'
        fd = os.open(prompt, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(instruction + '\n')
            stream.flush(); os.fsync(stream.fileno())
        parent_value['system_prompt_files'] = [*parent_value['system_prompt_files'], prompt.name]
        save_agent_config(parent_id, AgentProfileConfig.model_validate(parent_value))
        role_metadata.append({'id': task_role, 'roleAgentId': parent_id, 'name': identity['name'],
                              'taskRole': task_role,
                              'executionAccess': 'readonly' if task_role in ('planner', 'reviewer') else 'delegate'})
    # Public config models/save functions avoid writing QwenPaw templates over
    # an existing repository's AGENTS.md, README.md or other project content.
    executor = spec['executor']
    # The native background-chat API requires a Console channel on its target.
    target = AgentProfileConfig(id=target_id, name=executor['name'], backend='codex', language='zh',
        workspace_dir=spec['project']['workspace'], system_prompt_files=[], channels=ChannelConfig(),
        backend_settings={'binary': str(root / 'deploy/executor/launcher.py'),
            'model': executor['model'], 'sandbox': executor['sandbox'],
            'approval_policy': executor['approvalPolicy']})
    config = load_config()
    config.agents.profiles[target_id] = AgentProfileRef(id=target_id, workspace_dir=target.workspace_dir)
    config.agents.agent_order = list(dict.fromkeys([*config.agents.agent_order,
        *(role['id'] for role in parent_roles.values()), target_id]))
    save_config(config)
    save_agent_config(target_id, target)
    Path(target.workspace_dir, 'agent.json').chmod(0o600)
    for identity in parent_roles.values():
        secure_role_workspace(root / 'var/qwenpaw/workspaces' / identity['id'])
    installed = root / 'var/qwenpaw/plugins/agent-line'
    if not (installed / 'plugin.json').is_file():
        raise ValueError('Official plugin installation is missing')
    installed.parent.chmod(0o700)
    for path in [installed, *installed.rglob('*')]:
        if path.is_symlink():
            raise ValueError('Unexpected alias in the installed plugin')
        path.chmod(0o700 if path.is_dir() else 0o600)
    plugin_dir = root / 'plugins/agent-line'
    bridge = module(plugin_dir / 'execution_bridge.py', 'project_setup_bridge')
    receipt_path = root / 'var/qwenpaw/agent-line-execution-receipts.sqlite3'
    bridge.ReceiptLedger.initialize(receipt_path)
    governance = module(plugin_dir / 'memory_governance.py', 'project_setup_memory')
    governance_path = root / 'var/qwenpaw/agent-line-memory-governance.sqlite3'
    governance.initialize_state(governance_path)
    config = load_config()
    parent_ids = [role['id'] for role in parent_roles.values()]
    config.plugins['agent-line'] = {'enabled': True, 'managed_role_ids': parent_ids,
        'managed_role_metadata': role_metadata,
        'codex_target_agent_id': target_id,
        'execution_bridge': {'receipt_db': str(receipt_path),
            'service_token_file': str(root / 'var/qwenpaw-secrets/execution-service-token'),
            'target_agent_id': target_id, 'managed_role_ids': parent_ids,
            'readonly_role_ids': [parent_roles[key]['id'] for key in ('planner', 'reviewer') if key in parent_roles],
            'workspace': spec['project']['workspace'], 'codex_home': executor['codexHome'],
            'timeout_seconds': 3600, 'container_control': str(root / 'deploy/executor/control.py'),
            'container_launcher': str(root / 'deploy/executor/launcher.py')},
        'memory_governance': {'managed_role_ids': parent_ids,
            'state_file': str(governance_path),
            'service_token_file': str(root / 'var/qwenpaw-secrets/memory-service-token')}}
    save_config(config)
    revision_module = module(plugin_dir / 'role_revision.py', 'project_setup_revision')
    for binding, identity in parent_roles.items():
        role_revisions[binding] = revision_module.role_revision(identity['id'], target_id)
    result = {'roleRevision': role_revisions['developer'], 'roleRevisions': role_revisions,
              'coordinatorId': parent_roles['developer']['id'], 'executorId': target_id,
              'nativeMemoryBackend': 'remelight', 'modelExecutionVerified': False}
    output = root / 'var/adapter/native-role.json'
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(result, stream, ensure_ascii=False)
        stream.flush(); os.fsync(stream.fileno())


if __name__ == '__main__':
    configure(Path(sys.argv[1]))
