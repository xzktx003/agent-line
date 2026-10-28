#!/usr/bin/env python3
"""Manage only this checkout's isolated QwenPaw service; never display credentials."""
import argparse
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / 'var/qwenpaw'
SECRETS = ROOT / 'var/qwenpaw-secrets'
STATE = DATA / 'agent-line-process.json'
CREDENTIALS = SECRETS / 'local-login.json'
LOG = DATA / 'service.log'
CLI = ROOT / '.venv/bin/qwenpaw'


def checked_path(value, *, directory=False, private=False, writable=True, allow_empty=False):
    """Read-only checks: do not let native defaults repair a missing install."""
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('启动受阻：必须使用原安装的绝对数据路径')
    for parent in reversed((path, *path.parents)):
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError('启动受阻：数据路径或父目录不能是符号链接')
        if parent != path and not stat.S_ISDIR(info.st_mode):
            raise ValueError('启动受阻：数据父路径不是目录')
        if stat.S_ISDIR(info.st_mode):
            marker = parent / 'RESTORE-DO-NOT-START.json'
            if marker.exists() or marker.is_symlink():
                raise ValueError('启动受阻：恢复隔离标记尚未解除，请核对原安装恢复记录')
    info = path.stat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError('启动受阻：安装文件须为当前用户持有的普通文件或目录')
    mode = stat.S_IMODE(info.st_mode)
    if private and mode & 0o077:
        raise ValueError('启动受阻：密钥和凭证必须保持私有权限')
    required = 0o400 | (0o100 if directory else 0) | (0o200 if writable else 0)
    access = os.R_OK | (os.X_OK if directory else 0) | (os.W_OK if writable else 0)
    if mode & required != required or not os.access(path, access):
        raise ValueError('启动受阻：原安装数据不可读写，请恢复权限或挂载')
    if not directory and not allow_empty and info.st_size == 0:
        raise ValueError('启动受阻：原安装文件为空，禁止重新初始化')
    return path


def checked_json(path, *, private=False):
    value = json.loads(checked_path(path, private=private).read_text())
    if not isinstance(value, dict):
        raise ValueError('启动受阻：原安装 JSON 配置格式无效')
    return value


def start_preflight():
    """Validate current official identities, never hashes of mutable memory/config."""
    try:
        checked_path(ROOT, directory=True)
        checked_path(DATA, directory=True)
        checked_path(SECRETS, directory=True, private=True)
        config = checked_json(DATA / 'config.json')
        agents = config.get('agents')
        profiles = agents.get('profiles') if isinstance(agents, dict) else None
        if not isinstance(profiles, dict) or not profiles:
            raise ValueError('启动受阻：原角色目录为空或损坏，禁止创建空团队')
        for key, profile in profiles.items():
            if not isinstance(profile, dict) or not isinstance(key, str) or not key or profile.get('id') != key:
                raise ValueError('启动受阻：角色身份与原配置不一致')
            workspace = checked_path(profile['workspace_dir'], directory=True)
            agent = checked_json(workspace / 'agent.json')
            if agent.get('id') != key or agent.get('workspace_dir') != str(workspace):
                raise ValueError('启动受阻：角色工作区配置缺失或身份不一致')
        credentials = checked_json(CREDENTIALS, private=True)
        auth = checked_json(SECRETS / 'auth.json', private=True)
        checked_path(SECRETS / '.master_key', private=True)
        user = auth.get('user')
        if (not all(isinstance(credentials.get(k), str) and credentials[k] for k in ('username', 'password'))
                or not isinstance(user, dict)
                or not all(isinstance(user.get(k), str) and user[k] for k in ('username', 'password_hash', 'password_salt'))
                or user['username'] != credentials['username']
                or not isinstance(auth.get('jwt_secret'), str) or not auth['jwt_secret']):
            raise ValueError('启动受阻：原账户或认证资料缺失，禁止生成新账户或密钥')
        plugins = config.get('plugins', {})
        extension = plugins.get('agent-line', {}) if isinstance(plugins, dict) else None
        if not isinstance(extension, dict):
            raise ValueError('启动受阻：扩展配置格式无效')
        for name, field in (('execution_bridge', 'receipt_db'), ('memory_governance', 'state_file')):
            if name in extension:
                settings = extension[name]
                if not isinstance(settings, dict) or not isinstance(settings.get(field), str):
                    raise ValueError('启动受阻：扩展持久状态配置无效')
                state = checked_path(settings[field], private=True)
                checked_path(state.parent, directory=True)
        for optional in (STATE, LOG, SECRETS / 'restart-settings.json'):
            if optional.exists() or optional.is_symlink():
                checked_path(optional, private=optional != LOG, allow_empty=optional == LOG)
    except (OSError, TypeError, KeyError, json.JSONDecodeError) as error:
        raise ValueError('启动受阻：原安装数据缺失或不可读；请恢复原目录，不能使用 init 修复') from error


