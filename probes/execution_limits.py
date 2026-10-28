"""Real isolated HTTP lifecycle with an explicitly synthetic execution gateway.

No native/model call, main-state mutation, or invented cancellation confirmation.
"""
import copy
import json
import os
from pathlib import Path
import socket
import threading
import time

import httpx
import uvicorn

from agent_line.dispatcher import Dispatcher
from agent_line.service import ServiceConfig, create_app
from agent_line.store import TaskStore

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'var/execution-limits-20261022/http'


class SyntheticGateway:
    def __init__(self):
        self.submissions = []
        self.cancellations = []
        self.retire = False

    def submit(self, *, task_id, **_kwargs):
        self.submissions.append(task_id)
        return {'status': 'accepted', 'identity': {'upstreamTaskId': 'synthetic-' + task_id}}

    def observe(self, task_id):
        if self.retire:
            return {'status': 'canceled', 'identity': {'upstreamTaskId': 'synthetic-' + task_id},
                    'terminalConfirmed': True, 'resourcesReleased': True}
        return {'status': 'unknown'}

    def cancel(self, task_id):
        self.cancellations.append(task_id)
        return {'status': 'unknown'}


def wait_for(check, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(.05)
    raise AssertionError('isolated_http_check_timeout')


def start(store, gateway, workspace):
    cfg = ServiceConfig(tokens={'isolated-fixture-token': {'fixture'}},
        projects={'fixture': {'workspace': str(workspace)}},
        bindings={'dev': {'projectId': 'fixture', 'roleAgentId': 'fixture-role'}},
        controlled_execution='confirmed')
    app = create_app(store, cfg, dispatcher=Dispatcher(store, {'fixture': gateway}))
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(128)
    server = uvicorn.Server(uvicorn.Config(app, log_level='critical', access_log=False))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
    thread.start()
    wait_for(lambda: server.started and thread.is_alive())
    return server, thread, listener, 'http://127.0.0.1:' + str(listener.getsockname()[1])


def stop(server, thread, listener):
    server.should_exit = True
    thread.join(15)
    assert not thread.is_alive(), 'owned_test_server_not_drained'
    listener.close()


def main():
    os.umask(0o077)
    OUT.mkdir(mode=0o700, parents=True, exist_ok=True)
    assert not (OUT / 'tasks.sqlite').exists(), 'existing_probe_is_observation_only'
    workspace = OUT / 'workspace'
    workspace.mkdir(mode=0o700)
    store = TaskStore.initialize(OUT / 'tasks.sqlite')
    gateway = SyntheticGateway()
    body = {'schemaVersion': '1.0', 'idempotencyKey': 'isolated-one', 'projectId': 'fixture',
        'roleBindingId': 'dev', 'input': {}, 'memoryPolicyId': 'memory', 'outputSchemaId': 'result',
        'workflow': {'id': 'workflow', 'revision': 'revision', 'runId': 'run',
                     'nodeId': 'node', 'nodeExecutionId': 'activation-one', 'attemptId': '1'},
        'limits': {'maxExecutionSeconds': 1, 'maxBusinessAttempts': 1}}
    headers = {'Authorization': 'Bearer isolated-fixture-token'}
    server, thread, listener, origin = start(store, gateway, workspace)
    try:
        with httpx.Client(base_url=origin, timeout=10, trust_env=False, headers=headers) as client:
            response = client.post('/extension/v1/node-tasks', json=body)
            assert response.status_code == 202
            task_id = response.json()['taskId']
            def cancelled_pending():
                result = client.get('/extension/v1/node-tasks/' + task_id).json()
                return result if result.get('executionStatus') == 'cancel_requested' else None
            pending = wait_for(cancelled_pending)
            assert not pending['resourcesReleased'] and gateway.cancellations
            assert pending['executionLimits']['attemptsUsed'] == 1
            assert pending['executionLimits']['usage'] == {'tokens': None, 'modelCalls': None, 'cost': None}
            assert len(gateway.submissions) == 1
    finally:
        stop(server, thread, listener)
    # Reconstruct the complete app/worker with the same persisted database.
    store = TaskStore(OUT / 'tasks.sqlite')
    server, thread, listener, origin = start(store, gateway, workspace)
    try:
        with httpx.Client(base_url=origin, timeout=10, trust_env=False, headers=headers) as client:
            current = client.get('/extension/v1/node-tasks/' + task_id).json()
            assert current['executionStatus'] == 'cancel_requested' and not current['resourcesReleased']
            assert len(gateway.submissions) == 1
            gateway.retire = True
            wait_for(lambda: client.get('/extension/v1/node-tasks/' + task_id).json()['resourcesReleased'])
            second = copy.deepcopy(body)
            second['idempotencyKey'] = 'isolated-two'
            second['workflow'].update(nodeExecutionId='activation-two', attemptId='2')
            response = client.post('/extension/v1/node-tasks', json=second)
            assert response.status_code == 202
            second_id = response.json()['taskId']
            result = client.get('/extension/v1/node-tasks/' + second_id).json()
            assert result['executionQueue']['reason'] == 'business_attempt_limit'
            assert result['executionLimits']['attemptsUsed'] == 1
            assert result['executionLimits']['admissionAllowed'] is False
            assert result['executionStatus'] == 'registered'
            assert len(gateway.submissions) == 1
            assert client.get('/extension/v1/node-tasks/' + task_id, headers={'Authorization': ''}).status_code == 401
    finally:
        stop(server, thread, listener)
    report = {'passed': True, 'scope': 'real_isolated_http_with_synthetic_gateway',
        'databaseReopened': True, 'appAndWorkerRestarted': True, 'deadlineRequestedCancel': True,
        'unknownCancelKeptLeaseAndIdentity': True, 'cancelConfirmedOnlyBySyntheticReceipt': True,
        'attemptQuotaPersisted': True, 'submissions': len(gateway.submissions),
        'secondAttemptBlocked': True, 'unknownUsageIsNull': True, 'ownedServersStopped': True,
        'modelCalls': 0, 'nativeCalls': 0, 'mainStateWritten': False}
    (OUT / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
