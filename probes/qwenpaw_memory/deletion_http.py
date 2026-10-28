#!/usr/bin/env python3
"""Delete only the generated review_http probe memory; never claim index erasure.

prepare explicitly backs up/upgrades this probe's ledger and installs the owned
plugin through the official host API. run consumes the existing review receipt.
Repeated runs observe the original intent; they never recreate the probe memory.
"""
import argparse
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import httpx
from fastapi.testclient import TestClient
from agent_line.adapters.qwenpaw_memory import content_hash
from agent_line.memory import MemoryCommitStore
from agent_line.runtime import load_app
from agent_line.store import TaskStore
from deploy.qwenpaw.manage import environment, live
from probes.sim.workflow_probe import write_private

DIRECTORY = ROOT / 'var/memory-review-http'
TASK = 'task-00d41e38e770420b8881543dcfa9c903'
ROLE = 'agent-line-g0-parent'
TOKEN = ROOT / 'var/qwenpaw-secrets/memory-deletion-service-token'
STATE = ROOT / 'var/qwenpaw/memory-governance.sqlite'


@contextmanager
def host():
    process = json.loads((ROOT / 'var/qwenpaw/agent-line-process.json').read_text())
    assert live(process), 'Owned host must already be running'
    client = httpx.Client(base_url='http://127.0.0.1:' + str(process['port']),
                         timeout=120, follow_redirects=False, trust_env=False)
    try:
        response = client.post('/api/auth/login', json=json.loads(
            (ROOT / 'var/qwenpaw-secrets/local-login.json').read_text()))
        assert response.status_code == 200, 'Host authentication failed'
        login = response.json()
        client.headers['Authorization'] = 'Bearer ' + (login.get('token') or login['access_token'])
        yield client
    finally:
        client.close()


def install():
    with host() as client:
        response = client.post('/api/plugins/install', json={
            'source': str(ROOT / 'plugins/agent-line'), 'force': True})
        assert response.status_code == 200, 'Official plugin install failed'
    for name in ('plugin.py', 'execution_bridge.py', 'memory_governance.py'):
        assert (ROOT / 'plugins/agent-line' / name).read_bytes() == (
            ROOT / 'var/qwenpaw/plugins/agent-line' / name).read_bytes()


