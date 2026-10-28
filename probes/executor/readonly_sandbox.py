#!/usr/bin/env python3
"""Probe Codex's read-only Linux sandbox inside the existing executor boundary."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
PRIVATE_ROOT = ROOT / 'var/readonly-sandbox-20261022'
PRIVATE = PRIVATE_ROOT / f'run-{uuid.uuid4().hex[:12]}'
TIMEOUT_SECONDS = 30
IMAGE = 'sha256:1404c176b4453557838fbea513a6900d878247da72635de81ad2f5bc1b1d9903'
CONTROL_SCRIPT = (
    'set -eu; printf outer-write-ok > "$1/.outer-control"; '
    'test "$(cat "$1/.outer-control")" = outer-write-ok; rm "$1/.outer-control"; '
    'printf OUTER_WRITABLE_OK\\n'
)
INNER_SCRIPT = r'''set -eu
test "$(cat sentinel.txt)" = readonly-sentinel-v1
if printf changed > sentinel.txt 2>/dev/null; then exit 41; fi
if printf changed > write-test.txt 2>/dev/null; then exit 42; fi
if printf changed > via-link 2>/dev/null; then exit 43; fi
test "$(cat sentinel.txt)" = readonly-sentinel-v1
printf INNER_READ_OK_WRITES_REFUSED\n
'''


def private_write(path, data):
    path = Path(path)
    path.write_text(data)
    path.chmod(0o600)


def owner(tag):
    token = uuid.uuid4().hex
    return {
        'name': f'agent-line-readonly-{tag}-{token[:16]}',
        'owner': token,
        'pid': os.getpid(),
        'start': str(time.time_ns()),
        'uid': os.getuid(),
        'workspace': str((PRIVATE / 'workspace').resolve()),
    }


def docker_inspect(record):
    result = subprocess.run(
        ['docker', 'inspect', record['name']], capture_output=True, timeout=5,
    )
    if result.returncode:
        return None
    objects = json.loads(result.stdout)
    if len(objects) != 1:
        raise RuntimeError('inspect-result-count')
    container = objects[0]
    labels = container.get('Config', {}).get('Labels') or {}
    expected = {
        'agent-line.owner': record['owner'],
        'agent-line.pid': str(record['pid']),
        'agent-line.start': record['start'],
        'agent-line.uid': str(record['uid']),
    }
    if any(labels.get(key) != value for key, value in expected.items()):
        raise RuntimeError('ownership-label-mismatch')
    if record.get('containerId') and record['containerId'] != container['Id']:
        raise RuntimeError('container-id-mismatch')
    record['containerId'] = container['Id']
    host = container.get('HostConfig', {})
    network = container.get('NetworkSettings', {}).get('Networks', {})
    record['inspectProvenance'] = {
        'id': container['Id'],
        'imageId': container.get('Image'),
        'imageReference': container.get('Config', {}).get('Image'),
        'user': container.get('Config', {}).get('User'),
        'readOnlyRootfs': host.get('ReadonlyRootfs'),
        'capDrop': host.get('CapDrop'),
        'securityOptions': host.get('SecurityOpt'),
        'networkMode': host.get('NetworkMode'),
        'networkNames': sorted(network),
        'mounts': [
            {'source': item.get('Source'), 'destination': item.get('Destination'),
             'readOnly': item.get('RW') is False}
            for item in container.get('Mounts', [])
        ],
        'labels': expected,
    }
    return container


def cleanup_owned(record):
    """Stop/remove only after inspecting this run's unique name and labels."""
    container = docker_inspect(record)
    if container is None:
        return 'already-absent'
    container_id = container['Id']
    if container['State'].get('Running'):
        subprocess.run(['docker', 'stop', '--time', '3', container_id],
                       capture_output=True, timeout=6)
        container = docker_inspect(record)
        if container is None or container['Id'] != container_id:
            raise RuntimeError('container-changed-during-stop')
    removed = subprocess.run(['docker', 'rm', container_id],
                             capture_output=True, timeout=6)
    if removed.returncode:
        raise RuntimeError('docker-remove-failed')
    return 'removed-after-owner-inspect'


def run_container(launcher, args, record, log_path):
    argv = launcher.command(args, probe=True, owner=record)
    # probe=True deliberately supplies --network none and the official fixed
    # mounts; this control only changes Codex's executable entrypoint.
    code = argv.index('--entrypoint')
    if argv[code + 1] != '/bin/sh':
        raise RuntimeError('unexpected-probe-entrypoint')
    argv[code + 1] = '/usr/local/bin/codex'
    codex_argv = args
    try:
        result = subprocess.run(argv, capture_output=True, timeout=TIMEOUT_SECONDS)
        private_write(log_path, result.stdout.decode('utf-8', 'replace') +
                      result.stderr.decode('utf-8', 'replace'))
        code = result.returncode
        outcome = 'exit-0' if code == 0 else 'nonzero-exit'
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout or b''
        stderr = error.stderr or b''
        if isinstance(stdout, str):
            stdout = stdout.encode()
        if isinstance(stderr, str):
            stderr = stderr.encode()
        private_write(log_path, (stdout + stderr).decode('utf-8', 'replace'))
        code = None
        outcome = 'timeout'
    finally:
        cleanup = cleanup_owned(record)
    log_text = Path(log_path).read_text(errors='replace')
    if 'No permissions to create a new namespace' in log_text:
        error_class = 'bwrap-user-namespace-denied'
    elif 'filesystem-restricted execution requires bubblewrap to isolate app-server sockets' in log_text:
        error_class = 'sandbox-app-server-socket-isolation-panic'
    elif outcome == 'exit-0':
        error_class = None
    elif outcome == 'timeout':
        error_class = 'timeout'
    else:
        error_class = 'unclassified-nonzero-exit'
    return {'outcome': outcome, 'exitCode': code, 'errorClass': error_class,
            'codexArgv': codex_argv, 'cleanup': cleanup,
            'containerName': record['name'], 'containerId': record.get('containerId'),
            'inspectProvenance': record.get('inspectProvenance'),
            'log': str(log_path.relative_to(ROOT))}


