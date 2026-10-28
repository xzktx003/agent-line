"""One explicit fresh-project bootstrap; replays never register or submit again.

This CLI does not start services, change roles, or assert business completion.
Without --execute it reads existing local facts only. With --execute, an
existing attempt reconciles only its original persisted case tasks.
"""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import time
import uuid

from agent_line.dispatcher import Dispatcher
from agent_line.execution_readiness import (
    _database, _fingerprint, _json, _ReadOnlyStore, evaluate_readiness, prepare_readiness,
)
from agent_line.execution_runtime import _path, load_execution_gateways
from agent_line.store import TaskStore, canonical
from agent_line.verification_runtime import load_verifier
from .setup import ROOT, MARKER, update_marker, write_new

STATE = 'project-verification.json'
LOCK = 'project-verification.lock'


def require(condition, code):
    if not condition:
        raise ValueError(code)


@contextmanager
def attempt_lock(directory):
    fd = os.open(directory / LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1, 'unsafe_verification_lock')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        current = os.stat(directory / LOCK, follow_symlinks=False)
        require((current.st_dev, current.st_ino) == (info.st_dev, info.st_ino), 'verification_lock_replaced')
        yield
    finally:
        os.close(fd)


def settings(root):
    """Only the original setup installation is eligible; never adopt main state."""
    root = _path(str(root), directory=True)
    directory = _path(str(root / 'var/adapter'), directory=True)
    info = directory.stat()
    require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700, 'private_adapter_required')
    marker = _json(root / 'var' / MARKER)
    require(marker.get('phase') == 'configured' and marker.get('controlledExecution') == 'unknown',
            'fresh_configured_installation_required')
    service = _json(directory / 'service.json')
    spec = _json(directory / 'project-spec.json')
    project = marker['projectId']
    team = 'team' in spec
    binding_ids = ['planner', 'developer', 'reviewer'] if team else ['developer']
    require(set(service['projects']) == {project} and set(service['bindings']) == set(binding_ids)
            and spec['project']['id'] == project and service['schemaVersion'] == 1,
            'original_project_bindings_required')
    roles = {'developer': spec['coordinator'], **(spec.get('team') or {})}
    revision_map = marker.get('roleRevisions', {'developer': marker['roleRevision']})
    for binding_id in binding_ids:
        binding = service['bindings'][binding_id]
        role = roles[binding_id]
        require(binding['projectId'] == project and binding['roleAgentId'] == role['id']
                and binding['roleRevision'] == revision_map[binding_id]
                and binding['verificationPolicy'] == spec['verificationPolicy']
                and service['projects'][project]['workspace'] == spec['project']['workspace']
                and binding['memoryPolicyIds'] == ['native-role']
                and binding['outputSchemaIds'] == ['task-result'], 'setup_binding_changed')
    expected = {'executionFile': 'execution.json', 'authoringFile': 'authoring.json',
                'approvalFile': 'approvals.json', 'memoryFile': 'memory.json', 'taskFlowFile': 'task-flow.json',
                'readinessFile': 'readiness.json'}
    require(all(service.get(key) == value for key, value in expected.items()), 'setup_config_paths_changed')
    require(service['database'] == str(directory / 'tasks.sqlite')
            and service['verification']['evidenceDirectory'] == str(directory / 'evidence'), 'setup_storage_changed')
    execution = _json(directory / 'execution.json')
    require(len(execution['gateways']) == len(binding_ids), 'original_gateway_count_required')
    gateways = {gateway.get('roleBindingId'): gateway for gateway in execution['gateways']}
    require(set(gateways) == set(binding_ids), 'setup_gateway_bindings_changed')
    for binding_id in binding_ids:
        gateway = gateways[binding_id]
        require(gateway['installationId'] == marker['installationId']
                and gateway['projectId'] == project and gateway['roleBindingId'] == binding_id
                and gateway['roleAgentId'] == service['bindings'][binding_id]['roleAgentId']
                and gateway['workspace'] == spec['project']['workspace']
                and gateway['ledgerPath'] == str(directory / 'roles.sqlite')
                and gateway['baseURL'] == spec['qwenpaw']['baseURL'], 'setup_gateway_changed')
    names = ['service.json', 'execution.json', 'authoring.json', 'approvals.json', 'memory.json',
             'task-flow.json', 'project-spec.json', 'native-role.json']
    hashes = {name: hashlib.sha256(canonical(_json(directory / name)).encode()).hexdigest() for name in names}
    hashes['setup'] = hashlib.sha256(canonical(marker).encode()).hexdigest()
    return directory, service, project, hashes


