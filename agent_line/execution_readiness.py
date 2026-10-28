"""Read-only, deployment-bound execution admission; never runs an acceptance task.

A proof only names tasks already held by the configured service. Their native,
container and verifier facts are checked again instead of trusting a JSON grant.
"""
from contextlib import contextmanager
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import threading
from urllib.parse import urlsplit

import psutil
import httpx

from .automatic_verification import AutomaticVerifier
from .role_revision import validate_role_revision
from .store import TaskStore, canonical
from .verification import ArtifactPolicy, _check, _directory
from .verification_policy import validate_policy, policy_hash

UNKNOWN = '执行准入证据尚未核验或已失效，已阻止新执行；历史记录仍可查询'
_BINARY_DIGESTS = OrderedDict()
_BINARY_DIGEST_LOCK = threading.Lock()


def _require(condition):
    if not condition:
        raise ValueError('execution_readiness_unproven')


def _bytes(path, *, private=False, limit=32_000_000):
    path = Path(path).absolute()
    parent = _directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1)
            if private:
                _require(before.st_uid == os.getuid() and stat.S_IMODE(before.st_mode) == 0o600)
            value = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
            current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            _require(len(value) <= limit and (before.st_dev, before.st_ino, before.st_size,
                     before.st_mtime_ns, before.st_ctime_ns) == (after.st_dev, after.st_ino,
                     after.st_size, after.st_mtime_ns, after.st_ctime_ns))
            _require((current.st_dev, current.st_ino) == (after.st_dev, after.st_ino))
            return value
    finally:
        os.close(parent)



def _file_digest(path):
    """Bounded streaming hash for mounted Codex executables, which exceed JSON bounds."""
    path = Path(path)
    parent = _directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, 'rb') as stream:
            before = os.fstat(stream.fileno())
            _require(stat.S_ISREG(before.st_mode) and 0 < before.st_size <= 1_000_000_000
                     and os.access(path, os.R_OK | os.X_OK))
            key = (str(path), before.st_dev, before.st_ino, before.st_size,
                   before.st_mtime_ns, before.st_ctime_ns)
            with _BINARY_DIGEST_LOCK:
                cached = _BINARY_DIGESTS.get(key)
                if cached is not None:
                    _BINARY_DIGESTS.move_to_end(key)
            if cached is not None:
                after = os.fstat(stream.fileno())
                current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
                _require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                         == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns))
                _require((current.st_dev, current.st_ino) == (after.st_dev, after.st_ino))
                return cached
            digest = hashlib.sha256()
            total = 0
            while True:
                chunk = stream.read(1_048_576)
                if not chunk:
                    break
                total += len(chunk)
                _require(total <= 1_000_000_000)
                digest.update(chunk)
            after = os.fstat(stream.fileno())
            current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            _require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                     == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns))
            _require((current.st_dev, current.st_ino) == (after.st_dev, after.st_ino))
            result = digest.hexdigest()
            with _BINARY_DIGEST_LOCK:
                _BINARY_DIGESTS[key] = result
                _BINARY_DIGESTS.move_to_end(key)
                while len(_BINARY_DIGESTS) > 8:
                    _BINARY_DIGESTS.popitem(last=False)
            return result
    finally:
        os.close(parent)


def _optional_sandbox_helper(binary):
    """Bind a present official bwrap into freshness; preserve legacy absence."""
    path = Path(binary).parent.parent / 'codex-resources' / 'bwrap'
    try:
        resources = path.parent.lstat()
    except FileNotFoundError:
        return None
    _require(stat.S_ISDIR(resources.st_mode) and not stat.S_ISLNK(resources.st_mode))
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    _require(path.resolve(strict=True) == path and stat.S_ISREG(before.st_mode)
             and before.st_uid == os.getuid() and not before.st_mode & stat.S_IWOTH
             and before.st_mode & 0o111 and os.access(path, os.X_OK))
    digest = _file_digest(path)
    after = path.lstat()
    _require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns,
              before.st_uid, before.st_mode) ==
             (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns,
              after.st_uid, after.st_mode))
    return path, digest, after.st_mtime_ns


