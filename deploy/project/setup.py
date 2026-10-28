"""One-time single-project wiring after official QwenPaw initialization.

This is an offline deployment command, never a project-switching HTTP API.
Existing bridges, executor state and adapter histories are never adopted.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import stat
import subprocess
import sys
import time
from urllib.parse import urlsplit

from agent_line.execution_runtime import _json, _path
from .spec import normalize_spec

ROOT = Path(__file__).resolve().parents[2]
MARKER = 'single-project-setup.json'


def write_new(path, value):
    path = Path(path)
    payload = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def update_marker(path, value):
    temporary = path.with_suffix('.next')
    write_new(temporary, value)
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _private_directory(path):
    result = _path(str(path), directory=True)
    info = result.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError('安装数据目录必须为当前用户的0700目录')
    return result


def preflight(spec_path, *, root=ROOT):
    """Only local reads and a loopback listener check; no native config repair."""
    root = _path(str(root), directory=True)
    source = _path(str(spec_path), private=True)
    spec = normalize_spec(_json(source), root=root)
    data, private = root / 'var/qwenpaw', root / 'var/qwenpaw-secrets'
    _private_directory(root / 'var')
    _private_directory(data)
    _private_directory(private)
    expected_account = native_environment(root)['QWENPAW_KEYRING_ACCOUNT']
    if os.environ.get('QWENPAW_KEYRING_ACCOUNT', expected_account) != expected_account:
        raise ValueError('当前环境指定其他安装的密钥账户；请保留本安装的官方隔离账户')
    config_path = _path(str(data / 'config.json'), private=True)
    config = _json(config_path)
    if not isinstance(config, dict) or not isinstance(config.get('agents'), dict):
        raise ValueError('请先通过官方初始化准备QwenPaw配置')
    profiles = config['agents'].get('profiles')
    plugins = config.get('plugins', {})
    if not isinstance(profiles, dict) or not isinstance(plugins, dict):
        raise ValueError('原生角色或插件配置无效')
    extension = plugins.get('agent-line', {})
    if not isinstance(extension, dict) or set(extension) - {'enabled'}:
        raise ValueError('已有Agent Line配置，禁止重挂项目')
    roles = [spec['coordinator'], *(spec.get('team') or {}).values()]
    for role in [*roles, spec['executor']]:
        if role['id'] in profiles:
            raise ValueError('正式角色ID已存在，禁止收养或覆盖')
    login_path = _path(str(private / 'local-login.json'), private=True)
    login = _json(login_path)
    if (not isinstance(login, dict) or set(login) != {'username', 'password'}
            or any(not isinstance(v, str) or not v for v in login.values())):
        raise ValueError('请先准备官方主程序的私有登录配置')
    controls = {'QWENPAW_KEYRING_ACCOUNT', 'CODEX_HOME', 'CODEX_BINARY',
                'QWENPAW_RUNTIME_ID', 'QWENPAW_RUNTIME_TOKEN', 'QWENPAW_DESKTOP',
                'QWENPAW_RUNTIME_INTERNAL_TOKEN', 'QWENPAW_RUNTIME_READY_FILE'}
    for path in (private / 'envs.json', data / 'envs.json'):
        if path.exists() or path.is_symlink():
            values = _json(_path(str(path), private=True))
            if not isinstance(values, dict) or any(key.upper() in controls for key in values):
                raise ValueError('原生持久环境含安装控制项，不能安全隔离新项目')
    # The single bridge cannot preserve old task observations after reassignment.
    # Even empty leftover directories are evidence of an interrupted installation.
    forbidden = [root / 'var/adapter', root / 'var/executor', root / 'var' / MARKER,
        data / 'agent-line-execution-receipts.sqlite3', data / 'agent-line-memory-governance.sqlite3',
        private / 'execution-service-token', private / 'memory-service-token',
        private / 'restart-settings.json',
        *(data / 'workspaces' / role['id'] for role in roles)]
    if any(p.exists() or p.is_symlink() for p in forbidden):
        raise ValueError('检测到已有接线或初始化记录；禁止覆盖、重试或切换项目')
    # A recorded live host is rejected even if its port differs from the spec.
    state_path = data / 'agent-line-process.json'
    if state_path.exists() or state_path.is_symlink():
        state = _json(_path(str(state_path), private=True))
        pid = state.get('pid')
        if type(pid) is not int or pid < 1 or Path('/proc', str(pid)).exists():
            raise ValueError('QwenPaw尚未确认停机，不能离线安装项目')
    origin = urlsplit(spec['qwenpaw']['baseURL'])
    try:
        with socket.create_connection((origin.hostname, origin.port or (443 if origin.scheme == 'https' else 80)), timeout=.3):
            raise ValueError('QwenPaw地址仍在监听，请先正常停止原生服务')
    except ConnectionRefusedError:
        pass
    except OSError:
        raise ValueError('QwenPaw监听状态未确认，不能离线安装项目') from None
    for relative in ('deploy/executor/launcher.py', 'deploy/executor/control.py',
                     'plugins/agent-line/plugin.json', 'plugins/agent-line/role_revision.py'):
        _path(str(root / relative))
    return spec, hashlib.sha256(source.read_bytes()).hexdigest()


def native_environment(root):
    env = dict(os.environ, QWENPAW_WORKING_DIR=str(root / 'var/qwenpaw'),
        QWENPAW_SECRET_DIR=str(root / 'var/qwenpaw-secrets'), QWENPAW_AUTH_ENABLED='true')
    for key in ('QWENPAW_RUNTIME_ID', 'QWENPAW_RUNTIME_TOKEN', 'QWENPAW_DESKTOP',
                'QWENPAW_RUNTIME_INTERNAL_TOKEN', 'QWENPAW_RUNTIME_READY_FILE', 'CODEX_HOME', 'CODEX_BINARY'):
        env.pop(key, None)
    # Same account as the official SECRET_DIR-derived default, independent of
    # any inherited account override belonging to another installation.
    env['QWENPAW_KEYRING_ACCOUNT'] = 'master_key:' + hashlib.sha256(
        str(root / 'var/qwenpaw-secrets').encode()).hexdigest()[:16]
    return env


def native_command(root, arguments):
    result = subprocess.run([sys.executable, *arguments], env=native_environment(root),
        capture_output=True, timeout=180, check=False)
    # Native output may include provider/installation details. Never forward it
    # to the CLI, exception chains, public reports or a model prompt.
    if result.returncode:
        write_new(root / 'var/adapter/native-step-error.json', {
            'step': 'official-create' if arguments[:2] == ['-m', 'qwenpaw'] else 'official-configure',
            'returncode': result.returncode,
            'stdout': result.stdout[-16384:].decode('utf-8', errors='replace'),
            'stderr': result.stderr[-16384:].decode('utf-8', errors='replace')})
        raise ValueError('官方离线配置步骤失败；保留原安装记录，禁止自动重做')


def initialize_stores(root):
    from agent_line.store import TaskStore
    from agent_line.role_dispatch import RoleDispatchLedger
    from agent_line.sim_journal import SimJournal
    from agent_line.approvals import ApprovalLedger
    from agent_line.approval_discovery import WorkflowRunLedger
    from agent_line.pause_bindings import PauseBindings
    from agent_line.memory import MemoryCommitStore
    from agent_line.memory_review import MemoryReviewLedger
    directory = root / 'var/adapter'
    for name, cls in [('tasks', TaskStore), ('roles', RoleDispatchLedger), ('sim-journal', SimJournal),
                     ('approvals', ApprovalLedger), ('runs', WorkflowRunLedger), ('pauses', PauseBindings),
                     ('memory-commits', MemoryCommitStore), ('memory-reviews', MemoryReviewLedger)]:
        cls.initialize(directory / (name + '.sqlite'))


def adapter_configuration(root, spec, revision, installation):
    """Use the existing deployment schemas; no new runtime configuration layer."""
    project = spec['project']['id']
    bindings = {'developer': spec['coordinator']}
    bindings.update(spec.get('team') or {})
    revisions = revision if isinstance(revision, dict) else {'developer': revision}
    directory = root / 'var/adapter'
    private = root / 'var/qwenpaw-secrets'
    workspace = spec['project']['workspace']
    service_token, editor_token, approval_token, memory_token = [secrets.token_urlsafe(48) for _ in range(4)]
    expiry = time.time() + 365 * 86400
    def credential(token, subject, capabilities):
        return {'token': token, 'subject': subject, 'projectScopes': [project],
                'capabilities': capabilities, 'expiresAt': expiry}
    service = {'schemaVersion': 1, 'database': str(directory / 'tasks.sqlite'),
        'projects': {project: {'name': spec['project']['name'], 'workspace': workspace}},
        'tokens': {service_token: [project]},
        'bindings': {binding: {'projectId': project, 'roleAgentId': role['id'], 'roleRevision': revisions[binding],
            'name': role['name'], 'taskRole': binding,
            'executionAccess': 'readonly' if binding in ('planner', 'reviewer') else 'delegate',
            'memoryPolicyIds': ['native-role'], 'outputSchemaIds': ['task-result'],
            'verificationPolicy': spec['verificationPolicy']} for binding, role in bindings.items()},
        'sim': {'authFile': spec['sim']['authFile'], 'journal': str(directory / 'sim-journal.sqlite'),
                'installationId': installation, 'bindings': {}},
        'executionFile': 'execution.json', 'authoringFile': 'authoring.json',
        'approvalFile': 'approvals.json', 'memoryFile': 'memory.json', 'taskFlowFile': 'task-flow.json',
        'verification': {'evidenceDirectory': str(directory / 'evidence'),
                         'testExecutor': {'imageId': spec['verification']['imageId']}},
        'readinessFile': 'readiness.json'}
    execution = {'schemaVersion': 1, 'gateways': [{'projectId': project, 'roleBindingId': binding,
        'roleAgentId': role['id'], 'workspace': workspace, 'installationId': installation,
        'ledgerPath': str(directory / 'roles.sqlite'), 'baseURL': spec['qwenpaw']['baseURL'],
        'hostCredentialsFile': str(private / 'local-login.json'),
        'serviceTokenFile': str(private / 'execution-service-token'),
        'memoryQuery': spec['memoryQuery'], 'requestTimeoutSeconds': 60, 'parentTimeoutSeconds': 3600}
        for binding, role in bindings.items()]}
    authoring = {'schemaVersion': 1, 'workspaces': {project: spec['sim']['workspaceId']},
        'credentials': [credential(editor_token, 'project-owner-editor', ['edit_workflows', 'publish_workflows'])]}
    approvals = {'schemaVersion': 1, 'ledgerPath': str(directory / 'approvals.sqlite'),
        'runLedgerPath': str(directory / 'runs.sqlite'),
        'credentials': [credential(approval_token, 'project-owner-approval', ['approve'])]}
    memory = {'schemaVersion': 1, 'reviewLedgerPath': str(directory / 'memory-reviews.sqlite'),
        'credentials': [credential(memory_token, 'project-owner-memory', ['review_memory', 'delete_memory'])],
        'bindings': {binding: {'projectId': project, 'roleBindingId': binding, 'roleAgentId': role['id'],
            'baseURL': spec['qwenpaw']['baseURL'], 'hostCredentialsFile': str(private / 'local-login.json'),
            'commitLedgerPath': str(directory / 'memory-commits.sqlite'),
            'deletionServiceTokenFile': str(private / 'memory-service-token')}
            for binding, role in bindings.items()}}
    flow = {'schemaVersion': 1, 'runLedgerPath': str(directory / 'runs.sqlite'),
            'pauseBindingsPath': str(directory / 'pauses.sqlite')}
    for name, data in [('service', service), ('execution', execution), ('authoring', authoring),
                       ('approvals', approvals), ('memory', memory), ('task-flow', flow)]:
        write_new(directory / (name + '.json'), data)


def install(spec_path, *, root=ROOT, run_native=native_command):
    spec, digest = preflight(spec_path, root=root)
    root = Path(root)
    marker = root / 'var' / MARKER
    state = {'schemaVersion': 1, 'phase': 'installing', 'specSha256': digest,
        'projectId': spec['project']['id'], 'installationId': secrets.token_hex(16),
        'startedAt': time.time(), 'controlledExecution': 'unknown'}
    write_new(marker, state)  # Exclusive durable intent before any official write.
    try:
        for directory in (root / 'var/adapter', root / 'var/executor'):
            directory.mkdir(mode=0o700)
        for directory in (root / 'var/adapter/evidence', root / 'var/executor/launches'):
            directory.mkdir(mode=0o700)
        write_new(root / 'var/adapter/project-spec.json', spec)
        for filename in ('execution-service-token', 'memory-service-token'):
            path = root / 'var/qwenpaw-secrets' / filename
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as stream:
                stream.write(secrets.token_urlsafe(48))
                stream.flush(); os.fsync(stream.fileno())
        write_new(root / 'var/executor/config.json', {
            'workspace': spec['project']['workspace'], 'codex_home': spec['executor']['codexHome'],
            'binary': spec['executor']['binary'], 'image': spec['executor']['imageId']})
        write_new(root / 'var/qwenpaw-secrets/restart-settings.json', {
            'CODEX_HOME': spec['executor']['codexHome'],
            'CODEX_BINARY': str(root / 'deploy/executor/launcher.py')})
        coordinator = spec['coordinator']
        run_native(root, ['-m', 'qwenpaw', 'plugin', 'install', str(root / 'plugins/agent-line'), '--force'])
        for role in [*(spec.get('team') or {}).values(), coordinator]:
            run_native(root, ['-m', 'qwenpaw', 'agents', 'create', '--name', role['name'],
                '--agent-id', role['id'], '--language', 'zh', '--provider-id', role['providerId'],
                '--model-id', role['modelId'], '--workspace-dir',
                str(root / 'var/qwenpaw/workspaces' / role['id'])])
        run_native(root, [str(Path(__file__).with_name('native.py')), str(root)])
        native_record = _json(_path(str(root / 'var/adapter/native-role.json'), private=True))
        revision = native_record.get('roleRevisions', {'developer': native_record['roleRevision']})
        from agent_line.role_revision import validate_role_revision
        for value in revision.values():
            validate_role_revision(value)
        initialize_stores(root)
        adapter_configuration(root, spec, revision, state['installationId'])
        state.update(phase='configured', configuredAt=time.time(), roleRevision=native_record['roleRevision'],
            roleRevisions=revision,
            nativeExecutionVerified=False, serviceStarted=False, readinessGenerated=False)
        update_marker(marker, state)
    except BaseException:
        state.update(phase='incomplete', errorCode='single_project_setup_incomplete')
        update_marker(marker, state)
        raise
    return public_status(root)


def public_status(root=ROOT):
    state = _json(_path(str(Path(root) / 'var' / MARKER), private=True))
    return {key: state[key] for key in ('phase', 'projectId', 'controlledExecution',
        'nativeExecutionVerified', 'serviceStarted', 'readinessGenerated') if key in state}


def main():
    parser = argparse.ArgumentParser(description='为全新安装登记唯一代码项目；不会启动服务或模型')
    parser.add_argument('action', choices=('check', 'install', 'status'))
    parser.add_argument('--spec', type=Path)
    args = parser.parse_args()
    try:
        if args.action == 'status':
            result = public_status()
        elif args.spec is None:
            parser.error('check/install需要私有 --spec 文件')
        elif args.action == 'check':
            spec, _ = preflight(args.spec)
            result = {'localChecks': 'passed', 'projectId': spec['project']['id'],
                      'controlledExecution': 'unknown', 'mutations': 0}
        else:
            result = install(args.spec)
    except (ValueError, OSError, subprocess.SubprocessError):
        # Input paths, model identifiers and native error output remain private.
        print('项目配置未完成：请检查私有配置、停机状态及原安装记录；已有安装不会覆盖。', file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
