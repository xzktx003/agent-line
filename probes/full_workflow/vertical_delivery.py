#!/usr/bin/env python3
"""One real run: plan, Codex implementation, isolated test, conditions, review, human approval.

The delivery template still requires three different formal agents. This probe
lifts only that constraint: plan, implementation, and review reuse the existing
agent-line-native-loop-parent through three probe-local bindings. Each binding
has its own exact verification policy. Role files, Python sources, plugins, and
the main service configuration stay unchanged.
"""
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import httpx
from fastapi.testclient import TestClient
from agent_line.adapters.sim import SimClient
from agent_line.approval_discovery import ApprovalDiscovery, WorkflowRunLedger
from agent_line.approval_api import ApprovalAccess, ApprovalAPI
from agent_line.approval_runtime import ApprovalDiscoveryWorker
from agent_line.approvals import ApprovalCoordinator, ApprovalLedger, TrustedPrincipal
from agent_line.authoring_api import AuthoringAPI, AuthoringAccess, state_hash
from agent_line.automatic_verification import AutomaticVerifier
from agent_line.dispatcher import Dispatcher
from agent_line.pause_bindings import PauseBindings
from agent_line.role_dispatch import RoleDispatchGateway, RoleDispatchLedger
from agent_line.service import ServiceConfig, create_app
from agent_line.sim_authoring import SimAuthoringClient, compile_document, native_id
from agent_line.sim_journal import SimJournal
from agent_line.store import TaskStore
from agent_line.task_flow_runtime import load_task_flow
from agent_line.test_executor import DockerTestExecutor
from agent_line.verification_policy import validate_policy
from agent_line.workflows import WorkflowGateway
from probes.qwenpaw_memory.native_roundtrip import quiet_host
from probes.sim.authoring_probe import write

DATA = ROOT / 'var/vertical-delivery-20261027'
PROJECT = 'native-memory-roundtrip'
ROLE = 'agent-line-native-loop-parent'
REVISION = 'qwenpaw-role-v1:0169b015eb19d1d40062a5a65dc02175177dce4a9d036f54f4aafbe9e0813362'
BOOTSTRAP = 'main-bootstrap-45be61aebaa6d15b.py'
BOOTSTRAP_SHA = '63098273a7aaa630762d6f145dfcf9c61c8c9b290c6ebeb2a05fea5854ccb476'


def artifact_policy(policy_id, path, payload):
    raw = {'schemaVersion': 1, 'policyId': policy_id, 'kind': 'artifact_sha256', 'requireTests': False,
           'artifacts': [{'path': path, 'sha256': hashlib.sha256(payload).hexdigest(),
                          'exactSize': len(payload), 'maxBytes': 1024}]}
    return validate_policy(raw)


def develop_policy(policy_id, path, payload, environment_id):
    code = ("import runpy,unittest\n"
            "add=runpy.run_path(" + repr(path) + ")['add']\n"
            "class AddTests(unittest.TestCase):\n"
            "    def test_positive(self): self.assertEqual(add(2,3),5)\n"
            "    def test_negative(self): self.assertEqual(add(-2,3),1)\n"
            "    def test_zero(self): self.assertEqual(add(0,0),0)\n"
            "unittest.main(verbosity=2)\n")
    raw = {'schemaVersion': 2, 'policyId': policy_id, 'kind': 'command_and_artifacts', 'requireTests': True,
           'testEnvironmentId': environment_id,
           'artifacts': [{'path': path, 'sha256': hashlib.sha256(payload).hexdigest(),
                          'exactSize': len(payload), 'maxBytes': 1024}],
           'tests': [{'id': 'addition-unittests', 'argv': ['python3', '-c', code],
                      'timeoutSeconds': 60, 'maxOutputBytes': 65536}]}
    return validate_policy(raw)