def _json(path, *, private=True):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result)
            result[key] = value
        return result
    return json.loads(_bytes(path, private=private), object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite_json')))


@contextmanager
def _database(path):
    path = Path(path).absolute()
    parent = _directory(path.parent)
    fd = None
    db = None
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        before = os.fstat(fd)
        _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                 and before.st_uid == os.getuid() and stat.S_IMODE(before.st_mode) == 0o600)
        db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        _require((current.st_dev, current.st_ino) == (before.st_dev, before.st_ino))
        yield db
        current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        _require((current.st_dev, current.st_ino) == (before.st_dev, before.st_ino))
    finally:
        if db is not None:
            db.close()
        if fd is not None:
            os.close(fd)
        os.close(parent)


@dataclass(frozen=True)
class _ReadOnlyStore:
    path: Path

    def connect(self):
        return _database(self.path)


def _digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _role_controls(config, role):
    roles = config.get('readonly_role_ids', [])
    _require(isinstance(roles, list) and all(isinstance(item, str) and item for item in roles)
             and len(set(roles)) == len(roles))
    return {'sandbox': 'read-only', 'approval_policy': 'never'} if role in roles else None


_APPROVAL_POLICIES = ('never', 'on-request', 'on-failure', 'untrusted')
_DRIFT_CACHE = {}


def _approval_policy_explains_revision(pinned, live, current, revisions):
    """True when the live revision matches the pinned one except Codex approval policy."""
    if live == pinned:
        return True
    if revisions(None) != live:
        return False
    for policy in _APPROVAL_POLICIES:
        if policy == current:
            continue
        if revisions(policy) == pinned:
            return True
    return False


def _role_revision_module():
    import importlib.util
    path = Path(__file__).resolve().parents[1] / 'plugins/agent-line/role_revision.py'
    spec = importlib.util.spec_from_file_location('agent_line_role_revision_drift', path)
    if spec is None or spec.loader is None:
        raise ValueError('role revision source unavailable')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _revise_role(role_id, target_id, approval_policy):
    """Hash the registered pair. None uses the file; a policy is substituted in memory."""
    module = _role_revision_module()
    if approval_policy is None:
        return module.role_revision(role_id, target_id)
    from qwenpaw.config.utils import load_agent_config, load_config
    from qwenpaw.providers.provider_manager import ProviderManager
    global_config = load_config()
    for agent_id in (role_id, target_id):
        reference = global_config.agents.profiles.get(agent_id)
        if reference is None or not (Path(reference.workspace_dir) / 'agent.json').is_file():
            raise ValueError('Registered role configuration unavailable')
        if reference.enabled is not True:
            raise ValueError('Registered role is disabled')
    parent, target = load_agent_config(role_id), load_agent_config(target_id)
    if parent.backend != 'qwenpaw' or target.backend != 'codex':
        raise ValueError('Role backend differs from execution contract')
    settings = dict(target.backend_settings)
    settings['approval_policy'] = approval_policy
    target = target.model_copy(update={'backend_settings': settings})
    manager = ProviderManager.get_instance()
    value = {'revisionSchema': 2, 'parent': module.snapshot(parent, global_config, manager),
             'executor': module.snapshot(target, global_config, manager)}
    data = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    return module.PREFIX + hashlib.sha256(data.encode()).hexdigest()


def _current_approval_policy(target_id):
    from qwenpaw.config.utils import load_agent_config
    settings = load_agent_config(target_id).backend_settings
    if not isinstance(settings, dict):
        raise ValueError('Codex approval policy unavailable')
    return settings.get('approval_policy')


_ADMISSION_ENV = 'AGENT_LINE_ADMIT_APPROVAL_POLICY_DRIFT'
_ADMISSION_INSTALLED = False


def _codex_target_id():
    working = os.environ.get('QWENPAW_WORKING_DIR')
    if not working:
        return None
    try:
        config = json.loads((Path(working) / 'config.json').read_text())
        target = config['plugins']['agent-line']['codex_target_agent_id']
    except (OSError, json.JSONDecodeError, KeyError, TypeError, AttributeError):
        return None
    return target if isinstance(target, str) and target else None


def _role_is_readonly(role):
    working = os.environ.get('QWENPAW_WORKING_DIR')
    if not working:
        return True
    try:
        config = json.loads((Path(working) / 'config.json').read_text())
        roles = config['plugins']['agent-line']['execution_bridge'].get('readonly_role_ids', [])
    except (OSError, json.JSONDecodeError, KeyError, TypeError, AttributeError):
        return True
    return not isinstance(roles, list) or role in roles


