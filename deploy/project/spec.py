"""Read-only validation of one trusted, fresh-install project specification."""
import copy
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit

from agent_line.adapters.sim import segment
from agent_line.execution_runtime import _json, _origin, _path
from agent_line.test_executor import DockerTestExecutor
from agent_line.verification_policy import validate_policy


# The Codex target is registered with official save_agent_config, without the
# native-role template initializer. Reserve actual Workspace/Harness services'
# persistent state and capability roots; retain ordinary project AGENTS/SOUL/
# README files. get_workspace_skills_dir can rename legacy skill/ to skills/.
RESERVED_WORKSPACE_NAMES = frozenset({
    'agent.json', 'harnesses', 'sessions', 'memory', 'skills', 'skill', 'skill.json',
    'jobs.json', 'chats.json', 'drivers', 'credentials.yaml',
})


def _fields(value, names):
    if not isinstance(value, dict) or set(value) != set(names):
        raise ValueError('Project specification has missing or unknown fields')
    return value


def _text(value, maximum=4000):
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > maximum or any(ord(char) < 32 for char in value)):
        raise ValueError('Project specification requires nonempty trimmed strings')
    return value


def _owner_directory(value, *, private=False):
    path = _path(_text(value), directory=True)
    info = path.stat()
    if (info.st_uid != os.getuid()
            or (private and stat.S_IMODE(info.st_mode) != 0o700)):
        raise ValueError('Project directories must be owned; Codex home must be 0700')
    return path


def _binary(value):
    path = _path(_text(value))
    info = path.stat()
    if (info.st_uid != os.getuid() or info.st_mode & 0o022
            or not info.st_mode & 0o111 or not os.access(path, os.X_OK)):
        raise ValueError('Executor binaries must be owned, executable and not publicly writable')
    return path


def _image(value):
    if not isinstance(value, str) or re.fullmatch(r'sha256:[0-9a-f]{64}', value) is None:
        raise ValueError('Full immutable image ID required')
    return value


def _role_id(value):
    # Keep preflight independent of QwenPaw package/bootstrap side effects.
    # The isolated official CLI remains the authoritative final validator.
    value = _text(value, 64)
    if (value == 'default'
            or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,62}[A-Za-z0-9]', value) is None):
        raise ValueError('Role ID must use 2..64 safe characters and cannot be reserved')
    return value


def _fresh_codex_home(path):
    # Authentication may be explicitly pre-provisioned, but no prior runtime,
    # project configuration, plugin or rollout state may be adopted.
    for entry in path.iterdir():
        if entry.name != 'auth.json':
            raise ValueError('Fresh Codex home may contain only pre-provisioned auth.json')
        _path(str(entry), private=True)


def _separate(left, right):
    if left.is_relative_to(right) or right.is_relative_to(left):
        raise ValueError('Project workspace, installation and Codex home must be separate')


def normalize_spec(data, *, root):
    """Validate without writing files, contacting services, or granting readiness.

    The return value contains only the Sim credential file path, never its key.
    Test environment identity is computed by the existing executor constructor:
    it only reads the already-private Codex directory and makes no Docker calls.
    Its evidence-directory path is deliberately absent from that identity.
    """
    required = {'schemaVersion', 'project', 'coordinator', 'executor', 'qwenpaw',
                'sim', 'verification', 'memoryQuery'}
    if not isinstance(data, dict) or set(data) not in (required, required | {'team'}):
        raise ValueError('Project specification has missing or unknown fields')
    if type(data['schemaVersion']) is not int or data['schemaVersion'] != 1:
        raise ValueError('Project specification requires integer schemaVersion 1')
    project = _fields(data['project'], {'id', 'name', 'workspace'})
    coordinator = _fields(data['coordinator'], {'id', 'name', 'providerId', 'modelId'})
    team = None
    if 'team' in data:
        team = _fields(data['team'], {'planner', 'reviewer'})
        team = {key: _fields(team[key], {'id', 'name', 'providerId', 'modelId'})
                for key in ('planner', 'reviewer')}
    executor = _fields(data['executor'], {'id', 'name', 'model', 'sandbox', 'approvalPolicy',
                                          'binary', 'imageId', 'codexHome'})
    qwenpaw = _fields(data['qwenpaw'], {'baseURL'})
    sim = _fields(data['sim'], {'authFile', 'workspaceId'})
    verification = _fields(data['verification'], {'policyId', 'imageId', 'tests'})
    segment(_text(project['id'], 160))
    _text(project['name'])
    parents = [coordinator, *(team.values() if team else [])]
    for role in [*parents, executor]:
        _role_id(role['id'])
        _text(role['name'])
    ids = [role['id'] for role in parents] + [executor['id']]
    if len(set(ids)) != len(ids):
        raise ValueError('Planner, developer, reviewer and executor require different official IDs')
    for role in parents:
        for key in ('providerId', 'modelId'):
            _text(role[key])
    for key in ('model', 'sandbox', 'approvalPolicy'):
        _text(executor[key])
    if executor['sandbox'] not in {'read-only', 'workspace-write', 'danger-full-access'}:
        raise ValueError('Unsupported Codex sandbox')
    if executor['approvalPolicy'] not in {'untrusted', 'on-failure', 'on-request', 'never'}:
        raise ValueError('Unsupported Codex approval policy')
    _text(data['memoryQuery'])
    install_root = _owner_directory(str(root))
    workspace = _owner_directory(project['workspace'])
    codex_home = _owner_directory(executor['codexHome'], private=True)
    _fresh_codex_home(codex_home)
    if ',' in str(workspace) or ',' in str(codex_home):
        raise ValueError('Container mount paths cannot contain commas')
    _separate(workspace, install_root)
    _separate(workspace, codex_home)
    for name in RESERVED_WORKSPACE_NAMES:
        candidate = workspace / name
        if candidate.exists() or candidate.is_symlink():
            raise ValueError('Project workspace contains an official runtime reserved name')
    binary = _binary(executor['binary'])
    if ',' in str(binary):
        raise ValueError('Container binary mount paths cannot contain commas')
    _binary(str(binary.parent / 'codex-code-mode-host'))
    _image(executor['imageId'])
    verification_image = _image(verification['imageId'])
    origin = _origin(_text(qwenpaw['baseURL']))
    native = urlsplit(origin)
    # The current host/proof contract records an explicit IPv4 HTTP listener.
    # The LAN/TLS entry remains the existing outer gateway, not this backend.
    if (native.scheme != 'http' or native.hostname != '127.0.0.1'
            or native.port is None or not 1 <= native.port <= 65535):
        raise ValueError('Current QwenPaw host requires http://127.0.0.1 with an explicit port')
    segment(_text(sim['workspaceId'], 160))
    auth_path = _path(_text(sim['authFile']), private=True)
    auth = _fields(_json(auth_path), {'baseUrl', 'apiKey'})
    _origin(_text(auth['baseUrl']))
    _text(auth['apiKey'])
    environment = DockerTestExecutor(codex_home, verification_image).environment_id
    policy = validate_policy({'schemaVersion': 2, 'policyId': verification['policyId'],
                              'kind': 'command_and_artifacts', 'requireTests': True,
                              'artifacts': [], 'tests': verification['tests'],
                              'testEnvironmentId': environment})
    normalized = copy.deepcopy(data)
    normalized['qwenpaw']['baseURL'] = origin
    if team:
        normalized['team'] = team
    normalized['verificationPolicy'] = policy
    return normalized
