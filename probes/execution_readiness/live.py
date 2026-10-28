#!/usr/bin/env python3
"""One explicit native acceptance task, then read-only observation on every replay.

This prepares a private candidate service only. It never changes the main
service, native roles, ReMe settings, upstream sources or an existing attempt.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import httpx
from fastapi.testclient import TestClient

from agent_line.automatic_verification import AutomaticVerifier
from agent_line.dispatcher import Dispatcher
from agent_line.execution_runtime import load_execution_gateways
from agent_line.role_dispatch import RoleDispatchLedger
from agent_line.role_revision import validate_role_revision
from agent_line.runtime import load_app
from agent_line.store import TaskStore
from agent_line.test_executor import DockerTestExecutor
from agent_line.verification_policy import validate_policy
from probes.full_workflow.parallel_roles import check_exit, installed_plugin_ready, main_snapshot
from probes.qwenpaw_memory.native_roundtrip import quiet_host, save

DATA = ROOT / 'var/execution-readiness-native'
PROJECT = 'native-memory-roundtrip'
ROLE = 'agent-line-native-loop-parent'
BINDING = 'native-readiness-check'


def require(condition, code):
    if not condition:
        raise RuntimeError(code)


def failure_summary(error):
    code = str(error) if type(error) is RuntimeError else ''
    if (not 1 <= len(code) <= 96 or not code.replace('_', '').isalnum()
            or code != code.lower() or any(not (c.isascii() and (c.isalnum() or c == '_')) for c in code)):
        code = 'native_readiness_probe_incomplete'
    frames = traceback.extract_tb(error.__traceback__)[-4:]
    return {'type': type(error).__name__, 'code': code,
            'frames': [{'file': Path(frame.filename).name, 'function': frame.name,
                        'line': frame.lineno} for frame in frames]}


def policy(artifact, marker, environment, suffix):
    content = (marker + '\n').encode()
    # The independent trusted command reads only the declared file. Codex does
    # not choose this command, its expected bytes or its verification result.
    command = ('from pathlib import Path; import sys; '
               'actual=Path(sys.argv[1]).read_bytes(); '
               'assert actual == (sys.argv[2]+"\\n").encode(), "artifact bytes differ"; '
               'print("independent exact-byte test passed")')
    return validate_policy({'schemaVersion': 2, 'policyId': 'readiness-' + suffix,
        'kind': 'command_and_artifacts', 'requireTests': True,
        'testEnvironmentId': environment,
        'artifacts': [{'path': artifact, 'sha256': hashlib.sha256(content).hexdigest(),
                       'exactSize': len(content), 'maxBytes': 1024}],
        'tests': [{'id': 'independent-exact-bytes', 'argv': ['python3', '-c', command, artifact, marker],
                   'timeoutSeconds': 60, 'maxOutputBytes': 65536}]})


def submit_once(dispatcher, state, persist):
    require(not state.get('submissionIntent'), 'submission_intent_already_exists')
    state['submissionIntent'] = True
    persist()
    return dispatcher.submit(state['taskId'])


@contextmanager
def readonly_database(path):
    # SQLite mode=ro can still create -wal/-shm companions. An immutable,
    # checkpointed snapshot avoids that; a live nonempty WAL is uncertainty,
    # never permission to ignore transactions or start a replacement task.
    path = path.resolve(strict=True)
    wal = Path(str(path) + '-wal')
    require(not wal.exists() or wal.stat().st_size == 0, 'observation_database_has_live_wal')
    before = path.stat()
    signature = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    db = sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)
    try:
        db.execute('PRAGMA query_only=ON')
        yield db
        require(not wal.exists() or wal.stat().st_size == 0, 'observation_database_has_live_wal')
        require(signature(path.stat()) == signature(before), 'observation_database_changed')
    finally:
        db.close()


def observe_existing(directory, state, *, client_factory=httpx.Client):
    """No login, app, gateway, verifier, Docker, registration or mkdir on replay."""
    result = {'mode': 'observation_only', 'newSubmissions': 0, 'nativeWritesThisInvocation': 0,
              'status': state.get('status', 'incomplete'), 'tasks': []}
    tasks = directory / 'tasks.sqlite'
    if tasks.exists():
        with readonly_database(tasks) as db:
            db.row_factory = sqlite3.Row
            result['tasks'] = [dict(row) for row in db.execute(
                'SELECT id,execution_status,resources_released,verification_status FROM tasks ORDER BY id')]
            result['activeLeaseCount'] = db.execute('SELECT count(*) FROM workspace_leases').fetchone()[0]
    ledger = directory / 'roles.sqlite'
    if not state.get('taskId') or not ledger.exists():
        return result
    with readonly_database(ledger) as db:
        row = db.execute('SELECT parent_id FROM deliveries WHERE task_id=?', (state['taskId'],)).fetchone()
    if row and row[0]:
        require(isinstance(state.get('hostToken'), str) and bool(state['hostToken']), 'original_host_token_missing')
        with client_factory(timeout=30, trust_env=False, follow_redirects=False) as client:
            response = client.get(state['baseURL'] + '/api/console/chat/task/' + row[0],
                headers={'Authorization': 'Bearer ' + state['hostToken'], 'X-Agent-Id': ROLE})
            # An expired token must never trigger reauthentication or a replay.
            require(response.status_code == 200, 'original_parent_observation_unavailable')
            result['parentStatus'] = response.json().get('status')
    return result


def candidate_settings(root, directory, workspace, base, revision, verification_policy, image, memory_query=None):
    projects = {PROJECT: {'workspace': str(workspace)}}
    bindings = {BINDING: {'projectId': PROJECT, 'roleAgentId': ROLE, 'roleRevision': revision,
                           'verificationPolicy': verification_policy}}
    token = secrets.token_urlsafe(40)
    service = {'schemaVersion': 1, 'database': str(directory / 'tasks.sqlite'),
        'tokens': {token: [PROJECT]}, 'projects': projects, 'bindings': bindings,
        'executionFile': 'execution.json',
        'verification': {'evidenceDirectory': str(directory / 'evidence'), 'testExecutor': {'imageId': image}}}
    execution = {'schemaVersion': 1, 'gateways': [{'projectId': PROJECT, 'roleBindingId': BINDING,
        'roleAgentId': ROLE, 'workspace': str(workspace), 'installationId': 'agent-line-g0-installation',
        'ledgerPath': str(directory / 'roles.sqlite'), 'baseURL': base,
        'hostCredentialsFile': str(root / 'var/qwenpaw-secrets/local-login.json'),
        'serviceTokenFile': str(root / 'var/qwenpaw-secrets/execution-service-token'),
        'memoryQuery': memory_query or '复用本项目已有的原生角色经验；当前任务的权限、文件和验收标准以本次可信输入为准。',
        'parentTimeoutSeconds': 900, 'requestTimeoutSeconds': 60}]}
    return service, execution, token


def request_body(suffix, instruction):
    return {'schemaVersion': '1.0', 'idempotencyKey': 'native-readiness-' + suffix, 'projectId': PROJECT,
        'roleBindingId': BINDING,
        'workflow': {'id': 'native-readiness', 'revision': '1', 'runId': suffix,
                     'nodeId': 'native-check', 'nodeExecutionId': 'native-check-' + suffix, 'attemptId': '1'},
        'input': {'instruction': instruction}, 'memoryPolicyId': 'native-role', 'outputSchemaId': 'task-result'}


def check_candidate_http(path, token, body, *, store, task_id=None, guarded=False):
    """Exercise the actual production app factory; no second model task."""
    headers = {'Authorization': 'Bearer ' + token}
    with TestClient(load_app(path)) as client:
        response = client.get('/extension/v1/health', headers=headers)
        require(response.status_code == 200, 'candidate_health_unavailable')
        health = response.json()
        confirmed = task_id is not None
        require(health.get('controlledExecution') == ('confirmed' if confirmed else 'unknown')
                and health.get('readyForExecution') is (confirmed and not guarded), 'candidate_health_mismatch')
        before = len(store.list_tasks({PROJECT})['tasks'])
        if not confirmed or guarded:
            denied = client.post('/extension/v1/node-tasks', headers=headers, json=body)
            require(denied.status_code == 503, 'candidate_new_task_not_denied')
            require(len(store.list_tasks({PROJECT})['tasks']) == before, 'denied_request_registered_task')
        if task_id:
            history = client.get('/extension/v1/node-tasks/' + task_id, headers=headers)
            events = client.get('/extension/v1/node-tasks/' + task_id + '/events', headers=headers)
            require(history.status_code == events.status_code == 200, 'candidate_history_unavailable')
        return {'controlledExecution': health['controlledExecution'],
                'readyForExecution': health['readyForExecution'], 'taskCount': before,
                'newTaskRejected': not confirmed or guarded, 'historyReadable': bool(task_id)}


def _execute(root, directory, *, memory_query=None, instruction_extra='', instruction_prefix=''):
    process = json.loads((root / 'var/qwenpaw/agent-line-process.json').read_text())
    before = main_snapshot(root)
    deployment = installed_plugin_ready(root, process)
    quiet_host(process['pid'])
    config = json.loads((root / 'var/qwenpaw/config.json').read_text())
    bridge = config['plugins']['agent-line']['execution_bridge']
    require(bool(bridge.get('container_control')), 'container_control_required')
    workspace = Path(bridge['workspace']).resolve(strict=True)
    base = 'http://127.0.0.1:' + str(process['port'])
    suffix = secrets.token_hex(8)
    artifact = 'native-readiness-' + suffix + '.txt'
    marker = 'NATIVE_READINESS_' + suffix
    require(not (workspace / artifact).exists(), 'artifact_already_exists')
    require(not directory.exists(), 'existing_directory_must_not_be_adopted')
    directory.mkdir(mode=0o700)
    state = {'status': 'prepared', 'suffix': suffix, 'artifact': artifact, 'marker': marker,
             'baseURL': base, 'createdAt': time.time(), 'mainBefore': before, 'deployment': deployment}
    persist = lambda: save(directory / 'state.json', state)
    persist()
    gateways = {}
    try:
        evidence = directory / 'evidence'; evidence.mkdir(mode=0o700)
        store = TaskStore.initialize(directory / 'tasks.sqlite')
        RoleDispatchLedger.initialize(directory / 'roles.sqlite')
        image = json.loads((root / 'var/executor/config.json').read_text())['image']
        executor = DockerTestExecutor(evidence, image)
        with httpx.Client(timeout=60, trust_env=False, follow_redirects=False) as host:
            login = host.post(base + '/api/auth/login',
                json=json.loads((root / 'var/qwenpaw-secrets/local-login.json').read_text()))
            require(login.status_code == 200, 'host_authentication_failed')
            state['hostToken'] = login.json().get('token') or login.json().get('access_token')
            require(isinstance(state['hostToken'], str) and bool(state['hostToken']), 'host_token_missing')
            persist()
            response = host.get(base + '/api/agent-line/execution-binding', headers={
                'Authorization': 'Bearer ' + state['hostToken'],
                'X-Agent-Line-Service-Token': (root / 'var/qwenpaw-secrets/execution-service-token').read_text().strip()})
            require(response.status_code == 200, 'binding_unavailable')
            descriptor = response.json()
            revision = validate_role_revision(descriptor.get('roleRevisions', {}).get(ROLE))
            require(descriptor.get('workspace') == str(workspace) and ROLE in descriptor.get('managedRoleIds', []),
                    'binding_scope_mismatch')
            profile = host.get(base + '/api/agents/' + ROLE,
                headers={'Authorization': 'Bearer ' + state['hostToken'], 'X-Agent-Id': ROLE})
            require(profile.status_code == 200 and profile.json().get('running', {}).get('memory_manager_backend') == 'remelight',
                    'native_memory_role_required')
        verification_policy = policy(artifact, marker, executor.environment_id, suffix)
        service, execution, token = candidate_settings(root, directory, workspace, base, revision, verification_policy, image, memory_query)
        save(directory / 'execution.json', execution); save(directory / 'service.json', service)
        instruction = instruction_prefix + ('Delegate exactly once to Codex. Use your own shell, no subagents. Write ONLY the relative file '
            + artifact + ' in the assigned workspace, containing exactly ' + marker
            + ' plus one newline. Shell-check its contents. Do not change any other file, memory configuration, '
              'permissions or use network. Ignore unrelated markers from memory. Independent tests decide acceptance.')
        if instruction_extra:
            instruction += instruction_extra
        body = request_body(suffix, instruction)
        state['unknownAdmission'] = check_candidate_http(directory / 'service.json', token, body, store=store)
        persist()
        gateways = load_execution_gateways(directory / 'execution.json', projects=service['projects'], bindings=service['bindings'])
        verifier = AutomaticVerifier(store, evidence, test_executor=executor)
        dispatcher = Dispatcher(store, gateways, verifier=verifier)
        payload = {k: v for k, v in body.items() if k != 'idempotencyKey'}
        payload.update(roleAgentId=ROLE, workspace=str(workspace), roleRevision=revision,
                       verificationPolicy=verification_policy)
        state['registrationIntent'] = True; persist()
        task, reused = store.register(body['idempotencyKey'], payload)
        require(not reused, 'unexpected_existing_task')
        state['taskId'] = task['id']; persist()
        submit_once(dispatcher, state, persist)
        deadline = time.monotonic() + 1200
        last_print = 0
        while time.monotonic() < deadline:
            task = dispatcher.reconcile(task['id'])
            if task['executionStatus'] == 'succeeded' and task['resourcesReleased']:
                verifier.verify_pending(task['id']); task = store.get(task['id'])
                if task['verificationStatus'] == 'passed':
                    break
                require(task['verificationStatus'] == 'pending', 'independent_verification_failed')
            require(task['executionStatus'] not in ('failed', 'canceled'), 'native_task_failed')
            if time.monotonic() - last_print > 30:
                print(json.dumps({'status': 'observing_original_task', 'taskId': task['id'],
                                  'executionStatus': task['executionStatus']}), flush=True)
                last_print = time.monotonic()
            time.sleep(2)
        else:
            raise RuntimeError('original_task_observation_deadline')
        gateway = gateways[(PROJECT, BINDING)]
        parent = gateway._gateway.ledger.get(task['id'])
        for _ in range(150):
            response = gateway._client.get(base + '/api/console/chat/task/' + parent['parentTaskId'],
                headers=gateway._gateway.headers)
            require(response.status_code == 200, 'original_parent_observation_unavailable')
            if response.json().get('status') == 'finished':
                break
            time.sleep(2)
        else:
            raise RuntimeError('original_parent_observation_deadline')
        check_exit(task, bridge)
        from agent_line.execution_readiness import prepare_readiness
        proof = prepare_readiness(root=root, projects=service['projects'], bindings=service['bindings'],
            gateways=gateways, tasks=store, verification_directory=evidence,
            coverage=[{'projectId': PROJECT, 'roleBindingId': BINDING, 'taskId': task['id']}])
        save(directory / 'readiness.json', proof)
        service['readinessFile'] = 'readiness.json'; save(directory / 'service.json', service)
        state['confirmedAdmission'] = check_candidate_http(directory / 'service.json', token, body,
                                                            store=store, task_id=task['id'])
        state['maintenanceIntent'] = True; persist()
        state['maintenanceOwner'] = store.enter_maintenance(); persist()
        denied_body = {**body, 'idempotencyKey': body['idempotencyKey'] + '-guarded',
                       'workflow': {**body['workflow'], 'nodeExecutionId': 'guarded-' + suffix}}
        state['guardedAdmission'] = check_candidate_http(directory / 'service.json', token, denied_body,
            store=store, task_id=task['id'], guarded=True)
        require(main_snapshot(root) == before, 'main_configuration_or_guard_changed')
        quiet_host(process['pid'])
        with store.connect() as db:
            require(db.execute('SELECT count(*) FROM workspace_leases').fetchone()[0] == 0, 'candidate_lease_remains')
        state['status'] = 'passed_live_guarded'; persist()
        return {'status': state['status'], 'taskId': task['id'], 'activeLeaseCount': 0,
                'newSubmissions': 1, 'mainUnchanged': True, 'candidateGuardRetained': True}
    except Exception as exc:
        # Only fixed codes and exception class are persisted; model/native body
        # and HTTP request details never enter errors or terminal output.
        state['status'] = 'incomplete'
        state['lastError'] = failure_summary(exc)
        persist()
        raise RuntimeError('native_readiness_probe_incomplete') from None
    finally:
        for gateway in gateways.values():
            gateway.close()


def run_probe(*, execute=False, directory=DATA, root=ROOT, client_factory=httpx.Client):
    directory = Path(directory)
    path = directory / 'state.json'
    if path.exists():
        return observe_existing(directory, json.loads(path.read_text()), client_factory=client_factory)
    if not execute:
        return {'status': 'not_executed', 'newSubmissions': 0}
    return _execute(Path(root), directory)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    try:
        print(json.dumps(run_probe(execute=args.execute), ensure_ascii=False), flush=True)
    except Exception as error:
        print(json.dumps({'status': 'incomplete', 'errorType': type(error).__name__,
                          'code': 'native_readiness_probe_incomplete'}), flush=True)
        raise SystemExit(1)