def run_outer_control(launcher, config):
    record = owner('outer')
    argv = launcher.command(['-c', CONTROL_SCRIPT, 'readonly-control',
                             str(config['workspace'])], probe=True, owner=record)
    log = PRIVATE / 'outer-control.log'
    try:
        result = subprocess.run(argv, capture_output=True, timeout=TIMEOUT_SECONDS)
        private_write(log, result.stdout.decode('utf-8', 'replace') +
                      result.stderr.decode('utf-8', 'replace'))
        outcome = result.returncode == 0 and b'OUTER_WRITABLE_OK' in result.stdout
        code = result.returncode
    except subprocess.TimeoutExpired as error:
        output = error.stdout or b''
        if isinstance(output, str):
            output = output.encode()
        private_write(log, output.decode('utf-8', 'replace'))
        outcome, code = False, None
    finally:
        cleanup = cleanup_owned(record)
    return {'passed': outcome, 'exitCode': code, 'cleanup': cleanup,
            'containerName': record['name'], 'containerId': record.get('containerId'),
            'inspectProvenance': record.get('inspectProvenance'),
            'log': str(log.relative_to(ROOT))}


def main():
    PRIVATE.mkdir(mode=0o700, parents=True)
    workspace = PRIVATE / 'workspace'
    codex_home = PRIVATE / 'codex-home'
    workspace.mkdir(mode=0o700)
    codex_home.mkdir(mode=0o700)
    original = json.loads((ROOT / 'var/executor/config.json').read_text())
    if original['image'] != IMAGE:
        raise SystemExit('Configured immutable image ID differs from expected image')
    config = dict(original, workspace=str(workspace.resolve()),
                  codex_home=str(codex_home.resolve()))
    config_path = PRIVATE / 'config.json'
    private_write(config_path, json.dumps(config, indent=2))
    (workspace / 'sentinel.txt').write_text('readonly-sentinel-v1')
    (workspace / 'sentinel.txt').chmod(0o600)
    (workspace / 'via-link').symlink_to(workspace / 'sentinel.txt')

    sys.path.insert(0, str(ROOT / 'deploy/executor'))
    import launcher  # pylint: disable=import-outside-toplevel
    launcher.CONFIG = config_path

    try:
        features = subprocess.run(
            [config['binary'], 'features', 'list'], capture_output=True,
            timeout=TIMEOUT_SECONDS, check=True,
            env=dict(os.environ, CODEX_HOME=str(codex_home), HOME=str(codex_home)),
        )
        feature_text = features.stdout.decode('utf-8', 'replace')
        feature_line = next((line.strip() for line in feature_text.splitlines()
                             if line.strip().startswith('use_legacy_landlock ')), None)
        private_write(PRIVATE / 'feature-list.log', feature_text)
        if feature_line != 'use_legacy_landlock                      deprecated         false':
            raise RuntimeError('unexpected-legacy-feature-state')
    except (subprocess.SubprocessError, OSError) as error:
        feature_line = f'error:{type(error).__name__}'

    outer = run_outer_control(launcher, config)
    if not outer['passed']:
        raise RuntimeError('outer-workspace-writable-control-failed')

    attempts = {}
    for key, options in (
        ('default', ['--sandbox', 'read-only']),
        ('legacy', ['--enable', 'use_legacy_landlock', '--sandbox', 'read-only']),
    ):
        command = options + ['sandbox', '--', '/bin/sh', '-c', INNER_SCRIPT]
        attempt = run_container(launcher, command, owner(key), PRIVATE / f'{key}.log')
        attempt['innerReadAndWriteRefusalPassed'] = attempt['outcome'] == 'exit-0'
        attempts[key] = attempt
    intact = (workspace / 'sentinel.txt').read_text() == 'readonly-sentinel-v1'
    report = {
        'schemaVersion': 1,
        'probe': 'native-codex-read-only-landlock',
        'version': '0.163.0',
        'imageId': config['image'],
        'binary': config['binary'],
        'workspace': str(workspace.resolve()),
        'codexHomeStartedEmpty': True,
        'featureStateBeforeProbe': feature_line,
        'legacyFeatureOverrideActiveVerified': False,
        'outerWritableControl': outer,
        'attempts': attempts,
        'sentinelIntactAfterBothAttempts': intact,
    }
    report_path = PRIVATE / 'report.json'
    private_write(report_path, json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0 if intact else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        # Keep exception text/paths private; terminal output contains only class.
        print(f'probe-error:{type(error).__name__}', file=sys.stderr)
        raise SystemExit(2)
