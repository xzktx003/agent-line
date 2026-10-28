#!/usr/bin/env python3
"""Review a real completed probe task through runtime and official memory HTTP."""
import json
from pathlib import Path
import secrets
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import httpx
from fastapi.testclient import TestClient
from agent_line.memory import MemoryCommitStore
from agent_line.memory_review import MemoryReviewLedger
from agent_line.runtime import load_app
from agent_line.store import TaskStore
from probes.sim.workflow_probe import write_private


def main():
    directory = ROOT / 'var/memory-review-http'
    directory.mkdir(mode=0o700, exist_ok=True)
    source = TaskStore(ROOT / 'var/role-dispatch/tasks.sqlite')
    task_id = 'task-00d41e38e770420b8881543dcfa9c903'
    task = source.get(task_id)
    assert task['executionStatus'] == 'succeeded' and task['resourcesReleased'] is True
    project = task['payload']['projectId']
    role = task['payload']['roleAgentId']
    binding = task['payload']['roleBindingId']
    state_path = directory / 'state.json'
    if not state_path.exists():
        write_private(state_path, {'decisionId': 'memory-review-http-' + secrets.token_hex(12),
            'content': '# 经人工审核的隔离验收记录\n\n'
                       '来源任务：' + task_id + '。该记录描述一次已结束且资源已退出的历史验收，'
                       '不代表未来任务的结果。独立记忆审核标记：HTTP_REVIEW_' + secrets.token_hex(12)})
    state = json.loads(state_path.read_text())
    service_path = directory / 'service.json'
    if not service_path.exists():
        commits = directory / 'commits.sqlite'
        reviews = directory / 'reviews.sqlite'
        if not commits.exists(): MemoryCommitStore.initialize(commits)
        if not reviews.exists(): MemoryReviewLedger.initialize(reviews)
        process = json.loads((ROOT / 'var/qwenpaw/agent-line-process.json').read_text())
        write_private(directory / 'memory.json', {'schemaVersion': 1,
            'reviewLedgerPath': str(reviews), 'credentials': [{
                'token': secrets.token_urlsafe(32), 'subject': 'memory-review-probe-owner',
                'projectScopes': [project], 'capabilities': ['review_memory'],
                'expiresAt': time.time() + 86400}],
            'bindings': {'probe-memory': {'projectId': project, 'roleBindingId': binding,
                'roleAgentId': role, 'baseURL': 'http://127.0.0.1:' + str(process['port']),
                'hostCredentialsFile': str(ROOT / 'var/qwenpaw-secrets/local-login.json'),
                'commitLedgerPath': str(commits)}}})
        write_private(service_path, {'schemaVersion': 1, 'database': str(source.path),
            'tokens': {secrets.token_urlsafe(32): [project]},
            'projects': {project: {'workspace': task['payload']['workspace']}},
            'bindings': {binding: {'projectId': project, 'roleAgentId': role}},
            'memoryFile': 'memory.json'})
    config = json.loads(service_path.read_text())
    memory = json.loads((directory / 'memory.json').read_text())
    human = {'Authorization': 'Bearer ' + memory['credentials'][0]['token']}
    ordinary = {'Authorization': 'Bearer ' + next(iter(config['tokens']))}
    path = '/extension/v1/memories/probe-memory/' + task_id
    body = {key: state[key] for key in ('decisionId', 'content')}
    with TestClient(load_app(service_path)) as http:
        assert http.post(path + '/review', headers=ordinary, json=body).status_code == 401
        response = http.post(path + '/review', headers=human, json=body)
        assert response.status_code == 202
        first = response.json()
        assert first['status'] == 'committed'
        assert first['actor'] == 'memory-review-probe-owner'
        assert first['content'] == state['content'].strip()
        assert http.post(path + '/review', headers=human,
                         json={**body, 'content': body['content'] + '\nchanged'}).status_code == 409
    after_first = source.get(task_id)
    with TestClient(load_app(service_path)) as http:
        repeated = http.post(path + '/review', headers=human, json=body)
        assert repeated.status_code == 202 and repeated.json() == first
        listing = http.get('/extension/v1/memories', headers=human)
        assert listing.status_code == 200 and len(listing.json()['memories']) == 1
        assert 'content' not in listing.json()['memories'][0]
    after_restart = source.get(task_id)
    assert after_restart == after_first
    assert after_restart['executionStatus'] == task['executionStatus']
    assert after_restart['executionIdentity'] == task['executionIdentity']
    assert after_restart['output'] == task['output'] and after_restart['evidence'] == task['evidence']
    assert after_restart['memoryStatus'] == 'committed'
    process = json.loads((ROOT / 'var/qwenpaw/agent-line-process.json').read_text())
    with httpx.Client(base_url='http://127.0.0.1:' + str(process['port']), timeout=30) as upstream:
        login = upstream.post('/api/auth/login', json=json.loads(
            (ROOT / 'var/qwenpaw-secrets/local-login.json').read_text()))
        assert login.status_code == 200
        token = login.json().get('token') or login.json().get('access_token')
        readback = upstream.get('/api/workspace/memory/agent-line-task-' + task_id + '.md',
                                params={'section': 'digest'}, headers={
                                    'Authorization': 'Bearer ' + token, 'X-Agent-Id': role})
        assert readback.status_code == 200 and readback.json()['content'].strip() == body['content'].strip()
    report = {'verified': True, 'taskId': task_id, 'roleAgentId': role,
              'status': first['status'], 'actor': first['actor'],
              'contentSha256': first['contentSha256'], 'officialReadbackMatches': True,
              'restartReceiptPreserved': True, 'taskExecutionUnchanged': True,
              'serviceCredentialRejected': True, 'changedContentRejected': True,
              'scope': 'human_review_commit_only', 'modelRecallValidated': False,
              'crossProjectIsolationValidated': False}
    write_private(directory / 'report.json', report)
    print(json.dumps(report))


if __name__ == '__main__':
    main()
