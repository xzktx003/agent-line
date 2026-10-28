#!/usr/bin/env python3
"""One original ReMe role recalls committed experience after the real host restart.

Default performs no model calls or writes. Only a first explicit --execute can submit. Once state exists,
all invocations observe the original identities through GET and SQLite mode=ro;
expired credentials, unknown submission or failed work never permit another task.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import httpx
import psutil

from agent_line.dispatcher import Dispatcher
from agent_line.role_dispatch import RoleDispatchGateway, RoleDispatchLedger
from agent_line.role_revision import validate_role_revision
from agent_line.store import TaskStore, canonical
from agent_line.verification import ArtifactPolicy, verify_artifacts
from probes.full_workflow.parallel_roles import check_exit, installed_plugin_ready, main_snapshot
from probes.qwenpaw_memory.native_roundtrip import BINDING, PROJECT, ROLE, quiet_host, submit_once, verify_chain
from probes.sim.authoring_probe import write

DATA = ROOT / 'var/native-memory-after-restart'
SOURCE = ROOT / 'var/native-memory-roundtrip'
SOURCE_TASK = 'task-b3014c27509442dd9ee01b013be585cd'
SOURCE_SHA = 'efe8b88e7d46941ee114f58088f7b17a3bef788434ba6da7ca350906264c6069'


def read(path):
    return json.loads(Path(path).read_text())


@contextmanager
def ro(path):
    # Resolve strictly: observations must not initialize a missing database.
    db = sqlite3.connect(Path(path).resolve(strict=True).as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        yield db
    finally:
        db.close()


def source_memory_proof(directory, host_birth, official_content):
    """The source JSON is a locator, never the authority for approved memory."""
    state = read(directory / 'state-attempt-4.json')
    if state.get('verified') is not True or state['A']['taskId'] != SOURCE_TASK:
        raise RuntimeError('original_verified_source_required')
    with ro(directory / 'tasks.sqlite') as db:
        task = dict(db.execute('SELECT * FROM tasks WHERE id=?', (SOURCE_TASK,)).fetchone() or {})
    if (task.get('execution_status') != 'succeeded' or task.get('verification_status') != 'passed'
            or task.get('resources_released') != 1):
        raise RuntimeError('source_success_and_independent_verification_required')
    payload, evidence, identity = (json.loads(task[k]) for k in ('payload', 'evidence', 'execution_identity'))
    if any(payload.get(k) != v for k, v in {
            'projectId': PROJECT, 'roleAgentId': ROLE, 'roleBindingId': BINDING}.items()):
        raise RuntimeError('source_task_scope_mismatch')
    expected = (state['marker'] + '\n').encode()
    checks = [e for e in evidence if e.get('kind') == 'verification' and e.get('status') == 'passed']
    exits = [e for e in evidence if e.get('kind') == 'resource_exit']
    if (len(checks) != 1 or checks[0].get('taskId') != SOURCE_TASK
            or checks[0].get('executionIdentity') != identity or len(checks[0].get('checks', [])) != 1
            or read(directory / 'artifacts' / checks[0]['evidenceFile']) != checks[0]):
        raise RuntimeError('source_independent_artifact_evidence_missing')
    check = checks[0]['checks'][0]
    if any(check.get(k) != v for k, v in {
            'path': state['A']['artifact'], 'kind': 'artifact_sha256', 'status': 'passed',
            'expectedSha256': hashlib.sha256(expected).hexdigest(),
            'actualSha256': hashlib.sha256(expected).hexdigest(),
            'expectedSize': len(expected), 'actualSize': len(expected)}.items()):
        raise RuntimeError('source_artifact_policy_mismatch')
    if (len(exits) != 1 or exits[0].get('executionIdentity') != identity
            or exits[0].get('receipt', {}).get('running') is not False
            or exits[0]['receipt'].get('pid') != 0
            or exits[0]['receipt'].get('source') != 'docker-inspect-before-remove'
            or exits[0]['receipt'].get('workspace') != payload['workspace']
            or not any(i.get('nativeTerminal') is True and all(i.get(k) == identity.get(k)
                       for k in ('codexThreadId', 'codexTurnId'))
                       for i in exits[0]['receipt'].get('identities', []))):
        raise RuntimeError('source_native_exit_missing')
    with ro(directory / 'commits.sqlite') as db:
        commit = dict(db.execute('SELECT * FROM memory_commits WHERE role_id=? AND task_id=?',
                                (ROLE, SOURCE_TASK)).fetchone() or {})
        times = [r[0] for r in db.execute("SELECT created_at FROM memory_events WHERE role_id=? "
                 "AND task_id=? AND kind='committed'", (ROLE, SOURCE_TASK))]
    with ro(directory / 'reviews.sqlite') as db:
        review = dict(db.execute('SELECT * FROM memory_reviews WHERE role_id=? AND task_id=?',
                                (ROLE, SOURCE_TASK)).fetchone() or {})
    source = {'workflow': payload['workflow'], 'executionStatus': task['execution_status'],
              'verificationStatus': task['verification_status'],
              'evidenceSha256': hashlib.sha256(canonical(evidence).encode()).hexdigest()}
    if (commit.get('status') != 'committed' or commit.get('content_hash') != SOURCE_SHA
            or hashlib.sha256(commit.get('content', '').encode()).hexdigest() != SOURCE_SHA
            or commit.get('content') != official_content or review.get('content_hash') != SOURCE_SHA
            or review.get('project_id') != PROJECT or review.get('decision_id') != commit.get('decision_id')
            or not review.get('actor') or json.loads(review.get('source', '{}')) != source
            or len(times) != 1 or not review.get('created_at', host_birth) <= times[0] < host_birth):
        raise RuntimeError('committed_review_content_or_real_restart_timeline_unproven')
    with ro(directory / 'roles.sqlite') as db:
        delivery = db.execute('SELECT payload,parent_id FROM deliveries WHERE task_id=?', (SOURCE_TASK,)).fetchone()
    parent = read(directory / 'attempt4-A-parent-final.json')
    report = read(directory / 'attempt4-report.json')['tasks']['A']
    result = parent.get('result') or {}
    if (not delivery or report.get('id') != SOURCE_TASK or report.get('parentTaskId') != delivery['parent_id']
            or json.loads(delivery['payload']).get('roleAgentId') != ROLE
            or json.loads(delivery['payload']).get('roleSessionId') != 'agent-line-' + SOURCE_TASK
            or report.get('executionIdentity') != identity
            or parent.get('status') != 'finished' or result.get('status') != 'completed'
            or result.get('session_id') != 'agent-line-' + SOURCE_TASK
            or not datetime.fromisoformat(result['completed_at']).timestamp() < host_birth):
        raise RuntimeError('source_original_finished_parent_unproven')
    calls, linked = set(), False
    for message in result.get('output', []):
        for block in message.get('content', []):
            data = block.get('data') or {}
            if data.get('name') != 'agent_line_delegate':
                continue
            if message.get('type') == 'plugin_call' and data.get('call_id'):
                calls.add(data['call_id'])
            if (message.get('type') == 'plugin_call_output' and data.get('call_id') in calls
                    and data.get('state') == 'success'):
                output = data.get('output')
                output = json.loads(output) if isinstance(output, str) else output
                linked = linked or (isinstance(output, dict) and output.get('taskId') == SOURCE_TASK
                    and output.get('identity', {}).get('upstreamTaskId') == identity.get('upstreamTaskId'))
    if not linked:
        raise RuntimeError('source_parent_actual_delegation_unproven')
    return {'sourceTaskId': SOURCE_TASK, 'sourceFile': 'agent-line-task-' + SOURCE_TASK + '.md',
            'contentSha256': SOURCE_SHA, 'committedAt': times[0], 'hostBirth': host_birth,
            'originalParentTaskId': delivery['parent_id'], 'originalParentFinished': True,
            'roleWorkspace': state['roleWorkspace'], 'query': state['query'], 'marker': state['marker']}


def instruction_for(proof, artifact):
    instruction = ('检索“' + proof['query'] + '”，从该项目已审核的历史任务经验取得验收标记。'
        '委派 Codex 本人用 shell 新建 ' + artifact + '，精确写入检索所得标记加换行并验证。'
        '不得复用旧文件名，不生成子 agent，不读其他文件或联网，不修改角色或记忆。')
    if proof['marker'] in instruction or proof['marker'] in proof['query']:
        raise RuntimeError('marker_must_only_come_from_native_memory')
    return instruction


def native_input_proof(codex_home, task, marker, artifact):
    identity = task['executionIdentity']
    thread, turn = identity['codexThreadId'], identity['codexTurnId']
    files = list((Path(codex_home) / 'sessions').rglob('*' + thread + '.jsonl'))
    if len(files) != 1:
        raise RuntimeError('original_codex_rollout_ambiguous')
    found = []
    session_matches = contextualized = completed = False
    active_turn = None
    for line in files[0].read_text().splitlines():
        event = json.loads(line); payload = event.get('payload', {})
        if event.get('type') == 'session_meta':
            if payload.get('id') != thread:
                raise RuntimeError('native_rollout_session_identity_mismatch')
            session_matches = True
        if event.get('type') == 'event_msg':
            if payload.get('type') == 'task_started':
                active_turn = payload.get('turn_id')
            elif payload.get('type') == 'task_complete':
                if active_turn == turn and payload.get('turn_id') == turn:
                    completed = True
                active_turn = None
        if (event.get('type') == 'turn_context' and active_turn == turn
                and payload.get('turn_id') == turn):
            contextualized = True
        if (event.get('type') == 'response_item' and payload.get('type') == 'message'
                and session_matches and active_turn == turn
                and isinstance(payload.get('internal_chat_message_metadata_passthrough'), dict)
                and payload['internal_chat_message_metadata_passthrough'].get('turn_id') == turn
                and payload.get('role') == 'user' and marker in json.dumps(payload)
                and artifact in json.dumps(payload)):
            found.append(event.get('timestamp'))
    if not found or not contextualized or not completed:
        raise RuntimeError('recalled_memory_missing_from_actual_codex_input')
    return {'rollout': str(files[0]), 'nativeInputTimestamps': found,
            'codexThreadId': thread, 'codexTurnId': turn,
            'matchingSessionAndCompletedTurn': True}


def observe_existing(directory, state):
    """No Dispatcher, login, reconciliation, initialization or persistent writes."""
    result = {'mode': 'observation_only', 'newSubmissions': 0, 'nativeWritesThisInvocation': 0,
              'phase': state.get('phase'), 'task': None}
    if not state.get('task', {}).get('taskId'):
        return {**result, 'observation': 'missing_task_identity'}
    if not (directory / 'tasks.sqlite').exists():
        return {**result, 'observation': 'missing_task_database'}
    with ro(directory / 'tasks.sqlite') as db:
        row = db.execute('SELECT * FROM tasks WHERE id=?', (state['task']['taskId'],)).fetchone()
        result['task'] = dict(row) if row else None
        result['activeLeaseCount'] = db.execute('SELECT count(*) FROM workspace_leases').fetchone()[0]
    with ro(directory / 'roles.sqlite') as db:
        delivery = db.execute('SELECT parent_id FROM deliveries WHERE task_id=?', (state['task']['taskId'],)).fetchone()
    if delivery and delivery['parent_id']:
        credentials = read(directory / 'host-auth.json')
        with httpx.Client(timeout=60, trust_env=False) as client:
            response = client.get(state['baseURL'] + '/api/console/chat/task/' + delivery['parent_id'],
                                  headers=credentials, follow_redirects=False)
            result['parentHTTPStatus'] = response.status_code
            if response.status_code == 200:
                result['parent'] = response.json()
    return result


def wait_task(dispatcher, store, ledger, task_id, timeout, persist, *, clock=time.monotonic, sleep=time.sleep):
    deadline = clock() + timeout + 300
    while clock() < deadline:
        task = dispatcher.reconcile(task_id); persist(task)
        if task['executionStatus'] in ('succeeded', 'failed', 'canceled') and task['resourcesReleased']:
            return task
        parent = ledger.get(task_id).get('observation') or {}
        if (not task['executionIdentity'] and parent.get('status') == 'finished'
                and parent.get('result', {}).get('status') == 'failed'
                and task['executionStatus'] != 'cancel_requested'):
            store.request_cancel(task_id)
        sleep(2)
    store.request_cancel(task_id)
    for _ in range(90):
        task = dispatcher.reconcile(task_id); persist(task)
        if task['resourcesReleased']:
            break
        sleep(2)
    raise RuntimeError('deadline_original_cancel_intent_retained_never_resubmit')


def run_probe(*, execute=False, directory=DATA):
    path = directory / 'state.json'
    if path.exists():
        return observe_existing(directory, read(path))
    if not execute:
        return {'status': 'not_executed', 'newSubmissions': 0, 'nativeWritesThisInvocation': 0}
    if directory.exists():
        raise RuntimeError('preexisting_unowned_probe_directory')
    process = read(ROOT / 'var/qwenpaw/agent-line-process.json')
    quiet_host(process['pid'])
    plugin = installed_plugin_ready(ROOT, process)
    before = main_snapshot(ROOT)
    birth = psutil.Process(process['pid']).create_time()
    config = read(ROOT / 'var/qwenpaw/config.json')
    bridge = config['plugins']['agent-line']['execution_bridge']
    base = 'http://127.0.0.1:' + str(process['port'])
    gateway = None
    with httpx.Client(timeout=60, trust_env=False) as host:
        login = host.post(base + '/api/auth/login', json=read(ROOT / 'var/qwenpaw-secrets/local-login.json'))
        login.raise_for_status()
        token = login.json().get('token') or login.json().get('access_token')
        headers = {'Authorization': 'Bearer ' + token, 'X-Agent-Id': ROLE}
        def get(route):
            response = host.get(base + '/api/' + route, headers=headers, follow_redirects=False)
            response.raise_for_status(); return response.json()
        source_file = 'agent-line-task-' + SOURCE_TASK + '.md'
        proof = source_memory_proof(SOURCE, birth, get('workspace/memory/' + source_file + '?section=digest')['content'])
        profile = get('agents/' + ROLE)
        if (profile.get('workspace_dir') != proof['roleWorkspace']
                or profile.get('running', {}).get('memory_manager_backend') != 'remelight'):
            raise RuntimeError('original_native_reme_role_required')
        service_token = (ROOT / 'var/qwenpaw-secrets/execution-service-token').read_text().strip()
        descriptor_response = host.get(base + '/api/agent-line/execution-binding',
            headers={**headers, 'X-Agent-Line-Service-Token': service_token}, follow_redirects=False)
        descriptor_response.raise_for_status(); descriptor = descriptor_response.json()
        revision = validate_role_revision(descriptor.get('roleRevisions', {}).get(ROLE))
        if descriptor.get('workspace') != bridge['workspace'] or ROLE not in descriptor.get('managedRoleIds', []):
            raise RuntimeError('original_execution_binding_required')
        suffix = uuid.uuid4().hex[:16]; artifact = 'native-after-restart-' + suffix + '.txt'
        workspace = Path(bridge['workspace']).resolve(strict=True)
        if (workspace / artifact).exists():
            raise RuntimeError('fresh_artifact_required')
        instruction = instruction_for(proof, artifact)
        directory.mkdir(mode=0o700)
        state = {'phase': 'prepared', 'source': proof, 'mainBefore': before, 'pluginReadiness': plugin,
                 'baseURL': base, 'roleRevision': revision, 'artifact': artifact,
                 'roleProfileBefore': profile, 'task': {}, 'parentTimeoutSeconds': 900}
        persist = lambda: write(path, state)
        persist(); write(directory / 'host-auth.json', headers)
        store = TaskStore.initialize(directory / 'tasks.sqlite')
        ledger = RoleDispatchLedger.initialize(directory / 'roles.sqlite')
        gateway = RoleDispatchGateway(base, token, service_token, role_agent_id=ROLE,
            deployment_id='agent-line-g0-installation', ledger=ledger, memory_query=proof['query'],
            client=host, timeout_seconds=900)
        try:
            payload = {'projectId': PROJECT, 'roleAgentId': ROLE, 'roleBindingId': BINDING,
                'roleRevision': revision, 'workspace': str(workspace),
                'workflow': {'id': PROJECT, 'revision': '1', 'runId': suffix,
                             'nodeId': 'after-restart', 'nodeExecutionId': suffix, 'attemptId': '1'},
                'input': {'instruction': instruction}}
            task, _ = store.register('native-after-restart-' + suffix, payload)
            state['task'] = {'taskId': task['id'], 'roleSessionId': 'agent-line-' + task['id']}; persist()
            dispatcher = Dispatcher(store, {PROJECT: gateway})
            submit_once(dispatcher, state, 'task', persist)
            def persist_task(task):
                state['lastTask'] = task; persist()
            task = wait_task(dispatcher, store, ledger, task['id'], 900, persist_task)
            if task['executionStatus'] != 'succeeded':
                raise RuntimeError('original_task_failed_no_replacement')
            state['exitProof'] = check_exit(task, bridge); persist()
            artifacts = directory / 'artifacts'; artifacts.mkdir(mode=0o700)
            expected = (proof['marker'] + '\n').encode()
            receipt = verify_artifacts(store, task['id'], workspace=workspace,
                policies=[ArtifactPolicy(artifact, hashlib.sha256(expected).hexdigest(), len(expected))], evidence_dir=artifacts)
            store.record_verification(task['id'], receipt)
            if receipt['status'] != 'passed':
                raise RuntimeError('independent_exact_bytes_failed')
            for _ in range(90):
                parent = ledger.get(task['id'])
                if (parent.get('observation') or {}).get('status') == 'finished':
                    break
                gateway.observe(task['id']); time.sleep(1)
            state['parent'] = parent
            with ro(bridge['receipt_db']) as db:
                binding = db.execute('SELECT role_id,session_id,role_revision FROM role_bindings WHERE task_id=?',
                                     (task['id'],)).fetchone()
            if (not binding or tuple(binding) != (ROLE, 'agent-line-' + task['id'], revision)
                    or parent.get('payload', {}).get('roleRevision') != revision):
                raise RuntimeError('original_frozen_role_revision_unproven')
            state['chain'] = verify_chain(parent.get('observation') or {}, proof['marker'], proof['sourceFile'], task['id'], task['executionIdentity'])
            state['nativeInput'] = native_input_proof(bridge['codex_home'], task, proof['marker'], artifact)
            after_profile = get('agents/' + ROLE)
            if after_profile != profile:
                raise RuntimeError('original_native_role_configuration_changed')
            after = main_snapshot(ROOT)
            if after != before or psutil.Process(process['pid']).create_time() != birth:
                raise RuntimeError('original_main_or_host_identity_changed')
            with ro(directory / 'tasks.sqlite') as db:
                if db.execute('SELECT count(*) FROM workspace_leases').fetchone()[0]:
                    raise RuntimeError('probe_workspace_lease_not_released')
            state.update(phase='verified', verified=True, lastTask=store.get(task['id']), mainAfter=after,
                secondPromptContainsMarker=False, nativeMemoryBackend='remelight')
            persist()
            return {'status': 'verified', 'sourceTaskId': SOURCE_TASK, 'taskId': task['id'], 'newSubmissions': 1}
        except Exception as exc:
            code = str(exc)
            state.update(phase='stopped', failure={'type': type(exc).__name__,
                'code': code if isinstance(exc, RuntimeError) and code.replace('_', '').isalnum() else 'inspect_original_evidence'})
            persist(); write(directory / 'last-error.json', state['failure'])
            raise
        finally:
            if gateway is not None:
                gateway.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    try:
        result = run_probe(execute=args.execute)
        print(json.dumps({k: v for k, v in result.items() if k not in ('task', 'parent')}, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'status': 'stopped', 'errorType': type(exc).__name__, 'evidence': str(DATA)}))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
