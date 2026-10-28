#!/usr/bin/env python3
"""Real independent test failures exhaust one three-round native Sim loop.

Default does not read credentials or create files. Existing state is always
observation-only, even with --execute. No role/profile/plugin/guard is changed.
The intentional subtraction artifact is byte-correct, but all three independent
addition unittest runs must exit 1; completing the loop is not business success.
"""
import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import time
import threading
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import httpx
from fastapi.testclient import TestClient

from agent_line.adapters.sim import SimClient
from agent_line.approval_discovery import WorkflowRunLedger
from agent_line.approvals import TrustedPrincipal
from agent_line.authoring_api import AuthoringAPI, AuthoringAccess, state_hash
from agent_line.automatic_verification import AutomaticVerifier
from agent_line.dispatcher import Dispatcher
from agent_line.pause_bindings import PauseBindings
from agent_line.role_dispatch import RoleDispatchGateway, RoleDispatchLedger
from agent_line.role_revision import validate_role_revision
from agent_line.service import ServiceConfig, create_app
from agent_line.sim_authoring import SimAuthoringClient, compile_document, decode_document, native_id
from agent_line.sim_journal import SimJournal
from agent_line.store import TaskStore, canonical
from agent_line.task_flow_runtime import load_task_flow
from agent_line.test_executor import DockerTestExecutor
from agent_line.verification_policy import policy_hash
from agent_line.workflows import WorkflowGateway
from probes.full_workflow.parallel_roles import installed_plugin_ready, main_snapshot, check_exit, once
from probes.qwenpaw_memory.native_roundtrip import quiet_host
from probes.sim.authoring_probe import write
from probes.sim.cron_delivery_live import backup_journal
from probes.sim.triggers_live import private_file, require

DATA = ROOT / 'var/pinned-test-loop-exhaustion'
SOURCE = ROOT / 'var/automatic-loop-flow'
PROJECT = 'native-memory-roundtrip'
BINDING = 'native-loop-parent'
ROLE = 'agent-line-native-loop-parent'
TEMPLATE = '22fdefa4-08fd-4ba5-bee6-53189ada57a2'
KIND = 'pinned-test-loop-exhaustion-v1'
CONTENT = b'def add(left, right):\n    return left - right\n'
FAILURE_LINE = 'FAILED (failures=2)'
INSTRUCTION = (
    'This is an explicitly authorized fault-injection bounded-loop test. Read the nested taskInput.input artifact. '
    'Delegate actual file work to Codex. At every zero-based repair.iteration 0,1,2, replace ONLY that dedicated '
    'relative .py file with exactly "def add(left, right):\\n    return left - right\\n"; '
    'the subtraction is deliberate even when prior addition tests fail. Do not fix it to addition. '
    'Shell-check exact bytes. Do not change other files, tests, network, permissions, memory, or guessed absolute paths. '
    'Independent trusted tests, rather than the model, decide verification. '
    'For each nonzero iteration copy the actual repair.previousResult.taskId, the failed test_command testId, '
    'the literal exitCode=1 and its actual stderr line FAILED (failures=2) into the agent_line_delegate instruction. '
    'These must come from the previousResult evidence; do not invent prior feedback. '
    'Do not ask Codex to call coordination or memory tools. The parent must not run shell or compute hashes.'
)


def document():
    def node(identifier, kind, config, x):
        return {'id': identifier, 'type': kind, 'name': identifier,
                'position': {'x': x, 'y': 0}, 'config': config}
    return {'schemaVersion': 1, 'variables': {}, 'nodes': [
        node('start', 'start', {}, 0),
        node('repair', 'loop', {'body': 'agent', 'maxIterations': 3}, 200),
        node('agent', 'agent', {'roleBindingId': BINDING, 'instruction': INSTRUCTION,
            'inputFrom': {'nodeId': 'start', 'field': 'input'}, 'memoryPolicyId': 'native-role',
            'outputSchemaId': 'task-result', 'requireMemoryCommit': False, 'allowFailureBranch': True}, 50),
        node('end', 'end', {'resultFrom': 'agent'}, 750)], 'edges': [
            {'id': 'a', 'source': 'start', 'target': 'repair'},
            {'id': 'b', 'source': 'repair', 'target': 'agent', 'branch': 'body'},
            {'id': 'c', 'source': 'repair', 'target': 'end', 'branch': 'done'}]}


