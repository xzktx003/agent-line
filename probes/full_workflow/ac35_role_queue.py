#!/usr/bin/env python3
"""AC-35: two live runs contend for one role and workspace.

taskField stays closed. Opening it would give each run a separate Codex
session and hide the queue. Sandbox, AppArmor, user namespace, ReMe, the
role model, and activation checks stay unchanged. The model remains the
already configured gpt-6-luna.
"""
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path('/data01/home/zukang.xu/workspace/ai_agent/agentteams/agent_line')
DIR = ROOT / 'var/ac35-role-queue-20261027'
STATE = DIR / 'state.json'
REPORT = DIR / 'report.json'
SERVICE = ROOT / 'var/adapter/service.json'
DB = ROOT / 'var/adapter/tasks.sqlite'
PROJECT = 'native-memory-roundtrip'
BINDING = 'native-loop-parent'
TEMPLATE = 'ac35-same-role-queue-20261027'
NODE = 'native-check'
INSTRUCTION = (
    'Delegate exactly once to Codex. Codex must itself execute one foreground '
    'shell command in its assigned cwd: sleep 25 seconds, then write exactly '
    'these bytes to the relative file main-bootstrap-45be61aebaa6d15b.py and '
    'shell-check them: def add(left, right):\\n    return left + right\\n'
    'The sleep must finish before the write. Do not background it, delegate a '
    'child agent, shorten it, change any other file, change memory or '
    'permissions, or use the network. This wait lets a second run of the same '
    'role be observed in the declared serial queue.'
)


