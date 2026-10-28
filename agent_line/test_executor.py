"""Durable, single-attempt test execution in a pinned, read-only container."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import time

from .verification import _directory, _persist, _task_scope
from .verification_policy import policy_hash, validate_test_check


# This exact program is part of the pinned execution environment. Child output
# never streams directly into the Docker logging driver.
COLLECTOR = r"""
import os, selectors, signal, subprocess, sys
limit = int(sys.argv[1])
streams = [bytearray(), bytearray()]
child = None
selector = selectors.DefaultSelector()
code = 125
try:
    child = subprocess.Popen(sys.argv[2:], stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    for index, pipe in enumerate((child.stdout, child.stderr)):
        os.set_blocking(pipe.fileno(), False)
        selector.register(pipe, selectors.EVENT_READ, index)
    overflow = False
    while selector.get_map() and not overflow:
        for key, _ in selector.select(.2):
            remaining = limit + 1 - sum(map(len, streams))
            data = os.read(key.fileobj.fileno(), min(65536, remaining))
            if not data:
                selector.unregister(key.fileobj)
                continue
            streams[key.data].extend(data)
            if sum(map(len, streams)) > limit:
                overflow = True
                break
    if overflow:
        try: os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        code = 125
    else:
        code = child.wait()
        if code < 0: code = 128 - code
except Exception:
    code = 125
finally:
    selector.close()
    if child is not None and child.poll() is None:
        try: os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        child.wait()
    for index, data in enumerate(streams):
        view = memoryview(data)
        while view:
            count = os.write(index + 1, view[:65536])
            view = view[count:]
sys.exit(code)
"""


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _private(fd):
    s = os.fstat(fd)
    if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) != 0o600 or s.st_nlink != 1:
        raise ValueError('Unsafe test evidence file')


def _command(argv, limit=65536, timeout=15):
    """Bound both downloaded output and waiting time, including docker logs."""
    child = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    selector = selectors.DefaultSelector()
    output = [bytearray(), bytearray()]
    truncated = False
    try:
        for index, pipe in enumerate((child.stdout, child.stderr)):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, index)
        deadline = time.monotonic() + timeout
        while selector.get_map():
            if time.monotonic() >= deadline:
                raise TimeoutError('Docker observation timed out')
            for key, _ in selector.select(min(.1, max(0, deadline-time.monotonic()))):
                data = os.read(key.fileobj.fileno(), min(65536, limit+1))
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                remaining = limit-sum(map(len, output))
                output[key.data].extend(data[:max(0, remaining)])
                if len(data) > remaining:
                    truncated = True
                    child.kill()
                    return (-1, bytes(output[0]), bytes(output[1]), True)
        return child.wait(timeout=max(.01, deadline-time.monotonic())), bytes(output[0]), bytes(output[1]), truncated
    finally:
        selector.close()
        if child.poll() is None:
            child.kill()
        child.wait()
        child.stdout.close()
        child.stderr.close()


class DockerTestExecutor:
    def __init__(self, evidence_directory, image_id):
        if not isinstance(image_id, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', image_id):
            raise ValueError('Immutable full image ID required')
        self.directory = Path(evidence_directory).absolute()
        self.image_id = image_id
        self.descriptor = dict(image=image_id, uid=os.getuid(), gid=os.getgid(), network='none',
                               readonlyRoot=True, readonlyWorkspace=True, timeoutBinary='/usr/bin/timeout',
                               timeoutArguments=['--signal=KILL', '--kill-after=2', '--'],
                               collectorPython='/usr/bin/python3', collectorSha256=hashlib.sha256(COLLECTOR.encode()).hexdigest(),
                               logConfig={'Type':'json-file','Config':{'max-size':'8m','max-file':'1'}},
                               cpus=2, memory='2g', pids=128, capDrop='ALL', noNewPrivileges=True,
                               init=True, environment={'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':'/tmp',
                                                      'PYTHONDONTWRITEBYTECODE':'1','PYTHONUNBUFFERED':'1'}, tmpfs='/tmp:rw,nosuid,nodev,size=128m,mode=1777', version=1)
        self.environment_id = 'docker-test-v1:' + _hash(self.descriptor)
        fd = self._directory()
        os.close(fd)

    def _directory(self):
        fd = _directory(self.directory)
        s = os.fstat(fd)
        if s.st_uid != os.getuid() or stat.S_IMODE(s.st_mode) != 0o700:
            os.close(fd)
            raise ValueError('Private test evidence directory required')
        return fd

    def validate_policy(self, policy):
        if policy.get('testEnvironmentId') != self.environment_id:
            raise ValueError('Test environment does not match frozen policy')

    def _inspect(self, name):
        code, out, _, cut = _command(['docker', 'inspect', '--type', 'container', name], limit=262144)
        if code != 0 or cut:
            return None
        values = json.loads(out)
        if not isinstance(values, list) or len(values) != 1:
            raise ValueError('Invalid container observation')
        return values[0]

    def _matches(self, value, binding, name, argv):
        config, host = value['Config'], value['HostConfig']
        mounts = value['Mounts']
        env = config.get('Env') or []
        for key, expected in self.descriptor['environment'].items():
            if [item for item in env if item.startswith(key+'=')] != [key+'='+expected]:
                raise ValueError('Container environment mismatch')
        if (not re.fullmatch(r'[0-9a-f]{64}', value['Id']) or value['Name'] != '/'+name
                or value['Image'] != self.image_id or config['Labels'].get('agent-line.test-binding') != _hash(binding)
                or config['Labels'].get('agent-line.test-owner') != _hash(binding)[:32]
                or config.get('Entrypoint') != ['/usr/bin/timeout'] or config['Cmd'] != argv
                or config['WorkingDir'] != binding['workspace'] or config['User'] != f'{os.getuid()}:{os.getgid()}'
                or host['NetworkMode'] != 'none' or host['ReadonlyRootfs'] is not True
                or host['Privileged'] is not False or host['Init'] is not True
                or host['PidsLimit'] != 128 or host['Memory'] != 2147483648 or host['NanoCpus'] != 2000000000
                or host['CapDrop'] != ['ALL'] or host['SecurityOpt'] != ['no-new-privileges']
                or host.get('LogConfig') != self.descriptor['logConfig']
                or host.get('CapAdd') or host.get('Devices') or host.get('Binds')
                or host.get('Tmpfs') != {'/tmp': 'rw,nosuid,nodev,size=128m,mode=1777'}
                or len(mounts) != 1 or mounts[0]['Type'] != 'bind' or mounts[0]['RW'] is not False
                or mounts[0]['Source'] != binding['workspace'] or mounts[0]['Destination'] != binding['workspace']):
            raise ValueError('Container binding mismatch')

    def _argv(self, test):
        return [*self.descriptor['timeoutArguments'], str(test['timeoutSeconds'])+'s',
                self.descriptor['collectorPython'], '-c', COLLECTOR, str(test['maxOutputBytes']), *test['argv']]

    def _cleanup(self, record, binding, name, argv):
        check = record['check']
        full_id = check['containerId']
        if record.get('containerId') != full_id:
            raise ValueError('Cleanup identity mismatch')
        if record.get('removed') is True:
            return {'status':'completed', 'check':check}
        value = self._inspect(full_id)
        if value is None:
            # An inspect error alone is not absence. A successful daemon query
            # with an exact immutable ID filter must positively return no rows.
            code, out, _, cut = _command(['docker', 'ps', '-a', '--no-trunc', '--filter', 'id='+full_id, '--format', '{{.ID}}'])
            if code != 0 or cut or out.strip():
                return {'status':'blocked', 'reason':'test_cleanup_unconfirmed'}
        else:
            self._matches(value, binding, name, argv)
            state = value['State']
            proof = check['resourceExitReceipt']
            if (value['Id'] != full_id or state.get('Running') is not False
                    or type(state.get('Pid')) is not int or state['Pid'] != 0
                    or state.get('Status') != 'exited' or state.get('ExitCode') != proof['exitCode']
                    or state.get('StartedAt') != proof['startedAt'] or state.get('FinishedAt') != proof['finishedAt']):
                raise ValueError('Cleanup exit proof changed')
            code, _, _, cut = _command(['docker', 'rm', full_id])
            if code != 0 or cut:
                return {'status':'waiting', 'reason':'test_cleanup_pending'}
        record['removed'] = True
        _persist(self.directory, record)
        return {'status':'completed', 'check':check}

    def check(self, task, policy, test):
        try:
            return self._check(task, policy, test)
        except Exception:
            # Docker errors may include host paths, environment or command contents.
            return {'status': 'blocked', 'reason': 'test_executor_evidence_or_transport_unavailable'}

    def cancel(self, task, policy, test):
        try:
            return self._check(task, policy, test, cancel_only=True)
        except Exception:
            return {'status':'blocked', 'reason':'test_cancellation_evidence_or_transport_unavailable'}

    def _canceled(self, record, binding, *, check=None):
        proof = {key: binding[key] for key in ('taskId', 'executionIdentity', 'policySha256', 'testId', 'environmentId')}
        proof['disposition'] = 'exited' if check is not None else 'not_started'
        if check is not None:
            proof.update(containerId=check['containerId'], resourceExitReceipt=check['resourceExitReceipt'], check=check)
        record['cancellation'] = proof
        _persist(self.directory, record)
        return {'status':'completed', 'cancellation':proof}

    def _finish(self, record, binding, name, argv, cancel_only):
        result = self._cleanup(record, binding, name, argv)
        if cancel_only and result['status'] == 'completed':
            return self._canceled(record, binding, check=result['check'])
        return result

    def _check(self, task, policy, test, *, cancel_only=False):
        self.validate_policy(policy)
        workspace = Path(task['payload']['workspace'])
        identity = _task_scope(task, workspace)
        if workspace != workspace.resolve(strict=True) or ',' in str(workspace):
            raise ValueError('Canonical workspace required')
        if self.directory.is_relative_to(workspace) or workspace.is_relative_to(self.directory):
            raise ValueError('Evidence must be separate from workspace')
        wf = _directory(workspace)
        os.close(wf)
        if (not isinstance(test, dict) or set(test) != {'id', 'argv', 'timeoutSeconds', 'maxOutputBytes'}
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,159}', test['id'])
                or type(test['timeoutSeconds']) is not int or not 1 <= test['timeoutSeconds'] <= 300
                or type(test['maxOutputBytes']) is not int or not 1 <= test['maxOutputBytes'] <= 1048576
                or not isinstance(test['argv'], list) or not 1 <= len(test['argv']) <= 64
                or any(not isinstance(x, str) or not x or '\0' in x for x in test['argv'])
                or sum(len(x) for x in test['argv']) > 16384
                or test not in policy.get('tests', [])):
            raise ValueError('Frozen test policy required')
        binding = dict(taskId=task['id'], executionIdentity=identity, policySha256=policy_hash(policy),
                       testId=test['id'], environmentId=self.environment_id, workspace=str(workspace), test=test)
        key = _hash({'taskId': task['id'], 'testId': test['id']})
        name = 'agent-line-test-'+key[:40]
        filename = 'test-'+key+'.json'
        fd = self._directory()
        lock = None
        try:
            lock = os.open('test-'+key+'.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=fd)
            _private(lock)
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                handle = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            except FileNotFoundError:
                record = dict(evidenceFile=filename, binding=binding, name=name, createIntent=not cancel_only, startIntent=False)
                if cancel_only:
                    record['cancelRequested'] = True
                    return self._canceled(record, binding)
                _persist(self.directory, record)
                fresh = True
            else:
                try:
                    _private(handle)
                    raw = os.read(handle, 8_000_001)
                    if len(raw) > 8_000_000:
                        raise ValueError('Oversized test receipt')
                    record = json.loads(raw)
                finally:
                    os.close(handle)
                fresh = False
                if record.get('binding') != binding or record.get('name') != name or record.get('evidenceFile') != filename:
                    raise ValueError('Test receipt binding conflict')
                if cancel_only:
                    record['cancelRequested'] = True
                    _persist(self.directory, record)
                if record.get('cancellation', {}).get('disposition') == 'not_started':
                    if record.get('cancelRequested') is not True:
                        raise ValueError('Missing durable cancellation')
                    if cancel_only:
                        return self._canceled(record, binding)
                    return {'status':'blocked', 'reason':'test_canceled'}
                if 'check' in record:
                    validate_test_check(record['check'], test, policy, task['id'], identity)
                    if record['check']['resourceExitReceipt']['workspace'] != str(workspace):
                        raise ValueError('Receipt workspace mismatch')
                    return self._finish(record, binding, name, self._argv(test), cancel_only)
            if record.get('cancelRequested') and not cancel_only:
                return {'status':'blocked', 'reason':'test_canceled'}
            argv = self._argv(test)
            if fresh:
                create = ['docker', 'create', '--pull=never', '--name', name, '--label', 'agent-line.test-binding='+_hash(binding),
                          '--label', 'agent-line.test-owner='+_hash(binding)[:32],
                          '--log-driver', 'json-file', '--log-opt', 'max-size=8m', '--log-opt', 'max-file=1',
                          '--init', '--network', 'none', '--read-only', '--cap-drop', 'ALL',
                          '--security-opt', 'no-new-privileges', '--pids-limit', '128', '--memory', '2g', '--cpus', '2',
                          '--user', f'{os.getuid()}:{os.getgid()}', '--tmpfs', self.descriptor['tmpfs'],
                          '--mount', f'type=bind,src={workspace},dst={workspace},readonly', '--workdir', str(workspace),
                          *[part for key, value in self.descriptor['environment'].items() for part in ('--env', key+'='+value)],
                          '--entrypoint', '/usr/bin/timeout', self.image_id, *argv]
                try:
                    _command(create)
                except Exception:
                    pass  # Only inspect the original name after an ambiguous create.
            value = self._inspect(name)
            if value is None:
                return {'status': 'blocked', 'reason': 'container_creation_unconfirmed'}
            self._matches(value, binding, name, argv)
            if record.get('containerId') is not None and record['containerId'] != value['Id']:
                raise ValueError('Original container replaced')
            state = value['State']
            if state['Status'] == 'created':
                if record.get('startIntent'):
                    return {'status': 'blocked', 'reason': 'container_start_unconfirmed'}
                if cancel_only:
                    # Created under our exact immutable binding, never submitted start.
                    code, _, _, cut = _command(['docker', 'rm', value['Id']])
                    if code != 0 or cut:
                        return {'status':'waiting', 'reason':'test_cleanup_pending'}
                    record['containerId'] = value['Id']
                    record['removed'] = True
                    return self._canceled(record, binding)
                record['startIntent'] = True
                record['containerId'] = value['Id']
                _persist(self.directory, record)
                try:
                    _command(['docker', 'start', value['Id']])
                except Exception:
                    pass
                return {'status': 'waiting', 'reason': 'container_start_submitted'}
            if record.get('containerId') != value['Id'] or record.get('startIntent') is not True:
                raise ValueError('Unowned started container')
            if state['Running'] is True:
                if cancel_only:
                    # The intent was fsynced above; an acknowledgement is not exit.
                    _command(['docker', 'kill', '--signal', 'KILL', value['Id']])
                    return {'status':'waiting', 'reason':'test_cancellation_submitted'}
                return {'status': 'waiting', 'reason': 'test_running'}
            if state['Status'] != 'exited' or state['Running'] is not False or type(state['Pid']) is not int or state['Pid'] != 0 or type(state['ExitCode']) is not int:
                return {'status': 'blocked', 'reason': 'container_exit_unconfirmed'}
            if any(not isinstance(state.get(k), str) or not state[k].endswith('Z') or state[k].startswith('0001-') for k in ('StartedAt', 'FinishedAt')):
                raise ValueError('Missing real container timestamps')
            if type(state.get('OOMKilled')) is not bool:
                raise ValueError('Missing OOM evidence')
            proof = dict(source='docker-inspect-before-remove', containerId=value['Id'], taskId=task['id'],
                         executionIdentity=identity, policySha256=binding['policySha256'], testId=test['id'],
                         owner=_hash(binding)[:32], workspace=str(workspace), startedAt=state['StartedAt'], finishedAt=state['FinishedAt'],
                         environmentId=self.environment_id, oomKilled=state['OOMKilled'], running=False, pid=0, status='exited', exitCode=state['ExitCode'])
            logs_code, stdout, stderr, cut = _command(['docker', 'logs', value['Id']], limit=test['maxOutputBytes'])
            if logs_code != 0 and not cut:
                return {'status': 'blocked', 'reason': 'test_logs_unavailable'}
            reason = ('output_limit_exceeded' if cut else 'oom_killed' if state['OOMKilled']
                      else 'command_exit_zero' if state['ExitCode'] == 0 else 'command_exit_nonzero')
            check = dict(kind='test_command', testId=test['id'], argv=test['argv'],
                         timeoutSeconds=test['timeoutSeconds'], maxOutputBytes=test['maxOutputBytes'],
                         environmentId=self.environment_id, exitCode=state['ExitCode'],
                         status='passed' if reason == 'command_exit_zero' else 'failed', reason=reason,
                         containerId=value['Id'], resourceExitReceipt=proof,
                         stdout=stdout.decode('utf-8', 'ignore'), stderr=stderr.decode('utf-8', 'ignore'), outputTruncated=cut)
            validate_test_check(check, test, policy, task['id'], identity)
            record['check'] = check
            _persist(self.directory, record)
            return self._finish(record, binding, name, argv, cancel_only)
        finally:
            if lock is not None:
                os.close(lock)
            os.close(fd)
