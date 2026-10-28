"""Scratch-only QwenPaw source runtime; reuses installed dependencies, never pip installs."""
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import subprocess
import time

import httpx


class Runtime:
    def __init__(self, root, commit, instance_dir, plugin_dir=None):
        self.root = Path(root).resolve(strict=True)
        if not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{40}', commit):
            raise ValueError('Explicit lowercase full candidate SHA required')
        self.commit = commit
        base = self.root / 'var/upgrade-runtime'
        self.instance = Path(os.path.abspath(instance_dir))
        if self.instance == base or not self.instance.is_relative_to(base):
            raise ValueError('Runtime must be below project var/upgrade-runtime')
        for path in (self.instance, *self.instance.parents):
            if path.is_symlink():
                raise ValueError('Runtime storage cannot traverse symlinks')
            if path == self.root: break
        self.data = self.instance / 'data'
        self.secret_dir = self.instance / 'secrets'
        self.logs = self.instance / 'logs'
        self.state_path = self.instance / 'process.json'
        self.login_path = self.secret_dir / 'probe-login.json'
        self.python = self.root / '.venv/bin/python'
        self.candidate = self.root / 'var/upgrade-candidates' / ('qwenpaw-' + commit)
        spec = importlib.util.spec_from_file_location('upgrade_candidate_check', self.root / 'scripts/prepare_upgrade_candidate.py')
        self.checker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.checker)
        self.plugin = Path(plugin_dir).resolve() if plugin_dir else None
        if self.plugin is not None and (self.plugin.is_symlink() or not (self.plugin / 'plugin.json').is_file()):
            raise ValueError('Explicit plugin source must be a real directory containing plugin.json')
        self._process = None
        self._verify_candidate()

    def _verify_candidate(self):
        state = json.loads(self.candidate.with_name(self.candidate.name + '.state.json').read_text())
        remote = self.checker.OFFICIAL_REMOTES['qwenpaw']
        if (state.get('status') != 'ready' or state.get('commit') != self.commit
                or state.get('repository') != 'qwenpaw' or state.get('remote') != remote
                or state.get('path') != str(self.candidate.relative_to(self.root))):
            raise ValueError('Candidate has not completed preparation')
        self.checker.verify(self.candidate, remote, self.commit)

    def environment(self):
        # Exclude parent Qwen runtime, provider credentials and inherited Python paths.
        env = {key: os.environ[key] for key in
               ('PATH', 'HOME', 'LANG', 'LC_ALL', 'TZ', 'SSL_CERT_FILE', 'SSL_CERT_DIR') if key in os.environ}
        env.update(PYTHONPATH=str(self.candidate / 'src'), PYTHONDONTWRITEBYTECODE='1',
                   PYTHONNOUSERSITE='1', QWENPAW_WORKING_DIR=str(self.data),
                   QWENPAW_SECRET_DIR=str(self.secret_dir), QWENPAW_AUTH_ENABLED='true',
                   QWENPAW_DISABLE_KEYRING='true', QWENPAW_KEYRING_ACCOUNT='agent-line-upgrade-' + self.instance.name,
                   AGENT_LINE_PROBE_LOGIN=str(self.login_path))
        return env

    @staticmethod
    def _private(path, value):
        if path.is_symlink(): raise ValueError('Private file cannot be a symlink')
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream: json.dump(value, stream, indent=2)
        path.chmod(0o600)

    @property
    def credentials(self):
        if (self.login_path.is_symlink() or self.login_path.stat().st_uid != os.getuid()
                or self.login_path.stat().st_mode & 0o077):
            raise ValueError('Login credentials must be private and owned')
        value = json.loads(self.login_path.read_text())
        if set(value) != {'username', 'password'} or not all(isinstance(x, str) and x for x in value.values()):
            raise ValueError('Invalid private login file')
        return value

    def _run(self, args, timeout=120):
        result = subprocess.run([str(self.python), '-B', *args], cwd=self.instance,
            env=self.environment(), capture_output=True, text=True, timeout=timeout)
        with (self.logs / 'bootstrap.log').open('a') as stream:
            stream.write(result.stdout); stream.write(result.stderr)
        (self.logs / 'bootstrap.log').chmod(0o600)
        if result.returncode:
            raise RuntimeError('Candidate command failed; inspect private bootstrap log')
        return result.stdout

    def _prepare_directories(self):
        for path in (self.instance, self.data, self.secret_dir, self.logs):
            if path.is_symlink(): raise ValueError('Scratch directories cannot be symlinks')
            path.mkdir(parents=True, exist_ok=True); path.chmod(0o700)

    def verify_source(self):
        self._verify_candidate()
        self._prepare_directories()
        source = self._run(['-c', """import json,importlib.metadata as metadata,tomllib
from pathlib import Path
from packaging.requirements import Requirement
import qwenpaw
from qwenpaw.__version__ import __version__
project=tomllib.loads((Path(qwenpaw.__file__).resolve().parents[2]/'pyproject.toml').read_text())
versions=[]; errors=[]
for declaration in project['project']['dependencies']:
    requirement=Requirement(declaration)
    if requirement.marker and not requirement.marker.evaluate(): continue
    try: installed=metadata.version(requirement.name)
    except metadata.PackageNotFoundError:
        errors.append({'name':requirement.name,'reason':'missing'}); continue
    versions.append({'requirement':declaration,'installed':installed})
    if installed not in requirement.specifier:
        errors.append({'name':requirement.name,'reason':'version_mismatch'})
print(json.dumps({'modulePath':qwenpaw.__file__,'version':__version__,
                  'dependencyVersions':versions,'dependencyErrors':errors}))
"""])
        value = json.loads(source)
        if Path(value['modulePath']).resolve() != (self.candidate / 'src/qwenpaw/__init__.py').resolve():
            raise RuntimeError('Candidate source did not win Python import resolution')
        versions = value.get('dependencyVersions')
        if not isinstance(versions, list) or not versions or value.get('dependencyErrors') != []:
            raise RuntimeError('Candidate declared dependencies are missing or incompatible; inspect private bootstrap log')
        fingerprint = hashlib.sha256(json.dumps(versions, sort_keys=True).encode()).hexdigest()
        return {'modulePath': value['modulePath'], 'version': value['version'], 'commit': self.commit,
                'dependencies': 'shared-existing-venv-not-upgraded', 'dependencyScope': 'direct-declarations-current-markers',
                'dependencyCount': len(versions), 'dependencyFingerprint': fingerprint}

    def initialize(self):
        self._verify_candidate()
        if self.state_path.exists() and self._alive(json.loads(self.state_path.read_text())):
            raise RuntimeError('Do not initialize a running scratch instance')
        self._prepare_directories()
        self.verify_source()
        if not self.login_path.exists():
            self._private(self.login_path, {'username': 'agent-line-upgrade', 'password': secrets.token_urlsafe(32)})
        self.credentials
        if not (self.data / 'config.json').exists():
            self._run(['-m', 'qwenpaw', 'init', '--defaults', '--accept-security'])
        self._run(['-c', '''import json,os
from qwenpaw.config.utils import load_config,save_config
from qwenpaw.app.auth import register_user,has_registered_users,authenticate
cfg=load_config(); cfg.security.allow_no_auth_hosts=[]; save_config(cfg)
with open(os.environ['AGENT_LINE_PROBE_LOGIN']) as f: credentials=json.load(f)
if not has_registered_users(): assert register_user(**credentials)
assert authenticate(**credentials), 'Scratch authentication mismatch'
'''])
        source = self.plugin if self.plugin is not None else self.root / 'plugins/agent-line'
        self._run(['-m', 'qwenpaw', 'plugin', 'install', str(source), '--force'])
        self._private(self.instance / 'initialized.json', {'commit': self.commit, 'dependencies': 'shared-existing-venv-not-upgraded'})

    def _alive(self, state):
        try:
            proc = Path('/proc') / str(state['pid'])
            fields = (proc / 'stat').read_text().split()
            if fields[2] == 'Z': return False
            return fields[21] == state['startTicks']
        except (OSError, KeyError, IndexError): return False

    def _owned(self, state):
        if state.get('commit') != self.commit or state.get('instance') != str(self.instance) or not self._alive(state):
            return False
        proc = Path('/proc') / str(state['pid'])
        try:
            cmd = (proc / 'cmdline').read_bytes().split(b'\0')
            env = dict(item.split(b'=', 1) for item in (proc / 'environ').read_bytes().split(b'\0') if b'=' in item)
            return (cmd[:5] == [str(self.python).encode(), b'-B', b'-m', b'qwenpaw', b'app']
                    and env.get(b'PYTHONPATH') == str(self.candidate / 'src').encode()
                    and env.get(b'QWENPAW_WORKING_DIR') == str(self.data).encode()
                    and proc.stat().st_uid == os.getuid())
        except OSError: return False

    def start(self):
        self._verify_candidate()
        self._prepare_directories()
        if not (self.data / 'config.json').is_file() or not self.login_path.is_file():
            raise RuntimeError('Initialize or restore scratch data and credentials first')
        self.credentials
        self.verify_source()
        previous = json.loads(self.state_path.read_text()) if self.state_path.exists() else None
        if previous and self._alive(previous):
            if not self._owned(previous): raise RuntimeError('Existing live process does not match this runtime')
            return previous['baseUrl']
        port = previous['port'] if previous else 0
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('127.0.0.1', port)); port = sock.getsockname()[1]
        command = [str(self.python), '-B', '-m', 'qwenpaw', 'app', '--host', '127.0.0.1', '--port', str(port)]
        log_path = self.logs / ('service-' + self.commit + '.log')
        with log_path.open('ab') as log:
            log_path.chmod(0o600)
            self._process = subprocess.Popen(command, cwd=self.instance, env=self.environment(),
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        state = {'pid': self._process.pid, 'startTicks': (Path('/proc') / str(self._process.pid) / 'stat').read_text().split()[21],
                 'commit': self.commit, 'instance': str(self.instance), 'port': port,
                 'baseUrl': f'http://127.0.0.1:{port}', 'log': str(log_path)}
        self._private(self.state_path, state)
        try:
            for _ in range(90):
                if not self._alive(state): raise RuntimeError('Candidate exited during startup')
                try:
                    response = httpx.get(state['baseUrl'] + '/api/agent-line/health', timeout=2)
                    if response.status_code == 401: return state['baseUrl']
                    if response.status_code == 200: raise RuntimeError('Scratch authentication is bypassed')
                except httpx.HTTPError: pass
                time.sleep(1)
            raise RuntimeError('Candidate startup deadline exceeded')
        except BaseException:
            self.stop()
            raise

    def stop(self):
        if not self.state_path.exists(): return False
        state = json.loads(self.state_path.read_text())
        if not self._alive(state): return False
        if not self._owned(state): raise RuntimeError('Refuse to signal an unowned process')
        os.kill(state['pid'], signal.SIGTERM)
        for _ in range(40):
            if not self._alive(state):
                if self._process is not None: self._process.wait(timeout=2)
                return True
            time.sleep(1)
        raise RuntimeError('Owned scratch runtime still shutting down')
