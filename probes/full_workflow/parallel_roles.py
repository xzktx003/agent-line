#!/usr/bin/env python3
"""Real static Sim branches using the original QwenPaw role and native memory.

An explicit --execute first invocation alone can submit. Without it, readiness
checks never submit. Every invocation with state.json is GET-only;
unknown create/start/parent/resume intentions are retained without retry. Graph
branches overlap in Sim, while the existing workspace lease serializes Codex.
This probe installs nothing and never changes main admission or role settings.
"""
import argparse
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import httpx
import psutil
from fastapi.testclient import TestClient

from agent_line.adapters.sim import SimClient
from agent_line.approval_discovery import WorkflowRunLedger
from agent_line.approvals import TrustedPrincipal
from agent_line.authoring_api import AuthoringAPI, AuthoringAccess, state_hash
from agent_line.automatic_verification import AutomaticVerifier
from agent_line.completion_relay import frozen_result_matches
from agent_line.dispatcher import Dispatcher
from agent_line.pause_bindings import PauseBindings
from agent_line.role_dispatch import RoleDispatchGateway, RoleDispatchLedger
from agent_line.role_revision import validate_role_revision
from agent_line.service import ServiceConfig, create_app
from agent_line.sim_authoring import SimAuthoringClient, compile_document, native_id
from agent_line.sim_journal import SimJournal
from agent_line.store import TaskStore, canonical
from agent_line.task_flow_runtime import load_task_flow
from agent_line.verification_policy import policy_hash
from agent_line.workflows import WorkflowGateway
from probes.qwenpaw_memory.native_roundtrip import quiet_host
from probes.sim.authoring_probe import write

DATA = ROOT / 'var/pinned-parallel-role-flow'
PROJECT = 'native-memory-roundtrip'
ROLE = 'agent-line-native-loop-parent'
BRANCHES = ('left', 'right')
BINDINGS = {branch: 'native-parallel-' + branch for branch in BRANCHES}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def document():
    def node(nid, kind, config, x, y=0):
        return {'id': nid, 'type': kind, 'name': nid,
                'position': {'x': x, 'y': y}, 'config': config}
    def agent(branch):
        return {'roleBindingId': BINDINGS[branch],
                'instruction': (
                    'Read ONLY taskInput.branches.' + branch + ' as your assigned work item. '
                    'Its artifact, marker and nonce belong to this branch; ignore the other branch. '
                    'Delegate to Codex to write ONLY that relative artifact in its assigned workspace, '
                    'with exactly marker plus one newline, then shell-check its bytes. '
                    'Do not substitute markers from memory or alter verifier policy. '
                    'Do not modify other files, network, memory configuration or permissions; '
                    'do not guess an absolute cwd. Report the assigned branch and nonce.'),
                'inputFrom': {'nodeId': 'start', 'field': 'input'},
                'memoryPolicyId': 'native-role', 'outputSchemaId': 'task-result',
                'requireMemoryCommit': False, 'allowFailureBranch': True}
    return {'schemaVersion': 1, 'variables': {}, 'nodes': [
        node('start', 'start', {}, 0),
        node('fork', 'parallel', {'branches': list(BRANCHES), 'join': 'merge',
                                  'maxConcurrency': 2}, 200),
        node('left', 'agent', agent('left'), 400),
        node('right', 'agent', agent('right'), 400, 220),
        node('merge', 'join', {'parallelId': 'fork', 'policy': 'all_success',
                             'requiredBranches': []}, 650),
        node('end', 'end', {'resultFrom': 'merge'}, 900)], 'edges': [
            {'id': 'a', 'source': 'start', 'target': 'fork'},
            {'id': 'b', 'source': 'fork', 'target': 'left'},
            {'id': 'c', 'source': 'fork', 'target': 'right'},
            {'id': 'd', 'source': 'left', 'target': 'merge'},
            {'id': 'e', 'source': 'right', 'target': 'merge'},
            {'id': 'f', 'source': 'merge', 'target': 'end'}]}


def work_items(suffix):
    if not isinstance(suffix, str) or len(suffix) != 16 or any(c not in '0123456789abcdef' for c in suffix):
        raise ValueError('bounded_suffix_required')
    return {branch: {'branch': branch, 'artifact': 'parallel-' + suffix + '-' + branch + '.txt',
                     'nonce': suffix + '-' + branch,
                     'marker': 'PINNED_PARALLEL_' + suffix + '_' + branch.upper()}
            for branch in BRANCHES}