def build_document(contract):
    common = (' Read only your own relative filename from taskInput. Delegate the file write to Codex. '
              'Do not modify ' + BOOTSTRAP + ' or any other file, do not use the network, and do not copy recalled experience into the file. '
              'Prior memory is background only. Do not decide human approval and do not claim the external verifier passed.')
    plan = ('Write only taskInput.planFile. Its content is taskInput.planMarker plus exactly one newline.' + common)
    develop = ('Create only taskInput.codeFile. Its entire content is exactly these two lines and no others:\n'
               'def add(left, right):\n'
               '    return left + right\n'
               'The file ends with a single newline after the return line. Do not add a shebang, docstring, or blank line.' + common)
    review = ('Write only taskInput.reviewFile. Its content is taskInput.reviewMarker plus exactly one newline. '
              'Do not change the implementation file.' + common)
    stages = (('plan', '需求分析', 'vertical-plan', plan),
              ('develop', '实现任务', 'vertical-develop', develop),
              ('review', '代码审查', 'vertical-review', review))

    def node(identifier, kind, name, config, x, y):
        return {'id': identifier, 'type': kind, 'name': name, 'position': {'x': x, 'y': y}, 'config': config}

    def agent(identifier, name, binding, instruction, x):
        return node(identifier, 'agent', name, {
            'roleBindingId': binding, 'instruction': instruction,
            'inputFrom': {'nodeId': 'start', 'field': 'input'}, 'memoryPolicyId': 'native-role',
            'outputSchemaId': 'task-result', 'requireMemoryCommit': False, 'allowFailureBranch': True}, x, 70)

    def gate(identifier, name, source, rule, x, y):
        return node(identifier, 'condition', name, {'rules': [{
            'id': rule, 'operator': 'eq', 'source': source, 'field': 'result.verificationStatus', 'value': 'passed'}]}, x, y)

    nodes = [node('start', 'start', '开始', {}, 40, 70)]
    nodes.append(agent('plan', '需求分析', 'vertical-plan', plan, 250))
    nodes.append(gate('planGate', '分析验收', 'plan', 'planPassed', 250, 250))
    nodes.append(agent('develop', '实现任务', 'vertical-develop', develop, 520))
    nodes.append(gate('testGate', '独立测试', 'develop', 'testPassed', 520, 250))
    nodes.append(agent('review', '代码审查', 'vertical-review', review, 790))
    nodes.append(gate('reviewGate', '审查验收', 'review', 'reviewPassed', 790, 250))
    nodes.append(node('human', 'human', '人工验收', {'requiredBoolean': 'approved'}, 1040, 250))
    nodes.append(node('decision', 'condition', '验收决策', {'rules': [{
        'id': 'approved', 'operator': 'eq', 'source': 'human', 'field': 'result.approval.approved', 'value': True}]}, 1040, 470))
    nodes.append(node('success', 'end', '完成', {'resultFrom': 'review'}, 790, 470))
    nodes.append(node('rejected', 'end', '退回', {'output': {'status': 'rejected', 'businessVerified': False}}, 1280, 470))
    nodes.append(node('failed', 'end', '验收未通过', {'output': {'status': 'verification_failed', 'businessVerified': False}}, 1280, 250))
    edges = [
        {'id': 'e0', 'source': 'start', 'target': 'plan'},
        {'id': 'e1', 'source': 'plan', 'target': 'planGate'},
        {'id': 'e2', 'source': 'planGate', 'target': 'develop', 'branch': 'planPassed'},
        {'id': 'e3', 'source': 'planGate', 'target': 'failed', 'branch': 'else'},
        {'id': 'e4', 'source': 'develop', 'target': 'testGate'},
        {'id': 'e5', 'source': 'testGate', 'target': 'review', 'branch': 'testPassed'},
        {'id': 'e6', 'source': 'testGate', 'target': 'failed', 'branch': 'else'},
        {'id': 'e7', 'source': 'review', 'target': 'reviewGate'},
        {'id': 'e8', 'source': 'reviewGate', 'target': 'human', 'branch': 'reviewPassed'},
        {'id': 'e9', 'source': 'reviewGate', 'target': 'failed', 'branch': 'else'},
        {'id': 'e10', 'source': 'human', 'target': 'decision'},
        {'id': 'e11', 'source': 'decision', 'target': 'success', 'branch': 'approved'},
        {'id': 'e12', 'source': 'decision', 'target': 'rejected', 'branch': 'else'},
    ]
    document = {'schemaVersion': 1, 'nodes': nodes, 'edges': edges, 'variables': {}}
    for item in stages:
        if len(item[3]) > 4000:
            raise RuntimeError('instruction_too_long')
    compile_document(document, 'vertical-delivery-shape')
    return document, contract