def require_fresh_installation():
    checked_path(ROOT, directory=True)
    for path in (DATA, SECRETS):
        # Validate the existing parents even when the final directory is absent.
        parent = path
        while not parent.exists() and not parent.is_symlink():
            parent = parent.parent
        checked_path(parent, directory=True)
        if path.exists() or path.is_symlink():
            checked_path(path, directory=True)
            if any(path.iterdir()):
                raise ValueError('初始化受阻：检测到已有安装数据；请使用 start 或恢复原安装，不会重新初始化')

def environment():
    env = dict(os.environ, QWENPAW_WORKING_DIR=str(DATA), QWENPAW_SECRET_DIR=str(SECRETS), QWENPAW_AUTH_ENABLED='true', QWENPAW_CONSOLE_STATIC_DIR=str(ROOT / 'upstream/qwenpaw/console/dist'))
    saved = SECRETS / 'restart-settings.json'
    if saved.exists():
        if saved.stat().st_uid != os.getuid() or saved.stat().st_mode & 0o077:
            raise ValueError('Restart settings must be private and owned')
        settings = json.loads(saved.read_text())
        if not isinstance(settings, dict) or set(settings) - {'CODEX_HOME', 'CODEX_BINARY'} or any(not isinstance(v, str) or not v for v in settings.values()):
            raise ValueError('Invalid persisted runtime environment')
        env.update(settings)
    for name in ('QWENPAW_RUNTIME_ID', 'QWENPAW_RUNTIME_TOKEN', 'QWENPAW_DESKTOP'):
        env.pop(name, None)
    return env

def write_private(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(value, stream)
    path.chmod(0o600)

def init():
    require_fresh_installation()
    for path in (DATA, SECRETS):
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)
    if not (DATA / 'config.json').exists():
        subprocess.run([str(CLI), 'init', '--defaults', '--accept-security'], env=environment(), check=True, stdout=subprocess.DEVNULL)
    if not CREDENTIALS.exists():
        write_private(CREDENTIALS, {'username': 'agent-line-local', 'password': secrets.token_urlsafe(32)})
    bootstrap = '''
import json, os
from pathlib import Path
from qwenpaw.config.utils import load_config, save_config
from qwenpaw.app.auth import register_user, has_registered_users, authenticate
cfg=load_config()
cfg.security.allow_no_auth_hosts=[]
save_config(cfg)
credentials=json.loads((Path(os.environ['QWENPAW_SECRET_DIR'])/'local-login.json').read_text())
if not has_registered_users():
    assert register_user(**credentials)
assert authenticate(**credentials), 'Existing account differs from isolated login file'
'''
    subprocess.run([str(ROOT / '.venv/bin/python'), '-c', bootstrap], env=environment(), check=True, stdout=subprocess.DEVNULL)
    subprocess.run([str(CLI), 'plugin', 'install', str(ROOT / 'plugins/agent-line'), '--force'], env=environment(), check=True, stdout=subprocess.DEVNULL)
    print('Isolated configuration and plugin installed; credentials remain in a private file.')

def live(state):
    try:
        path = Path('/proc') / str(state['pid'])
        command = (path / 'cmdline').read_bytes()
        birth = (path / 'stat').read_text().split()[21]
        return birth == state['start_ticks'] and str(CLI).encode() in command
    except (OSError, KeyError):
        return False

