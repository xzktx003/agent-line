#!/usr/bin/env python3
"""Bounded loopback proof for the durable synthetic execution queue.

The Agent Line API and SQLite database are real. Only the execution gateway is
synthetic; no native executor, model, or upstream service is contacted.
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

from agent_line.dispatcher import Dispatcher
from agent_line.dispatcher import DispatchLoop
from agent_line.service import ServiceConfig
from agent_line.service import create_app
from agent_line.store import TaskStore


OUT = ROOT / 'var/execution-queue-20261022/http'
PROJECT = 'execution-queue-http-project'
ROLE = 'execution-queue-http-role'
BINDING = 'execution-queue-http-binding'


def start_server(app):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
                                           log_level='warning', access_log=False))
    thread = threading.Thread(target=server.run, name='execution-queue-http', daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.02)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=10)
        raise RuntimeError('loopback service failed to start')
    return server, thread, 'http://127.0.0.1:' + str(port)


def stop_server(server, thread):
    server.should_exit = True
    thread.join(timeout=10)
    if thread.is_alive():
        server.force_exit = True
        thread.join(timeout=2)
    if thread.is_alive():
        raise RuntimeError('loopback service did not stop')


class SyntheticGateway:
    """Deterministic fake with one ambiguous submit and inspectable counters."""
    role = ROLE

    def __init__(self, workspace, *, lose_first_response=True):
        self.workspace = str(workspace)
        self.lose_first_response = lose_first_response
        self.submissions = []
        self.observations = []
        self.states = {}

    def submit(self, *, task_id, **_kwargs):
        self.submissions.append(task_id)
        if self.lose_first_response and len(self.submissions) == 1:
            self.states[task_id] = 'accepted'
            raise OSError('synthetic response lost after acceptance')
        self.states[task_id] = 'accepted'
        return {'status': 'accepted', 'identity': identity(task_id)}

    def observe(self, task_id):
        self.observations.append(task_id)
        status = self.states.get(task_id, 'unknown')
        if status == 'complete':
            return {'status': 'succeeded', 'identity': identity(task_id),
                    'terminalConfirmed': True, 'resourcesReleased': True,
                    'resourceExitReceipt': exit_receipt(task_id, self.workspace),
                    'output': {'probe': 'synthetic completion'}}
        if status == 'accepted':
            return {'status': 'running', 'identity': identity(task_id)}
        return {'status': 'unknown'}


def identity(task_id):
    suffix = task_id.removeprefix('task-')
    return {'upstreamTaskId': 'synthetic-upstream-' + suffix,
            'codexThreadId': 'synthetic-thread-' + suffix,
            'codexTurnId': 'synthetic-turn-' + suffix}


def exit_receipt(task_id, workspace):
    native = identity(task_id)
    return {'source': 'docker-inspect-before-remove', 'workspace': str(workspace),
            'owner': 'a' * 32, 'containerId': 'b' * 64, 'running': False,
            'pid': 0, 'status': 'exited', 'finishedAt': '2026-10-22T00:00:00Z',
            'identities': [native]}


def payload(key, *, project=PROJECT, binding=BINDING):
    return {'schemaVersion': '1.0', 'idempotencyKey': key,
            'workflow': {'id': 'synthetic-workflow', 'revision': '1',
                         'runId': 'synthetic-run-' + key, 'nodeId': 'synthetic-node',
                         'nodeExecutionId': 'synthetic-node-' + key,
                         'attemptId': 'synthetic-attempt-' + key},
            'projectId': project, 'roleBindingId': binding,
            'input': {'probe': 'synthetic', 'key': key},
            'memoryPolicyId': 'synthetic-memory-policy',
            'outputSchemaId': 'synthetic-output-schema'}


def register(client, headers, key, *, independent=False):
    project = 'execution-queue-independent-project' if independent else PROJECT
    binding = 'execution-queue-independent-binding' if independent else BINDING
    response = client.post('/extension/v1/node-tasks', headers=headers,
                           json=payload(key, project=project, binding=binding))
    assert response.status_code == 202, (response.status_code, response.text)
    return response.json()['taskId']


def main():
    global WORKSPACE, INDEPENDENT_WORKSPACE
    os.umask(0o077)
    OUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    OUT.chmod(0o700)
    run_id = uuid.uuid4().hex[:12]
    run_dir = OUT / ('run-' + run_id)
    run_dir.mkdir(mode=0o700)
    WORKSPACE = run_dir / 'shared-workspace'
    INDEPENDENT_WORKSPACE = run_dir / 'independent-workspace'
    WORKSPACE.mkdir(mode=0o700)
    INDEPENDENT_WORKSPACE.mkdir(mode=0o700)
    token = secrets.token_urlsafe(32)
    task_path = run_dir / 'tasks.sqlite'
    tasks = TaskStore.initialize(task_path)
    gateway = SyntheticGateway(WORKSPACE)
    # No dispatcher is attached to the service, so Uvicorn starts no worker.
    # The existing Dispatcher/DispatchLoop are driven explicitly below.
    independent_project = 'execution-queue-independent-project'
    independent_binding = 'execution-queue-independent-binding'
    independent_role = 'execution-queue-independent-role'
    config = ServiceConfig(tokens={token: {PROJECT, independent_project}},
                           projects={PROJECT: {'workspace': str(WORKSPACE)},
                                     independent_project: {'workspace': str(INDEPENDENT_WORKSPACE)}},
                           bindings={BINDING: {'projectId': PROJECT, 'roleAgentId': ROLE},
                                     independent_binding: {'projectId': independent_project,
                                                           'roleAgentId': independent_role}},
                           controlled_execution='confirmed')
    app = create_app(tasks, config)
    server = thread = client = None
    report = {'mode': 'synthetic_loopback_http_sqlite_queue', 'verified': False,
              'syntheticGatewayOnly': True, 'nativeExecutorCalled': False,
              'modelCalled': False, 'mainTaskMutated': False}
    try:
        server, thread, origin = start_server(app)
        client = httpx.Client(base_url=origin, timeout=10, trust_env=False)
        headers = {'Authorization': 'Bearer ' + token}
        # Register in durable event order, then deliberately attempt reverse FIFO.
        first = register(client, headers, 'fifo-first')
        second = register(client, headers, 'fifo-second')
        third = register(client, headers, 'fifo-third')
        dispatcher = Dispatcher(tasks, {(PROJECT, BINDING): gateway})
        assert dispatcher.submit(third)['executionStatus'] == 'registered'
        assert dispatcher.submit(second)['executionStatus'] == 'registered'
        assert gateway.submissions == []

        # The head reaches the fake gateway once, whose response is lost. It is
        # durably uncertain and owns the conflicting lease across restart.
        head = dispatcher.submit(first)
        assert head['executionStatus'] == 'uncertain'
        assert gateway.submissions == [first]
        client.close()
        client = None
        stop_server(server, thread)
        server = thread = None

        reopened = TaskStore(task_path)
        dispatcher = Dispatcher(reopened, {(PROJECT, BINDING): gateway})
        restarted_loop = DispatchLoop(dispatcher, allow_submissions=False)
        restarted_loop.tick()
        assert reopened.get(first)['executionStatus'] == 'accepted'
        assert gateway.submissions == [first]
        assert dispatcher.submit(second)['executionStatus'] == 'registered'
        assert dispatcher.submit(third)['executionStatus'] == 'registered'
        assert gateway.submissions == [first]

        # Reopen authenticated HTTP and prove GET projection is read-only.
        app = create_app(reopened, config)
        server, thread, origin = start_server(app)
        client = httpx.Client(base_url=origin, timeout=10, trust_env=False)
        headers = {'Authorization': 'Bearer ' + token}
        before_events = {task_id: reopened.events(task_id) for task_id in (second, third)}
        before_versions = {task_id: reopened.get(task_id)['version'] for task_id in (second, third)}
        projections = []
        for _ in range(2):
            response = client.get('/extension/v1/node-tasks/' + second, headers=headers)
            assert response.status_code == 200
            projections.append(response.json()['executionQueue'])
        assert projections[0] == projections[1]
        assert projections[0] == {'policy': 'serial_fifo', 'state': 'waiting',
                                  'reason': 'workspace_in_use'}
        assert before_events == {task_id: reopened.events(task_id) for task_id in (second, third)}
        assert before_versions == {task_id: reopened.get(task_id)['version'] for task_id in (second, third)}
        assert gateway.submissions == [first]

        # Native terminal evidence and exit confirmation release the head; the
        # FIFO successor can then be the only conflicting submit.
        gateway.states[first] = 'complete'
        assert dispatcher.reconcile(first)['resourcesReleased'] is True
        assert dispatcher.submit(second)['executionStatus'] == 'accepted'
        assert gateway.submissions == [first, second]
        assert dispatcher.submit(third)['executionStatus'] == 'registered'
        canceled = client.post('/extension/v1/node-tasks/' + third + '/cancel', headers=headers)
        assert canceled.status_code == 200 and canceled.json()['executionStatus'] == 'canceled'
        canceled_row = reopened.get(third)
        assert canceled_row['payload']['input'] == {'probe': 'synthetic', 'key': 'fifo-third'}
        assert any(event['kind'] == 'registered' for event in reopened.events(third))
        assert gateway.submissions == [first, second]

        # Prove a disjoint role/workspace may make progress while the second
        # same-role task still holds its workspace lease.
        independent = register(client, headers, 'independent', independent=True)
        independent_gateway = SyntheticGateway(INDEPENDENT_WORKSPACE, lose_first_response=False)
        independent_gateway.role = independent_role
        independent_dispatcher = Dispatcher(reopened, {
            (PROJECT, BINDING): gateway,
            (independent_project, independent_binding): independent_gateway,
        })
        independent_payload = reopened.get(independent)['payload']
        assert independent_payload['workspace'] == str(INDEPENDENT_WORKSPACE.resolve())
        assert independent_payload['projectId'] == independent_project
        assert independent_payload['roleAgentId'] == independent_role
        assert independent_dispatcher.submit(independent)['executionStatus'] == 'accepted'
        assert independent_gateway.submissions == [independent]

        report.update({'verified': True, 'taskIds': [first, second, third, independent],
                       'reverseOrderBlockedWithoutGatewayCalls': True,
                       'uncertainLeaseSurvivedRestartWithoutResubmit': True,
                       'queueProjectionReadOnly': True,
                       'headTerminalEvidenceReleasedBeforeFifoSuccessor': True,
                       'queuedCancellationPreservedRegisteredPayload': True,
                       'independentRoleAndWorkspaceProgressed': True,
                       'syntheticGatewaySubmissions': gateway.submissions,
                       'syntheticIndependentGatewaySubmissions': independent_gateway.submissions,
                       'submissionCountByTask': {task_id: gateway.submissions.count(task_id)
                                                 for task_id in (first, second, third)},
                       'syntheticIdentities': {task_id: identity(task_id)
                                               for task_id in (first, second)},
                       'sqliteFiles': ['tasks.sqlite']})
    except Exception as exc:
        report['failureType'] = type(exc).__name__
    finally:
        if client is not None:
            client.close()
        if server is not None and thread is not None:
            stop_server(server, thread)
    report_path = run_dir / 'report.json'
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    report_path.chmod(0o600)
    print(json.dumps({'report': str(report_path.relative_to(ROOT)), **report}, ensure_ascii=False))
    if not report['verified']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
