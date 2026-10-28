#!/usr/bin/env python3
"""Loopback HTTP probe for explicit memory retry using synthetic task state.

Only the upstream QwenPaw memory HTTP transport is mocked. Agent Line API
requests use a real loopback Uvicorn server and the real SQLite stores.
"""
import json
import os
from pathlib import Path
import secrets
import socket
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import httpx
import uvicorn

from agent_line.adapters.qwenpaw_memory import QwenPawMemoryClient
from agent_line.approval_api import ApprovalAccess
from agent_line.approvals import TrustedPrincipal
from agent_line.memory import MemoryCommitStore, MemoryCoordinator
from agent_line.memory_api import MemoryAPI
from agent_line.memory_review import MemoryReviewLedger
from agent_line.service import ServiceConfig, create_app
from agent_line.store import TaskStore


OUT = ROOT / 'var/memory-write-retry-20261022/http'
PROJECT = 'retry-http-project'
ROLE_BINDING = 'retry-http-binding'
ROLE = 'retry-http-role'
CONTENT = '合成记忆重试探针：保留原审核内容，不触发新的执行。'


def start_server(app):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
                                           log_level='warning', access_log=False))
    thread = threading.Thread(target=server.run, name='memory-retry-http', daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=10)
        raise RuntimeError('loopback service failed to start')
    return server, thread, f'http://127.0.0.1:{port}'


def stop_server(server, thread):
    server.should_exit = True
    thread.join(timeout=10)
    if thread.is_alive():
        server.force_exit = True
        thread.join(timeout=2)
    if thread.is_alive():
        raise RuntimeError('loopback service did not stop')


def make_api(tasks, commits, reviews, human_token, transport):
    adapter_http = httpx.Client(transport=transport, timeout=5, trust_env=False)
    adapter = QwenPawMemoryClient('http://127.0.0.1:3194', 'synthetic-upstream-token',
                                  role_agent_id=ROLE, client=adapter_http,
                                  native_write_rejections=True)
    principal = TrustedPrincipal('synthetic-reviewer', frozenset({PROJECT}),
                                 frozenset({'approve', 'review_memory'}), time.time() + 3600)
    access = ApprovalAccess({human_token: principal})
    api = MemoryAPI(access, {'retry-http': MemoryCoordinator(commits, adapter)}, reviews, tasks,
                    bindings={'retry-http': {'projectId': PROJECT,
                                             'roleBindingId': ROLE_BINDING,
                                             'roleAgentId': ROLE}})
    config = ServiceConfig(tokens={'synthetic-service-token': {PROJECT}},
                           projects={PROJECT: {'workspace': str(OUT)}}, bindings={})
    return create_app(tasks, config, memories=api), api


def make_task(tasks, run_id, run_dir):
    workspace = run_dir / 'workspace'
    workspace.mkdir(mode=0o700)
    identity = {'upstreamTaskId': 'synthetic-upstream-' + run_id,
                'codexThreadId': 'synthetic-thread-' + run_id,
                'codexTurnId': 'synthetic-turn-' + run_id}
    task, _ = tasks.register('memory-retry-http-' + run_id, {
        'projectId': PROJECT, 'roleBindingId': ROLE_BINDING, 'roleAgentId': ROLE,
        'workspace': str(workspace),
        'workflow': {'id': 'synthetic-workflow', 'runId': 'synthetic-run-' + run_id,
                     'nodeExecutionId': 'synthetic-node', 'attemptId': 'synthetic-attempt'},
        'input': {'probe': True},
    })
    tasks.claim(task['id'], str(workspace))
    tasks.accept(task['id'], identity)
    evidence = [{'kind': 'execution_terminal', 'executionId': identity['codexTurnId'],
                 'confirmed': True, 'probe': 'synthetic'}]
    output = {'result': 'synthetic execution completed', 'probe': True}
    tasks.finish(task['id'], 'succeeded', output, evidence)
    tasks.confirm_resources_released(task['id'], identity['codexTurnId'], {
        'source': 'docker-inspect-before-remove', 'workspace': str(workspace),
        'owner': 'a' * 32, 'containerId': 'b' * 64, 'running': False,
        'pid': 0, 'status': 'exited', 'finishedAt': '2026-10-22T00:00:00Z',
        'identities': [identity],
    })
    return tasks.get(task['id'])