def _find_task(store, state):
    with _database(store.path) as db:
        row = db.execute('SELECT * FROM tasks WHERE idempotency_key=?', (state['key'],)).fetchone()
        if row is None:
            require(state.get('taskId') is None, 'original_registration_missing')
            return None
        task = TaskStore.decode(row)
        require(task['payload'] == state['payload'] and state.get('taskId') in (None, task['id']),
                'original_task_identity_mismatch')
        return task


def public_result(state, task=None, *, status=None):
    result = {'status': status or state.get('status', 'pending'), 'projectId': state.get('projectId'),
              'newSubmissions': 0, 'businessTaskCompleted': False}
    if task is not None:
        result.update(taskId=task['id'], executionStatus=task['executionStatus'],
                      verificationStatus=task['verificationStatus'], resourcesReleased=task['resourcesReleased'])
    if state.get('errorCode'):
        result['errorCode'] = state['errorCode']
    return result


def _state(directory, project, hashes, binding_ids=None):
    state = _json(directory / STATE)
    if binding_ids and binding_ids != ['developer']:
        require(state.get('schemaVersion') == 2 and state.get('projectId') == project
                and state.get('configurationHash') == hashes and state.get('bindingIds') == binding_ids
                and isinstance(state.get('cases'), list) and len(state['cases']) == len(binding_ids),
                'verification_attempt_changed')
        for case, binding_id in zip(state['cases'], binding_ids):
            require(case.get('roleBindingId') == binding_id and isinstance(case.get('key'), str)
                    and case['key'] and isinstance(case.get('payload'), dict)
                    and type(case.get('registrationIntent')) is bool
                    and type(case.get('submissionIntent')) is bool
                    and (case.get('taskId') is None or isinstance(case.get('taskId'), str))
                    and (case.get('status') != 'confirmed' or (case['registrationIntent']
                         and case['submissionIntent'] and case.get('taskId'))),
                    'verification_attempt_changed')
        require(len({case['key'] for case in state['cases']}) == len(binding_ids),
                'verification_attempt_changed')
    else:
        require(state.get('schemaVersion') == 1 and state.get('projectId') == project
                and state.get('configurationSha256') == hashes and state.get('registrationIntent') is True
                and isinstance(state.get('key'), str) and state['key'], 'verification_attempt_changed')
    return state


def _payload(service, project, binding_id='developer'):
    identity = uuid.uuid4().hex
    binding = service['bindings'][binding_id]
    return {'schemaVersion': '1.0', 'projectId': project, 'roleBindingId': binding_id,
        'roleAgentId': binding['roleAgentId'], 'roleRevision': binding['roleRevision'],
        'workspace': service['projects'][project]['workspace'],
        'workflow': {'id': 'project-initial-verification', 'revision': '1', 'runId': identity,
                     'nodeId': 'native-read-check', 'nodeExecutionId': uuid.uuid4().hex, 'attemptId': '1'},
        'input': {'instruction': f'这是本安装首次受控执行核对（{binding_id}角色），不是业务开发任务。请仅委派一次给固定Codex角色，'
            '让其读取当前项目的说明、代码和测试入口，简要核对项目结构及可用的测试方式。'
            '不得修改或创建业务文件、安装依赖、调整权限、修改记忆配置或发起子代理；不要执行网络操作。'
            '结合本次原生记忆检索结果，但历史记忆不是本次验收证据。'
            '通过现有状态工具等待并核对本次原执行身份和真实完成状态，未知时如实报告。'
            '可信独立测试将使用配置中的原始策略另行执行；不要声称业务任务已完成或自行批准权限。'},
        'memoryPolicyId': 'native-role', 'outputSchemaId': 'task-result',
        'verificationPolicy': binding['verificationPolicy']}


def _proof(directory, root, service, gateways, store, verifier, state):
    _, _, project, hashes = settings(root)
    config_hash = state.get('configurationHash', state.get('configurationSha256'))
    require(project == state['projectId'] and hashes == config_hash,
            'configuration_changed_during_verification')
    context = dict(root=root, projects=service['projects'], bindings=service['bindings'], gateways=gateways,
                   tasks=store, verification_directory=verifier.directory)
    target = directory / service['readinessFile']
    if target.exists() or target.is_symlink():
        readiness = evaluate_readiness(target, **context)
        require(readiness.confirmed, 'existing_readiness_unproven')
        expected_coverage = ([{'projectId': state['projectId'], 'roleBindingId': case['roleBindingId'],
            'taskId': case['taskId']} for case in state['cases']] if state.get('schemaVersion') == 2 else
            [{'projectId': state['projectId'], 'roleBindingId': 'developer', 'taskId': state['taskId']}])
        require(_json(target)['coverage'] == expected_coverage, 'existing_readiness_identity_mismatch')
    else:
        coverage = ([{'projectId': state['projectId'], 'roleBindingId': case['roleBindingId'],
            'taskId': case['taskId']} for case in state['cases']] if state.get('schemaVersion') == 2 else
            [{'projectId': state['projectId'], 'roleBindingId': 'developer', 'taskId': state['taskId']}])
        proof = prepare_readiness(**context, coverage=coverage)
        write_new(target, proof)
    state.update(status='confirmed', readinessGenerated=True)
    state.pop('errorCode', None)
    update_marker(directory / STATE, state)