def _approval_policy_admission(role, pinned, live):
    """Return the live revision to freeze, or None when the pinned revision must stand."""
    if not isinstance(role, str) or not isinstance(pinned, str) or not isinstance(live, str):
        return None
    if live == pinned:
        return pinned
    if _role_is_readonly(role):
        return None
    target = _codex_target_id()
    if target is None or not _codex_approval_policy_drift(role, target, pinned, live):
        return None
    return live


def _fetch_live_role_revision(gateway):
    try:
        response = gateway.client.get(gateway.base + '/agent-line/execution-binding',
                                       headers=gateway.headers, follow_redirects=False)
        response.raise_for_status()
        descriptor = response.json()
        actual = descriptor.get('roleRevisions', {}).get(gateway.role)
        validate_role_revision(actual)
        return actual
    except (httpx.HTTPError, ValueError, AttributeError, TypeError, KeyError):
        raise ValueError('role_revision_unavailable') from None


def _rewrite_admitted_revision(gateway, proof, task_id, pinned):
    with gateway._revision_proof_lock:
        identity = gateway._revision_proofs.get(proof)
        if (not isinstance(identity, tuple) or len(identity) != 3 or identity[0] != task_id
                or identity[1] != pinned or not isinstance(identity[2], str) or identity[2] == pinned):
            return pinned
        gateway._revision_proofs[proof] = (task_id, identity[2])
        return identity[2]


def _codex_approval_policy_drift(role_id, target_id, pinned, live):
    """Allow only an in-memory Codex approval_policy difference. Other drift stays rejected."""
    if live == pinned:
        return True
    if not isinstance(live, str) or not isinstance(pinned, str):
        return False
    key = (role_id, target_id, pinned, live)
    if _DRIFT_CACHE.get(key) is True:
        return True
    working = os.environ.get('QWENPAW_WORKING_DIR')
    if not working or not Path(working).is_dir():
        return False
    try:
        current = _current_approval_policy(target_id)
        allowed = _approval_policy_explains_revision(
            pinned, live, current, lambda policy: _revise_role(role_id, target_id, policy))
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ImportError, RuntimeError):
        return False
    if allowed:
        _DRIFT_CACHE[key] = True
        if len(_DRIFT_CACHE) > 32:
            _DRIFT_CACHE.clear()
            _DRIFT_CACHE[key] = True
    return allowed


def _host(root, gateways):
    state = _json(root / 'var/qwenpaw/agent-line-process.json')
    pid, ticks, port = state['pid'], state['start_ticks'], state['port']
    _require(type(pid) is int and pid > 0 and type(port) is int and 1 <= port <= 65535)
    _require(isinstance(ticks, str) and ticks.isdecimal() and state['host'] == '127.0.0.1')
    proc = psutil.Process(pid)
    data = Path(f'/proc/{pid}/stat').read_text()
    fields = data[data.rfind(')') + 1:].split()
    _require(fields[0] != 'Z' and fields[19] == ticks and proc.uids().real == os.getuid())
    argv = proc.cmdline()
    _require(str(root / '.venv/bin/qwenpaw') in argv and 'app' in argv
             and '--port' in argv and argv[argv.index('--port') + 1] == str(port))
    _require(any(c.status == psutil.CONN_LISTEN and c.laddr.ip == '127.0.0.1'
                 and c.laddr.port == port for c in proc.net_connections(kind='tcp')))
    for gateway in gateways.values():
        origin = urlsplit(gateway._gateway.base[:-4])
        _require(origin.scheme == 'http' and origin.hostname == '127.0.0.1' and origin.port == port)
    return {'pid': pid, 'startTicks': ticks, 'port': port, 'bornAt': proc.create_time()}