def policies(items, revision, suffix):
    result = {}
    for branch in BRANCHES:
        item = items[branch]
        content = (item['marker'] + '\n').encode()
        result[BINDINGS[branch]] = {
            'projectId': PROJECT, 'roleAgentId': ROLE, 'roleRevision': revision,
            'verificationPolicy': {'schemaVersion': 1, 'policyId': 'parallel-bytes-' + suffix + '-' + branch,
                'kind': 'artifact_sha256', 'requireTests': False, 'artifacts': [
                    {'path': item['artifact'], 'sha256': hashlib.sha256(content).hexdigest(),
                     'exactSize': len(content), 'maxBytes': 1024}]}}
    return result


def once(state, key, operation, persist):
    if key in state['steps']:
        raise RuntimeError('operation_already_has_intent')
    state['steps'][key] = {'status': 'intent'}
    persist()
    result = operation()
    state['steps'][key] = {'status': 'received', 'result': result}
    persist()
    return result


def installed_plugin_ready(root, process):
    """Disk equality alone does not prove the running host loaded that deployment."""
    source = root / 'plugins/agent-line'
    installed = root / 'var/qwenpaw/plugins/agent-line'
    sources = sorted(source.glob('*.py'))
    if not sources or {p.name for p in sources} != {p.name for p in installed.glob('*.py')}:
        raise RuntimeError('installed_plugin_source_mismatch')
    compared = {}
    for path in sources:
        actual = installed / path.name
        if digest(path) != digest(actual):
            raise RuntimeError('installed_plugin_source_mismatch')
        compared[path.name] = digest(path)
    running = psutil.Process(process['pid'])
    stat = (Path('/proc') / str(process['pid']) / 'stat').read_text()
    if stat[stat.rfind(')') + 1:].split()[19] != process['start_ticks']:
        raise RuntimeError('qwenpaw_process_identity_changed')
    if running.create_time() <= max((installed / path.name).stat().st_mtime for path in sources):
        raise RuntimeError('running_plugin_deployment_unproven')
    return {'sourceFileCount': len(compared), 'sourceSha256': compared,
            'runningDeploymentLoadedAfterInstall': True, 'processId': process['pid'],
            'processStartTicks': process['start_ticks']}


def main_snapshot(root):
    baseline = json.loads((root / 'var/native-permissions-baseline.json').read_text())
    files = {name: digest(root / name) for name in baseline['files']}
    if files != baseline['files']:
        raise RuntimeError('main_configuration_baseline_changed')
    path = root / 'var/adapter/service.json'
    config = json.loads(path.read_text())
    database = Path(config['database'])
    if not database.is_absolute():
        database = path.parent / database
    store = TaskStore(database)
    guard = store.maintenance_status()
    if guard != baseline['mainGuard']:
        raise RuntimeError('main_guard_baseline_changed')
    with store.connect() as db:
        leases = db.execute('SELECT count(*) FROM workspace_leases').fetchone()[0]
        active = db.execute("SELECT count(*) FROM tasks WHERE execution_status NOT IN "
                            "('succeeded','failed','canceled') OR resources_released=0").fetchone()[0]
    if leases or active:
        raise RuntimeError('main_adapter_not_quiet')
    return {'files': files, 'guard': guard, 'mainActiveTaskCount': active, 'mainLeaseCount': leases}


def observe_existing(directory, state, sim, calls):
    """No host login, service app, dispatcher or store write is opened on repeat."""
    result = {'mode': 'observation_only', 'newSubmissions': 0,
              'nativeWritesThisInvocation': 0, 'tasks': []}
    if state.get('workflowId') and state.get('runId'):
        targets = [state['runId']]
        if state.get('finalRunId'):
            targets.append(state['finalRunId'])
        result['runs'] = {rid: sim.observe(state['workflowId'], rid) for rid in dict.fromkeys(targets)}
    task_file = directory / 'tasks.sqlite'
    if task_file.exists():
        # TaskStore.connect commits and configures rw; observation instead opens true SQLite ro.
        with sqlite3.connect(task_file.resolve().as_uri() + '?mode=ro', uri=True) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute('SELECT * FROM tasks ORDER BY id').fetchall()
            result['tasks'] = [dict(row) for row in rows]
            result['activeLeaseCount'] = db.execute('SELECT count(*) FROM workspace_leases').fetchone()[0]
    writes = sum(call['method'] != 'GET' for call in calls)
    if writes:
        raise RuntimeError('observation_must_not_write')
    result['nativeCalls'] = calls
    write(directory / 'observation.json', result)
    return {'mode': 'observation_only', 'newSubmissions': 0, 'nativeWritesThisInvocation': 0,
            'taskCount': len(result['tasks'])}