def verification_policy(artifact, environment, suffix):
    code = ("import runpy, unittest\nadd = runpy.run_path(" + repr(artifact) + ")[\"add\"]\n"
            "class AddTests(unittest.TestCase):\n"
            "    def test_positive(self): self.assertEqual(add(2, 3), 5)\n"
            "    def test_negative(self): self.assertEqual(add(-2, 3), 1)\n"
            "    def test_zero(self): self.assertEqual(add(0, 0), 0)\n"
            "unittest.main(verbosity=2)\n")
    return {'schemaVersion': 2, 'policyId': 'three-test-loop-' + suffix,
            'kind': 'command_and_artifacts', 'requireTests': True, 'testEnvironmentId': environment,
            'artifacts': [{'path': artifact, 'sha256': hashlib.sha256(CONTENT).hexdigest(),
                           'exactSize': len(CONTENT), 'maxBytes': 1024}],
            'tests': [{'id': 'addition-unittests', 'argv': ['python3', '-c', code],
                       'timeoutSeconds': 60, 'maxOutputBytes': 65536}]}


def checked_template(source, sim, api):
    """Every tolerated historical pause must be in an exact accepted resume chain."""
    source = Path(source)
    require((source / 'root-check.json').is_file(), 'loop_root_check_missing')
    old, report, proof = (private_file(source / name) for name in ('state.json', 'report.json', 'root-check.json'))
    wid = old.get('workflowId')
    require(wid == TEMPLATE and old.get('result') == 'passed_live'
            and report.get('status') == 'passed_live' and report.get('workflowId') == wid
            and proof.get('status') == 'passed_read_only' and proof.get('workflowId') == wid
            and set(old.get('cases', {})) == {'early_success', 'exhausted'}
            and set(proof.get('cases', {})) == set(old['cases']), 'root_checked_template_required')
    require(api.get_detail(wid).get('id') == wid
            and compile_document(decode_document(api.get_state(wid)), wid) == compile_document(old['document'], wid),
            'known_loop_draft_changed')
    version = api.get_version(wid, old['publication']['revision'])
    require(version.get('id') == old['publication']['deploymentVersionId'] and version.get('isActive') is True
            and compile_document(decode_document(version['state']), wid) == compile_document(old['document'], wid),
            'known_loop_active_publication_changed')
    allowed, finals, roots = set(), set(), set()
    with sqlite3.connect((source / 'sim.sqlite').resolve().as_uri() + '?mode=ro', uri=True) as db:
        for name, case in old['cases'].items():
            checked = proof['cases'][name]
            require(case.get('result') == 'passed_live'
                    and checked['runId'] == case['runId'] and checked['finalRunId'] == case['finalRunId']
                    and report['cases'][name]['finalRunId'] == case['finalRunId'], 'old_case_proof_mismatch')
            tasks = sorted(case['tasks'].values(), key=lambda value: int(value['task']['payload']['workflow']['attemptId']))
            require(len(tasks) == (2 if name == 'early_success' else 3), 'old_round_count_mismatch')
            chain = [case['runId']]
            rows = db.execute('SELECT context,status,receipt FROM operations WHERE workflow=? AND run=?',
                              (wid, case['runId'])).fetchall()
            machine_rows = {context: (status, receipt) for context, status, receipt in rows if context.startswith(native_id(wid, 'agent') + '_loop')}
            require(len(machine_rows) == len(tasks), 'old_unknown_context_or_round')
            for iteration, entry in enumerate(tasks):
                context = native_id(wid, 'agent') + '_loop' + str(iteration)
                task = entry['task']
                require(task['payload']['workflow']['nodeExecutionId'] == context
                        and task['executionStatus'] == 'succeeded' and task['resourcesReleased'] is True
                        and any(e.get('kind') == 'resource_exit' for e in task['evidence'])
                        and task['verificationStatus'] in ('passed', 'failed'),
                        'old_activation_unconfirmed')
                status, encoded = machine_rows[context]
                receipt = json.loads(encoded) if encoded else {}
                require(status == 'accepted' and receipt.get('workflowId') == wid
                        and receipt.get('runId') == case['runId'] and receipt.get('contextId') == context
                        and entry['resumeReceipt']['receipt'] == receipt, 'old_resume_not_accepted')
                child = receipt.get('resumedRunId')
                require(isinstance(child, str) and child and child not in chain, 'old_child_identity_invalid')
                chain.append(child)
            require(chain[-1] == case['finalRunId'] and checked['acceptedChainRunIds'] == chain,
                    'old_root_chain_proof_mismatch')
            for index, run_id in enumerate(chain):
                observed = sim.observe(wid, run_id)
                require(observed.get('status') in (('completed',) if index == len(chain) - 1 else ('paused', 'completed')),
                        'old_accepted_chain_not_terminal')
                body = observed.get('response', {}).get('data', {})
                require(body.get('runId') == run_id and body.get('workflowId') == wid, 'old_chain_observation_identity_mismatch')
            allowed.update(chain); finals.add(chain[-1]); roots.add(chain[0])
    response = sim._client.get(sim.base_url + '/api/v2/workflows/' + wid + '/runs', params={'limit': 100},
                              headers={'x-api-key': sim._key}, follow_redirects=False)
    response.raise_for_status(); listing = response.json()
    require(not listing.get('nextCursor') and isinstance(listing.get('data'), list), 'old_history_incomplete')
    seen = set()
    for row in listing['data']:
        rid = row.get('runId')
        require(row.get('workflowId') == wid and rid in roots and rid not in seen
                and row.get('status') in ('paused', 'completed'),
                'old_unknown_or_unresolved_run')
        seen.add(rid)
    require(seen == roots, 'old_root_missing_from_history')
    return old, {'allowedRunIds': sorted(allowed), 'finalRunIds': sorted(finals), 'history': listing['data']}