def _find_case_task(store, case):
    with _database(store.path) as db:
        row = db.execute('SELECT * FROM tasks WHERE idempotency_key=?', (case['key'],)).fetchone()
        if row is None:
            require(case.get('taskId') is None, 'original_registration_missing')
            return None
        task = TaskStore.decode(row)
        require(task['payload'] == case['payload'] and case.get('taskId') in (None, task['id']),
                'original_task_identity_mismatch')
        return task


def _team_result(state, *, status=None, cases=None, new_submissions=0):
    values = cases if cases is not None else state['cases']
    return {'status': status or state.get('status', 'pending'), 'projectId': state['projectId'],
        'newSubmissions': new_submissions, 'businessTaskCompleted': False,
        'cases': [{'roleBindingId': case['roleBindingId'], 'status': case.get('status', 'not_started'),
            **({'taskId': case['taskId']} if case.get('taskId') else {})} for case in values]}


def _has_successful_readonly_operation(task):
    """Require native command evidence under the bridge's frozen readonly policy."""
    policy = any(isinstance(item, dict) and item.get('kind') == 'execution_policy'
        and item.get('source') == 'qwenpaw-plugin-task-binding'
        and item.get('controls') == {'sandbox': 'read-only', 'approval_policy': 'never'}
        for item in task.get('evidence', []))
    output = task.get('output')
    operations = output.get('toolEvidence', []) if isinstance(output, dict) else []
    succeeded = any(isinstance(item, dict) and item.get('type') == 'CommandExecution'
        and item.get('status') == 'completed' and type(item.get('exit_code')) is int
        and item['exit_code'] == 0 for item in operations)
    return policy and succeeded