def check_frozen(branch, task, parent, state, run_snapshot, bindings, bridge):
    wait = native_id(state['workflowId'], branch)
    if (task['payload']['roleBindingId'] != BINDINGS[branch]
            or task['payload']['workflow']['nodeExecutionId'] != wait
            or task['payload']['input']['taskInput'] != state['input']
            or task['payload']['input']['instruction'] != next(
                n['config']['instruction'] for n in state['document']['nodes'] if n['id'] == branch)):
        raise RuntimeError('branch_task_scope_mismatch')
    pinned = {'publication': state['publication']['externalTasks']['agentPolicies'][wait]['roleRevision'],
              'run': run_snapshot['agentPolicies'][wait]['roleRevision'],
              'task': task['payload']['roleRevision'], 'parent': parent['payload']['roleRevision']}
    dbpath = Path(bridge['receipt_db']).resolve(strict=True)
    with sqlite3.connect(dbpath.as_uri() + '?mode=ro', uri=True) as db:
        row = db.execute('SELECT role_id,session_id,role_revision FROM role_bindings WHERE task_id=?',
                         (task['id'],)).fetchone()
    if not row or row[:2] != (ROLE, 'agent-line-' + task['id']):
        raise RuntimeError('plugin_binding_identity_mismatch')
    pinned['pluginBinding'] = row[2]
    if any(pin != state['roleRevision'] for pin in pinned.values()):
        raise RuntimeError('frozen_role_revision_mismatch')
    frozen_policies = [state['publication']['externalTasks']['agentPolicies'][wait]['verificationPolicy'],
                       run_snapshot['agentPolicies'][wait]['verificationPolicy'],
                       task['payload']['verificationPolicy']]
    expected = policy_hash(bindings[BINDINGS[branch]]['verificationPolicy'])
    if any(policy_hash(policy) != expected for policy in frozen_policies):
        raise RuntimeError('frozen_verification_policy_mismatch')
    return {'roleRevisions': pinned, 'verificationPolicySha256': expected,
            'roleSessionId': row[1], 'waitContextId': wait}


def check_exit(task, bridge):
    identity = task['executionIdentity']
    if not all(isinstance(identity.get(k), str) and identity[k] for k in ('upstreamTaskId', 'codexThreadId', 'codexTurnId')):
        raise RuntimeError('actual_codex_identity_missing')
    exits = [e for e in task['evidence'] if e.get('kind') == 'resource_exit']
    if task['resourcesReleased'] is not True or len(exits) != 1:
        raise RuntimeError('actual_container_exit_missing')
    call = [sys.executable, bridge['container_control'], 'observe', '--thread', identity['codexThreadId'],
            '--turn', identity['codexTurnId'], '--workspace', bridge['workspace']]
    completed = subprocess.run(call, capture_output=True, text=True, check=True, timeout=25)
    proof = json.loads(completed.stdout)
    launcher = proof.get('launcherExitProof') or {}
    candidate = proof.get('exitReceipt') or {}
    if (proof.get('resourcesReleased') is not True or proof.get('codexThreadId') != identity['codexThreadId']
            or proof.get('codexTurnId') != identity['codexTurnId']
            or candidate != exits[0].get('receipt') or exits[0].get('executionIdentity') != identity
            or candidate.get('source') != 'docker-inspect-before-remove'
            or candidate.get('workspace') != bridge['workspace'] or candidate.get('running') is not False
            or candidate.get('pid') != 0 or candidate.get('status') not in ('exited', 'dead')
            or not candidate.get('finishedAt')
            or launcher.get('source') != 'procfs-launcher-exit'
            or launcher.get('state') not in ('absent', 'pid_reused')
            or launcher.get('owner') != candidate.get('owner')
            or launcher.get('containerId') != candidate.get('containerId')):
        raise RuntimeError('launcher_and_container_exit_unproven')
    with sqlite3.connect(Path(bridge['receipt_db']).resolve(strict=True).as_uri() + '?mode=ro', uri=True) as db:
        row = db.execute('SELECT payload,record FROM receipts WHERE task_id=?', (task['id'],)).fetchone()
    if row is None:
        raise RuntimeError('original_plugin_receipt_missing')
    payload, record = (json.loads(value) for value in row)
    retired = record.get('runtimeRetirement')
    expected = {'schemaVersion': 1, 'source': 'qwenpaw-harness-runtime-stop',
                'taskId': task['id'], 'targetAgentId': bridge['target_agent_id'],
                'workspace': bridge['workspace'], 'identity': identity,
                'owner': candidate['owner'], 'containerId': candidate['containerId']}
    if (payload.get('taskId') != task['id'] or payload.get('roleSessionId') != 'agent-line-' + task['id']
            or payload.get('targetAgentId') != bridge['target_agent_id'] or payload.get('workspace') != bridge['workspace']
            or record.get('taskId') != task['id'] or record.get('identity') != identity
            or record.get('terminalConfirmed') is not True or record.get('resourcesReleased') is not True
            or record.get('resourceExitReceipt') != candidate or record.get('status') != 'succeeded'
            or not isinstance(retired, dict) or type(retired.get('schemaVersion')) is not int
            or retired not in ({**expected, 'state': 'stopped'}, {**expected, 'state': 'unloaded'})):
        raise RuntimeError('original_runtime_retirement_unproven')
    proof['runtimeRetirement'] = retired
    return proof