def failed_test(task):
    require(task['executionStatus'] == 'succeeded' and task['resourcesReleased'] is True
            and task['verificationStatus'] == 'failed', 'round_not_independently_failed')
    receipts = [e for e in task['evidence'] if e.get('kind') == 'verification']
    require(len(receipts) == 1 and receipts[0]['status'] == 'failed', 'one_verification_receipt_required')
    tests = [c for c in receipts[0]['checks'] if c.get('kind') == 'test_command']
    artifacts = [c for c in receipts[0]['checks'] if c.get('kind') == 'artifact_sha256']
    require(len(artifacts) == 1 and artifacts[0].get('status') == 'passed'
            and len(tests) == 1 and tests[0].get('testId') == 'addition-unittests'
            and tests[0].get('status') == 'failed' and tests[0].get('exitCode') == 1
            and 'Ran 3 tests' in tests[0].get('stderr', '') and FAILURE_LINE in tests[0].get('stderr', ''),
            'actual_three_test_failure_missing')
    return tests[0]


def feedback_proof(parent, task, previous, codex_home):
    repair = task['payload']['input']['repair']
    if previous is None:
        require(repair['iteration'] == 0 and repair['previousResult'] is None, 'first_round_has_feedback')
        return None
    frozen = repair['previousResult']
    check = failed_test(previous)
    require(frozen['taskId'] == previous['id'] and frozen['verificationStatus'] == 'failed'
            and check in [c for e in frozen['evidence'] if e.get('kind') == 'verification' for c in e['checks']],
            'actual_previous_failure_not_propagated')
    required = (previous['id'], check['testId'], 'exitCode=1', FAILURE_LINE)
    calls, matched = {}, None
    for message in parent.get('result', {}).get('output', []):
        for block in message.get('content', []):
            data = block.get('data') or {}; call = data.get('call_id')
            if message.get('type') == 'plugin_call' and data.get('name') == 'agent_line_delegate':
                args = data.get('arguments'); args = json.loads(args) if isinstance(args, str) else args
                if isinstance(args, dict) and all(part in args.get('instruction', '') for part in required):
                    calls[call] = args['instruction']
            if message.get('type') == 'plugin_call_output' and data.get('name') == 'agent_line_delegate' and call in calls and data.get('state') == 'success':
                result = data.get('output'); result = json.loads(result) if isinstance(result, str) else result
                if (isinstance(result, dict) and result.get('taskId') == task['id']
                        and result.get('identity', {}).get('upstreamTaskId') == task['executionIdentity']['upstreamTaskId']):
                    matched = call
    require(matched is not None, 'parent_delegate_missing_actual_test_failure')
    paths = list((Path(codex_home) / 'sessions').rglob('*' + task['executionIdentity']['codexThreadId'] + '.jsonl'))
    require(len(paths) == 1, 'original_codex_rollout_ambiguous')
    matched_inputs = 0
    for line in paths[0].read_text().splitlines():
        event = json.loads(line); payload = event.get('payload', {})
        if (event.get('type') == 'response_item' and payload.get('type') == 'message'
                and payload.get('role') == 'user' and all(part in json.dumps(payload) for part in required)):
            matched_inputs += 1
    require(matched_inputs > 0, 'codex_user_input_missing_actual_test_failure')
    return {'previousTaskId': previous['id'], 'testId': check['testId'], 'exitCode': 1,
            'failureLine': FAILURE_LINE, 'logSha256': hashlib.sha256((check['stdout'] + '\n' + check['stderr']).encode()).hexdigest(),
            'delegateCallId': matched, 'delegateInstruction': calls[matched], 'nativeUserInputCount': matched_inputs}