def main():
    os.umask(0o077)
    OUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    OUT.chmod(0o700)
    run_id = uuid.uuid4().hex[:12]
    run_dir = OUT / ('run-' + run_id)
    run_dir.mkdir(mode=0o700)
    human_token = secrets.token_urlsafe(32)
    task_path = run_dir / 'tasks.sqlite'
    commit_path = run_dir / 'commits.sqlite'
    review_path = run_dir / 'reviews.sqlite'
    tasks = TaskStore.initialize(task_path)
    commits = MemoryCommitStore.initialize(commit_path)
    reviews = MemoryReviewLedger.initialize(review_path)
    task = make_task(tasks, run_id, run_dir)
    content = CONTENT + ' 标识=' + run_id
    decision_id = 'synthetic-review-' + run_id

    native_state = {'content': None, 'puts': 0, 'gets': 0, 'statuses': []}

    def upstream(request):
        if request.method == 'GET':
            native_state['gets'] += 1
            if native_state['content'] is None:
                native_state['statuses'].append(('GET', 404))
                return httpx.Response(404)
            native_state['statuses'].append(('GET', 200))
            return httpx.Response(200, json={'content': native_state['content']})
        if request.method == 'PUT':
            native_state['puts'] += 1
            if native_state['puts'] == 1:
                native_state['statuses'].append(('PUT', 403))
                return httpx.Response(403, json={'detail': 'synthetic pre-write rejection'})
            if native_state['puts'] == 2:
                native_state['content'] = json.loads(request.content)['content']
                native_state['statuses'].append(('PUT', 200))
                return httpx.Response(200, json={'written': True})
            raise AssertionError('unexpected extra upstream PUT')
        raise AssertionError('unexpected upstream method')

    app, api = make_api(tasks, commits, reviews, human_token, httpx.MockTransport(upstream))
    apis = [api]
    server = thread = client = None
    report = {'mode': 'synthetic_loopback_http_upstream_mock', 'verified': False,
              'nativeQwenPawCalled': False, 'modelCalled': False, 'mainTaskMutated': False}
    try:
        server, thread, origin = start_server(app)
        client = httpx.Client(base_url=origin, timeout=10, trust_env=False)
        headers = {'Authorization': 'Bearer ' + human_token}
        path = '/extension/v1/memories/retry-http/' + task['id']
        before = tasks.get(task['id'])

        reviewed = client.post(path + '/review', headers=headers,
                               json={'decisionId': decision_id, 'content': content})
        assert reviewed.status_code == 202, (reviewed.status_code, reviewed.text)
        first = reviewed.json()
        assert first['status'] == 'failed' and first['reason'] == 'official_write_rejected'
        expected_version = first['commitVersion']
        failed_row = commits.get(ROLE, task['id'])
        assert failed_row['version'] == expected_version
        after_review = tasks.get(task['id'])
        assert after_review['memoryStatus'] == 'failed'
        for key in ('executionIdentity', 'executionStatus', 'verificationStatus', 'output', 'evidence'):
            assert after_review[key] == before[key], key
        assert native_state['puts'] == 1

        failed_before_reconcile = commits.get(ROLE, task['id'])
        task_before_reconcile = tasks.get(task['id'])
        put_count, get_count = native_state['puts'], native_state['gets']
        reconciled = client.post(path + '/reconcile', headers=headers, json={})
        assert reconciled.status_code == 200, (reconciled.status_code, reconciled.text)
        assert reconciled.json()['status'] == 'failed'
        assert commits.get(ROLE, task['id']) == failed_before_reconcile
        assert tasks.get(task['id']) == task_before_reconcile
        assert native_state['puts'] == put_count and native_state['gets'] == get_count + 1

        retried = client.post(path + '/retry', headers=headers,
                              json={'expectedVersion': expected_version})
        assert retried.status_code == 200, (retried.status_code, retried.text)
        assert retried.json()['status'] == 'committed'
        assert retried.json()['commitVersion'] == expected_version + 2
        assert native_state['puts'] == 2
        assert native_state['content'] == content
        after_retry = tasks.get(task['id'])
        assert after_retry['memoryStatus'] == 'committed'
        for key in ('executionIdentity', 'executionStatus', 'verificationStatus', 'output', 'evidence'):
            assert after_retry[key] == before[key], key

        repeated = client.post(path + '/retry', headers=headers,
                               json={'expectedVersion': expected_version})
        assert repeated.status_code == 409
        assert native_state['puts'] == 2

        client.close()
        client = None
        stop_server(server, thread)
        server = thread = None
        api.close()

        reopened_tasks = TaskStore(task_path)
        reopened_commits = MemoryCommitStore(commit_path)
        reopened_reviews = MemoryReviewLedger(review_path)
        restarted_app, restarted_api = make_api(reopened_tasks, reopened_commits,
                                                reopened_reviews, human_token,
                                                httpx.MockTransport(upstream))
        apis.append(restarted_api)
        server, thread, restarted_origin = start_server(restarted_app)
        client = httpx.Client(base_url=restarted_origin, timeout=10, trust_env=False)
        restarted_path = '/extension/v1/memories/retry-http/' + task['id']
        restart_reconcile = client.post(restarted_path + '/reconcile', headers=headers, json={})
        assert restart_reconcile.status_code == 200
        assert restart_reconcile.json()['status'] == 'committed'
        stale_retry = client.post(restarted_path + '/retry', headers=headers,
                                  json={'expectedVersion': expected_version})
        assert stale_retry.status_code == 409
        assert native_state['puts'] == 2
        assert reopened_commits.get(ROLE, task['id'])['status'] == 'committed'
        assert reopened_tasks.get(task['id'])['memoryStatus'] == 'committed'
        for key in ('executionIdentity', 'executionStatus', 'verificationStatus', 'output', 'evidence'):
            assert reopened_tasks.get(task['id'])[key] == before[key], key
        report.update({
            'verified': True,
            'taskId': task['id'],
            'originalFailedVersion': expected_version,
            'failedReconcileWasGetOnlyAndUnchanged': True,
            'explicitRetryCommitted': True,
            'replayedOldVersionRejected': True,
            'restartPreservedCommitWithoutPut': True,
            'upstreamMockCalls': {'PUT': native_state['puts'], 'GET': native_state['gets'],
                                  'statuses': native_state['statuses']},
            'taskExecutionOutputEvidenceUnchanged': True,
            'sqliteFiles': ['tasks.sqlite', 'commits.sqlite', 'reviews.sqlite'],
        })
    except Exception as exc:
        report.update({'failureType': type(exc).__name__})
    finally:
        if client is not None:
            client.close()
        if server is not None and thread is not None:
            stop_server(server, thread)
        for current_api in apis:
            try:
                current_api.close()
            except Exception:
                pass
    report_path = run_dir / 'report.json'
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    report_path.chmod(0o600)
    print(json.dumps({'report': str(report_path.relative_to(ROOT)), **report}, ensure_ascii=False))
    if not report['verified']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