def prepare():
    proof = json.loads((DIRECTORY / 'report.json').read_text())
    assert proof['verified'] is True and proof['taskId'] == TASK and proof['roleAgentId'] == ROLE
    task = TaskStore(ROOT / 'var/role-dispatch/tasks.sqlite').get(TASK)
    assert task['executionStatus'] == 'succeeded' and task['resourcesReleased'] is True
    os.environ.update(environment())
    from qwenpaw.config.utils import load_config, save_config
    config = load_config()
    plugin = dict(config.plugins['agent-line'])
    backup = ROOT / 'var/qwenpaw-secrets/plugin-before-memory-governance.json'
    if not backup.exists():
        write_private(backup, plugin)
    if not TOKEN.exists():
        fd = os.open(TOKEN, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(secrets.token_urlsafe(48))
    spec = importlib.util.spec_from_file_location('probe_memory_governance',
        ROOT / 'plugins/agent-line/memory_governance.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not STATE.exists():
        module.initialize_state(STATE)
    expected = {'managed_role_ids': [ROLE], 'service_token_file': str(TOKEN), 'state_file': str(STATE)}
    assert plugin.get('memory_governance', expected) == expected, 'Preserve unrelated governance configuration'
    plugin['memory_governance'] = expected
    module.MemoryGovernance(expected)  # Validate private paths before config write.
    config.plugins['agent-line'] = plugin
    save_config(config)
    commits = DIRECTORY / 'commits.sqlite'
    backup = DIRECTORY / 'commits-before-deletion.sqlite'
    if not backup.exists():
        fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with sqlite3.connect(commits.as_uri() + '?mode=ro', uri=True) as source, sqlite3.connect(backup) as target:
            source.backup(target)
    MemoryCommitStore.upgrade(commits)
    path = DIRECTORY / 'memory.json'
    memory = json.loads(path.read_text())
    backup = DIRECTORY / 'memory-before-deletion.json'
    if not backup.exists():
        write_private(backup, memory)
    binding = memory['bindings']['probe-memory']
    assert binding['roleAgentId'] == ROLE
    binding['deletionServiceTokenFile'] = str(TOKEN)
    memory['credentials'][0]['capabilities'] = ['review_memory', 'delete_memory']
    write_private(path, memory)
    install()
    print(json.dumps({'prepared': True, 'probeSchema': 2, 'pluginInstalled': True,
                      'mainDeletionEnabled': False, 'upstreamSourceModified': False}))


def run():
    memory = json.loads((DIRECTORY / 'memory.json').read_text())
    state = json.loads((DIRECTORY / 'state.json').read_text())
    digest = content_hash(state['content'])
    source = TaskStore(ROOT / 'var/role-dispatch/tasks.sqlite')
    original = source.get(TASK)
    commits = MemoryCommitStore(DIRECTORY / 'commits.sqlite')
    commit = commits.get(ROLE, TASK)
    assert commit['content_hash'] == digest and commit['decision_id'] == state['decisionId']
    try:
        intent = commits.get_deletion(ROLE, TASK)
    except KeyError:
        intent = None
    assert intent is not None or commit['status'] == 'committed'
    deletion_state = DIRECTORY / 'deletion-state.json'
    if not deletion_state.exists():
        assert intent is None, 'Never replace an unknown deletion identity'
        write_private(deletion_state, {'decisionId': 'memory-delete-probe-' + secrets.token_hex(12),
                                       'expectedContentSha256': digest})
    body = json.loads(deletion_state.read_text())
    assert body['expectedContentSha256'] == digest
    human = {'Authorization': 'Bearer ' + memory['credentials'][0]['token']}
    path = '/extension/v1/memories/probe-memory/' + TASK
    native_path = '/api/agent-line/memory/' + ROLE + '/' + TASK
    calls = []
    send = httpx.Client.send

    def traced(client, request, **kwargs):
        if request.url.path.startswith(native_path) or (
                request.method == 'PUT' and '/api/workspace/memory/' in request.url.path):
            calls.append((request.method, request.url.path))
        return send(client, request, **kwargs)

    with patch.object(httpx.Client, 'send', traced):
        with TestClient(load_app(DIRECTORY / 'service.json')) as client:
            response = client.post(path + '/delete', headers=human, json=body)
            assert response.status_code == 202, 'Deletion API rejected probe'
            first = response.json()
            receipt = first['deletion']['receipt']
            assert first['content'] == '' and first['status'] == 'deleting'
            assert receipt['fileAbsent'] is True and receipt['indexSourceAbsent'] is False
            assert receipt['roleBlocked'] is True
        # Reload the official plugin as well as the adapter to prove durable block.
        install()
        with TestClient(load_app(DIRECTORY / 'service.json')) as client:
            response = client.post(path + '/delete', headers=human, json=body)
            assert response.status_code == 202 and response.json()['content'] == ''
            response = client.get(path + '/deletion', headers=human)
            assert response.status_code == 200
            latest = response.json()
            assert latest['status'] == 'deleting' and latest['deletion']['receipt']['roleBlocked'] is True
            replay = client.post(path + '/review', headers=human, json=state)
            assert replay.status_code == 202 and replay.json()['content'] == ''
            assert replay.json()['status'] == 'deleting'
            listing = client.get('/extension/v1/memories', headers=human)
            assert listing.status_code == 200 and state['content'] not in listing.text
    assert sum(method == 'POST' for method, _ in calls) == (1 if intent is None else 0)
    assert not any(method == 'PUT' for method, _ in calls)
    with host() as client:
        response = client.get('/api/workspace/memory/agent-line-task-' + TASK + '.md',
            params={'section': 'digest'}, headers={'X-Agent-Id': ROLE})
        assert response.status_code == 404
        response = client.get(native_path + '/deletion', params={'expectedContentSha256': digest})
        assert response.status_code == 401, 'Host login alone must not authorize governance'
        config = json.loads((ROOT / 'var/qwenpaw/config.json').read_text())['plugins']['agent-line']['execution_bridge']
        with sqlite3.connect(Path(config['receipt_db']).as_uri() + '?mode=ro', uri=True) as db:
            row = db.execute('SELECT r.payload FROM receipts r JOIN role_bindings b ON b.task_id=r.task_id '
                             'WHERE b.role_id=? ORDER BY r.task_id LIMIT 1', (ROLE,)).fetchone()
            before_count = db.execute('SELECT count(*) FROM receipts').fetchone()[0]
        assert row is not None, 'Use an already recorded execution; never create a probe coding task'
        payload = json.loads(row[0])
        execution_headers = {'X-Agent-Line-Service-Token': Path(config['service_token_file']).read_text().strip()}
        response = client.post('/api/agent-line/executions', headers=execution_headers,
                               json={key: payload[key] for key in ('taskId', 'roleSessionId', 'instruction')})
        assert response.status_code == 409 and '记忆索引删除' in response.json()['detail']
        response = client.get('/api/agent-line/executions/' + payload['taskId'], headers=execution_headers)
        assert response.status_code == 200 and response.json()['taskId'] == payload['taskId']
        with sqlite3.connect(Path(config['receipt_db']).as_uri() + '?mode=ro', uri=True) as db:
            assert db.execute('SELECT count(*) FROM receipts').fetchone()[0] == before_count
    after = source.get(TASK)
    for key in ('payload', 'executionStatus', 'executionIdentity', 'verificationStatus',
                'output', 'evidence', 'resourcesReleased'):
        assert after[key] == original[key], key
    assert after['memoryStatus'] == 'deleting'
    assert commits.get(ROLE, TASK)['content'] == ''
    report = {'verified': True, 'taskId': TASK, 'roleAgentId': ROLE, 'contentSha256': digest,
              'status': 'deleting', 'officialFileAbsent': True, 'indexSourceAbsent': False,
              'roleBlockedAfterPluginReload': True, 'adapterRestartPreservedIntent': True,
              'oldReviewDidNotRestoreContent': True, 'taskExecutionUnchanged': True,
              'boundExecutionSubmissionRejected': True, 'existingExecutionReadable': True,
              'nativeDeletePostsThisRun': sum(method == 'POST' for method, _ in calls),
              'nativeObservationGetsThisRun': sum(method == 'GET' for method, _ in calls),
              'memoryPutsThisRun': 0, 'mainDeletionEnabled': False,
              'scope': 'generated_probe_memory_only; index erasure and global recall remain unverified'}
    write_private(DIRECTORY / ('deletion-replay-report.json' if intent is not None else 'deletion-report.json'), report)
    print(json.dumps(report))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare', 'run'])
    globals()[parser.parse_args().action]()