def available_port(previous=None):
    port = 0 if previous is None else previous.get('port')
    if type(port) is not int or (previous is not None and not 1 <= port <= 65535):
        raise ValueError('Recorded service port is invalid')
    with socket.socket() as sock:
        # Match Uvicorn's reusable bind after its accepted sockets enter TIME_WAIT.
        # A live listener or a non-reusable owner still prevents this bind.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('127.0.0.1', port))
        return sock.getsockname()[1]


def project_endpoint(previous):
    """Only a fresh single-project install has a configured first-start port."""
    marker = ROOT / 'var/single-project-setup.json'
    if not marker.exists() and not marker.is_symlink():
        return previous
    state = checked_json(marker, private=True)
    if state.get('phase') != 'configured':
        raise ValueError('启动受阻：单项目初始化尚未完成，不能启动部分配置')
    spec = checked_json(ROOT / 'var/adapter/project-spec.json', private=True)
    try:
        native = urlsplit(spec['qwenpaw']['baseURL'])
        port = native.port
        if (native.scheme != 'http' or native.hostname != '127.0.0.1'
                or port is None or not 1 <= port <= 65535 or native.path
                or native.username or native.password or native.query or native.fragment):
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise ValueError('启动受阻：项目原生入口必须为显式端口的IPv4回环HTTP地址') from None
    if previous is not None and (previous.get('port') != port
            or previous.get('host', '127.0.0.1') != '127.0.0.1'):
        raise ValueError('启动受阻：原服务端口与项目接线不一致，不能自动改挂')
    return previous if previous is not None else {'port': port}

def start():
    start_preflight()
    previous = json.loads(STATE.read_text()) if STATE.exists() else None
    previous = project_endpoint(previous)
    if previous and live(previous):
        print('Already running'); return
    port = available_port(previous)
    with LOG.open('ab') as log:
        process = subprocess.Popen([str(CLI), 'app', '--host', '127.0.0.1', '--port', str(port)], env=environment(), cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    birth = (Path('/proc') / str(process.pid) / 'stat').read_text().split()[21]
    write_private(STATE, {'pid': process.pid, 'start_ticks': birth, 'host': '127.0.0.1', 'port': port, 'log': str(LOG)})
    print(f'Started isolated QwenPaw pid={process.pid} url=http://127.0.0.1:{port}')

def request(url, body=None, token=None):
    headers = {'Content-Type': 'application/json'}
    if token: headers['Authorization'] = 'Bearer ' + token
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, {}

def verify():
    state = json.loads(STATE.read_text())
    if not live(state): raise SystemExit('Recorded service is not alive')
    base = f"http://127.0.0.1:{state['port']}"
    status, _ = request(base + '/api/agent-line/health')
    assert status == 401, f'Unauthenticated request: {status}'
    credentials = json.loads(CREDENTIALS.read_text())
    status, body = request(base + '/api/auth/login', credentials)
    assert status == 200, f'Login: {status}'
    token = body.get('token') or body.get('access_token')
    assert token, 'Login did not return token'
    for _ in range(30):
        status, health = request(base + '/api/agent-line/health', token=token)
        if status != 404:
            break
        time.sleep(1)  # Official plugin load follows lazy workspace startup.
    assert status == 200 and health['scope'] == 'plugin-registration-only'
    status, capabilities = request(base + '/api/agent-line/capabilities', token=token)
    assert status == 200 and capabilities['capabilities']['codex_execution'] == 'unknown'
    print('PASS: real HTTP rejects anonymous; login authenticates; plugin health and unknown capabilities verified.')

def stop():
    if not STATE.exists(): return
    state = json.loads(STATE.read_text())
    if live(state):
        os.kill(state['pid'], signal.SIGTERM)
        for _ in range(30):
            if not live(state):
                print('Verified isolated service stopped')
                return
            time.sleep(1)
        raise SystemExit('Isolated service still shutting down; do not start another instance')
    else: print('Recorded PID is no longer the isolated service; no signal sent')

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['init', 'start', 'verify', 'status', 'stop'])
    action = parser.parse_args().action
    if action == 'status':
        state = json.loads(STATE.read_text()) if STATE.exists() else {}
        print(json.dumps({**state, 'live': live(state)}))
    else: globals()[action]()

if __name__ == '__main__': main()