def serialized_events(store, tasks):
    """The existing lease releases after bytes verification, before the next claim."""
    with store.connect() as db:
        events = [dict(row) for row in db.execute('SELECT * FROM events ORDER BY seq')]
        leases = db.execute('SELECT count(*) FROM workspace_leases').fetchone()[0]
    claims = [e for e in events if e['kind'] == 'submission_intent']
    if len(claims) != 2 or {e['task_id'] for e in claims} != {t['id'] for t in tasks}:
        raise RuntimeError('expected_two_original_claims')
    first, second = claims
    between = [e for e in events if first['seq'] < e['seq'] < second['seq']
               and e['task_id'] == first['task_id']]
    if (leases or not any(e['kind'] == 'resources_released' for e in between)
            or not any(e['kind'] == 'verification_passed' for e in between)):
        raise RuntimeError('shared_workspace_serialization_unproven')
    return {'firstTaskId': first['task_id'], 'secondTaskId': second['task_id'],
            'firstSubmissionSeq': first['seq'], 'secondSubmissionSeq': second['seq'],
            'firstResourcesReleasedBeforeSecondSubmission': True,
            'firstVerificationPassedBeforeSecondSubmission': True,
            'activeLeaseCount': leases, 'simStaticBranches': 2, 'concurrentWorkspaceWriters': False}


def check_contexts(runtime, journal, state, tasks):
    wid, run = state['workflowId'], state['runId']
    projection = runtime.discovery.wait_client.read_pauses(wid, run)
    expected = {native_id(wid, branch) for branch in BRANCHES}
    points = projection['pausePoints']
    if (projection['totalPauseCount'] != 2 or projection['resumedCount'] != 2 or len(points) != 2
            or {p['contextId'] for p in points} != expected
            or any(p['contextId'] != p['blockId'] or p['resumeStatus'] != 'resumed' for p in points)):
        raise RuntimeError('original_two_native_pause_contexts_unproven')
    for branch, task in tasks.items():
        wait = native_id(wid, branch)
        binding = runtime.pauses.get(task['id'])
        frozen = journal.resume_input(wid, run, wait)
        if (binding.task_id != task['id'] or binding.workflow_id != wid or binding.run_id != run
                or binding.context_id != wait or binding.block_id != wait or binding.allow_multiple is not True
                or journal.get(wid, run, wait)['status'] != 'accepted' or not frozen_result_matches(task, frozen)):
            raise RuntimeError('original_branch_pause_or_resume_result_mismatch')
    return projection