def frozen_proof(task, parent, state, snapshot, bridge):
    wait = native_id(state['workflowId'], 'agent')
    pins = {'publication': state['publication']['externalTasks']['agentPolicies'][wait]['roleRevision'],
            'run': snapshot['agentPolicies'][wait]['roleRevision'], 'task': task['payload']['roleRevision'],
            'parent': parent['payload']['roleRevision']}
    with sqlite3.connect(Path(bridge['receipt_db']).resolve(strict=True).as_uri() + '?mode=ro', uri=True) as db:
        row = db.execute('SELECT role_id,session_id,role_revision FROM role_bindings WHERE task_id=?', (task['id'],)).fetchone()
    require(row and row[:2] == (ROLE, 'agent-line-' + task['id']), 'plugin_binding_identity_mismatch')
    pins['pluginBinding'] = row[2]
    require(all(pin == state['roleRevision'] for pin in pins.values()), 'five_role_pins_differ')
    policies = [state['publication']['externalTasks']['agentPolicies'][wait]['verificationPolicy'],
                snapshot['agentPolicies'][wait]['verificationPolicy'], task['payload']['verificationPolicy']]
    expected = policy_hash(state['verificationPolicy'])
    require(all(policy_hash(policy) == expected for policy in policies), 'frozen_test_policy_mismatch')
    return {'roleRevisions': pins, 'verificationPolicySha256': expected}


def check_guard(native, state, last_result):
    wid = state['workflowId']; outputs = native.get('blockOutputs', {})
    guard = outputs.get(native_id(wid, 'repair') + '--guard', {}).get('result', {})
    expected = {'agentLineResult': last_result}
    require(native.get('workflowId') == wid and native.get('runId') == state['finalRunId']
            and guard.get('needsRepair') is True and guard.get('result') == last_result
            and outputs.get(native_id(wid, 'end') + '.data') == expected
            and native.get('status') == 'completed', 'native_three_round_guard_not_exhausted')
    return {'maxIterations': 3, 'iterationExhausted': True, 'businessVerified': False, 'lastTaskId': last_result['taskId']}