def continue_paused_run(sim, state):
    """Reattach local workers to the accepted run without another submission."""
    gateway = host = http = worker = None
    path = DATA / 'state.json'
    driving = False

    def persist():
        write(path, state)

    try:
        if state.get('decisionIntent') or state.get('failure') or state.get('result') == 'passed_live':
            raise RuntimeError('resume_not_allowed')
        process = json.loads((ROOT / 'var/qwenpaw/agent-line-process.json').read_text())
        quiet_host(process['pid'])
        subprocess.run([sys.executable, 'scripts/check_upstreams.py'], cwd=ROOT, check=True, capture_output=True, text=True)
        config = json.loads((ROOT / 'var/qwenpaw/config.json').read_text())
        bridge = config['plugins']['agent-line']['execution_bridge']
        workspace = Path(bridge['workspace']).resolve(strict=True)
        bootstrap = workspace / BOOTSTRAP
        if hashlib.sha256(bootstrap.read_bytes()).hexdigest() != BOOTSTRAP_SHA or bootstrap.stat().st_size != 46:
            raise RuntimeError('bootstrap_not_at_readiness_bytes')
        suffix = state['suffix']
        names = state['files']
        code = b'def add(left, right):\n    return left + right\n'
        expected = {names['planFile']: ('VERTICAL_PLAN_' + suffix + '\n').encode(),
                    names['codeFile']: code,
                    names['reviewFile']: ('VERTICAL_REVIEW_' + suffix + '\n').encode()}
        if {name: hashlib.sha256(payload).hexdigest() for name, payload in expected.items()} != state['expectedSha256']:
            raise RuntimeError('resume_expectation_drift')
        base = 'http://127.0.0.1:' + str(process['port'])
        host = httpx.Client(timeout=60, trust_env=False)
        login = host.post(base + '/api/auth/login', json=json.loads((ROOT / 'var/qwenpaw-secrets/local-login.json').read_text()))
        login.raise_for_status()
        token = login.json().get('token') or login.json().get('access_token')
        prior_state = json.loads((ROOT / 'var/native-memory-roundtrip/state-attempt-4.json').read_text())
        profile = host.get(base + '/api/agents/' + ROLE, headers={'Authorization': 'Bearer ' + token, 'X-Agent-Id': ROLE})
        profile.raise_for_status()
        profile = profile.json()
        if profile.get('running', {}).get('memory_manager_backend') != 'remelight' or profile.get('workspace_dir') != prior_state['roleWorkspace']:
            raise RuntimeError('native_role_changed')
        service_token = (ROOT / 'var/qwenpaw-secrets/execution-service-token').read_text().strip()
        descriptor = host.get(base + '/api/agent-line/execution-binding', headers={'Authorization': 'Bearer ' + token, 'X-Agent-Line-Service-Token': service_token, 'X-Agent-Id': ROLE})
        descriptor.raise_for_status()
        if descriptor.json().get('roleRevisions', {}).get(ROLE) != REVISION:
            raise RuntimeError('role_revision_drift')
        store = TaskStore(DATA / 'tasks.sqlite')
        ledger = RoleDispatchLedger(DATA / 'roles.sqlite')
        journal = SimJournal(DATA / 'sim.sqlite')
        evidence = DATA / 'verification-evidence'
        image = json.loads((ROOT / 'var/executor/config.json').read_text())['image']
        executor = DockerTestExecutor(evidence, image)
        if state['policies']['vertical-develop']['testEnvironmentId'] != executor.environment_id:
            raise RuntimeError('test_environment_drift')
        projects = {PROJECT: {'workspace': str(workspace)}}
        bindings = {key: {'projectId': PROJECT, 'roleAgentId': ROLE, 'roleRevision': REVISION, 'verificationPolicy': policy}
                    for key, policy in state['policies'].items()}
        credential = json.loads((DATA / 'service-token.json').read_text())['token']
        human_token = json.loads((DATA / 'human-token.json').read_text())['token']
        author_token = secrets.token_urlsafe(40)
        access = AuthoringAccess({author_token: TrustedPrincipal('vertical-delivery-probe-editor', frozenset({PROJECT}), frozenset({'edit_workflows', 'publish_workflows'}), time.time() + 14400)})
        api = SimAuthoringClient(sim, json.loads((ROOT / 'var/sim/probe-auth.json').read_text())['workspaceId'])
        authoring = AuthoringAPI(access, journal, {PROJECT: api}, projects=projects, role_bindings=bindings)
        installation = 'vertical-delivery-' + suffix
        gateway = RoleDispatchGateway(base, token, service_token, role_agent_id=ROLE, deployment_id='agent-line-g0-installation',
                                      ledger=ledger, memory_query=prior_state['query'], client=host, timeout_seconds=900)
        flow = load_task_flow(DATA / 'task-flow.json', tasks=store, projects=projects, role_bindings=bindings, client=sim, journal=journal)
        workflows = WorkflowGateway(sim, journal, {}, installation_id=installation)
        workflows.task_flow = flow
        workflows.publication_verifier = flow.discovery.publication_verifier
        verifier = AutomaticVerifier(store, evidence, test_executor=executor)
        dispatcher = Dispatcher(store, {(PROJECT, key): gateway for key in bindings}, verifier=verifier)
        approval_access = ApprovalAccess({human_token: TrustedPrincipal('vertical-probe-independent-operator', frozenset({PROJECT}), frozenset({'approve'}), time.time() + 14400)})
        approval_ledger = ApprovalLedger(DATA / 'approvals.sqlite')
        approval_discovery = ApprovalDiscovery(flow.runs, approval_ledger, sim, publication_verifier=flow.discovery.publication_verifier)
        approvals = ApprovalAPI(approval_access, ApprovalCoordinator(approval_ledger, sim, journal, authorize=approval_access.authorize),
                                discovery=approval_discovery, publication_verifier=flow.discovery.publication_verifier)
        workflows.approval_discovery = approval_discovery
        app = create_app(store, ServiceConfig(tokens={credential: {PROJECT}}, projects=projects, bindings=bindings, controlled_execution='confirmed'),
                         workflows=workflows, dispatcher=dispatcher, task_flow=flow, authoring=authoring, approvals=approvals, verifier=verifier)
        if not authoring.approval_enabled or not authoring.verification_enabled:
            raise RuntimeError('vertical_runtime_not_enabled')
        worker = ApprovalDiscoveryWorker(approval_discovery)
        worker.start()
        http = TestClient(app)
        http.__enter__()
        driving = True
        state['controlResumeCount'] = int(state.get('controlResumeCount') or 0) + 1
        persist()
        wid = state['workflowId']
        run_id = state['runId']
        binding = state['bindingId']
        main_config_path = ROOT / 'var/adapter/service.json'
        main_config = json.loads(main_config_path.read_text())
        main_database = Path(main_config['database'])
        main_database = main_database if main_database.is_absolute() else main_config_path.parent / main_database
        print(json.dumps({'mode': 'resume_control_plane', 'workflowId': wid, 'runId': run_id, 'newWorkflowStarts': 0}, ensure_ascii=False), flush=True)
        deadline = time.monotonic() + 4800
        last_print = 0
        approval = None
        while time.monotonic() < deadline:
            tasks = [task for task in store.list_tasks({PROJECT})['tasks'] if task['payload']['workflow']['runId'] == run_id]
            by_stage = {}
            for task in tasks:
                node = task['payload']['workflow']['nodeId']
                stage = next((name for name in ('plan', 'develop', 'review') if node == native_id(wid, name)), node)
                if stage in by_stage:
                    raise RuntimeError('duplicate_stage_task')
                by_stage[stage] = {'id': task['id'], 'executionStatus': task['executionStatus'], 'verificationStatus': task['verificationStatus'], 'resourcesReleased': task['resourcesReleased']}
            state['stages'] = by_stage
            observed = sim.observe(wid, run_id)
            state['simStatus'] = observed['status']
            persist()
            if observed['status'] in ('completed', 'failed', 'cancelled'):
                raise RuntimeError('ended_before_human_' + observed['status'])
            listing = http.get('/extension/v1/approvals', headers={'Authorization': 'Bearer ' + human_token})
            listing.raise_for_status()
            found = [item for item in listing.json()['approvals'] if item['workflowId'] == wid and item['runId'] == run_id]
            if len(found) > 1:
                raise RuntimeError('multiple_human_approvals')
            ready = {'plan', 'develop', 'review'} <= set(by_stage) and all(by_stage[name]['verificationStatus'] == 'passed' and by_stage[name]['resourcesReleased'] for name in ('plan', 'develop', 'review'))
            if found and ready:
                approval = found[0]
                if approval['blockId'] != native_id(wid, 'human'):
                    raise RuntimeError('approval_bound_to_wrong_pause')
                break
            if time.monotonic() - last_print > 30:
                print(json.dumps({'phase': 'waiting', 'sim': observed['status'], 'stages': by_stage}, ensure_ascii=False), flush=True)
                last_print = time.monotonic()
            time.sleep(2)
        else:
            raise RuntimeError('human_wait_deadline_original_run_preserved')
        for name, payload in expected.items():
            actual = (workspace / name).read_bytes()
            if actual != payload:
                raise RuntimeError('artifact_mismatch_' + name)
        if hashlib.sha256(bootstrap.read_bytes()).hexdigest() != BOOTSTRAP_SHA:
            bootstrap.write_bytes(bytes.fromhex('64656620616464286c6566742c207269676874293a0a2020202072657475726e206c656674202b2072696768740a'))
            raise RuntimeError('bootstrap_modified_and_restored')
        aid = approval['approvalId']
        decision = {'decisionId': 'approve-' + suffix, 'decision': 'approve'}
        state['decisionPayload'] = decision
        state['decisionIntent'] = True
        persist()
        unauthorized = http.post('/extension/v1/approvals/' + aid + '/decision', headers={'Authorization': 'Bearer ' + credential}, json=decision)
        if unauthorized.status_code != 401:
            raise RuntimeError('service_credential_authorized_approval')
        decided = http.post('/extension/v1/approvals/' + aid + '/decision', headers={'Authorization': 'Bearer ' + human_token}, json=decision)
        state.update(decisionHTTP=decided.status_code, decisionReceipt=decided.json())
        persist()
        if decided.status_code != 200 or decided.json().get('deliveryStatus') != 'accepted':
            raise RuntimeError('human_decision_not_accepted')
        receipt = journal.get(wid, run_id, approval['contextId'])
        final_id = receipt['receipt']['resumedRunId']
        state['finalRunId'] = final_id
        persist()
        for _ in range(120):
            final = sim.observe(wid, final_id)
            state['finalObservation'] = {'status': final['status']}
            persist()
            if final['status'] in ('completed', 'failed', 'cancelled'):
                break
            time.sleep(1)
        if final['status'] != 'completed':
            raise RuntimeError('decision_branch_not_completed')
        selectors = ','.join([native_id(wid, 'decision'), native_id(wid, 'success') + '.data', native_id(wid, 'human') + '.resumeInput'])
        selected = sim._client.get(sim.base_url + '/api/v2/workflows/' + wid + '/runs/' + final_id, params={'selectedOutputs': selectors},
                                   headers={'x-api-key': sim._key}, follow_redirects=False)
        selected.raise_for_status()
        native = selected.json()['data']
        choice = native['blockOutputs'][native_id(wid, 'decision')]
        if choice.get('selectedPath', {}).get('blockId') != native_id(wid, 'success') or choice.get('selectedOption') != 'approved':
            raise RuntimeError('wrong_human_branch')
        tasks = [store.get(item['id']) for item in state['stages'].values()]
        if any(task['executionStatus'] != 'succeeded' or task['verificationStatus'] != 'passed' or not task['resourcesReleased'] for task in tasks):
            raise RuntimeError('stage_not_closed')
        develop = next(task for task in tasks if task['payload']['workflow']['nodeId'] == native_id(wid, 'develop'))
        checks = [item for item in develop['evidence'] if isinstance(item, dict) and item.get('kind') == 'verification']
        if len(checks) != 1 or checks[0].get('status') != 'passed' or not checks[0].get('verificationPolicy', {}).get('requireTests'):
            raise RuntimeError('develop_test_not_recorded')
        with store.connect() as db:
            leases = db.execute('SELECT count(*) FROM workspace_leases').fetchone()[0]
        if leases:
            raise RuntimeError('leases_remain')
        if TaskStore(main_database).maintenance_status() != state['mainGuardBefore']:
            raise RuntimeError('main_guard_changed')
        if hashlib.sha256(bootstrap.read_bytes()).hexdigest() != BOOTSTRAP_SHA:
            raise RuntimeError('bootstrap_changed')
        report = {'status': 'passed_live', 'workflowId': wid, 'runId': run_id, 'finalRunId': final_id, 'bindingId': binding,
                  'roleAgentId': ROLE, 'roleRevision': REVISION, 'stages': state['stages'], 'files': state['expectedSha256'],
                  'selectedOption': choice.get('selectedOption'), 'activeLeaseCount': 0, 'mainGuardUnchanged': True,
                  'liftedRestriction': state['liftedRestriction'], 'controlResumeCount': state.get('controlResumeCount'),
                  'limitations': ['One existing formal role used for plan, implementation, and review',
                                  'Human approval is the independent probe operator, not a named end user',
                                  'Repair is the verification failure exit; this passing run took the review branch']}
        write(DATA / 'report.json', report)
        state['result'] = 'passed_live'
        persist()
        print(json.dumps({'status': 'passed_live', 'workflowId': wid, 'runId': run_id, 'finalRunId': final_id}, ensure_ascii=False), flush=True)
        return 0
    except Exception as exc:
        if driving:
            state['failure'] = {'type': type(exc).__name__, 'code': str(exc) if type(exc) is RuntimeError else 'probe_failed'}
            try:
                persist()
            except Exception:
                pass
            write(DATA / 'failure.json', {'status': 'incomplete', **state.get('failure', {})})
        print(json.dumps({'status': 'incomplete', 'driving': driving, 'code': str(exc) if type(exc) is RuntimeError else 'probe_failed'}, ensure_ascii=False), flush=True)
        return 1
    finally:
        if worker is not None:
            worker.stop()
        if http is not None:
            http.__exit__(None, None, None)
        if gateway is not None:
            gateway.close()
        if host is not None:
            host.close()
        sim.close()