def check_native_end(sim, state, tasks):
    wid, child = state['workflowId'], state['finalRunId']
    selectors = ','.join([native_id(wid, 'merge'), native_id(wid, 'end') + '.data'])
    response = sim._client.get(sim.base_url + '/api/v2/workflows/' + wid + '/runs/' + child,
                              params={'selectedOutputs': selectors}, headers={'x-api-key': sim._key},
                              follow_redirects=False)
    response.raise_for_status()
    native = response.json()['data']
    if native.get('workflowId') != wid or native.get('runId') != child or native.get('status') != 'completed':
        raise RuntimeError('native_child_identity_or_status_mismatch')
    blocks = native['blockOutputs']
    result = blocks[native_id(wid, 'merge')]['result']
    end = blocks[native_id(wid, 'end') + '.data']['agentLineResult']
    if (result != end or result.get('executionStatus') != 'succeeded'
            or result.get('verificationStatus') != 'passed' or result.get('output', {}).get('policy') != 'all_success'
            or set(result['output'].get('successfulBranches', [])) != set(BRANCHES)
            or set(result['output'].get('branches', {})) != set(BRANCHES)):
        raise RuntimeError('native_join_result_mismatch')
    for branch in BRANCHES:
        item = result['output']['branches'][branch]
        task = tasks[branch]
        if (item.get('taskId') != task['id'] or item.get('executionStatus') != 'succeeded'
                or item.get('verificationStatus') != 'passed' or item.get('output') != task['output']):
            raise RuntimeError('native_branch_result_mismatch')
    return native