def observe(directory, state, sim):
    require(state.get('kind') == KIND, 'original_probe_identity_mismatch')
    runs = {rid: sim.observe(state['workflowId'], rid) for rid in
            dict.fromkeys([state.get('runId'), state.get('finalRunId')]) if rid}
    count = leases = 0
    if (directory / 'tasks.sqlite').exists():
        with sqlite3.connect((directory / 'tasks.sqlite').resolve().as_uri() + '?mode=ro', uri=True) as db:
            count = db.execute('SELECT count(*) FROM tasks').fetchone()[0]
            leases = db.execute('SELECT count(*) FROM workspace_leases').fetchone()[0]
    return {'status': 'observation_only', 'nativeWritesThisInvocation': 0, 'newSubmissions': 0,
            'runs': runs, 'taskCount': count, 'activeLeaseCount': leases}


def build_application(directory, state, workspace, store, journal, sim, api, gateway, executor):
    """Use the same real assembly for live execution and offline wiring checks."""
    suffix = state['suffix']
    evidence = executor.directory
    projects = {PROJECT: {'workspace': str(workspace)}}
    bindings = {BINDING: {'projectId': PROJECT, 'roleAgentId': ROLE, 'roleRevision': state['roleRevision'],
                          'verificationPolicy': state['verificationPolicy']}}
    author_token, runner = secrets.token_urlsafe(40), secrets.token_urlsafe(40)
    access = AuthoringAccess({author_token: TrustedPrincipal('test-loop-probe-editor', frozenset({PROJECT}),
                              frozenset({'edit_workflows', 'publish_workflows'}), time.time() + 7200)})
    authoring = AuthoringAPI(access, journal, {PROJECT: api}, projects=projects, role_bindings=bindings)
    write(directory / 'task-flow.json', {'schemaVersion': 1, 'runLedgerPath': str(directory / 'runs.sqlite'),
                                         'pauseBindingsPath': str(directory / 'pauses.sqlite')})
    runtime = load_task_flow(directory / 'task-flow.json', tasks=store, projects=projects,
                            role_bindings=bindings, client=sim, journal=journal)
    installation = 'test-loop-exhaustion-' + suffix
    workflow = WorkflowGateway(sim, journal, {}, installation_id=installation)
    workflow.task_flow = runtime; workflow.publication_verifier = runtime.discovery.publication_verifier
    app = create_app(store, ServiceConfig(tokens={runner: {PROJECT}}, projects=projects, bindings=bindings,
        controlled_execution='confirmed'), workflows=workflow, dispatcher=Dispatcher(store, {(PROJECT, BINDING): gateway}),
        task_flow=runtime, authoring=authoring, verifier=AutomaticVerifier(store, evidence, test_executor=executor))
    return app, runtime, author_token, runner, installation


