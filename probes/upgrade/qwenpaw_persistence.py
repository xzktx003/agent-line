#!/usr/bin/env python3
"""Isolated commit upgrade and offline backup restoration; no production switch."""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import httpx
from probes.upgrade.qwenpaw_runtime import Runtime
from scripts.prepare_upgrade_candidate import verify, OFFICIAL_REMOTES

OLD = '28f2b9f624d4917268f964cffc8c3cba519f8b87'
NEW = '502b59d88464d5c9e8be4a507347282d6a54e0be'
ROLE = 'upgrade-persistence-role'
MEMORY = '/api/workspace/memory/upgrade-fixture.md?section=digest'
BEFORE = '# 升级验收\n\n固定记忆：UPGRADE_BEFORE_cobalt_orchard\n'
AFTER = '# 升级验收\n\n升级后的独立写入：UPGRADE_AFTER_cobalt_orchard\n'


def save(path, value):
    temp = path.with_suffix('.next')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
    os.replace(temp, path)


@contextmanager
def authenticated(runtime, base):
    client = httpx.Client(base_url=base, timeout=30)
    try:
        anonymous = client.get('/api/agent-line/health')
        assert anonymous.status_code == 401, 'Anonymous plugin access not rejected'
        login = client.post('/api/auth/login', json=runtime.credentials)
        assert login.status_code == 200, 'Login failed'
        payload = login.json()
        token = payload.get('token') or payload.get('access_token')
        assert token, 'Login token absent'
        client.headers.update({'Authorization': 'Bearer ' + token, 'X-Agent-Id': ROLE})
        yield client
    finally:
        client.close()


def call(client, method, path, body=None):
    response = client.request(method, path, json=body)
    if response.status_code >= 400:
        raise RuntimeError('Fixture HTTP failure: ' + str(response.status_code))
    return response.json()


def inspect(client):
    deadline = time.monotonic() + 60
    while True:
        health = client.get('/api/agent-line/health')
        if health.status_code != 404 or time.monotonic() >= deadline:
            break
        time.sleep(.2)
    assert health.status_code == 200, 'Plugin route unavailable'
    assert health.json()['scope'] == 'plugin-registration-only'
    roles = call(client, 'GET', '/api/agents')['agents']
    matches = [role for role in roles if role['id'] == ROLE]
    assert len(matches) == 1
    role = matches[0]
    memory = call(client, 'GET', MEMORY)
    return {'role': {key: role[key] for key in ('id', 'name', 'backend')},
            'memory': memory['content'], 'anonymousDenied': True, 'pluginAuthenticated': True}


def copy_offline(source, target):
    # New destination only, no symlink dereference into unrelated data.
    if any(path.is_symlink() for path in source.rglob('*')):
        raise RuntimeError('Fixture backup refuses symlinks')
    shutil.copytree(source, target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--attempt', required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', args.attempt):
        raise ValueError('Invalid attempt name')
    for commit in (OLD, NEW):
        verify(ROOT / 'var/upgrade-candidates' / ('qwenpaw-' + commit),
               OFFICIAL_REMOTES['qwenpaw'], commit)
    directory = ROOT / 'var/upgrade-runtime' / args.attempt
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    report = {'scope': 'isolated_qwenpaw_commit_upgrade_and_backup_restore',
              'oldCommit': OLD, 'newCommit': NEW, 'verified': False,
              'productionChanged': False, 'crossReleaseValidated': False,
              'modelExecutionValidated': False, 'phase': 'initializing', 'observations': []}
    report_path = directory / 'report.json'
    save(report_path, report)
    active = None
    try:
        instance = directory / 'installation'
        active = Runtime(ROOT, OLD, instance)
        active.initialize()
        base = active.start()
        with authenticated(active, base) as http:
            call(http, 'POST', '/api/agents', {'id': ROLE, 'name': '升级持久角色',
                'description': '独立升级验证，无模型调用', 'backend': 'qwenpaw',
                'language': 'zh', 'skill_names': []})
            written = call(http, 'PUT', MEMORY, {'content': BEFORE})
            assert written.get('written') is True
            initial = inspect(http)
            assert initial['memory'].strip() == BEFORE.strip()
        report['observations'].append({'phase': 'old', 'source': active.verify_source(), **initial})
        active.stop()
        active = None
        backup = directory / 'offline-backup'
        backup.mkdir(mode=0o700)
        for name in ('data', 'secrets'):
            copy_offline(instance / name, backup / name)
        shutil.copy2(instance / 'initialized.json', backup / 'initialized.json')
        report['phase'] = 'backup_complete'
        save(report_path, report)

        active = Runtime(ROOT, NEW, instance)
        base = active.start()
        with authenticated(active, base) as http:
            upgraded = inspect(http)
            assert upgraded == initial, 'Existing role or memory changed during upgrade'
            call(http, 'PUT', MEMORY, {'content': AFTER})
            assert inspect(http)['memory'].strip() == AFTER.strip()
        report['observations'].append({'phase': 'new', 'source': active.verify_source(), **upgraded})
        active.stop()
        active = None
        report['phase'] = 'upgrade_verified'
        save(report_path, report)

        # Do not reconnect old code to potentially migrated new data.
        # Preserve the upgraded tree, then restore at the original scratch path:
        # official role configuration can contain absolute workspace references.
        preserved = directory / 'post-upgrade-installation'
        instance.rename(preserved)
        instance.mkdir(mode=0o700)
        (instance / 'logs').mkdir(mode=0o700)
        for name in ('data', 'secrets'):
            copy_offline(backup / name, instance / name)
        shutil.copy2(backup / 'initialized.json', instance / 'initialized.json')
        active = Runtime(ROOT, OLD, instance)
        base = active.start()
        with authenticated(active, base) as http:
            recovered = inspect(http)
            assert recovered == initial, 'Restored fixture differs from pre-upgrade snapshot'
            assert 'UPGRADE_AFTER' not in recovered['memory']
        report['observations'].append({'phase': 'restored_old', 'source': active.verify_source(), **recovered})
        active.stop()
        active = None
        report.update(phase='complete', verified=True, offlineBackupRestored=True,
                      postUpgradeDataPreservedAt=str(preserved.relative_to(ROOT)))
        save(report_path, report)
        print(json.dumps({key: value for key, value in report.items() if key != 'observations'}))
    except BaseException as error:
        report['failureType'] = type(error).__name__
        save(report_path, report)
        raise
    finally:
        if active is not None:
            active.stop()


if __name__ == '__main__':
    main()
