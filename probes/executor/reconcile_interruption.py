"""Reconcile an existing restart-probe task; never submits or fabricates evidence."""
import argparse
import json
import os
from pathlib import Path
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agent_line.dispatcher import Dispatcher
from agent_line.role_dispatch import RoleDispatchGateway, RoleDispatchLedger
from agent_line.store import TaskStore


def run(attempt):
    if attempt < 1:
        raise ValueError('Existing positive attempt number required')
    directory = ROOT / 'var/executor/host-restart' / ('attempt-' + str(attempt))
    original = json.loads((directory / 'receipt.json').read_text())
    task_id = original['taskId']
    store = TaskStore(ROOT / 'var/executor/host-restart/tasks.sqlite')
    ledger = RoleDispatchLedger(ROOT / 'var/executor/host-restart/roles.sqlite')
    before = store.get(task_id)
    identity = original['beforeIdentity']
    if before['executionIdentity'] != identity or not original['restartStarted']:
        raise RuntimeError('Original in-flight restart identity not established')
    host = json.loads((ROOT / 'var/qwenpaw/agent-line-process.json').read_text())
    origin = 'http://127.0.0.1:' + str(host['port'])
    calls = []
    def observe_request(request):
        calls.append((request.method, request.url.path))
    with httpx.Client(timeout=30, follow_redirects=False, trust_env=False,
                      event_hooks={'request': [observe_request]}) as client:
        auth = client.post(origin + '/api/auth/login', json=json.loads(
            (ROOT / 'var/qwenpaw-secrets/local-login.json').read_text()))
        auth.raise_for_status(); credentials = auth.json()
        token = credentials.get('token') or credentials['access_token']
        gateway = RoleDispatchGateway(origin, token,
            (ROOT / 'var/qwenpaw-secrets/execution-service-token').read_text().strip(),
            role_agent_id=before['payload']['roleAgentId'],
            deployment_id=ledger.get(task_id)['payload']['deploymentId'], ledger=ledger,
            memory_query='Existing-task reconciliation only', client=client)
        after = Dispatcher(store, {before['payload']['projectId']: gateway}).reconcile(task_id)
        native = gateway.observe(task_id)
    reopened = TaskStore(store.path).get(task_id)
    with store.connect() as db:
        leases = [dict(row) for row in db.execute('SELECT * FROM workspace_leases WHERE task_id=?', (task_id,))]
    submissions = sum(method == 'POST' and path in (
        '/api/console/chat/task', '/api/agent-line/executions') for method, path in calls)
    interruption = [e for e in after['evidence'] if e.get('kind') == 'execution_interrupted']
    verified = (after['executionStatus'] == 'failed' and after['resourcesReleased'] is True
        and after['executionIdentity'] == identity and reopened == after and not leases
        and submissions == 0 and len(interruption) == 1
        and interruption[0]['nativeStatus'] == 'unknown'
        and interruption[0]['cancellationRequested'] is True
        and native.get('terminalConfirmed') is False
        and native.get('executorInterrupted') is True
        and native.get('resourcesReleased') is True
        and after['verificationStatus'] == before['verificationStatus'])
    path = directory / 'interruption-reconciliation.json'
    previous = json.loads(path.read_text()) if path.exists() else {'invocations': []}
    result = {'checkedAt': time.time(), 'taskId': task_id, 'beforeTask': before,
              'afterTask': after, 'native': native, 'remainingLeases': leases,
              'newSubmissions': submissions, 'verified': verified}
    previous['invocations'].append(result)
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(previous, stream, ensure_ascii=False, indent=2)
    print(json.dumps({'taskId': task_id, 'status': after['executionStatus'],
                      'resourcesReleased': after['resourcesReleased'], 'verified': verified,
                      'newSubmissions': submissions}))
    return 0 if verified else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--attempt', required=True, type=int)
    raise SystemExit(run(parser.parse_args().attempt))