def save(path, payload):
    path.parent.mkdir(mode=0o700, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump(payload, output, ensure_ascii=False, indent=2)
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def token():
    service = json.loads(SERVICE.read_text())
    return next(iter(service['tokens']))


def client():
    return httpx.Client(base_url='http://127.0.0.1:3193', timeout=30,
                        headers={'Authorization': 'Bearer ' + token()})


def body(run_id, execution_id, key):
    return {
        'schemaVersion': '1.0',
        'idempotencyKey': key,
        'projectId': PROJECT,
        'roleBindingId': BINDING,
        'memoryPolicyId': 'native-role',
        'outputSchemaId': 'task-result',
        'workflow': {
            'id': TEMPLATE, 'revision': '1', 'runId': run_id, 'nodeId': NODE,
            'nodeExecutionId': execution_id, 'attemptId': '1',
        },
        'input': {'instruction': INSTRUCTION},
        'limits': {'maxExecutionSeconds': 900, 'maxBusinessAttempts': 1},
    }


def task_view(http, task_id):
    response = http.get('/extension/v1/node-tasks/' + task_id)
    response.raise_for_status()
    data = response.json()
    return {
        'executionStatus': data.get('executionStatus'),
        'upstreamSubmissionStatus': data.get('upstreamSubmissionStatus'),
        'verificationStatus': data.get('verificationStatus'),
        'memoryStatus': data.get('memoryStatus'),
        'resourcesReleased': data.get('resourcesReleased'),
        'executionQueue': data.get('executionQueue'),
        'submissionBlock': data.get('submissionBlock'),
        'version': data.get('version'),
    }


def events(task_id):
    database = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
    rows = database.execute(
        'SELECT seq, kind, created_at FROM events WHERE task_id=? ORDER BY seq',
        (task_id,),
    ).fetchall()
    database.close()
    return [{'seq': seq, 'kind': kind, 'createdAt': created} for seq, kind, created in rows]


def containers():
    import subprocess
    result = subprocess.run(
        ['docker', 'ps', '--filter', 'label=agent-line.owner', '--format', '{{.ID}}'],
        check=True, capture_output=True, text=True, timeout=15,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def main():
    DIR.mkdir(mode=0o700, exist_ok=True)
    if REPORT.exists():
        previous = json.loads(REPORT.read_text())
        if previous.get('status') == 'passed':
            print(json.dumps({'status': 'already_passed', 'newSubmissions': 0}))
            return 0
    if STATE.exists():
        raise SystemExit('existing_state_not_replayed')
    runs = []
    for name in ('first', 'second'):
        runs.append({
            'name': name,
            'runId': uuid.uuid4().hex,
            'nodeExecutionId': name + '-' + uuid.uuid4().hex,
            'key': TEMPLATE + '-' + name + '-' + uuid.uuid4().hex,
        })
    state = {
        'status': 'registering',
        'openedRestriction': None,
        'keptClosed': ['taskField', 'codex_sandbox', 'apparmor', 'user_namespace',
                       'reme', 'role_model', 'activation_identity'],
        'model': 'gpt-6-luna',
        'runs': runs,
        'samples': [],
    }
    save(STATE, state)
    with client() as http:
        health = http.get('/extension/v1/health')
        health.raise_for_status()
        ready = health.json()
        if ready.get('readyForExecution') is not True or ready.get('controlledExecution') != 'confirmed':
            raise SystemExit('execution_not_ready')
        for run in runs:
            response = http.post('/extension/v1/node-tasks', json=body(
                run['runId'], run['nodeExecutionId'], run['key']))
            if response.status_code != 202:
                raise SystemExit('register_rejected:' + str(response.status_code) + ':' + response.text[:180])
            payload = response.json()
            run['taskId'] = payload['taskId']
            run['reused'] = payload.get('reusedExistingTask')
            if run['reused']:
                raise SystemExit('unexpected_existing_task')
        state['status'] = 'observing'
        save(STATE, state)
        deadline = time.monotonic() + 900
        saw_wait = False
        while time.monotonic() < deadline:
            views = {run['name']: task_view(http, run['taskId']) for run in runs}
            owned = containers()
            sample = {'at': time.time(), 'views': views, 'ownedContainerCount': len(owned)}
            state['samples'].append(sample)
            if len(state['samples']) > 400:
                state['samples'] = state['samples'][-400:]
            save(STATE, state)
            first = views['first']
            second = views['second']
            if (first['executionStatus'] != 'registered' and second['executionStatus'] == 'registered'
                    and isinstance(second.get('executionQueue'), dict)
                    and second['executionQueue'].get('policy') == 'serial_fifo'
                    and second['executionQueue'].get('state') == 'waiting'
                    and second['executionQueue'].get('reason') in ('role_in_use', 'earlier_registered_task', 'workspace_in_use')):
                saw_wait = True
            if (first['executionStatus'] not in ('succeeded', 'failed', 'canceled')
                    and second['executionStatus'] not in ('registered', 'succeeded', 'failed', 'canceled')):
                raise SystemExit('both_left_queue_before_first_finished')
            if len(owned) > 1:
                raise SystemExit('concurrent_owned_containers')
            if (first['resourcesReleased'] is True and second['resourcesReleased'] is True
                    and first['executionStatus'] in ('succeeded', 'failed', 'canceled')
                    and second['executionStatus'] in ('succeeded', 'failed', 'canceled')):
                break
            time.sleep(2)
        else:
            raise SystemExit('observation_deadline')
        final = {run['name']: task_view(http, run['taskId']) for run in runs}
    event_log = {run['name']: events(run['taskId']) for run in runs}
    def when(name, kind):
        matches = [item['createdAt'] for item in event_log[name] if item['kind'] == kind]
        return matches[0] if matches else None
    first_intent = when('first', 'submission_intent')
    second_intent = when('second', 'submission_intent')
    first_released = when('first', 'resources_released')
    report = {
        'status': 'passed' if saw_wait and first_intent and second_intent and first_intent < second_intent else 'failed',
        'sawSerialWait': saw_wait,
        'firstSubmissionIntent': first_intent,
        'secondSubmissionIntent': second_intent,
        'firstResourcesReleased': first_released,
        'secondStartedAfterFirstIntent': bool(first_intent and second_intent and first_intent < second_intent),
        'final': final,
        'events': event_log,
        'taskIds': {run['name']: run['taskId'] for run in runs},
        'runIds': {run['name']: run['runId'] for run in runs},
        'openedRestriction': None,
        'model': 'gpt-6-luna',
        'maxOwnedContainersObserved': max(sample['ownedContainerCount'] for sample in state['samples']),
    }
    if report['status'] != 'passed':
        save(REPORT, report)
        raise SystemExit('queue_not_proven')
    if final['second']['executionStatus'] == 'registered':
        save(REPORT, report)
        raise SystemExit('second_never_left_queue')
    save(REPORT, report)
    print(json.dumps({
        'status': report['status'],
        'tasks': report['taskIds'],
        'sawSerialWait': saw_wait,
        'final': {name: {'execution': item['executionStatus'], 'verification': item['verificationStatus'],
                         'released': item['resourcesReleased']} for name, item in final.items()},
    }, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