def main():
    DATA.mkdir(mode=0o700, exist_ok=True)
    path = DATA / 'state.json'
    auth = json.loads((ROOT / 'var/sim/probe-auth.json').read_text())
    sim = SimClient(auth['baseUrl'], auth['apiKey'])
    calls = []
    sim._client.event_hooks['request'].append(lambda request: calls.append({'method': request.method, 'path': request.url.path}))
    if path.exists():
        state = json.loads(path.read_text())
        resumable = (state.get('startHTTP') == 202 and (state.get('startReceipt') or {}).get('status') == 'accepted'
                     and state.get('result') != 'passed_live' and not state.get('failure') and not state.get('decisionIntent')
                     and state.get('workflowId') and state.get('runId'))
        if resumable:
            observed = sim.observe(state['workflowId'], state['runId'])
            if sum(item['method'] != 'GET' for item in calls):
                sim.close()
                raise RuntimeError('resume_precheck_wrote')
            if observed.get('safeToResubmit') is not False or observed.get('status') not in ('running', 'paused'):
                write(DATA / 'observation.json', {'mode': 'observation_only', 'newSubmissions': 0,
                                                  'run': {'status': observed.get('status'), 'safeToResubmit': observed.get('safeToResubmit')}})
                sim.close()
                print(json.dumps({'mode': 'observation_only', 'status': state.get('result', 'incomplete'), 'sim': observed.get('status')}, ensure_ascii=False))
                return 0
            return continue_paused_run(sim, state)
        result = {'mode': 'observation_only', 'newSubmissions': 0}
        if state.get('workflowId') and state.get('runId'):
            result['run'] = sim.observe(state['workflowId'], state.get('finalRunId', state['runId']))
        result['nativeWritesThisInvocation'] = sum(item['method'] != 'GET' for item in calls)
        if result['nativeWritesThisInvocation']:
            raise RuntimeError('observation_must_not_write')
        write(DATA / 'observation.json', result)
        sim.close()
        print(json.dumps({'mode': 'observation_only', 'status': state.get('result', 'incomplete')}, ensure_ascii=False))
        return 0
    gateway = None
    host = None
    http = None
    worker = None
    process = json.loads((ROOT / 'var/qwenpaw/agent-line-process.json').read_text())
    quiet_host(process['pid'])
    subprocess.run([sys.executable, 'scripts/check_upstreams.py'], cwd=ROOT, check=True, capture_output=True, text=True)
    config = json.loads((ROOT / 'var/qwenpaw/config.json').read_text())
    bridge = config['plugins']['agent-line']['execution_bridge']
    if not bridge.get('container_control'):
        raise RuntimeError('container_control_required')
    workspace = Path(bridge['workspace']).resolve(strict=True)
    bootstrap = workspace / BOOTSTRAP
    if hashlib.sha256(bootstrap.read_bytes()).hexdigest() != BOOTSTRAP_SHA or bootstrap.stat().st_size != 46:
        raise RuntimeError('bootstrap_not_at_readiness_bytes')
    prior = json.loads((ROOT / 'var/native-memory-roundtrip/attempt4-report.json').read_text())
    if prior.get('verified') is not True or prior.get('activeLeaseCount') != 0 or prior.get('ownedActiveContainerCount') != 0:
        raise RuntimeError('prior_native_proof_required')
    suffix = uuid.uuid4().hex[:12]
    code = b'def add(left, right):\n    return left + right\n'
    plan_marker = 'VERTICAL_PLAN_' + suffix
    review_marker = 'VERTICAL_REVIEW_' + suffix
    names = {'planFile': 'vertical-plan-' + suffix + '.txt', 'codeFile': 'vertical-add-' + suffix + '.py',
             'reviewFile': 'vertical-review-' + suffix + '.txt'}
    expected = {names['planFile']: (plan_marker + '\n').encode(), names['codeFile']: code,
                names['reviewFile']: (review_marker + '\n').encode()}
    if any((workspace / name).exists() for name in expected):
        raise RuntimeError('artifact_exists')
    main_config_path = ROOT / 'var/adapter/service.json'
    main_config = json.loads(main_config_path.read_text())
    main_database = Path(main_config['database'])
    main_database = main_database if main_database.is_absolute() else main_config_path.parent / main_database
    state = {'steps': {}, 'suffix': suffix, 'files': names, 'expectedSha256': {name: hashlib.sha256(payload).hexdigest() for name, payload in expected.items()},
             'createdAt': time.time(), 'roleAgentId': ROLE, 'roleRevision': REVISION, 'projectId': PROJECT,
             'liftedRestriction': 'delivery template distinct formal agents; one existing role, three probe-local policies',
             'mainGuardBefore': TaskStore(main_database).maintenance_status()}
    write(path, state)

    def persist():
        write(path, state)

    def once(key, operation):
        if key in state['steps']:
            raise RuntimeError('operation_already_has_intent')
        state['steps'][key] = {'status': 'intent'}
        persist()
        result = operation()
        state['steps'][key] = {'status': 'received', 'result': result}
        persist()
        return result

    try:
        base = 'http://127.0.0.1:' + str(process['port'])
        host = httpx.Client(timeout=60, trust_env=False)
        login = host.post(base + '/api/auth/login', json=json.loads((ROOT / 'var/qwenpaw-secrets/local-login.json').read_text()))
        login.raise_for_status()
        token = login.json().get('token') or login.json().get('access_token')
        profile = host.get(base + '/api/agents/' + ROLE, headers={'Authorization': 'Bearer ' + token, 'X-Agent-Id': ROLE})
        profile.raise_for_status()
        profile = profile.json()
        prior_state = json.loads((ROOT / 'var/native-memory-roundtrip/state-attempt-4.json').read_text())
        if profile.get('running', {}).get('memory_manager_backend') != 'remelight' or profile.get('workspace_dir') != prior_state['roleWorkspace']:
            raise RuntimeError('native_role_changed')
        service_token = (ROOT / 'var/qwenpaw-secrets/execution-service-token').read_text().strip()
        binding_headers = {'Authorization': 'Bearer ' + token, 'X-Agent-Line-Service-Token': service_token, 'X-Agent-Id': ROLE}
        descriptor = host.get(base + '/api/agent-line/execution-binding', headers=binding_headers)
        descriptor.raise_for_status()
        live_revision = descriptor.json().get('roleRevisions', {}).get(ROLE)
        if live_revision != REVISION:
            raise RuntimeError('role_revision_drift')
        for name, cls in [('tasks.sqlite', TaskStore), ('roles.sqlite', RoleDispatchLedger), ('sim.sqlite', SimJournal),
                          ('runs.sqlite', WorkflowRunLedger), ('pauses.sqlite', PauseBindings), ('approvals.sqlite', ApprovalLedger)]:
            cls.initialize(DATA / name)
            os.chmod(DATA / name, 0o600)
        store = TaskStore(DATA / 'tasks.sqlite')
        ledger = RoleDispatchLedger(DATA / 'roles.sqlite')
        journal = SimJournal(DATA / 'sim.sqlite')
        evidence = DATA / 'verification-evidence'
        evidence.mkdir(mode=0o700)
        image = json.loads((ROOT / 'var/executor/config.json').read_text())['image']
        executor = DockerTestExecutor(evidence, image)
        projects = {PROJECT: {'workspace': str(workspace)}}
        bindings = {
            'vertical-plan': {'projectId': PROJECT, 'roleAgentId': ROLE, 'roleRevision': REVISION,
                              'verificationPolicy': artifact_policy('vertical-plan-' + suffix, names['planFile'], expected[names['planFile']])},
            'vertical-develop': {'projectId': PROJECT, 'roleAgentId': ROLE, 'roleRevision': REVISION,
                                 'verificationPolicy': develop_policy('vertical-develop-' + suffix, names['codeFile'], expected[names['codeFile']], executor.environment_id)},
            'vertical-review': {'projectId': PROJECT, 'roleAgentId': ROLE, 'roleRevision': REVISION,
                                'verificationPolicy': artifact_policy('vertical-review-' + suffix, names['reviewFile'], expected[names['reviewFile']])},
        }
        state['policies'] = {key: value['verificationPolicy'] for key, value in bindings.items()}
        persist()
        api = SimAuthoringClient(sim, auth['workspaceId'])
        author_token = secrets.token_urlsafe(40)
        access = AuthoringAccess({author_token: TrustedPrincipal('vertical-delivery-probe-editor', frozenset({PROJECT}), frozenset({'edit_workflows', 'publish_workflows'}), time.time() + 14400)})
        authoring = AuthoringAPI(access, journal, {PROJECT: api}, projects=projects, role_bindings=bindings)
        write(DATA / 'task-flow.json', {'schemaVersion': 1, 'runLedgerPath': str(DATA / 'runs.sqlite'), 'pauseBindingsPath': str(DATA / 'pauses.sqlite')})
        installation = 'vertical-delivery-' + suffix
        gateway = RoleDispatchGateway(base, token, service_token, role_agent_id=ROLE, deployment_id='agent-line-g0-installation',
                                      ledger=ledger, memory_query=prior_state['query'], client=host, timeout_seconds=900)
        credential = secrets.token_urlsafe(40)
        write(DATA / 'service-token.json', {'token': credential})
        human_token = secrets.token_urlsafe(40)
        write(DATA / 'human-token.json', {'token': human_token})
        flow = load_task_flow(DATA / 'task-flow.json', tasks=store, projects=projects, role_bindings=bindings, client=sim, journal=journal)
        workflows = WorkflowGateway(sim, journal, {}, installation_id=installation)
        workflows.task_flow = flow
        workflows.publication_verifier = flow.discovery.publication_verifier
        verifier = AutomaticVerifier(store, evidence, test_executor=executor)
        dispatcher = Dispatcher(store, {(PROJECT, key): gateway for key in bindings}, verifier=verifier)
        approval_access = ApprovalAccess({human_token: TrustedPrincipal('vertical-probe-independent-operator', frozenset({PROJECT}), frozenset({'approve'}), time.time() + 14400)})
        approval_ledger = ApprovalLedger(DATA / 'approvals.sqlite')
        approval_discovery = ApprovalDiscovery(flow.runs, approval_ledger, sim, publication_verifier=flow.discovery.publication_verifier)
        approvals = ApprovalAPI(approval_access, ApprovalCoordinator(approval_ledger, sim, journal, authorize=approval_access.authorize),
                                discovery=approval_discovery, publication_verifier=flow.discovery.publication_verifier)
        workflows.approval_discovery = approval_discovery
        app = create_app(store, ServiceConfig(tokens={credential: {PROJECT}}, projects=projects, bindings=bindings, controlled_execution='confirmed'),
                         workflows=workflows, dispatcher=dispatcher, task_flow=flow, authoring=authoring, approvals=approvals, verifier=verifier)
        if not authoring.approval_enabled or not authoring.verification_enabled:
            raise RuntimeError('vertical_runtime_not_enabled')
        worker = ApprovalDiscoveryWorker(approval_discovery)
        worker.start()
        http = TestClient(app)
        http.__enter__()
        contract = {'planFile': names['planFile'], 'planMarker': plan_marker, 'codeFile': names['codeFile'],
                    'reviewFile': names['reviewFile'], 'reviewMarker': review_marker,
                    'implementation': 'def add(left, right): return left + right'}

        def author(operation, route, payload):
            def request():
                response = http.post('/extension/v1/authoring/projects/' + PROJECT + '/workflows' + route,
                                     headers={'Authorization': 'Bearer ' + author_token}, json=payload)
                if response.status_code != 202:
                    raise RuntimeError('authoring_http_' + str(response.status_code))
                data = response.json()
                if data.get('status') != 'accepted':
                    raise RuntimeError('authoring_not_accepted')
                return data
            return once(operation, request)['receipt']

        created = author('create', '', {'operationId': 'create-' + suffix, 'name': 'agent-line-vertical-delivery',
                                        'description': 'One real plan-implement-test-review-human run on one existing role'})
        wid = created['workflowId']
        state['workflowId'] = wid
        persist()
        document, _ = build_document(contract)
        state['document'] = document
        persist()
        saved = author('save', '/' + wid + '/save', {'operationId': 'save-' + suffix, 'expectedStateSha256': state_hash(api.get_state(wid)), 'document': document})
        if saved.get('outcome') != 'saved':
            raise RuntimeError('save_' + str(saved.get('outcome')))
        published = author('publish', '/' + wid + '/publish', {'operationId': 'publish-' + suffix, 'expectedStateSha256': state_hash(api.get_state(wid))})
        if published.get('outcome') != 'published':
            raise RuntimeError('publish_' + str(published.get('outcome')))
        binding = published['bindingId']
        state.update(bindingId=binding, publication=published)
        persist()
        body = {'idempotencyKey': 'vertical-' + suffix, 'input': {'input': contract}}
        run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, __import__('agent_line.store', fromlist=['canonical']).canonical([installation, PROJECT, wid, body['idempotencyKey'], binding])))
        state.update(runId=run_id, startPayload=body, startIntent=True)
        persist()
        started = http.post('/extension/v1/workflows/' + binding + '/runs', headers={'Authorization': 'Bearer ' + credential}, json=body)
        state.update(startHTTP=started.status_code, startReceipt=started.json())
        persist()
        if started.status_code != 202 or started.json().get('status') != 'accepted':
            raise RuntimeError('start_not_accepted')
        state['frozenPublication'] = journal.get_publication(wid, run_id)
        persist()
        deadline = time.monotonic() + 4800
        last_print = 0
        approval = None
        while time.monotonic() < deadline:
            tasks = [task for task in store.list_tasks({PROJECT})['tasks'] if task['payload']['workflow']['runId'] == run_id]
            by_stage = {}
            for task in tasks:
                node = task['payload']['workflow']['nodeId']
                stage = next((name for name in ('plan', 'develop', 'review') if node == native_id(wid, name)), node)
                if stage in by_stage:
                    raise RuntimeError('duplicate_stage_task')
                by_stage[stage] = {'id': task['id'], 'executionStatus': task['executionStatus'], 'verificationStatus': task['verificationStatus'], 'resourcesReleased': task['resourcesReleased']}
            state['stages'] = by_stage
            observed = sim.observe(wid, run_id)
            state['simStatus'] = observed['status']
            persist()
            if observed['status'] in ('completed', 'failed', 'cancelled'):
                raise RuntimeError('ended_before_human_' + observed['status'])
            listing = http.get('/extension/v1/approvals', headers={'Authorization': 'Bearer ' + human_token})
            listing.raise_for_status()
            found = [item for item in listing.json()['approvals'] if item['workflowId'] == wid and item['runId'] == run_id]
            if len(found) > 1:
                raise RuntimeError('multiple_human_approvals')
            ready = {'plan', 'develop', 'review'} <= set(by_stage) and all(by_stage[name]['verificationStatus'] == 'passed' and by_stage[name]['resourcesReleased'] for name in ('plan', 'develop', 'review'))
            if found and ready:
                approval = found[0]
                if approval['blockId'] != native_id(wid, 'human'):
                    raise RuntimeError('approval_bound_to_wrong_pause')
                break
            if time.monotonic() - last_print > 30:
                print(json.dumps({'phase': 'waiting', 'sim': observed['status'], 'stages': by_stage}, ensure_ascii=False), flush=True)
                last_print = time.monotonic()
            time.sleep(2)
        else:
            raise RuntimeError('human_wait_deadline_original_run_preserved')
        for name, payload in expected.items():
            actual = (workspace / name).read_bytes()
            if actual != payload:
                raise RuntimeError('artifact_mismatch_' + name)
        if hashlib.sha256(bootstrap.read_bytes()).hexdigest() != BOOTSTRAP_SHA:
            bootstrap.write_bytes(bytes.fromhex('64656620616464286c6566742c207269676874293a0a2020202072657475726e206c656674202b2072696768740a'))
            raise RuntimeError('bootstrap_modified_and_restored')
        aid = approval['approvalId']
        decision = {'decisionId': 'approve-' + suffix, 'decision': 'approve'}
        state['decisionPayload'] = decision
        state['decisionIntent'] = True
        persist()
        unauthorized = http.post('/extension/v1/approvals/' + aid + '/decision', headers={'Authorization': 'Bearer ' + credential}, json=decision)
        if unauthorized.status_code != 401:
            raise RuntimeError('service_credential_authorized_approval')
        decided = http.post('/extension/v1/approvals/' + aid + '/decision', headers={'Authorization': 'Bearer ' + human_token}, json=decision)
        state.update(decisionHTTP=decided.status_code, decisionReceipt=decided.json())
        persist()
        if decided.status_code != 200 or decided.json().get('deliveryStatus') != 'accepted':
            raise RuntimeError('human_decision_not_accepted')
        receipt = journal.get(wid, run_id, approval['contextId'])
        final_id = receipt['receipt']['resumedRunId']
        state['finalRunId'] = final_id
        persist()
        for _ in range(120):
            final = sim.observe(wid, final_id)
            state['finalObservation'] = {'status': final['status']}
            persist()
            if final['status'] in ('completed', 'failed', 'cancelled'):
                break
            time.sleep(1)
        if final['status'] != 'completed':
            raise RuntimeError('decision_branch_not_completed')
        selectors = ','.join([native_id(wid, 'decision'), native_id(wid, 'success') + '.data', native_id(wid, 'human') + '.resumeInput'])
        selected = sim._client.get(sim.base_url + '/api/v2/workflows/' + wid + '/runs/' + final_id, params={'selectedOutputs': selectors},
                                   headers={'x-api-key': sim._key}, follow_redirects=False)
        selected.raise_for_status()
        native = selected.json()['data']
        choice = native['blockOutputs'][native_id(wid, 'decision')]
        if choice.get('selectedPath', {}).get('blockId') != native_id(wid, 'success') or choice.get('selectedOption') != 'approved':
            raise RuntimeError('wrong_human_branch')
        tasks = [store.get(item['id']) for item in state['stages'].values()]
        if any(task['executionStatus'] != 'succeeded' or task['verificationStatus'] != 'passed' or not task['resourcesReleased'] for task in tasks):
            raise RuntimeError('stage_not_closed')
        develop = next(task for task in tasks if task['payload']['workflow']['nodeId'] == native_id(wid, 'develop'))
        checks = [item for item in develop['evidence'] if isinstance(item, dict) and item.get('kind') == 'verification']
        if len(checks) != 1 or checks[0].get('status') != 'passed' or not checks[0].get('verificationPolicy', {}).get('requireTests'):
            raise RuntimeError('develop_test_not_recorded')
        with store.connect() as db:
            leases = db.execute('SELECT count(*) FROM workspace_leases').fetchone()[0]
        if leases:
            raise RuntimeError('leases_remain')
        if TaskStore(main_database).maintenance_status() != state['mainGuardBefore']:
            raise RuntimeError('main_guard_changed')
        if hashlib.sha256(bootstrap.read_bytes()).hexdigest() != BOOTSTRAP_SHA:
            raise RuntimeError('bootstrap_changed')
        report = {'status': 'passed_live', 'workflowId': wid, 'runId': run_id, 'finalRunId': final_id, 'bindingId': binding,
                  'roleAgentId': ROLE, 'roleRevision': REVISION, 'stages': state['stages'], 'files': state['expectedSha256'],
                  'selectedOption': choice.get('selectedOption'), 'activeLeaseCount': 0, 'mainGuardUnchanged': True,
                  'liftedRestriction': state['liftedRestriction'],
                  'limitations': ['One existing formal role used for plan, implementation, and review',
                                  'Human approval is the independent probe operator, not a named end user',
                                  'Repair is the verification failure exit; this passing run took the review branch']}
        write(DATA / 'report.json', report)
        state['result'] = 'passed_live'
        persist()
        print(json.dumps({'status': 'passed_live', 'workflowId': wid, 'runId': run_id, 'finalRunId': final_id}, ensure_ascii=False), flush=True)
        return 0
    except Exception as exc:
        state['failure'] = {'type': type(exc).__name__, 'code': str(exc) if type(exc) is RuntimeError else 'probe_failed'}
        try:
            persist()
        except Exception:
            pass
        write(DATA / 'failure.json', {'status': 'incomplete', **state.get('failure', {})})
        print(json.dumps({'status': 'incomplete', **state.get('failure', {})}, ensure_ascii=False), flush=True)
        return 1
    finally:
        if worker is not None:
            worker.stop()
        if http is not None:
            http.__exit__(None, None, None)
        if gateway is not None:
            gateway.close()
        if host is not None:
            host.close()
        sim.close()


if __name__ == '__main__':
    raise SystemExit(main())