def _fingerprint(*, root, projects, bindings, gateways, tasks, verification_directory):
    root = Path(root).resolve(strict=True)
    _require(gateways and isinstance(projects, dict) and isinstance(bindings, dict))
    files = set(root.glob('agent_line/**/*.py')) | set(root.glob('deploy/executor/*.py'))
    plugin = root / 'plugins/agent-line'
    execution_files = set(plugin.rglob('*.py')) | {plugin / 'plugin.json', plugin / 'dist/index.js'}
    files |= execution_files | {root / 'requirements-runtime.lock.txt', root / 'upstreams.lock.json'}
    _require(execution_files and len(files) > 4)
    installed = root / 'var/qwenpaw/plugins/agent-line'
    expected = {p.relative_to(plugin) for p in execution_files}
    actual = {p.relative_to(installed) for p in installed.rglob('*.py')}
    actual |= {Path('plugin.json'), Path('dist/index.js')}
    _require(actual == expected)
    for path in execution_files:
        target = installed / path.relative_to(plugin)
        _require(_bytes(path) == _bytes(target))
        files.add(target)
    hashes = {str(p.relative_to(root)): hashlib.sha256(_bytes(p)).hexdigest() for p in sorted(files)}
    executor_path = root / 'var/executor/config.json'
    executor = _json(executor_path)
    _require(isinstance(executor['image'], str) and re.fullmatch(r'sha256:[a-f0-9]{64}', executor['image']))
    binary = Path(executor['binary']).resolve(strict=True)
    binary_host = binary.parent / 'codex-code-mode-host'
    hashes['executorConfig'] = _digest(executor)
    hashes['executorBinary'] = _file_digest(binary)
    hashes['executorCodeModeHost'] = _file_digest(binary_host)
    sandbox_helper = _optional_sandbox_helper(binary)
    freshness_files = {executor_path, binary, binary_host}
    if sandbox_helper is not None:
        helper_path, helper_digest, helper_mtime_ns = sandbox_helper
        hashes['executorSandboxHelper'] = helper_digest
        hashes['executorSandboxHelperMtimeNs'] = helper_mtime_ns
        freshness_files.add(helper_path)
    # This validator runs after acceptance, not inside the execution being
    # proved. Its bytes still invalidate old proofs; changing only the validator
    # permits read-only revalidation without needlessly repeating native work.
    execution_sources = files - {root / 'agent_line/execution_readiness.py'}
    latest = max(p.stat().st_mtime for p in execution_sources | freshness_files)
    host = _host(root, gateways)
    _require(host['bornAt'] > max((installed / p).stat().st_mtime for p in expected))
    lock = _json(root / 'upstreams.lock.json', private=False)
    pins = lock['repositories']
    _require(set(pins) == {'qwenpaw', 'sim'})
    for name, pin in pins.items():
        checkout = root / 'upstream' / name
        _require(pin['path'] == str(checkout.relative_to(root)))
        def git(*args):
            return subprocess.check_output(['git', '-C', str(checkout), *args],
                text=True, stderr=subprocess.DEVNULL, timeout=15,
                env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'}).strip()
        _require(git('rev-parse', '--show-toplevel') == str(checkout)
                 and git('rev-parse', 'HEAD') == pin['commit']
                 and git('remote', 'get-url', 'origin') == pin['remote']
                 and not git('status', '--porcelain', '--untracked-files=no')
                 and not git('ls-files', '--others', '--exclude-standard'))
    extension = _json(root / 'var/qwenpaw/config.json')['plugins']['agent-line']
    bridge = extension['execution_bridge']
    _require(extension['codex_target_agent_id'] == bridge['target_agent_id']
             and executor['workspace'] == bridge['workspace'] and executor['codex_home'] == bridge['codex_home'])
    scopes = []
    for (project, identifier), gateway in sorted(gateways.items()):
        binding = bindings[identifier]
        policy = validate_policy(binding['verificationPolicy'])
        revision = validate_role_revision(binding['roleRevision'])
        workspace = str(Path(projects[project]['workspace']).resolve(strict=True))
        _require(binding['projectId'] == project and binding['roleAgentId'] == gateway.role
                 and gateway.project_id == project and gateway.role_binding_id == identifier
                 and gateway.role_revision == revision and gateway.workspace == workspace)
        response = gateway._gateway.client.get(gateway._gateway.base + '/agent-line/execution-binding',
            headers=gateway._gateway.headers, follow_redirects=False)
        response.raise_for_status()
        descriptor = response.json()
        _require(isinstance(descriptor, dict) and type(descriptor.get('schemaVersion')) is int
                 and descriptor['schemaVersion'] == 1 and descriptor.get('ownedContainer') is True
                 and descriptor.get('workspace') == workspace
                 and isinstance(descriptor.get('managedRoleIds'), list)
                 and gateway.role in descriptor['managedRoleIds']
                 and descriptor.get('targetAgentId') == bridge['target_agent_id']
                 and gateway.role in extension['managed_role_ids'] and bridge['workspace'] == workspace
                 and isinstance(descriptor.get('roleRevisions'), dict))
        live_revision = descriptor['roleRevisions'].get(gateway.role)
        controls = _role_controls(bridge, gateway.role)
        if live_revision != revision:
            _require(controls is None and _codex_approval_policy_drift(
                gateway.role, bridge['target_agent_id'], revision, live_revision))
        scopes.append({'projectId': project, 'roleBindingId': identifier, 'roleAgentId': gateway.role,
                       'workspace': workspace, 'installationId': gateway.installation_id,
                       'roleRevision': revision, 'policySha256': policy_hash(policy),
                       'parentLedger': str(gateway._gateway.ledger.path), 'targetAgentId': descriptor['targetAgentId']})
        if controls is not None:
            _require(isinstance(descriptor.get('roleExecutionControls'), dict)
                     and descriptor['roleExecutionControls'].get(gateway.role) == controls)
            scopes[-1]['executionControls'] = controls
    return {'schemaVersion': 1, 'host': host, 'sourcesSha256': _digest(hashes),
            'sourcesLatestAt': latest, 'scopes': scopes, 'taskDatabase': str(Path(tasks.path).absolute()),
            'verificationDirectory': str(Path(verification_directory).absolute()),
            'bridgeSha256': _digest(bridge)}


def _time(value):
    _require(type(value) in (int, float) and math.isfinite(value) and value > 0)
    return value


def _timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    _require(parsed.tzinfo is not None)
    return _time(parsed.timestamp())


def _launcher(record):
    pid, start = record['pid'], record['start']
    _require(type(pid) is int and pid > 0 and record['uid'] == os.getuid()
             and isinstance(start, str) and start.isdecimal())
    path = Path(f'/proc/{pid}/stat')
    try:
        value = path.read_text()
    except FileNotFoundError:
        _require(not path.parent.exists())
        return
    fields = value[value.rfind(')') + 1:].split()
    _require(fields[19] != start)  # Includes zombies: original process must be reaped.


def _native(root, gateway, task, exit_receipt, boundary):
    plugin = _json(root / 'var/qwenpaw/config.json')['plugins']['agent-line']
    config = plugin['execution_bridge']
    _require(config['workspace'] == task['payload']['workspace']
             and task['payload']['roleAgentId'] in plugin['managed_role_ids']
             and plugin['codex_target_agent_id'] == config['target_agent_id']
             and Path(config['container_control']) == root / 'deploy/executor/control.py'
             and Path(config['container_launcher']) == root / 'deploy/executor/launcher.py')
    with _database(config['receipt_db']) as db:
        _require(db.execute('PRAGMA user_version').fetchone()[0] == 4)
        row = db.execute('SELECT * FROM receipts WHERE task_id=?', (task['id'],)).fetchone()
        pin = db.execute('SELECT * FROM role_bindings WHERE task_id=?', (task['id'],)).fetchone()
        _require(row is not None and pin is not None and not db.execute(
            'SELECT 1 FROM revoked_delegations WHERE task_id=?', (task['id'],)).fetchone())
        payload, record = json.loads(row['payload']), json.loads(row['record'])
        _require(pin['role_id'] == gateway.role and pin['session_id'] == 'agent-line-' + task['id']
                 and pin['role_revision'] == gateway.role_revision)
    _require(payload['taskId'] == task['id'] and payload['roleSessionId'] == pin['session_id']
             and payload['targetAgentId'] == config['target_agent_id']
             and payload['workspace'] == gateway.workspace and payload['instruction'].strip())
    controls = _role_controls(config, gateway.role)
    if controls is None:
        _require('executionControls' not in payload and 'executionControls' not in record)
    else:
        _require(payload.get('executionControls') == controls and record.get('executionControls') == controls)
        policies = [e for e in task['evidence'] if isinstance(e, dict) and e.get('kind') == 'execution_policy']
        _require(policies == [{'kind': 'execution_policy', 'source': 'qwenpaw-plugin-task-binding',
                               'executionId': task['executionIdentity']['codexTurnId'], 'controls': controls}])
    identity = task['executionIdentity']
    _require(set(identity) == {'upstreamTaskId', 'codexThreadId', 'codexTurnId'}
             and all(isinstance(v, str) and v for v in identity.values())
             and record.get('taskId') == task['id'] and record.get('identity') == identity
             and record.get('status') == 'succeeded' and record.get('terminalConfirmed') is True
             and record.get('resourcesReleased') is True and record.get('executorInterrupted') is False
             and record.get('nativeStatus') == 'task_complete'
             and record.get('resourceExitReceipt') == exit_receipt)
    retirement = {'schemaVersion': 1, 'source': 'qwenpaw-harness-runtime-stop',
                  'taskId': task['id'], 'targetAgentId': config['target_agent_id'],
                  'workspace': gateway.workspace, 'identity': identity,
                  'owner': exit_receipt['owner'], 'containerId': exit_receipt['containerId']}
    _require(record.get('runtimeRetirement') in ({**retirement, 'state': 'stopped'},
                                               {**retirement, 'state': 'unloaded'}))
    executor = _json(root / 'var/executor/config.json')
    _require(exit_receipt.get('imageId') == executor['image'])
    _require(boundary < _timestamp(exit_receipt['startedAt']) <= _timestamp(exit_receipt['finishedAt']))
    owner = exit_receipt['owner']  # Validated full hex by TaskStore first.
    launch = _json(root / 'var/executor/launches' / (owner + '.json'))
    _require(launch.get('owner') == owner and launch.get('containerId') == exit_receipt['containerId']
             and launch.get('workspace') == gateway.workspace and launch.get('exitReceipt') == exit_receipt
             and launch.get('accepting') is False and not launch.get('pendingTurns')
             and sum(isinstance(i, dict) and i.get('codexThreadId') == identity['codexThreadId']
                     and i.get('codexTurnId') == identity['codexTurnId'] and i.get('nativeTerminal') is True
                     for i in launch.get('identities', [])) == 1)
    _launcher(launch)
    mapping = _json(Path(gateway.workspace) / 'harnesses/codex_sessions.json', private=False)
    _require(mapping.get(pin['session_id']) == identity['codexThreadId'])
    home = Path(config['codex_home']).resolve(strict=True)
    rollouts = list((home / 'sessions').rglob('*' + identity['codexThreadId'] + '.jsonl'))
    _require(len(rollouts) == 1 and rollouts[0].resolve().is_relative_to(home))
    entries = [json.loads(line) for line in _bytes(rollouts[0]).splitlines() if line.strip()]
    metadata = [e.get('payload', {}) for e in entries if e.get('type') == 'session_meta']
    _require(len(metadata) == 1 and metadata[0].get('id') == identity['codexThreadId']
             and metadata[0].get('cwd') == gateway.workspace)
    events = [e for e in entries if e.get('type') == 'event_msg']
    starts = [e for e in events if e.get('payload', {}).get('type') == 'task_started']
    _require(len(starts) == 1 and starts[0]['payload'].get('turn_id') == identity['codexTurnId'])
    _require(_timestamp(starts[0]['timestamp']) > boundary)
    if controls is not None:
        contexts = [e.get('payload', {}) for e in entries if e.get('type') == 'turn_context'
                    and e.get('payload', {}).get('turn_id') == identity['codexTurnId']]
        _require(len(contexts) == 1 and contexts[0].get('sandbox_policy') == {'type': 'read-only'}
                 and contexts[0].get('approval_policy') == 'never')
    terminals = [e['payload'] for e in events if e.get('payload', {}).get('turn_id') == identity['codexTurnId']
                 and e['payload'].get('type') in {'task_complete', 'turn_aborted'}]
    _require(terminals and terminals[-1]['type'] == 'task_complete')


def _verification(directory, store, task, policy, boundary):
    receipts = [e for e in task['evidence'] if isinstance(e, dict) and e.get('kind') == 'verification']
    _require(len(receipts) == 1)
    receipt = receipts[0]
    name = 'verification-' + hashlib.sha256(canonical(
        [task['id'], task['executionIdentity'], policy_hash(policy)]).encode()).hexdigest() + '.json'
    _require(receipt == _json(Path(directory) / name) and receipt.get('status') == 'passed'
             and _time(receipt['checkedAt']) > boundary)
    validator = AutomaticVerifier.__new__(AutomaticVerifier)
    validator.store = _ReadOnlyStore(Path(store.path))
    validator.directory = Path(directory)
    fd = validator._directory()
    os.close(fd)
    validator._validate(receipt, task, policy, name)
    fd = _directory(task['payload']['workspace'])
    try:
        for artifact, previous in zip(policy['artifacts'], receipt['checks']):
            current = _check(fd, ArtifactPolicy(artifact['path'], artifact['sha256'],
                             artifact['exactSize'], artifact['maxBytes']))
            _require(current == previous and current['status'] == 'passed')
    finally:
        os.close(fd)
    for test, check in zip(policy.get('tests', []), receipt['checks'][len(policy['artifacts']):]):
        key = _digest({'taskId': task['id'], 'testId': test['id']})
        original = _json(Path(directory) / ('test-' + key + '.json'))
        binding = dict(taskId=task['id'], executionIdentity=task['executionIdentity'],
                       policySha256=policy_hash(policy), testId=test['id'],
                       environmentId=policy['testEnvironmentId'], workspace=task['payload']['workspace'], test=test)
        _require(original.get('evidenceFile') == 'test-' + key + '.json'
                 and original.get('binding') == binding and original.get('name') == 'agent-line-test-' + key[:40]
                 and original.get('check') == check and check.get('status') == 'passed'
                 and original.get('containerId') == check['containerId']
                 and original.get('removed') is True and original.get('startIntent') is True
                 and boundary < _timestamp(check['resourceExitReceipt']['startedAt'])
                 <= _timestamp(check['resourceExitReceipt']['finishedAt']) <= receipt['checkedAt'])


def _coverage(coverage, *, root, projects, bindings, gateways, tasks, verification_directory, fingerprint):
    _require(isinstance(coverage, list) and len(coverage) == len(gateways))
    keys = []
    boundary = max(fingerprint['host']['bornAt'], fingerprint['sourcesLatestAt'])
    for entry in coverage:
        _require(isinstance(entry, dict) and set(entry) == {'projectId', 'roleBindingId', 'taskId'})
        key = (entry['projectId'], entry['roleBindingId'])
        _require(key in gateways and key not in keys)
        keys.append(key)
        gateway, binding = gateways[key], bindings[entry['roleBindingId']]
        policy = validate_policy(binding['verificationPolicy'])
        validate_role_revision(binding['roleRevision'])
        with _database(tasks.path) as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (entry['taskId'],)).fetchone()
            _require(row is not None and _time(row['created_at']) > boundary)
            task = TaskStore.decode(row)
            _require(not db.execute('SELECT 1 FROM workspace_leases WHERE task_id=?', (task['id'],)).fetchone())
            events = list(db.execute('SELECT kind,created_at FROM events WHERE task_id=?', (task['id'],)))
            for kind in ('accepted', 'succeeded', 'resources_released', 'verification_passed'):
                _require(any(e['kind'] == kind and _time(e['created_at']) > boundary for e in events))
        payload = task['payload']
        _require(task['executionStatus'] == 'succeeded' and task['resourcesReleased'] is True
                 and task['verificationStatus'] == 'passed' and task['upstreamSubmissionStatus'] == 'accepted'
                 and payload['projectId'] == key[0] and payload['roleBindingId'] == key[1]
                 and payload['roleAgentId'] == gateway.role and payload['workspace'] == gateway.workspace
                 and payload['roleRevision'] == gateway.role_revision == binding['roleRevision']
                 and validate_policy(payload['verificationPolicy']) == policy
                 and str(Path(projects[key[0]]['workspace']).resolve()) == gateway.workspace)
        exits = [e for e in task['evidence'] if isinstance(e, dict) and e.get('kind') == 'resource_exit']
        _require(len(exits) == 1 and exits[0].get('taskId') == task['id']
                 and exits[0].get('executionIdentity') == task['executionIdentity'])
        TaskStore._validate_resource_exit(task['executionIdentity'], payload, exits[0].get('receipt'), strict=True)
        _require(any(isinstance(e, dict) and e.get('kind') == 'execution_terminal'
                     and e.get('executionId') == task['executionIdentity']['codexTurnId']
                     and e.get('confirmed') is True for e in task['evidence']))
        with _database(gateway._gateway.ledger.path) as db:
            parent = db.execute('SELECT * FROM deliveries WHERE task_id=?', (task['id'],)).fetchone()
            _require(parent is not None and parent['status'] == 'submitted' and parent['parent_id']
                     and _time(parent['created']) > boundary and not db.execute(
                         'SELECT 1 FROM cancellations WHERE task_id=?', (task['id'],)).fetchone())
            parent_payload = json.loads(parent['payload'])
        _require(parent_payload.get('roleAgentId') == gateway.role
                 and parent_payload.get('roleSessionId') == 'agent-line-' + task['id']
                 and parent_payload.get('deploymentId') == gateway.installation_id
                 and parent_payload.get('roleRevision') == gateway.role_revision
                 and parent_payload.get('instruction') == canonical(payload['input']))
        _native(Path(root), gateway, task, exits[0]['receipt'], boundary)
        _verification(verification_directory, tasks, task, policy, boundary)
    _require(set(keys) == set(gateways))