def _advance_team(root, directory, service, project, hashes, state, *, fresh, wait_seconds):
    store = TaskStore(directory / 'tasks.sqlite')
    binding_ids = ['planner', 'developer', 'reviewer']
    gateways = {}
    submitted = 0
    try:
        verifier = load_verifier(service['verification'], tasks=store, projects=service['projects'])
        for binding_id in binding_ids:
            verifier.validate_policy(service['bindings'][binding_id]['verificationPolicy'])
        gateways = load_execution_gateways(directory / service['executionFile'],
            projects=service['projects'], bindings=service['bindings'])
        context = dict(root=root, projects=service['projects'], bindings=service['bindings'],
            gateways=gateways, tasks=store, verification_directory=verifier.directory)
        _fingerprint(**context)
        dispatcher = Dispatcher(store, gateways, verifier=verifier)
        if fresh:
            with _database(store.path) as db:
                require(db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
                        and db.execute('SELECT count(*) FROM workspace_leases').fetchone()[0] == 0
                        and db.execute('SELECT count(*) FROM admission_guard').fetchone()[0] == 0,
                        'fresh_empty_unguarded_store_required')
            require(not (directory / service['readinessFile']).exists()
                    and not (directory / service['readinessFile']).is_symlink(), 'readiness_already_exists')
            cases = []
            for binding_id in binding_ids:
                cases.append({'roleBindingId': binding_id,
                    'key': f'project-bootstrap-{binding_id}-' + uuid.uuid4().hex,
                    'payload': _payload(service, project, binding_id), 'registrationIntent': False,
                    'submissionIntent': False, 'taskId': None, 'status': 'not_started'})
            state = {'schemaVersion': 2, 'status': 'pending', 'projectId': project,
                'configurationHash': hashes, 'bindingIds': binding_ids, 'cases': cases,
                'createdAt': time.time()}
            write_new(directory / STATE, state)
        deadline = time.monotonic() + wait_seconds
        for index, case in enumerate(state['cases']):
            binding_id = case['roleBindingId']
            if case.get('status') == 'confirmed':
                continue
            if index and time.monotonic() >= deadline:
                return _team_result(state, status='pending', new_submissions=submitted)
            if not case['registrationIntent']:
                case['registrationIntent'] = True
                case['status'] = 'registration_unknown'
                update_marker(directory / STATE, state)
                task, reused = store.register(case['key'], case['payload'])
                require(not reused, 'unexpected_registration_reuse')
                case['taskId'] = task['id']
                case['status'] = 'registered'
                update_marker(directory / STATE, state)
                case['submissionIntent'] = True
                case['status'] = 'submission_unknown'
                update_marker(directory / STATE, state)
                dispatcher.submit(task['id'])
                submitted += 1
            else:
                task = _find_case_task(store, case)
                if task is None:
                    return _team_result(state, status='registration_not_recorded', new_submissions=submitted)
                if case.get('taskId') is None:
                    case['taskId'] = task['id']
                    update_marker(directory / STATE, state)
                if not case['submissionIntent']:
                    return _team_result(state, status='original_submission_not_confirmed', new_submissions=submitted)
                if task['executionStatus'] == 'registered':
                    return _team_result(state, status='original_submission_not_confirmed', new_submissions=submitted)
            while True:
                task = dispatcher.reconcile(case['taskId'])
                if task['executionStatus'] in ('failed', 'canceled'):
                    case['status'] = 'failed'
                    state['status'] = 'failed'
                    update_marker(directory / STATE, state)
                    return _team_result(state, status='failed', new_submissions=submitted)
                if task['executionStatus'] == 'succeeded' and task['resourcesReleased'] is True:
                    verifier.verify_pending(task['id'])
                    task = store.get(task['id'])
                    if task['verificationStatus'] in ('failed', 'canceled'):
                        case['status'] = 'verification_failed'
                        state['status'] = 'verification_failed'
                        update_marker(directory / STATE, state)
                        return _team_result(state, status='verification_failed', new_submissions=submitted)
                    if task['verificationStatus'] == 'passed':
                        if binding_id in ('planner', 'reviewer') and not _has_successful_readonly_operation(task):
                            case['status'] = 'readonly_operation_unproven'
                            state['status'] = 'readonly_operation_unproven'
                            update_marker(directory / STATE, state)
                            return _team_result(state, status='readonly_operation_unproven',
                                                new_submissions=submitted)
                        gateway = gateways[(project, binding_id)]
                        parent = gateway._gateway.ledger.get(task['id'])
                        response = gateway._client.get(gateway._gateway.base + '/console/chat/task/'
                            + parent['parentTaskId'], headers=gateway._gateway.headers, follow_redirects=False)
                        response.raise_for_status()
                        if response.json().get('status') == 'finished':
                            case['status'] = 'confirmed'
                            update_marker(directory / STATE, state)
                            break
                if time.monotonic() >= deadline:
                    case['status'] = 'pending'
                    update_marker(directory / STATE, state)
                    return _team_result(state, status='pending', new_submissions=submitted)
                time.sleep(min(1, max(0, deadline - time.monotonic())))
        _proof(directory, root, service, gateways, store, verifier, state)
        return _team_result(state, status='confirmed', new_submissions=submitted)
    finally:
        for gateway in gateways.values():
            gateway.close()


