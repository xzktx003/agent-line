#!/usr/bin/env python3
"""One-shot native QwenPaw parent -> Agent Line -> Codex Luna proof.

All candidate state, logs, and model transcripts stay under one private var tree.
The probe never adopts main QwenPaw configuration or retries an unknown submit.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import secrets

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / 'var/luna-native-candidate-20261022'
INSTALL = DATA / 'install'
WORKSPACE = DATA / 'workspace'
CODEX_HOME = DATA / 'codex-home'
STATE = DATA / 'state.json'
PARENT = 'luna-candidate-parent'
TARGET = 'luna-candidate-codex'
PROJECT = 'luna-candidate-project'
TASK = 'luna-native-candidate-20261022'
SESSION = 'agent-line-' + TASK
MARKER = 'AGENT_LINE_LUNA_NATIVE_20261022'
MODEL = 'gpt-6-luna'
PYTHON = ROOT / '.venv/bin/python'
SOURCE_HOME = ROOT / 'var/executor/codex-home'
EXECUTOR_CONFIG = ROOT / 'var/executor/config.json'


def save(path, value, *, exclusive=False):
    path = Path(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW
    flags |= os.O_EXCL if exclusive else os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n'); stream.flush(); os.fsync(stream.fileno())


def read(path):
    return json.loads(Path(path).read_text())


def invoke(args, *, env, cwd, logfile, timeout=180):
    with Path(logfile).open('ab') as out:
        os.chmod(logfile, 0o600)
        result = subprocess.run(args, env=env, cwd=cwd, stdout=out,
                                stderr=out, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError('official_step_failed')


def private_env(root):
    env = {k: os.environ[k] for k in ('PATH', 'HOME', 'LANG', 'LC_ALL', 'TZ',
            'SSL_CERT_FILE', 'SSL_CERT_DIR',
            'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
            'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy') if k in os.environ}
    env.update(QWENPAW_WORKING_DIR=str(root / 'var/qwenpaw'),
        QWENPAW_SECRET_DIR=str(root / 'var/qwenpaw-secrets'),
        QWENPAW_AUTH_ENABLED='true', QWENPAW_DISABLE_KEYRING='true',
        QWENPAW_KEYRING_ACCOUNT='master_key:' + hashlib.sha256(
            str(root / 'var/qwenpaw-secrets').encode()).hexdigest()[:16],
        PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1',
        PYTHONPATH=str(ROOT / 'upstream/qwenpaw/src'))
    return env


def copy_sources():
    DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    if stat_mode(DATA) != 0o700:
        raise RuntimeError('private_directory_mode')
    for path in (INSTALL, WORKSPACE, CODEX_HOME):
        path.mkdir(mode=0o700, exist_ok=True)
        if stat_mode(path) != 0o700:
            raise RuntimeError('candidate_directory_mode')
    for rel in ('agent_line', 'deploy/project', 'plugins/agent-line'):
        source, destination = ROOT / rel, INSTALL / rel
        if not destination.exists():
            shutil.copytree(source, destination,
                ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    for name in ('launcher.py', 'control.py', 'lifecycle.py'):
        source = ROOT / 'deploy/executor' / name
        destination = INSTALL / 'deploy/executor' / name
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not destination.exists():
            shutil.copyfile(source, destination)
            destination.chmod(0o700)
        if destination.is_symlink() or destination.read_bytes() != source.read_bytes():
            raise RuntimeError('executor_source_copy_mismatch')
    (INSTALL / 'deploy/project/native.py').parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name in ('native.py', 'spec.py', 'setup.py'):
        source = ROOT / 'deploy/project' / name
        destination = INSTALL / 'deploy/project' / name
        if destination.exists():
            if destination.is_symlink() or destination.read_bytes() != source.read_bytes():
                raise RuntimeError('executor_source_copy_mismatch')
        else:
            shutil.copyfile(source, destination)
    # Candidate official setup imports the project modules as a namespace package.
    auth = SOURCE_HOME / 'auth.json'
    if auth.is_symlink() or not auth.is_file():
        raise RuntimeError('codex_auth_unavailable')
    copied_auth = CODEX_HOME / 'auth.json'
    if copied_auth.exists():
        if copied_auth.is_symlink() or copied_auth.read_bytes() != auth.read_bytes():
            raise RuntimeError('codex_auth_already_provisioned')
    else:
        shutil.copyfile(auth, copied_auth)
    os.chmod(copied_auth, 0o600)
    for item in CODEX_HOME.iterdir():
        if item.name != 'auth.json':
            raise RuntimeError('codex_home_not_fresh')


def stat_mode(path):
    return Path(path).stat().st_mode & 0o777


def request(base, path, *, body=None, method='GET', headers=None, timeout=120):
    payload = None if body is None else json.dumps(body).encode()
    request_headers = {'Content-Type': 'application/json'}
    request_headers.update(headers or {})
    req = urllib.request.Request(base + path, data=payload,
                                 method=method, headers=request_headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read()
        return json.loads(raw) if raw else {}


def extract_chain(parent, expected_task):
    calls, linked = set(), None
    for message in parent.get('result', {}).get('output', []):
        for content in message.get('content', []):
            data = content.get('data') or {}
            kind, name, call_id = message.get('type'), data.get('name'), data.get('call_id')
            if kind == 'plugin_call' and name == 'agent_line_delegate':
                args = data.get('arguments')
                args = json.loads(args) if isinstance(args, str) else args
                if not isinstance(args, dict) or set(args) != {'instruction'}:
                    raise RuntimeError('delegate_arguments_invalid')
                if MARKER not in args['instruction']:
                    raise RuntimeError('delegate_marker_missing')
                calls.add(call_id)
            if (kind == 'plugin_call_output' and name == 'agent_line_delegate'
                    and call_id in calls and data.get('state') == 'success'):
                output = data.get('output')
                output = json.loads(output) if isinstance(output, str) else output
                if output.get('taskId') != expected_task:
                    raise RuntimeError('delegate_task_mismatch')
                identity = output.get('identity') or {}
                upstream = identity.get('upstreamTaskId')
                linked = {'delegateCallId': call_id, 'upstreamTaskId': upstream,
                    'status': output.get('status'),
                    'terminalConfirmed': output.get('terminalConfirmed') is True,
                    'resourcesReleased': output.get('resourcesReleased') is True}
                return linked
    raise RuntimeError('linked_delegate_tool_receipt_missing')


def rollout_receipt(thread_id, turn_id):
    if not isinstance(thread_id, str) or not isinstance(turn_id, str):
        raise RuntimeError('native_model_identity_missing')
    matches = []
    for path in CODEX_HOME.rglob('rollout-*.jsonl'):
        if path.is_symlink() or not path.is_file():
            continue
        raw = path.read_bytes(); lines = raw.splitlines()
        thread_lines = [i + 1 for i, line in enumerate(lines) if thread_id.encode() in line]
        turn_lines = [i + 1 for i, line in enumerate(lines) if turn_id.encode() in line]
        model_lines = [i + 1 for i, line in enumerate(lines) if MODEL.encode() in line]
        if thread_lines and turn_lines and model_lines:
            matches.append({'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest(),
                'threadId': thread_id, 'turnId': turn_id, 'model': MODEL,
                'threadLines': thread_lines, 'turnLines': turn_lines, 'modelLines': model_lines})
    if len(matches) != 1:
        raise RuntimeError('codex_rollout_receipt_missing')
    return matches[0]


def continue_live_candidate():
    """Resume only the already-configured host before any parent task submit."""
    state = read(STATE)
    if (state.get('phase') != 'incomplete' or state.get('parentSubmission')
            or state.get('parentTaskId') or not state.get('host')):
        raise RuntimeError('candidate_not_resumable')
    install_status = read(INSTALL / 'var/single-project-setup.json')
    if install_status.get('phase') != 'configured':
        raise RuntimeError('candidate_install_not_configured')
    host = state['host']; pid = host.get('pid')
    proc = Path('/proc') / str(pid)
    fields = (proc / 'stat').read_text().split()
    if fields[21] != host.get('startTicks'):
        raise RuntimeError('candidate_host_identity_changed')
    base = f"http://127.0.0.1:{host['port']}"
    login = read(INSTALL / 'var/qwenpaw-secrets/local-login.json')
    # Authentication must be active before accepting any parent request.
    try:
        request(base, '/api/agent-line/health', timeout=5)
        raise RuntimeError('candidate_auth_not_enforced')
    except urllib.error.HTTPError as error:
        if error.code != 401: raise RuntimeError('candidate_auth_probe_failed') from None
    auth = request(base, '/api/auth/login', method='POST', body=login)
    token = auth.get('token') or auth.get('access_token')
    if not token: raise RuntimeError('host_login_failed')
    headers = {'Authorization': 'Bearer ' + token}
    service_token = (INSTALL / 'var/qwenpaw-secrets/execution-service-token').read_text().strip()
    parent_config = request(base, '/api/agents/' + PARENT, headers=headers)
    if parent_config.get('backend') != 'qwenpaw': raise RuntimeError('parent_role_missing')
    active_model = parent_config.get('active_model') or {}
    if (active_model.get('provider_id') != 'kilo'
            or active_model.get('model') != 'kilo-auto/free'):
        raise RuntimeError('parent_model_identity_mismatch')
    api_headers = {**headers, 'X-Agent-Id': PARENT,
        'X-Agent-Line-Service-Token': service_token}
    # Plugin startup can expose the base QwenPaw health route before loading
    # plugin routers. Poll this read-only descriptor until exact role bindings exist.
    descriptor = None; deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            candidate = request(base, '/api/agent-line/execution-binding', headers=api_headers, timeout=5)
            if (isinstance(candidate, dict) and candidate.get('targetAgentId') == TARGET
                    and PARENT in candidate.get('managedRoleIds', [])
                    and candidate.get('workspace') == str(WORKSPACE)):
                descriptor = candidate; break
        except urllib.error.HTTPError as error:
            if error.code not in (404, 405, 503): raise RuntimeError('candidate_descriptor_failed') from None
        time.sleep(2)
    if descriptor is None: raise RuntimeError('candidate_descriptor_deadline')
    revision = (descriptor.get('roleRevisions') or {}).get(PARENT)
    if not isinstance(revision, str) or not revision.startswith('qwenpaw-role-v1:'):
        raise RuntimeError('candidate_role_revision_missing')
    state.update(bindingIntent={'taskId': TASK, 'roleAgentId': PARENT,
        'roleSessionId': SESSION, 'roleRevision': revision}, bindingSubmission='intent',
        phase='binding_intent')
    save(STATE, state)
    try:
        binding = request(base, '/api/agent-line/delegations/bind', method='POST',
            headers=api_headers, body={'taskId': TASK, 'roleAgentId': PARENT,
                'roleSessionId': SESSION, 'roleRevision': revision})
    except urllib.error.HTTPError as error:
        state.update(bindingSubmission='rejected', bindingHTTPStatus=error.code,
                     phase='bind_rejected')
        save(STATE, state)
        raise RuntimeError('candidate_binding_rejected') from None
    state.update(bindingSubmission='received', bindingReceipt=binding, phase='bound')
    save(STATE, state)
    instruction = ('在固定 Codex 执行角色的工作目录中创建 luna-native-proof.txt，文件内容必须严格为 '
        + MARKER + ' 后跟一个换行；执行 shell 检查字节完全相同。仅做此文件操作，不访问网络、'
        '不读取凭据、不检查其他目录。完成后如实报告。')
    user_text = ('请直接执行一次 Agent Line 原生委派。调用 agent_line_delegate，唯一参数 instruction 内容为：'
        + instruction + '。不要自己调用其他工具。')
    state.update(parentSubmission='intent', phase='parent_submit_intent')
    save(STATE, state)
    parent_response = request(base, '/api/console/chat/task', method='POST', headers=api_headers,
        body={'session_id': SESSION, 'user_id': 'luna-candidate',
            'input': [{'role': 'user', 'content': [{'type': 'text', 'text': user_text}]}],
            'timeout': 180})
    task_id = parent_response.get('task_id')
    if not task_id: raise RuntimeError('parent_submit_receipt_missing')
    state.update(parentSubmission='submitted', parentTaskId=task_id, phase='parent_running')
    save(STATE, state)
    parent = None
    for _ in range(180):
        parent = request(base, '/api/console/chat/task/' + task_id, headers=api_headers)
        if parent.get('status') == 'finished': break
        time.sleep(2)
    if not parent or parent.get('status') != 'finished':
        raise RuntimeError('parent_nonterminal_observe_only')
    save(DATA / 'parent-result.json', parent, exclusive=True)
    if (parent.get('result') or {}).get('status') != 'completed': raise RuntimeError('parent_failed')
    chain = extract_chain(parent, TASK)
    state.update(parentChain=chain, phase='delegate_linked'); save(STATE, state)
    native = None
    for _ in range(180):
        native = request(base, '/api/agent-line/executions/' + TASK, headers=api_headers)
        if native.get('terminalConfirmed') and native.get('resourcesReleased'): break
        time.sleep(2)
    if not native or not native.get('terminalConfirmed') or not native.get('resourcesReleased'):
        raise RuntimeError('native_execution_nonterminal_observe_only')
    save(DATA / 'native-execution.json', native, exclusive=True)
    state.update(nativeTerminal=True, nativeResourcesReleased=True,
        nativeExecutionStatus=native.get('status'), nativeIdentity=native.get('identity'))
    save(STATE, state)
    # Once terminal and resources released, stop only this recorded candidate host.
    if pid and fields[21] == host['startTicks']:
        os.kill(pid, 15)
    for _ in range(50):
        if not proc.exists(): break
        time.sleep(.1)
    state['hostStopped'] = not proc.exists()
    state['phase'] = 'candidate_host_stopped'; save(STATE, state)
    if native.get('status') != 'succeeded': raise RuntimeError('native_execution_failed')
    if native.get('identity', {}).get('upstreamTaskId') != chain['upstreamTaskId']:
        raise RuntimeError('delegate_execution_identity_mismatch')
    artifact = WORKSPACE / 'luna-native-proof.txt'
    if artifact.read_bytes() != (MARKER + '\n').encode(): raise RuntimeError('artifact_bytes_mismatch')
    rollout = rollout_receipt(native['identity'].get('codexThreadId'),
                              native['identity'].get('codexTurnId'))
    state.update(phase='completed', status='success', modelReceipt=rollout,
        artifact={'path': str(artifact), 'sha256': hashlib.sha256(artifact.read_bytes()).hexdigest(),
                  'bytes': len(artifact.read_bytes())})
    save(STATE, state)
    print(json.dumps({'native_parent_delegate': True, 'fixed_model': MODEL,
        'linked_upstream_execution': True, 'terminal_resources_released': True,
        'artifact_bytes_verified': True, 'rollout_model_receipt': True}))


def observe_existing_candidate():
    """Observe the one submitted identity only; never resubmit or create work."""
    state = read(STATE); host = state.get('host') or {}
    task_id = state.get('parentTaskId')
    if state.get('parentSubmission') != 'submitted' or not task_id:
        raise RuntimeError('candidate_task_identity_missing')
    proc = Path('/proc') / str(host.get('pid'))
    fields = (proc / 'stat').read_text().split()
    if fields[21] != host.get('startTicks'):
        raise RuntimeError('candidate_host_identity_changed')
    base = f"http://127.0.0.1:{host['port']}"
    login = read(INSTALL / 'var/qwenpaw-secrets/local-login.json')
    auth = request(base, '/api/auth/login', method='POST', body=login)
    token = auth.get('token') or auth.get('access_token')
    if not token: raise RuntimeError('host_login_failed')
    headers = {'Authorization': 'Bearer ' + token, 'X-Agent-Id': PARENT,
        'X-Agent-Line-Service-Token': (INSTALL / 'var/qwenpaw-secrets/execution-service-token').read_text().strip()}
    latest_parent = None; latest_native = None
    for _ in range(180):
        latest_parent = request(base, '/api/console/chat/task/' + task_id, headers=headers)
        latest_native = request(base, '/api/agent-line/executions/' + TASK, headers=headers)
        state['lastObserved'] = {'parentStatus': latest_parent.get('status'),
            'nativeStatus': latest_native.get('status'),
            'terminalConfirmed': latest_native.get('terminalConfirmed') is True,
            'resourcesReleased': latest_native.get('resourcesReleased') is True,
            'observedAt': time.time()}
        save(STATE, state)
        if latest_parent.get('status') == 'finished' and not (DATA / 'parent-result.json').exists():
            save(DATA / 'parent-result.json', latest_parent, exclusive=True)
        if latest_native.get('terminalConfirmed') and latest_native.get('resourcesReleased'):
            break
        time.sleep(2)
    if not latest_native or not latest_native.get('terminalConfirmed') or not latest_native.get('resourcesReleased'):
        state.update(phase='incomplete', status='incomplete',
            failureType='NativeExecutionUnresolved', failureCode='observe_deadline')
        save(STATE, state)
        print(json.dumps({'native_parent_task_finished': latest_parent.get('status') == 'finished',
            'native_execution_terminal': False, 'resubmit': False}))
        return
    save(DATA / 'native-execution.json', latest_native, exclusive=True)
    identity = latest_native.get('identity') or {}
    chain = extract_chain(latest_parent, TASK) if latest_parent.get('status') == 'finished' else None
    # The tool output may have returned unknown after native acceptance; use the
    # route's exact identity only when it carries the same upstream receipt.
    if not chain or not chain.get('upstreamTaskId') or identity.get('upstreamTaskId') != chain.get('upstreamTaskId'):
        raise RuntimeError('delegate_execution_identity_mismatch')
    state.update(nativeTerminal=True, nativeResourcesReleased=True,
        nativeExecutionStatus=latest_native.get('status'), nativeIdentity=identity,
        parentChain=chain)
    save(STATE, state)
    pid = host['pid']
    if proc.exists() and fields[21] == host['startTicks']:
        os.kill(pid, 15)
    for _ in range(50):
        if not proc.exists(): break
        time.sleep(.1)
    state['hostStopped'] = not proc.exists(); state['phase'] = 'candidate_host_stopped'
    save(STATE, state)
    if latest_native.get('status') != 'succeeded':
        raise RuntimeError('native_execution_failed')
    artifact = WORKSPACE / 'luna-native-proof.txt'
    if artifact.read_bytes() != (MARKER + '\n').encode(): raise RuntimeError('artifact_bytes_mismatch')
    receipt = rollout_receipt(identity.get('codexThreadId'), identity.get('codexTurnId'))
    state.update(phase='completed', status='success', modelReceipt=receipt,
        artifact={'path': str(artifact), 'sha256': hashlib.sha256(artifact.read_bytes()).hexdigest(),
                  'bytes': len(artifact.read_bytes())})
    save(STATE, state)
    print(json.dumps({'native_parent_delegate': True, 'fixed_model': MODEL,
        'linked_upstream_execution': bool(chain), 'terminal_resources_released': True,
        'artifact_bytes_verified': True, 'rollout_model_receipt': True}))


def run():
    DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    if STATE.exists():
        state = read(STATE)
        initial_recovery = (state.get('candidate') == DATA.name
            and state.get('phase') == 'incomplete'
            and ((state.get('failureType') in {'FileExistsError', 'FileNotFoundError'})
                 or (state.get('failureType') == 'RuntimeError'
                     and state.get('failureCode') in {'executor_identity_invalid', 'official_step_failed'}))
            and not state.get('parentSubmission') and state.get('host') is None
            and not (INSTALL / 'var/single-project-setup.json').exists()
            and not (INSTALL / 'var/adapter').exists()
            and not (INSTALL / 'var/executor').exists())
        if initial_recovery:
            state.update(status='intent', phase='intent', submission='not_started')
            state.pop('failureType', None); state.pop('failureCode', None)
            save(STATE, state)
        elif (state.get('candidate') != DATA.name or state.get('phase') != 'intent'
                or state.get('submission') != 'not_started'):
            raise RuntimeError('existing_candidate_observe_only')
    else:
        state = {'schemaVersion': 1, 'status': 'intent', 'candidate': DATA.name,
        'projectId': PROJECT, 'parentRole': PARENT, 'executorRole': TARGET,
        'parentTaskIntent': TASK, 'parentSession': SESSION, 'model': MODEL,
        'marker': MARKER, 'submission': 'not_started', 'host': None,
        'createdAt': time.time()}
        save(STATE, state, exclusive=True)
    log = DATA / 'private.log'
    try:
        resume_offline_setup = (INSTALL / 'var/qwenpaw/config.json').exists()
        copy_sources()
        exec_config = read(EXECUTOR_CONFIG)
        source_binary = Path(exec_config['binary']).resolve(strict=True)
        source_home = SOURCE_HOME.resolve(strict=True)
        if source_home != SOURCE_HOME:
            raise RuntimeError('executor_identity_invalid')
        bin_dir = INSTALL / 'deploy/executor/codex-bin'
        bin_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        binary = bin_dir / 'codex'
        sidecar = bin_dir / 'codex-code-mode-host'
        for source, destination in ((source_binary, binary),
                (source_binary.parent / 'codex-code-mode-host', sidecar)):
            if source.is_symlink() or not source.is_file() or not source.stat().st_mode & 0o111:
                raise RuntimeError('executor_identity_invalid')
            if destination.exists():
                if destination.is_symlink() or destination.read_bytes() != source.read_bytes():
                    raise RuntimeError('executor_identity_invalid')
            else:
                shutil.copyfile(source, destination)
            destination.chmod(0o700)
        image = exec_config['image']
        if not image.startswith('sha256:'):
            raise RuntimeError('executor_image_invalid')
        (WORKSPACE / 'AGENTS.md').write_text(
            'For this isolated verification, create only luna-native-proof.txt with the requested exact bytes.\n')
        os.chmod(WORKSPACE / 'AGENTS.md', 0o600)
        env = private_env(INSTALL)
        for path in (INSTALL / 'var', INSTALL / 'var/qwenpaw', INSTALL / 'var/qwenpaw-secrets'):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        login_file = INSTALL / 'var/qwenpaw-secrets/local-login.json'
        if login_file.exists():
            login = read(login_file)
        else:
            login = {'username': 'luna-candidate-local', 'password': secrets.token_urlsafe(32)}
            save(login_file, login, exclusive=True)
        if not resume_offline_setup:
            invoke([str(PYTHON), '-B', '-m', 'qwenpaw', 'init', '--defaults', '--accept-security'],
                env=env, cwd=INSTALL, logfile=log)
        bootstrap = """import json,os