def prepare_readiness(*, root, projects, bindings, gateways, tasks, verification_directory, coverage):
    """Build a deployment record from existing facts, without persisting or running anything."""
    context = dict(root=Path(root), projects=projects, bindings=bindings, gateways=gateways,
                   tasks=tasks, verification_directory=verification_directory)
    fingerprint = _fingerprint(**context)
    _coverage(coverage, **context, fingerprint=fingerprint)
    _require(_fingerprint(**context) == fingerprint)
    return {'schemaVersion': 1, 'fingerprint': fingerprint, 'coverage': json.loads(canonical(coverage))}


@dataclass(frozen=True)
class ExecutionReadiness:
    confirmed: bool
    reason: str | None
    _check: object = field(repr=False, compare=False)

    def assert_current(self):
        try:
            _require(self.confirmed)
            self._check()
        except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError, sqlite3.Error,
                subprocess.SubprocessError, psutil.Error, httpx.HTTPError):
            raise ValueError('execution_readiness_stale') from None


def install_approval_policy_admission():
    """Bind the live revision when Codex approval_policy is the only drift.

    The admission proof keeps the pinned revision. Readonly roles stay exact.
    The adapter enables this only for the process that must record a real rejection.
    """
    global _ADMISSION_INSTALLED
    if os.environ.get(_ADMISSION_ENV) != '1' or _ADMISSION_INSTALLED:
        return False
    from .role_dispatch import RoleDispatchGateway, _RoleRevisionProof
    original_preflight = RoleDispatchGateway.preflight
    original_submit = RoleDispatchGateway.submit

    def preflight(self, *, task_id, role_revision=None):
        try:
            return original_preflight(self, task_id=task_id, role_revision=role_revision)
        except ValueError as error:
            if str(error) != 'role_revision_mismatch':
                raise
            admitted = _approval_policy_admission(self.role, role_revision, _fetch_live_role_revision(self))
            if not isinstance(admitted, str) or admitted == role_revision:
                raise
            proof = _RoleRevisionProof()
            with self._revision_proof_lock:
                self._revision_proofs[proof] = (task_id, role_revision, admitted)
            return proof

    def submit(self, *, task_id, role_session_id, instruction, role_revision=None, preflight_proof=None,
               resubmit_proof=None):
        if preflight_proof is not None and role_revision is not None:
            role_revision = _rewrite_admitted_revision(self, preflight_proof, task_id, role_revision)
        return original_submit(
            self, task_id=task_id, role_session_id=role_session_id, instruction=instruction,
            role_revision=role_revision, preflight_proof=preflight_proof, resubmit_proof=resubmit_proof)

    RoleDispatchGateway.preflight = preflight
    RoleDispatchGateway.submit = submit
    _ADMISSION_INSTALLED = True
    return True


def evaluate_readiness(path, *, root, projects, bindings, gateways, tasks, verification_directory):
    """Unknown is the default; invalid local/remote evidence never grants execution."""
    install_approval_policy_admission()
    context = dict(root=Path(root), projects=projects, bindings=bindings, gateways=gateways,
                   tasks=tasks, verification_directory=verification_directory)
    def check():
        _require(path is not None and verification_directory is not None)
        proof = _json(path)
        _require(isinstance(proof, dict) and set(proof) == {'schemaVersion', 'fingerprint', 'coverage'}
                 and type(proof['schemaVersion']) is int and proof['schemaVersion'] == 1)
        current = prepare_readiness(**context, coverage=proof['coverage'])
        _require(current == proof)
    try:
        check()
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError, sqlite3.Error,
            subprocess.SubprocessError, psutil.Error, httpx.HTTPError):
        return ExecutionReadiness(False, UNKNOWN, check)
    return ExecutionReadiness(True, None, check)