def main(*, execute=False):
    if type(execute) is not bool:
        raise ValueError('explicit_execute_boolean_required')
    DATA.mkdir(mode=0o700, exist_ok=True)
    path = DATA / 'state.json'
    auth = json.loads((ROOT / 'var/sim/probe-auth.json').read_text())
    sim = SimClient(auth['baseUrl'], auth['apiKey'])
    calls = []
    sim._client.event_hooks['request'].append(lambda r: calls.append({'method': r.method, 'path': r.url.path}))
    gateway = host = None
    state = None
    try:
        if path.exists():
            print(json.dumps(observe_existing(DATA, json.loads(path.read_text()), sim, calls)))
            return 0
        process = json.loads((ROOT / 'var/qwenpaw/agent-line-process.json').read_text())
        quiet_host(process['pid'])
        plugin_proof = installed_plugin_ready(ROOT, process)
        before = main_snapshot(ROOT)
        subprocess.run([sys.executable, 'scripts/check_upstreams.py'], cwd=ROOT,
                       check=True, capture_output=True, text=True)
        config = json.loads((ROOT / 'var/qwenpaw/config.json').read_text())
        bridge = config['plugins']['agent-line']['execution_bridge']
        for key, filename in [('container_control', 'control.py'), ('container_launcher', 'launcher.py')]:
            if Path(bridge[key]).resolve(strict=True) != ROOT / 'deploy/executor' / filename:
                raise RuntimeError('current_owned_executor_source_required')
        if 'launcherExitProof' not in Path(bridge['container_control']).read_text():
            raise RuntimeError('launcher_exit_gate_source_required')
        workspace = Path(bridge['workspace']).resolve(strict=True)
        bridge = {**bridge, 'workspace': str(workspace)}
        prior = json.loads((ROOT / 'var/native-memory-roundtrip/attempt4-report.json').read_text())
        prior_state = json.loads((ROOT / 'var/native-memory-roundtrip/state-attempt-4.json').read_text())
        if prior.get('verified') is not True or prior.get('activeLeaseCount') != 0 or prior.get('ownedActiveContainerCount') != 0:
            raise RuntimeError('prior_native_memory_proof_required')
        base = 'http://127.0.0.1:' + str(process['port'])
        host = httpx.Client(timeout=60, trust_env=False)
        login = host.post(base + '/api/auth/login', json=json.loads((ROOT / 'var/qwenpaw-secrets/local-login.json').read_text()))
        login.raise_for_status()
        token = login.json().get('token') or login.json().get('access_token')
        qheaders = {'Authorization': 'Bearer ' + token, 'X-Agent-Id': ROLE}
        profile_response = host.get(base + '/api/agents/' + ROLE, headers=qheaders)
        profile_response.raise_for_status()
        profile = profile_response.json()
        if (profile.get('running', {}).get('memory_manager_backend') != 'remelight'
                or profile.get('workspace_dir') != prior_state['roleWorkspace']):
            raise RuntimeError('original_native_reme_role_required')
        service_token = (ROOT / 'var/qwenpaw-secrets/execution-service-token').read_text().strip()
        response = host.get(base + '/api/agent-line/execution-binding', headers={
            'Authorization': 'Bearer ' + token, 'X-Agent-Line-Service-Token': service_token})
        response.raise_for_status()
        descriptor = response.json()
        revision = validate_role_revision(descriptor.get('roleRevisions', {}).get(ROLE))
        if descriptor.get('workspace') != str(workspace) or ROLE not in descriptor.get('managedRoleIds', []):
            raise RuntimeError('descriptor_binding_mismatch')
        suffix = uuid.uuid4().hex[:16]
        items = work_items(suffix)
        if any((workspace / item['artifact']).exists() for item in items.values()):
            raise RuntimeError('fresh_artifact_required')
        doc = document()
        compile_document(doc, 'offline-preflight')
        if not execute:
            write(DATA / 'readiness.json', {'status': 'ready_not_executed', 'newSubmissions': 0,
                'nativeWritesThisInvocation': 0, 'pluginReadiness': plugin_proof, 'mainSnapshot': before,
                'roleRevision': revision, 'nativeMemoryBackend': 'remelight',
                'executionRequiresExplicitFlag': True})
            print(json.dumps({'status': 'ready_not_executed', 'newSubmissions': 0,
                              'nativeWritesThisInvocation': 0}), flush=True)
            return 0
        state = {'kind': 'real-static-parallel', 'steps': {}, 'createdAt': time.time(),
                 'suffix': suffix, 'input': {'branches': items}, 'document': doc,
                 'projectId': PROJECT, 'roleAgentId': ROLE, 'roleRevision': revision,
                 'pluginReadiness': plugin_proof, 'mainBefore': before,
                 'nativeMemory': {'backend': 'remelight', 'roleWorkspace': prior_state['roleWorkspace']},
                 'executorSourceSha256': {key: digest(bridge[key]) for key in ('container_control', 'container_launcher')},
                 'tasks': {}}
        persist = lambda: write(path, state)
        persist()
        for filename, cls in [('tasks.sqlite', TaskStore), ('roles.sqlite', RoleDispatchLedger),
                              ('sim.sqlite', SimJournal), ('runs.sqlite', WorkflowRunLedger), ('pauses.sqlite', PauseBindings)]:
            cls.initialize(DATA / filename)
        store = TaskStore(DATA / 'tasks.sqlite')
        ledger = RoleDispatchLedger(DATA / 'roles.sqlite')
        journal = SimJournal(DATA / 'sim.sqlite')
        api = SimAuthoringClient(sim, auth['workspaceId'])
        projects = {PROJECT: {'workspace': str(workspace)}}
        bindings = policies(items, revision, suffix)
        state['verificationPolicies'] = {b: bindings[BINDINGS[b]]['verificationPolicy'] for b in BRANCHES}
        author_token = secrets.token_urlsafe(40)
        access = AuthoringAccess({author_token: TrustedPrincipal('real-parallel-probe-editor',
            frozenset({PROJECT}), frozenset({'edit_workflows', 'publish_workflows'}), time.time() + 7200)})
        authoring = AuthoringAPI(access, journal, {PROJECT: api}, projects=projects, role_bindings=bindings)
        write(DATA / 'task-flow.json', {'schemaVersion': 1, 'runLedgerPath': str(DATA / 'runs.sqlite'),
                                       'pauseBindingsPath': str(DATA / 'pauses.sqlite')})
        gateway = RoleDispatchGateway(base, token, service_token, role_agent_id=ROLE,
            deployment_id='agent-line-g0-installation', ledger=ledger, memory_query=prior_state['query'],
            client=host, timeout_seconds=900)
        dispatcher = Dispatcher(store, {(PROJECT, binding): gateway for binding in BINDINGS.values()})
        runtime = load_task_flow(DATA / 'task-flow.json', tasks=store, projects=projects,
                                role_bindings=bindings, client=sim, journal=journal)
        workflow = WorkflowGateway(sim, journal, {}, installation_id='pinned-parallel-' + suffix)
        workflow.task_flow = runtime
        workflow.publication_verifier = runtime.discovery.publication_verifier
        evidence_dir = DATA / 'verification-evidence'
        evidence_dir.mkdir(mode=0o700)
        credential = secrets.token_urlsafe(40)
        app = create_app(store, ServiceConfig(tokens={credential: {PROJECT}}, projects=projects,
            bindings=bindings, controlled_execution='confirmed'), workflows=workflow, dispatcher=dispatcher,
            task_flow=runtime, authoring=authoring, verifier=AutomaticVerifier(store, evidence_dir))
        headers = {'Authorization': 'Bearer ' + credential}
        with TestClient(app) as http:
            def author(key, route, payload):
                def request():
                    response = http.post('/extension/v1/authoring/projects/' + PROJECT + '/workflows' + route,
                                         headers={'Authorization': 'Bearer ' + author_token}, json=payload)
                    if response.status_code != 202 or response.json().get('status') != 'accepted':
                        raise RuntimeError('authoring_not_accepted')
                    return response.json()['receipt']
                return once(state, key, request, persist)
            created = author('create', '', {'operationId': 'create-' + suffix, 'name': 'agent-line-real-static-parallel',
                'description': 'Two static Sim branches; original native ReMe role; shared workspace serialized Codex'})
            wid = state['workflowId'] = created['workflowId']
            persist()
            saved = author('save', '/' + wid + '/save', {'operationId': 'save-' + suffix,
                'expectedStateSha256': state_hash(api.get_state(wid)), 'document': doc})
            if saved.get('outcome') != 'saved':
                raise RuntimeError('save_not_verified')
            published = author('publish', '/' + wid + '/publish', {'operationId': 'publish-' + suffix,
                'expectedStateSha256': state_hash(api.get_state(wid))})
            if published.get('outcome') != 'published' or published.get('externalTasks', {}).get('staticParallel') is not True:
                raise RuntimeError('static_publication_not_verified')
            binding = published['bindingId']
            state.update(publication=published, bindingId=binding)
            persist()
            quiet_host(process['pid'])
            if main_snapshot(ROOT) != before or installed_plugin_ready(ROOT, process) != plugin_proof:
                raise RuntimeError('pre_execution_main_or_plugin_changed')
            payload = {'idempotencyKey': 'real-parallel-' + suffix, 'input': {'input': state['input']}}
            run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, canonical([
                'pinned-parallel-' + suffix, PROJECT, wid, payload['idempotencyKey'], binding])))
            state.update(runId=run_id, startPayload=payload)
            persist()
            def start():
                response = http.post('/extension/v1/workflows/' + binding + '/runs', headers=headers, json=payload)
                if response.status_code != 202 or response.json().get('status') != 'accepted':
                    raise RuntimeError('start_not_accepted')
                return response.json()
            once(state, 'start', start, persist)
            state['frozenPublication'] = journal.get_publication(wid, run_id)
            persist()
            deadline, last_print = time.monotonic() + 1800, 0
            while time.monotonic() < deadline:
                tasks = store.list_tasks({PROJECT})['tasks']
                if len(tasks) > 2:
                    raise RuntimeError('unexpected_task_count')
                state['tasks'] = {task['id']: task for task in tasks}
                if any(t['executionStatus'] in ('failed', 'canceled') or t['verificationStatus'] == 'failed' for t in tasks):
                    raise RuntimeError('original_branch_execution_or_verification_failed')
                if len(tasks) == 2 and all(t['verificationStatus'] == 'passed' and t['resourcesReleased'] for t in tasks):
                    summary = journal.resume_summary(wid, run_id)
                    if summary and summary['latestAttempt'] == summary['latestAccepted'] and summary['latestAccepted']['runId']:
                        child = summary['latestAccepted']['runId']
                        observed = sim.observe(wid, child)
                        if observed.get('status') == 'completed':
                            state.update(finalRunId=child, finalObservation=observed)
                            persist()
                            break
                if time.monotonic() - last_print >= 30:
                    print(json.dumps({'phase': 'original_branch_observation', 'taskCount': len(tasks),
                                      'taskStatuses': [{k: t[k] for k in ('id', 'executionStatus', 'verificationStatus', 'resourcesReleased')} for t in tasks]}), flush=True)
                    persist()
                    last_print = time.monotonic()
                time.sleep(1)
            else:
                raise RuntimeError('original_parallel_observation_timeout_no_resubmit')
            tasks = store.list_tasks({PROJECT})['tasks']
            by_branch = {branch: next(t for t in tasks if t['payload']['roleBindingId'] == BINDINGS[branch]) for branch in BRANCHES}
            run_snapshot = WorkflowRunLedger(DATA / 'runs.sqlite').get(wid, run_id)
            state['branches'] = {}
            for branch, task in by_branch.items():
                parent = ledger.get(task['id'])
                chain = check_frozen(branch, task, parent, state, run_snapshot, bindings, bridge)
                chain.update(task=task, parent=parent, executorExit=check_exit(task, bridge))
                for _ in range(90):
                    response = host.get(base + '/api/console/chat/task/' + parent['parentTaskId'], headers=qheaders)
                    response.raise_for_status()
                    observed = response.json()
                    if observed.get('status') == 'finished':
                        if observed.get('result', {}).get('status') != 'completed':
                            raise RuntimeError('original_parent_not_completed')
                        chain['parentObservation'] = observed
                        break
                    time.sleep(1)
                else:
                    raise RuntimeError('original_parent_not_finished')
                actual = (workspace / items[branch]['artifact']).read_bytes()
                expected = (items[branch]['marker'] + '\n').encode()
                if actual != expected:
                    raise RuntimeError('branch_artifact_bytes_mismatch')
                chain['actualArtifactSha256'] = hashlib.sha256(actual).hexdigest()
                state['branches'][branch] = chain
                persist()
            for field in ('upstreamTaskId', 'codexThreadId', 'codexTurnId'):
                if len({task['executionIdentity'][field] for task in tasks}) != 2:
                    raise RuntimeError('independent_execution_identity_unproven')
            if len({b['parent']['parentTaskId'] for b in state['branches'].values()}) != 2:
                raise RuntimeError('independent_parent_identity_unproven')
            for field in ('owner', 'containerId'):
                if len({b['executorExit'][field] for b in state['branches'].values()}) != 2:
                    raise RuntimeError('independent_launcher_container_identity_unproven')
            state['workspaceSerialization'] = serialized_events(store, tasks)
            state['nativeContexts'] = check_contexts(runtime, journal, state, by_branch)
            state['nativeJoinAndEnd'] = check_native_end(sim, state, by_branch)
            before_replay = sum(c['method'] != 'GET' for c in calls)
            replay = http.post('/extension/v1/workflows/' + binding + '/runs', headers=headers, json=payload)
            if replay.status_code != 202 or sum(c['method'] != 'GET' for c in calls) != before_replay:
                raise RuntimeError('idempotent_replay_native_write')
            if {t['id'] for t in store.list_tasks({PROJECT})['tasks']} != {t['id'] for t in tasks}:
                raise RuntimeError('idempotent_replay_created_tasks')
            state['duplicateNativeWrites'] = 0
        quiet_host(process['pid'])
        if main_snapshot(ROOT) != before:
            raise RuntimeError('main_configuration_or_guard_changed')
        if (installed_plugin_ready(ROOT, process) != plugin_proof
                or state['executorSourceSha256'] != {key: digest(bridge[key]) for key in ('container_control', 'container_launcher')}):
            raise RuntimeError('plugin_or_executor_source_changed_during_execution')
        state.update(result='passed_live', mainUnchanged=True)
        persist()
        write(DATA / 'report.json', {'status': 'passed_live', 'workflowId': wid, 'rootRunId': run_id,
            'finalRunId': state['finalRunId'], 'taskCount': 2, 'branchCount': 2,
            'nativeCalls': calls, 'activeLeaseCount': 0, 'ownedActiveContainerCount': 0,
            'mainUnchanged': True, 'nativeMemoryBackend': 'remelight',
            'roleRevisionFrozenFivePlaces': True, 'verificationPoliciesFrozen': True,
            'launcherAndContainerExitProven': True, 'runtimeRetirementProven': True,
            'distinctLauncherContainers': True, 'duplicateNativeWrites': 0,
            'limitations': ['Byte verification only; no independent test command in this case',
                'Static Sim branches share one workspace; original leases serialize real Codex writing',
                'Native ReMe may update normally; no manual memory writes or replacement backend',
                'One positive real fork/join run; does not establish all fault or upgrade acceptance']})
        print(json.dumps({'status': 'passed_live', 'taskCount': 2, 'activeLeaseCount': 0,
                          'concurrentWorkspaceWriters': False}), flush=True)
        return 0
    except Exception as error:
        failure = {'status': 'incomplete', 'errorType': type(error).__name__,
                   'code': str(error) if type(error) is RuntimeError else 'probe_failed',
                   'nativeCalls': calls, 'nativeWritesThisInvocation': sum(c['method'] != 'GET' for c in calls),
                   'noRetryAuthorized': True}
        if state is not None:
            state['failure'] = failure
            write(path, state)
        write(DATA / ('failure.json' if path.exists() else 'readiness.json'), failure)
        print(json.dumps({k: v for k, v in failure.items() if k != 'nativeCalls'}), flush=True)
        return 1
    finally:
        if gateway is not None:
            gateway.close()
        if host is not None:
            host.close()
        sim.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true', help='Allow only a brand-new once-only real run; existing state stays observation-only')
    raise SystemExit(main(**vars(parser.parse_args())))