def run_probe(*, execute=False, directory=DATA, source=SOURCE, auth_path=None, sim_factory=SimClient):
    require(type(execute) is bool, 'explicit_execute_boolean_required')
    directory, source = Path(directory), Path(source)
    path = directory / 'state.json'
    if not path.exists() and not execute:
        return {'status': 'not_executed', 'nativeWritesThisInvocation': 0, 'newSubmissions': 0}
    auth = private_file(auth_path or ROOT / 'var/sim/probe-auth.json')
    with ExitStack() as stack:
        sim = sim_factory(auth['baseUrl'], auth['apiKey']); stack.callback(sim.close)
        if path.exists():
            sim._client.event_hooks['request'].append(lambda request: require(request.method == 'GET', 'observation_must_not_write'))
            return observe(directory, private_file(path), sim)
        require(not directory.exists(), 'partial_directory_requires_reconciliation')
        api = SimAuthoringClient(sim, auth['workspaceId'])
        known, history = checked_template(source, sim, api)  # Before any new directory or host login.
        process = private_file(ROOT / 'var/qwenpaw/agent-line-process.json')
        quiet_host(process['pid']); plugin = installed_plugin_ready(ROOT, process)
        before = main_snapshot(ROOT)
        subprocess.run([sys.executable, 'scripts/check_upstreams.py'], cwd=ROOT, check=True, capture_output=True, timeout=30)
        config = private_file(ROOT / 'var/qwenpaw/config.json'); bridge = config['plugins']['agent-line']['execution_bridge']
        workspace = Path(bridge['workspace']).resolve(strict=True)
        for key, filename in (('container_control', 'control.py'), ('container_launcher', 'launcher.py')):
            require(Path(bridge[key]).resolve(strict=True) == ROOT / 'deploy/executor' / filename, 'current_owned_executor_required')
        memory = private_file(ROOT / 'var/native-memory-roundtrip/state-attempt-4.json')
        suffix = uuid.uuid4().hex[:16]; artifact = 'test-loop-add-' + suffix + '.py'
        require(not (workspace / artifact).exists(), 'fresh_artifact_required')
        doc = document(); compile_document(doc, known['workflowId'])
        state = {'schemaVersion': 1, 'kind': KIND, 'steps': {}, 'nativeIntents': {}, 'createdAt': time.time(),
                 'workflowId': known['workflowId'], 'suffix': suffix, 'artifact': artifact, 'document': doc,
                 'pluginReadiness': plugin, 'mainBefore': before, 'knownTemplateHistory': history,
                 'tasks': {}, 'projectId': PROJECT, 'roleAgentId': ROLE, 'roleBindingId': BINDING}
        directory.mkdir(mode=0o700)
        lock = threading.RLock()
        def persist():
            with lock:
                write(path, state)
        persist()
        def native_request(request):
            if request.method == 'GET': return
            with lock:
                key = hashlib.sha256(canonical([request.method, request.url.path,
                    request.headers.get('X-Run-Id'), hashlib.sha256(request.content).hexdigest()]).encode()).hexdigest()
                require(key not in state['nativeIntents'], 'native_intent_exists_no_retry')
                state['nativeIntents'][key] = {'method': request.method, 'path': request.url.path,
                    'runId': request.headers.get('X-Run-Id'), 'bodySha256': hashlib.sha256(request.content).hexdigest(), 'status': 'intent'}
                request.extensions['test_loop_intent'] = key; persist()
        def native_response(response):
            key = response.request.extensions.get('test_loop_intent')
            if key:
                with lock:
                    state['nativeIntents'][key].update(status='received', httpStatus=response.status_code); persist()
        sim._client.event_hooks['request'].append(native_request); sim._client.event_hooks['response'].append(native_response)
        try:
            host = stack.enter_context(httpx.Client(timeout=60, trust_env=False))
            base = 'http://127.0.0.1:' + str(process['port']); login_result = []
            def login():
                response = host.post(base + '/api/auth/login', json=private_file(ROOT / 'var/qwenpaw-secrets/local-login.json'))
                response.raise_for_status(); login_result.append(response.json())
                return {'authenticated': True}
            once(state, 'host-login', login, persist)
            token = login_result[0].get('token') or login_result[0].get('access_token')
            qheaders = {'Authorization': 'Bearer ' + token, 'X-Agent-Id': ROLE}
            profile = host.get(base + '/api/agents/' + ROLE, headers=qheaders); profile.raise_for_status()
            require(profile.json().get('running', {}).get('memory_manager_backend') == 'remelight'
                    and profile.json().get('workspace_dir') == memory['roleWorkspace'], 'original_native_reme_role_required')
            service_token = (ROOT / 'var/qwenpaw-secrets/execution-service-token').read_text().strip()
            response = host.get(base + '/api/agent-line/execution-binding', headers={
                'Authorization': 'Bearer ' + token, 'X-Agent-Line-Service-Token': service_token})
            response.raise_for_status(); descriptor = response.json()
            require(descriptor.get('workspace') == str(workspace) and ROLE in descriptor.get('managedRoleIds', []), 'descriptor_binding_mismatch')
            state['roleRevision'] = validate_role_revision(descriptor.get('roleRevisions', {}).get(ROLE)); persist()
            backup_journal(source / 'sim.sqlite', directory / 'sim.sqlite')
            for filename, cls in (('tasks.sqlite', TaskStore), ('roles.sqlite', RoleDispatchLedger),
                                  ('runs.sqlite', WorkflowRunLedger), ('pauses.sqlite', PauseBindings)):
                cls.initialize(directory / filename)
            store = TaskStore(directory / 'tasks.sqlite'); ledger = RoleDispatchLedger(directory / 'roles.sqlite')
            journal = SimJournal(directory / 'sim.sqlite')
            evidence = directory / 'verification-evidence'; evidence.mkdir(mode=0o700)
            image = private_file(ROOT / 'var/executor/config.json')['image']
            executor = DockerTestExecutor(evidence, image)
            state['verificationPolicy'] = verification_policy(artifact, executor.environment_id, suffix); persist()
            gateway = RoleDispatchGateway(base, token, service_token, role_agent_id=ROLE,
                deployment_id='agent-line-g0-installation', ledger=ledger, memory_query=memory['query'], client=host, timeout_seconds=900)
            stack.callback(gateway.close)
            app, runtime, author_token, runner, installation = build_application(
                directory, state, workspace, store, journal, sim, api, gateway, executor)
            with TestClient(app) as http:
                def author(key, suffix_path, body):
                    def send():
                        response = http.post('/extension/v1/authoring/projects/' + PROJECT + '/workflows/' + state['workflowId'] + suffix_path,
                                             json=body, headers={'Authorization': 'Bearer ' + author_token})
                        require(response.status_code == 202 and response.json().get('status') == 'accepted', 'authoring_not_confirmed')
                        return response.json()['receipt']
                    return once(state, key, send, persist)
                wid = state['workflowId']
                # Recheck original draft/history immediately before publication mutation.
                checked_template(source, sim, api)
                saved = author('save', '/save', {'operationId': 'save-' + suffix,
                    'expectedStateSha256': state_hash(api.get_state(wid)), 'document': doc})
                require(saved.get('outcome') == 'saved', 'save_not_verified')
                published = author('publish', '/publish', {'operationId': 'publish-' + suffix,
                    'expectedStateSha256': state_hash(api.get_state(wid))})
                require(published.get('outcome') == 'published', 'publication_not_verified')
                state.update(publication=published, bindingId=published['bindingId']); persist()
                body = {'idempotencyKey': 'exhaustion-' + suffix, 'input': {'input': {'artifact': artifact}}}
                rid = str(uuid.uuid5(uuid.NAMESPACE_URL, canonical([installation, PROJECT, wid, body['idempotencyKey'], state['bindingId']])))
                state.update(runId=rid, startPayload=body); persist()
                route = '/extension/v1/workflows/' + state['bindingId'] + '/runs'
                def start():
                    response = http.post(route, json=body, headers={'Authorization': 'Bearer ' + runner})
                    require(response.status_code == 202 and response.json().get('runId') == rid, 'run_not_accepted_no_retry')
                    return response.json()
                once(state, 'start', start, persist)
                deadline, last_print = time.monotonic() + 3600, 0
                while time.monotonic() < deadline:
                    tasks = store.list_tasks({PROJECT})['tasks']
                    require(len(tasks) <= 3 and all(t['payload']['workflow']['runId'] == rid for t in tasks), 'fourth_or_foreign_task_forbidden')
                    tasks.sort(key=lambda task: int(task['payload']['workflow']['attemptId']))
                    for iteration, task in enumerate(tasks):
                        require(task['payload']['workflow']['nodeExecutionId'] == native_id(wid, 'agent') + '_loop' + str(iteration), 'activation_identity_mismatch')
                        require(task['executionStatus'] not in ('failed', 'canceled'), 'native_execution_not_succeeded')
                    if len(tasks) == 3 and all(t['verificationStatus'] == 'failed' and t['resourcesReleased'] for t in tasks):
                        summary = journal.resume_summary(wid, rid)
                        if summary and summary['latestAttempt'] == summary['latestAccepted'] and summary['latestAccepted']['runId']:
                            child = summary['latestAccepted']['runId']; observed = sim.observe(wid, child)
                            if observed.get('status') == 'completed':
                                state.update(finalRunId=child, finalObservation=observed); persist(); break
                    if time.monotonic() - last_print >= 30:
                        print(json.dumps({'phase': 'automatic_test_loop', 'taskCount': len(tasks),
                            'states': [{k: t[k] for k in ('id', 'executionStatus', 'verificationStatus', 'resourcesReleased')} for t in tasks]}), flush=True)
                        persist(); last_print = time.monotonic()
                    time.sleep(1)
                else:
                    raise RuntimeError('original_loop_observation_timeout_no_resubmit')
                snapshot = runtime.runs.get(wid, rid)
                for iteration, task in enumerate(tasks):
                    tid = task['id']; check = failed_test(task); parent = ledger.get(tid)
                    for _ in range(90):
                        response = host.get(base + '/api/console/chat/task/' + parent['parentTaskId'], headers=qheaders); response.raise_for_status()
                        parent_view = response.json()
                        if parent_view.get('status') == 'finished': break
                        time.sleep(1)
                    require(parent_view.get('status') == 'finished', 'original_parent_not_finished')
                    jobs = [private_file(p) for p in evidence.glob('test-*.json')]
                    jobs = [j for j in jobs if j.get('binding', {}).get('taskId') == tid]
                    require(len(jobs) == 1 and jobs[0].get('removed') is True and jobs[0].get('check') == check
                            and executor._inspect(check['containerId']) is None, 'independent_test_container_not_removed')
                    state['tasks'][tid] = {'task': task, 'parent': parent, 'parentObservation': parent_view,
                        'testJob': jobs[0], 'exitProof': check_exit(task, bridge),
                        'frozenProof': frozen_proof(task, parent, state, snapshot, bridge),
                        'feedbackProof': feedback_proof(parent_view, task, tasks[iteration - 1] if iteration else None, Path(bridge['codex_home'])),
                        'resumeReceipt': journal.get(wid, rid, task['payload']['workflow']['nodeExecutionId'])}
                    persist()
                last_result = journal.resume_input(wid, rid, tasks[-1]['payload']['workflow']['nodeExecutionId'])
                selected = sim._client.get(sim.base_url + f'/api/v2/workflows/{wid}/runs/' + state['finalRunId'],
                    params={'selectedOutputs': native_id(wid, 'repair') + '--guard,' + native_id(wid, 'end') + '.data'},
                    headers={'x-api-key': sim._key}, follow_redirects=False)
                selected.raise_for_status(); state['guardProof'] = check_guard(selected.json()['data'], state, last_result)
                require(len(store.list_tasks({PROJECT})['tasks']) == 3, 'fourth_task_created')
                with store.connect() as db:
                    require(db.execute('SELECT count(*) FROM workspace_leases').fetchone()[0] == 0, 'loop_leases_remain')
                require((workspace / artifact).read_bytes() == CONTENT, 'final_fault_artifact_changed')
                quiet_host(process['pid'])
            require(main_snapshot(ROOT) == before, 'main_configuration_or_guard_changed')
            state.update(result='passed_live', businessVerified=False); persist()
            report = {'status': 'passed_live', 'businessVerified': False, 'workflowId': wid, 'rootRunId': rid,
                'finalRunId': state['finalRunId'], 'taskCount': 3, 'testContainerCount': 3, 'maxIterations': 3,
                'testExitCodes': [1, 1, 1], 'activeLeaseCount': 0, 'mainSnapshotUnchanged': True,
                'nativeWritesThisInvocation': len(state['nativeIntents']), 'guardProof': state['guardProof']}
            write(directory / 'report.json', report)
            return report
        except Exception as exc:
            state['failure'] = {'type': type(exc).__name__, 'code': str(exc) if type(exc) is RuntimeError else 'probe_failed', 'replayPermitted': False}
            persist(); raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    try:
        result = run_probe(execute=args.execute)
        print(json.dumps({key: result[key] for key in ('status', 'nativeWritesThisInvocation')})); return 0
    except Exception as exc:
        print(json.dumps({'status': 'incomplete', 'errorType': type(exc).__name__, 'replayPermitted': False})); return 1


if __name__ == '__main__':
    raise SystemExit(main())
