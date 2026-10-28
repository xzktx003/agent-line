#!/usr/bin/env python3
"""AC16 on the original main installation, with one explicit first execution.

Default and every invocation with existing state are observation-only. An
interrupted intent is never resumed by submitting, restarting, canceling or
clearing a lease. Private evidence is deliberately separate from public output.
This probe installs nothing and does not establish execution readiness.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'deploy/executor')]

import httpx
import psutil

from agent_line.backup import create_backup, private_file
from agent_line.dispatcher import Dispatcher
from agent_line.execution_runtime import load_execution_gateways
from agent_line.store import TaskStore
from control import locate
from lifecycle import inspect_owned
from probes.sim.authoring_probe import write

DATA = ROOT / 'var/inflight-adapter-restart-20261022/runtime'
SERVICE = ROOT / 'var/adapter/service.json'
HOST_STATE = ROOT / 'var/qwenpaw/agent-line-process.json'
NATIVE_CONFIG = ROOT / 'var/qwenpaw/config.json'
PYTHON = ROOT / '.venv/bin/python'
MANAGER = [str(PYTHON), str(ROOT / 'deploy/qwenpaw/manage.py')]
PROJECT = 'native-memory-roundtrip'
BINDING = 'native-loop-parent'
ROLE = 'agent-line-native-loop-parent'
PORT = 3193
TERMINAL = {'succeeded', 'failed', 'canceled'}


def read(path):
    return json.loads(Path(path).read_text())


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def relative_to_service(value):
    path = Path(value)
    return (path if path.is_absolute() else SERVICE.parent / path).resolve(strict=True)


@contextmanager
def ro(path):
    db = sqlite3.connect(Path(path).resolve(strict=True).as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        yield db
    finally:
        db.close()


def rows(path, table):
    if table not in {'tasks', 'events', 'workspace_leases', 'deliveries', 'receipts', 'role_bindings'}:
        raise ValueError('unsupported_snapshot_table')
    with ro(path) as db:
        return [dict(row) for row in db.execute('SELECT * FROM ' + table)]


def birth(pid):
    value = Path(f'/proc/{pid}/stat').read_text()
    fields = value[value.rfind(')') + 1:].split()
    if fields[0] in {'Z', 'X', 'x'}:
        raise RuntimeError('process_not_live')
    return fields[19]


def listeners():
    return {item.pid for item in psutil.net_connections(kind='tcp')
            if item.status == psutil.CONN_LISTEN and item.laddr.port == PORT}


def adapter_identity(pid):
    process = psutil.Process(pid)
    before = birth(pid)
    argv = process.cmdline()
    if (process.uids().real != os.getuid() or process.uids().effective != os.getuid()
            or Path(process.cwd()).resolve() != ROOT
            or len(argv) < 5 or Path(argv[0]).resolve() != PYTHON.resolve()
            or argv[1:4] != ['-m', 'agent_line.runtime', 'serve']):
        raise RuntimeError('adapter_process_identity_mismatch')
    flags = argv[4:]
    if (len(flags) % 2 or len(set(flags[::2])) != len(flags[::2])
            or set(flags[::2]) - {'--config', '--host', '--port'}):
        raise RuntimeError('adapter_command_options_mismatch')
    options = dict(zip(flags[::2], flags[1::2]))
    if (Path(options.get('--config', '')).resolve(strict=True) != SERVICE
            or int(options.get('--port', 3193)) != PORT):
        raise RuntimeError('adapter_configuration_or_port_mismatch')
    if before != birth(pid):
        raise RuntimeError('adapter_process_birth_changed')
    return {'pid': pid, 'startTicks': before, 'uid': os.getuid(),
            'cwd': str(ROOT), 'argv': argv, 'host': options.get('--host', '127.0.0.1'), 'port': PORT}


def original_adapter():
    found = listeners()
    if len(found) != 1 or None in found:
        raise RuntimeError('main_listener_not_uniquely_owned')
    return adapter_identity(found.pop())


def same_process(identity):
    try:
        return adapter_identity(identity['pid']) == identity
    except (OSError, psutil.Error, RuntimeError, ValueError):
        return False


def stop_adapter(identity, deadline):
    if not same_process(identity) or listeners() != {identity['pid']}:
        raise RuntimeError('refuse_signal_unverified_adapter')
    os.kill(identity['pid'], signal.SIGTERM)
    stop_by = min(deadline, time.time() + 50)
    while time.time() < stop_by:
        if not same_process(identity) and not listeners():
            return
        time.sleep(.25)
    raise RuntimeError('adapter_shutdown_unconfirmed_no_force_kill')


def private_log(name):
    path = DATA / name
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    os.fchmod(descriptor, 0o600)
    return os.fdopen(descriptor, 'ab', buffering=0)


def start_adapter(original, state, persist, deadline):
    if listeners():
        raise RuntimeError('adapter_port_occupied_no_duplicate_start')
    assert_configuration(state)
    # Same interpreter/module/config/host/port; no alternate database or app.
    with private_log('adapter.stdout.log') as out, private_log('adapter.stderr.log') as err:
        process = subprocess.Popen(original['argv'], cwd=ROOT, stdin=subprocess.DEVNULL,
                                   stdout=out, stderr=err, start_new_session=True)
    identity = adapter_identity(process.pid)
    state.setdefault('startedAdapters', []).append(identity)
    persist()
    start_by = min(deadline, time.time() + 60)
    while time.time() < start_by:
        if not same_process(identity):
            raise RuntimeError('adapter_failed_to_start')
        if listeners() == {process.pid}:
            return identity
        time.sleep(.25)
    raise RuntimeError('adapter_start_deadline')


def public_get(config, task_id, *, timeout=5):
    if not 0 < timeout <= 5:
        raise TimeoutError('http_observation_deadline')
    token = next(key for key, scopes in config['tokens'].items() if PROJECT in scopes)
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        response = client.get(f'http://127.0.0.1:{PORT}/extension/v1/node-tasks/{task_id}',
                              headers={'Authorization': 'Bearer ' + token})
    response.raise_for_status()
    return response.json()


def observe_existing(state):
    """No rw store/factory/login/manager is constructed on this path."""
    result = {'phase': state.get('phase', 'incomplete'), 'taskId': state.get('taskId'),
              'status': 'intent_requires_observation'}
    if not state.get('taskId'):
        return result
    with ro(state['database']) as db:
        row = db.execute('SELECT execution_status FROM tasks WHERE id=?', (state['taskId'],)).fetchone()
    result['status'] = row['execution_status'] if row else 'original_task_missing'
    try:
        task = public_get(read(SERVICE), state['taskId'])
        if state.get('identity') and task['executionIdentity'] != state['identity']:
            result['status'] = 'identity_mismatch'
        else:
            observation = task.get('executionObservation') or {}
            result['status'] = observation.get('status', task['executionStatus'])
    except (httpx.HTTPError, OSError, ValueError, KeyError, StopIteration):
        result['status'] = 'http_observation_unavailable'
    return result


def copy_private(source, destination):
    with private_file(destination) as output:
        output.write(Path(source).read_bytes())
        output.flush()
        os.fsync(output.fileno())


def assert_configuration(state):
    # Agent JSON snapshots are evidence, not an immutable-provider/memory lock.
    # Only the main service/factory inputs must be identical for these restarts.
    paths = (str(SERVICE), state['executionFile'])
    if any(digest(path) != state['configurationHashes'][path] for path in paths):
        raise RuntimeError('original_configuration_changed')


def native_facts(state):
    """Freeze immutable claims, not mutable observations or native memory."""
    task_id = state['taskId']
    with ro(state['database']) as db:
        claims = db.execute("SELECT count(*) FROM events WHERE task_id=? AND kind='submission_intent'",
                            (task_id,)).fetchone()[0]
    deliveries = rows(state['ledger'], 'deliveries')
    native = rows(state['receiptDatabase'], 'receipts')
    bindings = rows(state['receiptDatabase'], 'role_bindings')
    own = [row for row in deliveries if row['task_id'] == task_id]
    receipts = [row for row in native if row['task_id'] == task_id]
    bound = [row for row in bindings if row['task_id'] == task_id]
    if len(own) != 1 or len(receipts) != 1 or len(bound) != 1 or claims != 1:
        raise RuntimeError('single_original_claim_required')
    return {'submissionClaims': claims, 'deliveryCount': len(deliveries),
            'nativeClaimCount': len(native), 'roleBindingCount': len(bindings),
            'delivery': {key: own[0][key] for key in ('task_id', 'payload', 'parent_id', 'created')},
            'nativePayload': receipts[0]['payload'], 'roleBinding': bound[0]}


def live_stage1(state, task):
    identity = task['executionIdentity']
    if (not all(isinstance(identity.get(key), str) and identity[key] for key in
                ('upstreamTaskId', 'codexThreadId', 'codexTurnId'))
            or task['executionStatus'] in TERMINAL or task['resourcesReleased']
            or (Path(state['workspace']) / state['stage2']).exists()):
        raise RuntimeError('not_an_inflight_native_task')
    first = Path(state['workspace']) / state['stage1']
    if first.is_symlink() or not first.is_file() or first.read_bytes() != (state['marker'] + '\n').encode():
        raise RuntimeError('stage1_not_observed')
    _, record = locate(identity['codexThreadId'], identity['codexTurnId'], state['workspace'])
    container = inspect_owned(record)
    if (not container or container['Id'] != record.get('containerId')
            or container['State'].get('Running') is not True
            or type(container['State'].get('Pid')) is not int or container['State']['Pid'] <= 0
            or record.get('uid') != os.getuid() or record.get('exitReceipt')):
        raise RuntimeError('owned_live_docker_proof_missing')
    return {'owner': record['owner'], 'containerId': container['Id'],
            'running': True, 'pid': container['State']['Pid'], 'observedAt': time.time()}


def manager(action, state, persist):
    number = len(state.setdefault('managerActions', []))
    entry = {'action': action, 'startedAt': time.time()}
    state['managerActions'].append(entry)
    persist()
    try:
        with private_log(f'manager-{number}.stdout.log') as out, private_log(f'manager-{number}.stderr.log') as err:
            result = subprocess.run(MANAGER + [action], cwd=ROOT, stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=err, timeout=60)
        entry['returnCode'] = result.returncode
        if result.returncode:
            raise RuntimeError('manager_operation_failed')
    finally:
        entry['finishedAt'] = time.time()
        persist()


def snapshot(config, state):
    backup = DATA / 'before'
    backup.mkdir(mode=0o700)
    original_tasks = rows(state['database'], 'tasks')
    native = read(NATIVE_CONFIG)
    profiles = native['agents']['profiles']
    if len(original_tasks) != 13 or len(profiles) != 9:
        raise RuntimeError('expected_original_13_tasks_9_agents')
    if rows(state['database'], 'workspace_leases') or any(
            row['execution_status'] not in TERMINAL or not row['resources_released'] for row in original_tasks):
        raise RuntimeError('original_main_not_idle')
    write(backup / 'tasks.json', original_tasks)
    write(backup / 'events.json', rows(state['database'], 'events'))
    write(backup / 'host-process.json', read(HOST_STATE))
    files = {'service.json': SERVICE, 'execution.json': Path(state['executionFile']),
             'native-config.json': NATIVE_CONFIG}
    for number, (role, profile) in enumerate(sorted(profiles.items())):
        path = Path(profile['workspace_dir']) / 'agent.json'
        files[f'agent-{number}.json'] = path
        state.setdefault('agentFiles', {})[role] = str(path)
    for name, path in files.items():
        copy_private(path, backup / name)
    state['configurationHashes'] = {str(path): digest(path) for path in files.values()}
    state['originalTaskIds'] = sorted(row['id'] for row in original_tasks)
    state['taskBackup'] = create_backup(state['database'], backup / 'sqlite')
    write(backup / 'deliveries.json', rows(state['ledger'], 'deliveries'))
    # SQLite backup is mandatory for the task DB; other ledgers are observed by
    # immutable facts below, and are never restored by this probe.
    state['originalHost'] = read(HOST_STATE)
    state['receiptDatabase'] = native['plugins']['agent-line']['execution_bridge']['receipt_db']


def execute(deadline_seconds):
    DATA.parent.mkdir(mode=0o700, exist_ok=True)
    DATA.mkdir(mode=0o700)  # Existing directory, even empty, is never a new attempt.
    DATA.chmod(0o700)
    state = {'phase': 'preparing', 'startedAt': time.time(), 'parentTimeoutSeconds': 900}
    persist = lambda: write(DATA / 'state.json', state)
    persist()
    gateways = {}
    adapter = None
    restore_host = False
    try:
        config = read(SERVICE)
        binding = config['bindings'][BINDING]
        if binding['projectId'] != PROJECT or binding['roleAgentId'] != ROLE:
            raise RuntimeError('canonical_binding_required')
        state.update(database=str(relative_to_service(config['database'])),
                     executionFile=str(relative_to_service(config['executionFile'])),
                     workspace=config['projects'][PROJECT]['workspace'])
        entry = next(item for item in read(state['executionFile'])['gateways']
                     if (item['projectId'], item['roleBindingId']) == (PROJECT, BINDING))
        if entry.get('parentTimeoutSeconds') != 900 or entry['roleAgentId'] != ROLE:
            raise RuntimeError('original_parent_timeout_and_role_required')
        state['ledger'] = entry['ledgerPath']
        original = original_adapter()
        state['originalAdapter'] = original
        state['configurationHashes'] = {str(path): digest(path) for path in
                                        (SERVICE, Path(state['executionFile']))}
        deadline = state['startedAt'] + deadline_seconds
        # Stop idle main before trusted bootstrap; public admission is unchanged.
        state['adapterStopIntent'] = True
        persist()
        stop_adapter(original, deadline)
        state['originalAdapterStopped'] = True
        persist()
        snapshot(config, state)
        state['phase'] = 'snapshotted'
        persist()
        gateways = load_execution_gateways(state['executionFile'], projects=config['projects'], bindings=config['bindings'])
        dispatcher = Dispatcher(TaskStore(state['database']), gateways)
        suffix = secrets.token_hex(8)
        state.update(stage1=f'ac16-{suffix}-stage1.txt', stage2=f'ac16-{suffix}-stage2.txt',
                     marker='AC16_' + suffix, idempotencyKey='ac16-inflight-' + suffix)
        for name in ('stage1', 'stage2'):
            if (Path(state['workspace']) / state[name]).exists():
                raise RuntimeError('new_stage_file_already_exists')
        script = (f"printf '%s\\n' {state['marker']} > {state['stage1']}\n"
                  f"sleep 180\nprintf '%s\\n' {state['marker']} > {state['stage2']}")
        payload = {'projectId': PROJECT, 'roleAgentId': ROLE, 'roleBindingId': BINDING,
                   'roleRevision': binding['roleRevision'], 'verificationPolicy': binding['verificationPolicy'],
                   'workspace': state['workspace'], 'workflow': {'id': 'ac16-inflight-adapter-restart',
                       'revision': '1', 'runId': suffix, 'nodeId': 'executor',
                       'nodeExecutionId': suffix, 'attemptId': '1'},
                   'input': {'instruction': 'Use your own shell and run '
                       'the following foreground script to completion. No subagents, extra delegation, network, '
                       'other file reads/writes, background execution or remembered filenames. '
                       'Do not change the verification policy or claim independent acceptance.\n```sh\n' + script + '\n```'}}
        state.update(registrationIntent=True, payload=payload, phase='registration_intent')
        persist()  # Unknown registration/submission is never retried on a later run.
        task, reused = dispatcher.store.register(state['idempotencyKey'], payload)
        if reused:
            raise RuntimeError('unexpected_reused_registration')
        state.update(taskId=task['id'], submissionIntent=True, phase='submission_intent')
        persist()
        dispatcher.submit(task['id'])  # Sole model-submission site in this file.
        # Parent receipt is enough to restore the public main service promptly;
        # the original dispatcher worker recovers this already claimed task.
        adapter = start_adapter(original, state, persist, deadline)
        state['phase'] = 'adapter_online_awaiting_stage1'
        persist()
        while time.time() < deadline:
            try:
                task = public_get(config, state['taskId'])
            except httpx.HTTPError:
                time.sleep(1)
                continue
            state['lastTask'] = task
            persist()
            if task['executionStatus'] in TERMINAL:
                raise RuntimeError('task_ended_before_inflight_restart')
            if (Path(state['workspace']) / state['stage1']).exists() and all(
                    task['executionIdentity'].get(key) for key in ('upstreamTaskId', 'codexThreadId', 'codexTurnId')):
                break
            time.sleep(1)
        else:
            raise TimeoutError('original_task_observation_deadline')
        state.update(identity=task['executionIdentity'], liveDockerBeforeAdapter=live_stage1(state, task),
                     frozenClaims=native_facts(state), phase='native_inflight')
        persist()
        # Actual adapter startup -> in-flight stop -> startup on the original port.
        state['httpBeforeAdapterRestart'] = wait_http(config, state, deadline)
        live_stage1(state, dispatcher.store.get(state['taskId']))
        state['inflightAdapterStopIntent'] = adapter
        persist()
        stop_adapter(adapter, deadline)
        state['inflightAdapterStopped'] = adapter
        persist()
        adapter = start_adapter(original, state, persist, deadline)
        state['httpAfterAdapterRestart'] = wait_http(config, state, deadline)
        assert_claims(state)
        state['liveDockerBeforeHost'] = live_stage1(state, dispatcher.store.get(state['taskId']))
        state['phase'] = 'host_restart_intent'
        persist()
        if read(HOST_STATE) != state['originalHost']:
            raise RuntimeError('host_process_identity_changed_before_stop')
        host = psutil.Process(state['originalHost']['pid'])
        if (host.uids().real != os.getuid() or Path(host.cwd()).resolve() != ROOT
                or birth(host.pid) != state['originalHost']['start_ticks']):
            raise RuntimeError('original_host_process_unverified')
        restore_host = True
        try:
            manager('stop', state, persist)
            unknown_by = min(deadline, time.time() + 15)
            while time.time() < unknown_by:
                remaining = unknown_by - time.time()
                if remaining <= 0:
                    break
                try:
                    task = public_get(config, state['taskId'], timeout=min(2, remaining))
                except httpx.HTTPError:
                    continue
                if task['executionIdentity'] != state['identity']:
                    raise RuntimeError('public_original_identity_changed')
                state['lastPublicTaskWhileHostStopped'] = task
                persist()
                if (task.get('executionObservation') or {}).get('status') == 'unknown':
                    state['unknownObservedAt'] = time.time()
                    break
                time.sleep(.25)
        finally:
            # Capture the unavailable observation briefly; never hold QwenPaw
            # down while waiting for the Codex sleep or a terminal outcome.
            manager('start', state, persist)
            restore_host = False
        after = read(HOST_STATE)
        if after['port'] != state['originalHost']['port'] or after['pid'] == state['originalHost']['pid']:
            raise RuntimeError('original_qwen_port_or_restart_mismatch')
        state['afterHost'] = after
        # No gateway/model retry after either restart, even when native identity
        # cannot yet be queried. The main service remains the sole observer.
        state['httpAfterHostRestart'] = wait_http(config, state, deadline)
        assert_claims(state)
        if not state.get('unknownObservedAt'):
            raise RuntimeError('host_unavailable_unknown_observation_not_captured')
        state['phase'] = 'observed_unknown_original_identity'
        persist()
        return {'phase': state['phase'], 'taskId': state['taskId'], 'status': 'unknown'}
    except Exception as error:
        state.update(phase='deadline' if isinstance(error, TimeoutError) else 'requires_reconciliation',
                     errorType=type(error).__name__)
        persist()
        return {'phase': state['phase'], 'taskId': state.get('taskId'), 'status': 'incomplete'}
    finally:
        # A failed stop/start still restores only the same managed host/port.
        # No cancellation, lease deletion, fabricated terminal, or retry occurs.
        if restore_host:
            manager('start', state, persist)
            if read(HOST_STATE)['port'] != state['originalHost']['port']:
                raise RuntimeError('host_restoration_port_mismatch')
        # Restore an adapter known to have been stopped by this invocation. An
        # occupied port, unknown process, or failed shutdown never permits a
        # duplicate or replacement process. Later invocations never enter here.
        if state.get('adapterStopIntent') and not listeners():
            known = state.get('startedAdapters', [])
            if not same_process(state['originalAdapter']) and not any(same_process(item) for item in known):
                adapter = start_adapter(state['originalAdapter'], state, persist, time.time() + 60)
        for gateway in gateways.values():
            gateway.close()


def assert_claims(state):
    if native_facts(state) != state['frozenClaims']:
        raise RuntimeError('original_claim_or_delivery_changed')
    with ro(state['database']) as db:
        ids = sorted(row[0] for row in db.execute('SELECT id FROM tasks'))
        row = db.execute('SELECT execution_identity FROM tasks WHERE id=?', (state['taskId'],)).fetchone()
    if ids != sorted(state['originalTaskIds'] + [state['taskId']]) or json.loads(row[0]) != state['identity']:
        raise RuntimeError('task_set_or_native_identity_changed')


def wait_http(config, state, deadline):
    while time.time() < deadline:
        try:
            task = public_get(config, state['taskId'])
            if task['executionIdentity'] != state['identity']:
                raise RuntimeError('public_original_identity_changed')
            return task
        except httpx.HTTPError:
            time.sleep(.5)
    raise TimeoutError('original_http_task_observation_deadline')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--deadline-seconds', type=int, default=1200)
    args = parser.parse_args()
    if not 240 <= args.deadline_seconds <= 1800:
        parser.error('deadline must be between 240 and 1800 seconds')
    try:
        if (DATA / 'state.json').exists():
            result = observe_existing(read(DATA / 'state.json'))
        elif not args.execute:
            result = {'phase': 'not_executed', 'taskId': None, 'status': 'observation_only'}
        else:
            result = execute(args.deadline_seconds)
    except Exception:
        # Diagnostics and identities already belong to private state/logs.
        # Never print traceback/request/command strings to the public console.
        try:
            original_task = read(DATA / 'state.json').get('taskId')
        except (OSError, ValueError, AttributeError):
            original_task = None
        result = {'phase': 'requires_reconciliation', 'taskId': original_task, 'status': 'incomplete'}
    print(json.dumps(result))


if __name__ == '__main__':
    main()