from pathlib import Path
from qwenpaw.config.utils import load_config,save_config
from qwenpaw.app.auth import register_user,authenticate
c=load_config();c.security.allow_no_auth_hosts=[];save_config(c)
v=json.loads((Path(os.environ['QWENPAW_SECRET_DIR'])/'local-login.json').read_text())
assert register_user(**v);assert authenticate(**v)
"""
        if not resume_offline_setup:
            invoke([str(PYTHON), '-B', '-c', bootstrap], env=env, cwd=INSTALL, logfile=log)
        port = state.get('port') if resume_offline_setup else None
        if port is None:
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        auth_file = INSTALL / 'var/sim-auth.json'
        if not auth_file.exists():
            save(auth_file, {'baseUrl': 'http://127.0.0.1:9', 'apiKey': 'unused-candidate-placeholder'}, exclusive=True)
        spec = {'schemaVersion': 1,
            'project': {'id': PROJECT, 'name': 'Luna Native Candidate', 'workspace': str(WORKSPACE)},
            'coordinator': {'id': PARENT, 'name': 'Luna Candidate Parent',
                'providerId': 'kilo', 'modelId': 'kilo-auto/free'},
            'executor': {'id': TARGET, 'name': 'Luna Candidate Codex', 'model': MODEL,
                'sandbox': 'danger-full-access', 'approvalPolicy': 'never',
                'binary': str(binary), 'imageId': image, 'codexHome': str(CODEX_HOME)},
            'qwenpaw': {'baseURL': f'http://127.0.0.1:{port}'},
            'sim': {'authFile': str(auth_file), 'workspaceId': 'unused-luna-candidate'},
            'verification': {'policyId': 'luna-candidate', 'imageId': image,
                'tests': [{'id': 'proof-exists', 'argv': ['python3', '-c',
                    "from pathlib import Path; assert Path('luna-native-proof.txt').read_bytes() == b'AGENT_LINE_LUNA_NATIVE_20261022\\n'"]}]},
            'memoryQuery': 'Luna native delegation verification'}
        spec_file = INSTALL / 'var/candidate-spec.json'
        if spec_file.exists():
            previous_spec = read(spec_file)
            if previous_spec != spec:
                if (not resume_offline_setup or (INSTALL / 'var/single-project-setup.json').exists()
                        or previous_spec.get('executor', {}).get('sandbox') != 'workspace-write'
                        or previous_spec.get('executor', {}).get('approvalPolicy') != 'on-request'):
                    raise RuntimeError('candidate_spec_mismatch')
                save(spec_file, spec)
        else:
            save(spec_file, spec, exclusive=True)
        state.update(phase='official_setup_intent', port=port,
            setupSpecSha256=hashlib.sha256(spec_file.read_bytes()).hexdigest(),
            sourceHashes={name: hashlib.sha256((INSTALL / 'deploy/executor' / name).read_bytes()).hexdigest()
                          for name in ('launcher.py', 'control.py', 'lifecycle.py')})
        save(STATE, state)
        invoke([str(PYTHON), '-B', '-m', 'deploy.project.setup', 'install', '--spec', str(spec_file)],
            env={**env, 'PYTHONPATH': str(ROOT) + os.pathsep + env['PYTHONPATH']},
            cwd=INSTALL, logfile=log, timeout=300)
        status = read(INSTALL / 'var/single-project-setup.json')
        if status.get('phase') != 'configured' or status.get('nativeExecutionVerified') is not False:
            raise RuntimeError('official_project_setup_incomplete')
        state['phase'] = 'configured'; save(STATE, state)
        process = subprocess.Popen([str(PYTHON), '-B', '-m', 'qwenpaw', 'app',
            '--host', '127.0.0.1', '--port', str(port)], env=env, cwd=INSTALL,
            stdin=subprocess.DEVNULL, stdout=log.open('ab'), stderr=subprocess.STDOUT,
            start_new_session=True)
        proc_stat = Path('/proc', str(process.pid), 'stat').read_text().split()
        state['host'] = {'pid': process.pid, 'startTicks': proc_stat[21], 'port': port}
        state['phase'] = 'host_starting'; save(STATE, state)
        base = f'http://127.0.0.1:{port}'
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if process.poll() is not None: raise RuntimeError('isolated_host_exited')
            try:
                request(base, '/api/agent-line/health', timeout=3)
            except urllib.error.HTTPError as error:
                if error.code == 401: break
                if error.code not in (404, 503): raise RuntimeError('host_health_unexpected')
            except OSError: pass
            time.sleep(1)
        else: raise RuntimeError('host_start_deadline')
        auth = request(base, '/api/auth/login', method='POST', body=login)
        token = auth.get('token') or auth.get('access_token')
        if not token: raise RuntimeError('host_login_failed')
        headers = {'Authorization': 'Bearer ' + token}
        service_token = (INSTALL / 'var/qwenpaw-secrets/execution-service-token').read_text().strip()
        parent_config = request(base, '/api/agents/' + PARENT, headers=headers)
        active_model = parent_config.get('active_model') or {}
        if (parent_config.get('backend') != 'qwenpaw'
                or active_model.get('provider_id') != 'kilo'
                or active_model.get('model') != 'kilo-auto/free'):
            raise RuntimeError('parent_role_missing')
        api_headers = {**headers, 'X-Agent-Id': PARENT,
            'X-Agent-Line-Service-Token': service_token}
        descriptor = None
        descriptor_deadline = time.monotonic() + 120
        while time.monotonic() < descriptor_deadline:
            try:
                candidate = request(base, '/api/agent-line/execution-binding',
                                    headers=api_headers, timeout=5)
                if (isinstance(candidate, dict)
                        and candidate.get('targetAgentId') == TARGET
                        and PARENT in candidate.get('managedRoleIds', [])
                        and candidate.get('workspace') == str(WORKSPACE)):
                    descriptor = candidate
                    break
            except urllib.error.HTTPError as error:
                if error.code not in (404, 405, 503):
                    raise RuntimeError('candidate_descriptor_failed') from None
            time.sleep(2)
        if descriptor is None:
            raise RuntimeError('candidate_descriptor_deadline')
        revision = (descriptor.get('roleRevisions') or {}).get(PARENT)
        if not isinstance(revision, str) or not revision.startswith('qwenpaw-role-v1:'):
            raise RuntimeError('candidate_role_revision_missing')
        state.update(bindingIntent={'taskId': TASK, 'roleAgentId': PARENT,
            'roleSessionId': SESSION, 'roleRevision': revision}, bindingSubmission='intent',
            phase='binding_intent')
        save(STATE, state)
        binding = request(base, '/api/agent-line/delegations/bind', method='POST',
            headers=api_headers, body={'taskId': TASK, 'roleAgentId': PARENT,
                'roleSessionId': SESSION, 'roleRevision': revision})
        state.update(bindingSubmission='received', bindingReceipt=binding, phase='bound')
        save(STATE, state)
        instruction = ('在固定 Codex 执行角色的工作目录中创建 luna-native-proof.txt，文件内容必须严格为 '
            + MARKER + ' 后跟一个换行；执行 shell 检查字节完全相同。仅做此文件操作，不访问网络、'
            '不读取凭据、不检查其他目录。完成后如实报告。')
        state.update(parentSubmission='intent', phase='parent_submit_intent')
        save(STATE, state)
        response = request(base, '/api/console/chat/task', method='POST', headers=api_headers,
            body={'session_id': SESSION, 'user_id': 'luna-candidate',
                'input': [{'role': 'user', 'content': [{'type': 'text', 'text':
                    '请直接执行一次 Agent Line 原生委派。调用 agent_line_delegate，唯一参数 instruction 内容为：'
                    + instruction + '。不要自己调用其他工具。'}]}], 'timeout': 180})
        task_id = response.get('task_id')
        if not task_id: raise RuntimeError('parent_submit_receipt_missing')
        state.update(parentSubmission='submitted', parentTaskId=task_id, phase='parent_running')
        save(STATE, state)
        parent = None
        for _ in range(180):
            parent = request(base, '/api/console/chat/task/' + task_id, headers=api_headers)
            if parent.get('status') == 'finished': break
            time.sleep(2)
        if not parent or parent.get('status') != 'finished':
            raise RuntimeError('parent_nonterminal_observe_only')
        if (parent.get('result') or {}).get('status') != 'completed':
            raise RuntimeError('parent_failed')
        chain = extract_chain(parent, TASK)
        state.update(parentChain=chain, phase='delegate_linked')
        save(STATE, state)
        terminal = None
        for _ in range(180):
            terminal = request(base, '/api/agent-line/executions/' + TASK, headers=api_headers)
            if terminal.get('terminalConfirmed') and terminal.get('resourcesReleased'):
                break
            time.sleep(2)
        if not terminal or not terminal.get('terminalConfirmed') or not terminal.get('resourcesReleased'):
            raise RuntimeError('native_execution_nonterminal_observe_only')
        if terminal.get('status') != 'succeeded': raise RuntimeError('native_execution_failed')
        artifact = WORKSPACE / 'luna-native-proof.txt'
        if artifact.read_bytes() != (MARKER + '\n').encode():
            raise RuntimeError('artifact_bytes_mismatch')
        identity = terminal.get('identity') or {}
        if identity.get('upstreamTaskId') != chain['upstreamTaskId']:
            raise RuntimeError('delegate_execution_identity_mismatch')
        state.update(phase='success', execution={'terminalConfirmed': True,
            'resourcesReleased': True, 'status': 'succeeded', 'identity': identity},
            artifact={'path': str(artifact), 'sha256': hashlib.sha256(artifact.read_bytes()).hexdigest(),
                      'bytes': len(artifact.read_bytes())})
        save(STATE, state)
        # Stop only this recorded candidate host, after all execution resources are terminal.
        process.terminate()
        try: process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait(timeout=5)
        state['hostStopped'] = process.poll() is not None
        state['phase'] = 'completed'; save(STATE, state)
    except BaseException as error:
        state = read(STATE)
        allowed = {'official_step_failed', 'private_directory_mode', 'candidate_directory_mode',
            'executor_source_copy_mismatch', 'codex_auth_already_provisioned',
            'codex_auth_unavailable', 'codex_home_not_fresh', 'executor_identity_invalid',
            'executor_image_invalid', 'official_project_setup_incomplete', 'host_health_unexpected',
            'isolated_host_exited', 'host_start_deadline', 'host_login_failed', 'parent_role_missing',
            'parent_submit_receipt_missing', 'parent_nonterminal_observe_only', 'parent_failed',
            'delegate_arguments_invalid', 'delegate_marker_missing', 'delegate_task_mismatch',
            'upstream_task_missing', 'linked_delegate_tool_receipt_missing',
            'native_execution_nonterminal_observe_only', 'native_execution_failed',
            'artifact_bytes_mismatch', 'delegate_execution_identity_mismatch',
            'existing_candidate_observe_only', 'existing_candidate_must_not_repeat'}
        code = str(error)
        state.update(phase='incomplete', status='incomplete',
            failureType=type(error).__name__,
            failureCode=code if code in allowed else 'candidate_step_failed')
        save(STATE, state)
        raise


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--resume-configured-host', action='store_true')
    parser.add_argument('--observe-existing', action='store_true')
    args = parser.parse_args()
    if args.observe_existing:
        observe_existing_candidate()
    elif args.resume_configured_host:
        continue_live_candidate()
    else:
        run()