def _advance(root, directory, service, project, hashes, state, *, fresh, wait_seconds):
    if set(service['bindings']) == {'planner', 'developer', 'reviewer'}:
        return _advance_team(root, directory, service, project, hashes, state,
                             fresh=fresh, wait_seconds=wait_seconds)
    store = TaskStore(directory / 'tasks.sqlite')
    gateways = {}
    try:
        verifier = load_verifier(service['verification'], tasks=store, projects=service['projects'])
        verifier.validate_policy(service['bindings']['developer']['verificationPolicy'])
        gateways = load_execution_gateways(directory / service['executionFile'],
            projects=service['projects'], bindings=service['bindings'])
        context = dict(root=root, projects=service['projects'], bindings=service['bindings'],
            gateways=gateways, tasks=store, verification_directory=verifier.directory)
        _fingerprint(**context)  # Read-only real deployment preflight, never a grant.
        dispatcher = Dispatcher(store, gateways, verifier=verifier)
        if fresh:
            with _database(store.path) as db:
                require(db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
                        and db.execute('SELECT count(*) FROM workspace_leases').fetchone()[0] == 0
                        and db.execute('SELECT count(*) FROM admission_guard').fetchone()[0] == 0,
                        'fresh_empty_unguarded_store_required')
            require(not (directory / service['readinessFile']).exists()
                    and not (directory / service['readinessFile']).is_symlink(), 'readiness_already_exists')
            state = {'schemaVersion': 1, 'status': 'pending', 'projectId': project,
                'configurationSha256': hashes, 'key': 'project-bootstrap-' + uuid.uuid4().hex,
                'payload': _payload(service, project), 'registrationIntent': True, 'taskId': None,
                'submissionIntent': False, 'createdAt': time.time()}
            write_new(directory / STATE, state)
            task, reused = store.register(state['key'], state['payload'])
            require(not reused, 'unexpected_registration_reuse')
            state['taskId'] = task['id']
            update_marker(directory / STATE, state)
            state['submissionIntent'] = True
            update_marker(directory / STATE, state)
            dispatcher.submit(task['id'])
        else:
            task = _find_task(store, state)
            if task is None:
                return public_result(state, status='registration_not_recorded')
            if state.get('taskId') is None:
                state['taskId'] = task['id']
                update_marker(directory / STATE, state)
            if not state.get('submissionIntent') or task['executionStatus'] == 'registered':
                return public_result(state, task, status='original_submission_not_confirmed')
        deadline = time.monotonic() + wait_seconds
        while True:
            task = dispatcher.reconcile(state['taskId'])
            if task['executionStatus'] in ('failed', 'canceled'):
                state['status'] = 'failed'
                update_marker(directory / STATE, state)
                return public_result(state, task)
            if task['executionStatus'] == 'succeeded' and task['resourcesReleased'] is True:
                verifier.verify_pending(task['id'])
                task = store.get(task['id'])
                if task['verificationStatus'] in ('failed', 'canceled'):
                    state['status'] = 'verification_failed'
                    update_marker(directory / STATE, state)
                    return public_result(state, task)
                if task['verificationStatus'] == 'passed':
                    gateway = gateways[(project, 'developer')]
                    parent = gateway._gateway.ledger.get(task['id'])
                    response = gateway._client.get(gateway._gateway.base + '/console/chat/task/' + parent['parentTaskId'],
                                                   headers=gateway._gateway.headers, follow_redirects=False)
                    response.raise_for_status()
                    if response.json().get('status') == 'finished':
                        _proof(directory, root, service, gateways, store, verifier, state)
                        return public_result(state, task)
            if time.monotonic() >= deadline:
                return public_result(state, task, status='pending')
            time.sleep(min(1, max(0, deadline - time.monotonic())))
    finally:
        for gateway in gateways.values():
            gateway.close()


def run_verify(*, root=ROOT, execute=False, wait_seconds=30):
    require(type(wait_seconds) in (int, float) and 0 <= wait_seconds <= 60, 'bounded_wait_required')
    root = Path(root)
    directory, service, project, hashes = settings(root)
    binding_ids = ['planner', 'developer', 'reviewer'] if 'planner' in service['bindings'] else ['developer']
    path = directory / STATE
    if not execute:
        if not path.exists() and not path.is_symlink():
            return {'status': 'not_executed', 'projectId': project, 'newSubmissions': 0,
                    'businessTaskCompleted': False}
        state = _state(directory, project, hashes, binding_ids)
        read_store = _ReadOnlyStore(directory / 'tasks.sqlite')
        if state['schemaVersion'] == 2:
            observed = []
            for case in state['cases']:
                task = _find_case_task(read_store, case) if case['registrationIntent'] else None
                observed.append({'roleBindingId': case['roleBindingId'],
                    'status': case.get('status', 'not_started'),
                    **({'taskId': task['id']} if task else {})})
            result = _team_result(state,
                status='recorded_confirmed' if state.get('status') == 'confirmed' else None,
                cases=observed)
        else:
            task = _find_task(read_store, state)
            result = public_result(state, task,
                status='recorded_confirmed' if state.get('status') == 'confirmed' else None)
        result.update(mode='local_observation', readinessCurrent='not_checked')
        return result
    with attempt_lock(directory):
        fresh = not path.exists() and not path.is_symlink()
        state = None if fresh else _state(directory, project, hashes, binding_ids)
        try:
            result = _advance(root, directory, service, project, hashes, state, fresh=fresh, wait_seconds=wait_seconds)
            if binding_ids == ['developer']:
                result['newSubmissions'] = int(fresh)
            return result
        except Exception:
            if path.exists():
                previous = _state(directory, project, hashes, binding_ids)
                previous['errorCode'] = 'project_verification_incomplete'
                update_marker(path, previous)
            raise


def main():
    parser = argparse.ArgumentParser(description='首次项目执行核对：默认只查本地记录；--execute只投递预登记case并重放原任务')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--wait-seconds', type=float, default=30)
    args = parser.parse_args()
    try:
        result = run_verify(execute=args.execute, wait_seconds=args.wait_seconds)
    except Exception:
        print(json.dumps({'status': 'incomplete', 'code': 'project_verification_incomplete',
                          'businessTaskCompleted': False}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
