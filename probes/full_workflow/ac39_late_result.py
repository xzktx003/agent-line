#!/usr/bin/env python3
"""Live AC-39: a round-1 result returns after round 2 has started.

Uses the existing native-loop role and gpt-6-luna executor. Does not enable
taskField, and does not relax sandbox, AppArmor, user namespaces, ReMe, or the
activation identity check. A second run only observes the original workflow.
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
from agent_line.approval_discovery import WorkflowRunLedger
from agent_line.completion_relay import CompletionRelay, ResumeBinding
from agent_line.dispatcher import Dispatcher
from agent_line.pause_bindings import PauseBindings
from agent_line.role_dispatch import RoleDispatchGateway, RoleDispatchLedger
from agent_line.service import ServiceConfig, create_app
from agent_line.sim_authoring import SimAuthoringClient, native_id
from agent_line.sim_journal import SimJournal
from agent_line.store import Conflict, TaskStore, canonical
from agent_line.task_flow_runtime import load_task_flow
from agent_line.automatic_verification import AutomaticVerifier
from agent_line.workflows import WorkflowGateway
from agent_line.authoring_api import AuthoringAPI, AuthoringAccess, state_hash
from agent_line.approvals import TrustedPrincipal
from probes.qwenpaw_memory.native_roundtrip import quiet_host
from probes.sim.authoring_probe import write

DATA = ROOT / 'var/ac39-late-result-20261027'
PROJECT = 'native-memory-roundtrip'
BINDING = 'native-loop-parent'
ROLE = 'agent-line-native-loop-parent'


def model_slug(task, codex_home):
    identity = task.get('executionIdentity') or {}
    thread_id = identity.get('codexThreadId')
    found = []
    if thread_id:
        rollouts = list(Path(codex_home).joinpath('sessions').rglob('*' + thread_id + '.jsonl'))
        if len(rollouts) == 1:
            for line in rollouts[0].read_text(errors='replace').splitlines():
                if 'gpt-6-luna' in line:
                    found.append('gpt-6-luna')
                    break
                if 'gpt-6.1-sol' in line:
                    found.append('gpt-6.1-sol')
                    break
    return {'codexThreadId': thread_id, 'model': found[0] if found else None}


def stable_view(task):
    return {
        'id': task['id'],
        'output': task.get('output'),
        'workflow': task['payload']['workflow'],
        'input': task['payload']['input'],
    }



def finish_native_reader(sim, calls, state, path):
    """Re-read the completed run after the guard wrapper path was opened.

    Does not submit, resume, or deliver. The live rounds and late-result
    isolation already finished; only the native guard reader stopped one
    level too high.
    """
    case = state['case']
    wid = state['workflowId']
    run_id = case['runId']
    store = TaskStore(DATA / 'tasks.sqlite')
    tasks = [task for task in store.list_tasks({PROJECT})['tasks'] if task['payload']['workflow']['runId'] == run_id]
    tasks.sort(key=lambda task: int(task['payload']['workflow']['attemptId']))
    if len(tasks) != 2:
        raise RuntimeError('round_count_changed_after_late_result')
    if tasks[0]['verificationStatus'] != 'failed' or tasks[1]['verificationStatus'] != 'passed':
        raise RuntimeError('round_outcomes_not_distinct')
    if tasks[1]['id'] != case['lateProof']['round2TaskId']:
        raise RuntimeError('round2_identity_changed')
    selectors = native_id(wid, 'repair') + '--guard,' + native_id(wid, 'end') + '.data'
    selected = sim._client.get(
        sim.base_url + f'/api/v2/workflows/{wid}/runs/' + case['finalRunId'],
        params={'selectedOutputs': selectors}, headers={'x-api-key': sim._key}, follow_redirects=False,
    )
    selected.raise_for_status()
    native = selected.json()['data']
    guard = native.get('blockOutputs', {}).get(native_id(wid, 'repair') + '--guard', {}).get('result') or {}
    guard_result = guard.get('result') if isinstance(guard, dict) else None
    case['nativeGuardAndEnd'] = {
        'workflowId': native.get('workflowId'), 'runId': native.get('runId'),
        'guardTaskId': guard_result.get('taskId') if isinstance(guard_result, dict) else None,
        'guardNeedsRepair': guard.get('needsRepair') if isinstance(guard, dict) else None,
        'endTaskId': ((native.get('blockOutputs', {}).get(native_id(wid, 'end') + '.data') or {}).get('agentLineResult') or {}).get('taskId'),
        'readerPath': 'blockOutputs.guard.result.result.taskId',
    }
    if native.get('workflowId') != wid or native.get('runId') != case['finalRunId']:
        raise RuntimeError('final_native_identity_mismatch')
    if (case['nativeGuardAndEnd']['endTaskId'] != tasks[1]['id']
            or case['nativeGuardAndEnd']['guardTaskId'] != tasks[1]['id']
            or case['nativeGuardAndEnd']['guardNeedsRepair'] is not False):
        raise RuntimeError('native_output_not_round2')
    if any(item['method'] != 'GET' for item in calls):
        raise RuntimeError('reader_finish_must_not_write')
    with store.connect() as db:
        leases = db.execute('SELECT count(*) FROM workspace_leases').fetchone()[0]
    if leases:
        raise RuntimeError('leases_remain')
    state['finalRunId'] = case['finalRunId']
    state['result'] = 'passed_live'
    state.pop('failure', None)
    state['openedReaderPath'] = 'native guard result.result.taskId'
    write(path, state)
    report = {
        'status': 'passed_live', 'acceptance': 'AC-39', 'workflowId': wid, 'runId': run_id,
        'finalRunId': case['finalRunId'], 'bindingId': state['bindingId'], 'lateProof': case['lateProof'],
        'rounds': [case['tasks'][task['id']] for task in tasks],
        'nativeGuardAndEnd': case['nativeGuardAndEnd'], 'activeLeaseCount': 0,
        'openedPath': state['openedPath'], 'openedReaderPath': state['openedReaderPath'],
        'restrictionsLeftClosed': state['restrictionsLeftClosed'],
        'nativeWritesThisFinish': 0,
        'limitations': [
            'One existing native role and gpt-6-luna executor',
            'Late result is the original round-one completion, not a second executor',
            'Main adapter admission and role configuration were not changed',
            'Finish only re-read the completed native guard wrapper; no new submission',
        ],
    }
    write(DATA / 'report.json', report)
    write(DATA / 'failure.json', {'status': 'superseded', 'code': 'native_reader_path_opened'})
    sim.close()
    print(json.dumps({'status': 'passed_live', 'taskCount': 2, 'lateProof': True, 'nativeWrites': 0}), flush=True)
    return 0


def main():
    DATA.mkdir(mode=0o700, exist_ok=True)
    path = DATA / 'state.json'
    auth = json.loads((ROOT / 'var/sim/probe-auth.json').read_text())
    sim = SimClient(auth['baseUrl'], auth['apiKey'])
    calls = []

    def note(request):
        context_id = None
        try:
            raw = request.content
            if raw:
                body = json.loads(raw)
                if isinstance(body, dict):
                    context_id = body.get('contextId')
        except Exception:
            context_id = None
        calls.append({'method': request.method, 'path': request.url.path, 'contextId': context_id})

    sim._client.event_hooks['request'].append(note)
    if path.exists() and json.loads(path.read_text()).get('result') == 'passed_live':
        state = json.loads(path.read_text())
        observed = sim.observe(state['workflowId'], state['finalRunId'])
        result = {
            'mode': 'observation_only',
            'newSubmissions': 0,
            'nativeStatus': observed.get('status') if isinstance(observed, dict) else None,
            'nativeWritesThisInvocation': sum(item['method'] != 'GET' for item in calls),
        }
        if result['nativeWritesThisInvocation']:
            raise RuntimeError('observation_must_not_write')
        write(DATA / 'observation.json', result)
        sim.close()
        print(json.dumps({'mode': 'observation_only', 'newSubmissions': 0}))
        return 0
    guard_report = json.loads((ROOT / 'var/sim-finite-loop-guard/report.json').read_text())
    if guard_report.get('status') != 'passed_live':
        raise RuntimeError('native_guard_live_proof_required')
    gateway = None
    host = None
    process = json.loads((ROOT / 'var/qwenpaw/agent-line-process.json').read_text())
    quiet_host(process['pid'])
    subprocess.run([sys.executable, 'scripts/check_upstreams.py'], cwd=ROOT, check=True, capture_output=True, text=True)
    config = json.loads((ROOT / 'var/qwenpaw/config.json').read_text())
    bridge = config['plugins']['agent-line']['execution_bridge']
    if not bridge.get('container_control'):
        raise RuntimeError('container_control_required')
    workspace = Path(bridge['workspace']).resolve(strict=True)
    prior = json.loads((ROOT / 'var/native-memory-roundtrip/attempt4-report.json').read_text())
    if prior.get('verified') is not True or prior.get('activeLeaseCount') != 0 or prior.get('ownedActiveContainerCount') != 0:
        raise RuntimeError('prior_native_proof_required')
    if path.exists():
        saved = json.loads(path.read_text())
        if saved.get('failure', {}).get('code') == 'native_output_not_round2' and saved.get('result') != 'passed_live':
            return finish_native_reader(sim, calls, saved, path)
        raise RuntimeError('incomplete_state_preserved')
    suffix = uuid.uuid4().hex[:16]
    case = {
        'artifact': 'ac39-late-' + suffix + '.txt',
        'correctMarker': 'AC39_CORRECT_' + suffix,
        'wrongMarker': 'AC39_WRONG_' + suffix,
        'expectedRounds': 2,
        'tasks': {},
    }
    if (workspace / case['artifact']).exists():
        raise RuntimeError('artifact_exists')
    instruction = (
        'This is an authorized bounded loop test. Read artifact, correctMarker and wrongMarker from the nested taskInput.input data; read the zero-based iteration from repair.iteration. '
        'Delegate actual file work to Codex. At iteration 0 write wrongMarker plus one newline. At iteration 1 use repair.previousResult verification failure to correct the file to correctMarker plus one newline. '
        'Only write the specified dedicated relative artifact in the execution workspace, using a shell command and checking exact content. Never modify other files, network, permissions or memory. '
        'Do not call coordination tools from Codex. Do not guess an absolute cwd. Do not claim independent verification: the external checker decides. '
        'In iteration 1 the delegated instruction MUST include the exact repair.previousResult.taskId and copy the previous failed artifact check reason and actualSha256 as evidence.'
    )
    state = {
        'steps': {}, 'case': case, 'suffix': suffix, 'instruction': instruction, 'createdAt': time.time(),
        'roleAgentId': ROLE, 'projectId': PROJECT, 'roleBindingId': BINDING, 'parentTimeoutSeconds': 900,
        'openedPath': 'existing native-loop role executor model gpt-6-luna',
        'restrictionsLeftClosed': [
            'taskField', 'codex sandbox', 'apparmor', 'user namespace', 'reme',
            'role model', 'activation identity check',
        ],
    }
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
        for name, cls in [
            ('tasks.sqlite', TaskStore), ('roles.sqlite', RoleDispatchLedger), ('sim.sqlite', SimJournal),
            ('runs.sqlite', WorkflowRunLedger), ('pauses.sqlite', PauseBindings),
        ]:
            cls.initialize(DATA / name)
            os.chmod(DATA / name, 0o600)
        store = TaskStore(DATA / 'tasks.sqlite')
        ledger = RoleDispatchLedger(DATA / 'roles.sqlite')
        journal = SimJournal(DATA / 'sim.sqlite')
        api = SimAuthoringClient(sim, auth['workspaceId'])
        original_http = sim._client.request
        def traced_http(method, url, **kwargs):
            response = original_http(method, url, **kwargs)
            if method != 'GET':
                keys = None
                try:
                    payload = response.json()
                    keys = list(payload)[:8] if isinstance(payload, dict) else type(payload).__name__
                except Exception as exc:
                    keys = type(exc).__name__
                write(DATA / 'sim-last.json', {
                    'status': response.status_code, 'keys': keys, 'method': method,
                    'path': str(url).split('/api/v2/')[-1][:120],
                })
            return response
        sim._client.request = traced_http
        projects = {PROJECT: {'workspace': str(workspace)}}
        live_binding = json.loads((ROOT / 'var/adapter/service.json').read_text())['bindings'][BINDING]
        if live_binding.get('projectId') != PROJECT or live_binding.get('roleAgentId') != ROLE:
            raise RuntimeError('live_role_binding_changed')
        expected = (case['correctMarker'] + '\n').encode()
        bindings = {BINDING: {
            'projectId': PROJECT,
            'roleAgentId': ROLE,
            'roleRevision': live_binding['roleRevision'],
            'verificationPolicy': {
                'schemaVersion': 1,
                'policyId': 'ac39-late-' + suffix,
                'kind': 'artifact_sha256',
                'requireTests': False,
                'artifacts': [{
                    'path': case['artifact'],
                    'sha256': hashlib.sha256(expected).hexdigest(),
                    'exactSize': len(expected),
                    'maxBytes': 1024,
                }],
            },
        }}
        state['publicationGate'] = 'existing role revision plus probe-local artifact policy; main service binding unchanged'
        persist()
        author_token = secrets.token_urlsafe(40)
        access = AuthoringAccess({author_token: TrustedPrincipal(
            'ac39-late-result-editor', frozenset({PROJECT}), frozenset({'edit_workflows', 'publish_workflows'}), time.time() + 7200,
        )})
        authoring = AuthoringAPI(access, journal, {PROJECT: api}, projects=projects, role_bindings=bindings)
        write(DATA / 'task-flow.json', {
            'schemaVersion': 1, 'runLedgerPath': str(DATA / 'runs.sqlite'), 'pauseBindingsPath': str(DATA / 'pauses.sqlite'),
        })
        flow = load_task_flow(DATA / 'task-flow.json', tasks=store, projects=projects, role_bindings=bindings, client=sim, journal=journal)
        installation = 'ac39-late-result-' + suffix
        wf = WorkflowGateway(sim, journal, {}, installation_id=installation)
        wf.task_flow = flow
        wf.publication_verifier = flow.discovery.publication_verifier
        service_token = (ROOT / 'var/qwenpaw-secrets/execution-service-token').read_text().strip()
        gateway = RoleDispatchGateway(
            base, token, service_token, role_agent_id=ROLE, deployment_id='agent-line-g0-installation',
            ledger=ledger, memory_query=prior_state['query'], client=host, timeout_seconds=900,
        )
        dispatcher = Dispatcher(store, {(PROJECT, BINDING): gateway})
        credential = secrets.token_urlsafe(40)
        write(DATA / 'service-token.json', {'token': credential})
        evidence = DATA / 'evidence'
        evidence.mkdir(mode=0o700)
        os.chmod(evidence, 0o700)
        verifier = AutomaticVerifier(store, evidence)
        app = create_app(
            store, ServiceConfig(tokens={credential: {PROJECT}}, projects=projects, bindings=bindings, controlled_execution='confirmed'),
            workflows=wf, dispatcher=dispatcher, task_flow=flow, authoring=authoring, verifier=verifier,
        )
        with TestClient(app) as http:
            def author(operation, route, payload):
                def request():
                    response = http.post(
                        '/extension/v1/authoring/projects/' + PROJECT + '/workflows' + route,
                        headers={'Authorization': 'Bearer ' + author_token}, json=payload,
                    )
                    if response.status_code != 202:
                        raise RuntimeError('authoring_http_' + str(response.status_code))
                    data = response.json()
                    if data.get('status') != 'accepted':
                        raise RuntimeError('authoring_not_accepted')
                    return data
                return once(operation, request)['receipt']

            created = author('create', '', {
                'operationId': 'create-' + suffix, 'name': 'agent-line-ac39-late-result-' + suffix,
                'description': 'Live late round-one result after round two starts',
            })
            wid = created['workflowId']
            state['workflowId'] = wid
            persist()
            agent_config = {
                'roleBindingId': BINDING, 'instruction': instruction,
                'inputFrom': {'nodeId': 'start', 'field': 'input'}, 'memoryPolicyId': 'native-role',
                'outputSchemaId': 'task-result', 'requireMemoryCommit': False, 'allowFailureBranch': True,
            }

            def node(node_id, kind, name, node_config, xpos):
                return {'id': node_id, 'type': kind, 'name': name, 'position': {'x': xpos, 'y': 0}, 'config': node_config}

            doc = {
                'schemaVersion': 1,
                'nodes': [
                    node('start', 'start', '开始', {}, 0),
                    node('repair', 'loop', '有界修复', {'body': 'agent', 'maxIterations': 2}, 200),
                    node('agent', 'agent', '执行节点', agent_config, 50),
                    node('end', 'end', '最终任务结果', {'resultFrom': 'agent'}, 750),
                ],
                'edges': [
                    {'id': 'a', 'source': 'start', 'target': 'repair'},
                    {'id': 'b', 'source': 'repair', 'target': 'agent', 'branch': 'body'},
                    {'id': 'c', 'source': 'repair', 'target': 'end', 'branch': 'done'},
                ],
                'variables': {},
            }
            state['document'] = doc
            persist()
            saved = author('save', '/' + wid + '/save', {
                'operationId': 'save-' + suffix, 'expectedStateSha256': state_hash(api.get_state(wid)), 'document': doc,
            })
            if saved.get('outcome') != 'saved':
                raise RuntimeError('save_not_verified')
            published = author('publish', '/' + wid + '/publish', {
                'operationId': 'publish-' + suffix, 'expectedStateSha256': state_hash(api.get_state(wid)),
            })
            if published.get('outcome') != 'published':
                raise RuntimeError('publish_not_verified')
            binding = published['bindingId']
            state['bindingId'] = binding
            state['publication'] = published
            persist()
            catalog = http.get('/extension/v1/workflows', headers={'Authorization': 'Bearer ' + credential})
            catalog.raise_for_status()
            if binding not in [item['bindingId'] for item in catalog.json()['workflows']]:
                raise RuntimeError('published_binding_not_discovered')
            if wf.bindings:
                raise RuntimeError('static_binding_forbidden')
            bid = native_id(wid, 'agent')
            quiet_host(process['pid'])
            body = {'idempotencyKey': 'late-' + suffix, 'input': {'input': {
                'artifact': case['artifact'], 'correctMarker': case['correctMarker'], 'wrongMarker': case['wrongMarker'],
            }}}
            run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, canonical([installation, PROJECT, wid, body['idempotencyKey'], binding])))
            case.update(runId=run_id, startPayload=body, startIntent=True)
            persist()
            started = http.post('/extension/v1/workflows/' + binding + '/runs', headers={'Authorization': 'Bearer ' + credential}, json=body)
            case.update(startHTTP=started.status_code, startReceipt=started.json())
            persist()
            if started.status_code != 202 or started.json().get('status') != 'accepted':
                raise RuntimeError('start_not_accepted')
            case['frozenPublication'] = journal.get_publication(wid, run_id)
            persist()
            deadline = time.monotonic() + 3600
            last_print = 0
            while time.monotonic() < deadline:
                tasks = [task for task in store.list_tasks({PROJECT})['tasks'] if task['payload']['workflow']['runId'] == run_id]
                tasks.sort(key=lambda task: int(task['payload']['workflow']['attemptId']))
                if len(tasks) > case['expectedRounds']:
                    raise RuntimeError('unexpected_extra_loop_task')
                for task in tasks:
                    iteration = int(task['payload']['workflow']['attemptId']) - 1
                    entry = case['tasks'].setdefault(task['id'], {})
                    entry['taskId'] = task['id']
                    entry['contextId'] = task['payload']['workflow']['nodeExecutionId']
                    if entry['contextId'] != bid + '_loop' + str(iteration):
                        raise RuntimeError('activation_identity_mismatch')
                    if iteration:
                        feedback = task['payload']['input']['repair']['previousResult']
                        if feedback.get('taskId') != tasks[iteration - 1]['id'] or feedback.get('verificationStatus') != 'failed':
                            raise RuntimeError('prior_feedback_missing')
                        entry['previousResumeStatus'] = journal.get(wid, run_id, bid + '_loop' + str(iteration - 1))['status']
                    if (task['executionStatus'] == 'succeeded' and task['resourcesReleased']
                            and task['verificationStatus'] in ('passed', 'failed') and not entry.get('verificationChecked')):
                        expected_status = 'passed' if iteration == 1 else 'failed'
                        actual = (workspace / case['artifact']).read_bytes()
                        wanted = (case['correctMarker'] if expected_status == 'passed' else case['wrongMarker']) + '\n'
                        if actual != wanted.encode() or task['verificationStatus'] != expected_status:
                            raise RuntimeError('controlled_artifact_outcome_mismatch')
                        entry['verificationChecked'] = True
                        entry['verification'] = task['verificationStatus']
                        persist()
                    if task['executionStatus'] in ('failed', 'canceled'):
                        raise RuntimeError('native_execution_not_succeeded')
                if len(tasks) == 2 and not case.get('lateProof') and tasks[1]['executionStatus'] not in ('succeeded', 'failed', 'canceled'):
                    current = store.get(tasks[0]['id'])
                    previous_record = journal.get(wid, run_id, bid + '_loop0')
                    if (current['verificationStatus'] == 'failed' and current['resourcesReleased'] is True
                            and previous_record['status'] == 'accepted'):
                        pauses = PauseBindings(DATA / 'pauses.sqlite')
                        relay = CompletionRelay(sim, journal)
                        real = pauses.get(current['id'])
                        if real.context_id != bid + '_loop0' or real.block_id != bid:
                            raise RuntimeError('round0_binding_mismatch')
                        before_file = (workspace / case['artifact']).read_bytes()
                        before_current = canonical(stable_view(store.get(tasks[1]['id'])))
                        before_frozen = canonical(journal.resume_input(wid, run_id, real.context_id))
                        before_receipt = canonical(previous_record.get('receipt'))
                        before_len = len(calls)
                        late = relay.deliver(store.get(current['id']), real)
                        forged = ResumeBinding(
                            real.task_id, real.workflow_id, real.run_id, real.block_id, bid + '_loop1',
                            real.purpose, real.require_memory_commit, real.allow_failure_branch, real.allow_multiple,
                        )
                        mismatch = None
                        try:
                            relay.deliver(store.get(current['id']), forged)
                        except ValueError as exc:
                            mismatch = str(exc)
                        mutated = dict(store.get(current['id']))
                        mutated['verificationStatus'] = 'passed'
                        conflict = None
                        try:
                            relay.deliver(mutated, real)
                        except Conflict:
                            conflict = 'Conflict'
                        delta = calls[before_len:]
                        posts = [item for item in delta if item['method'] != 'GET']
                        after_file = (workspace / case['artifact']).read_bytes()
                        after_current = canonical(stable_view(store.get(tasks[1]['id'])))
                        after_frozen = canonical(journal.resume_input(wid, run_id, real.context_id))
                        after_receipt = canonical(journal.get(wid, run_id, real.context_id).get('receipt'))
                        still = [task for task in store.list_tasks({PROJECT})['tasks'] if task['payload']['workflow']['runId'] == run_id]
                        if posts or after_file != before_file or after_current != before_current or after_frozen != before_frozen or after_receipt != before_receipt:
                            raise RuntimeError('late_result_overwrote_current_round')
                        if late.get('status') != 'accepted' or late.get('contextId') != real.context_id:
                            raise RuntimeError('late_result_not_historical')
                        if mismatch != 'Task does not belong to the bound workflow activation' or conflict != 'Conflict' or len(still) != 2:
                            raise RuntimeError('late_result_not_isolated')
                        case['lateProof'] = {
                            'round2TaskId': tasks[1]['id'],
                            'round2ContextId': tasks[1]['payload']['workflow']['nodeExecutionId'],
                            'round2ExecutionStatus': tasks[1]['executionStatus'],
                            'historicalStatus': late['status'],
                            'historicalContextId': late['contextId'],
                            'mismatch': mismatch,
                            'changedFacts': conflict,
                            'simMutations': posts,
                            'fileUnchanged': True,
                            'round2ViewUnchanged': True,
                            'frozenInputUnchanged': True,
                            'taskCount': 2,
                        }
                        persist()
                        print(json.dumps({'phase': 'late_result_isolated', 'round2TaskId': tasks[1]['id']}), flush=True)
                observed = http.get('/extension/v1/workflows/' + binding + '/runs/' + run_id, headers={'Authorization': 'Bearer ' + credential})
                observed.raise_for_status()
                view = observed.json()
                latest = view.get('resumption', {})
                execution = latest.get('execution', view['execution'])
                case['workers'] = {
                    'discovery': flow.workers[0].last_report,
                    'completion': flow.workers[1].last_report,
                    'execution': app.state.execution_worker.last_report,
                }
                persist()
                if time.monotonic() - last_print > 30:
                    print(json.dumps({
                        'phase': 'observing', 'taskCount': len(tasks),
                        'latestExecutionStatus': tasks[-1]['executionStatus'] if tasks else None,
                        'simStatus': execution.get('status'), 'lateProof': bool(case.get('lateProof')),
                    }), flush=True)
                    last_print = time.monotonic()
                if execution.get('status') == 'completed' and len(tasks) == case['expectedRounds']:
                    case['finalRunId'] = latest.get('runId', run_id)
                    break
                if execution.get('status') in ('failed', 'cancelled'):
                    raise RuntimeError('native_loop_run_failed')
                time.sleep(2)
            else:
                raise RuntimeError('observation_deadline_original_identities_preserved')
            if not case.get('lateProof'):
                raise RuntimeError('late_result_window_missed')
            tasks = [task for task in store.list_tasks({PROJECT})['tasks'] if task['payload']['workflow']['runId'] == run_id]
            tasks.sort(key=lambda task: int(task['payload']['workflow']['attemptId']))
            if len(tasks) != 2:
                raise RuntimeError('round_count_changed_after_late_result')
            for task in tasks:
                fresh = store.get(task['id'])
                if not fresh['resourcesReleased'] or not any(item.get('kind') == 'resource_exit' for item in fresh['evidence']):
                    raise RuntimeError('resource_exit_missing')
                case['tasks'][task['id']]['model'] = model_slug(fresh, Path(bridge['codex_home']))
                case['tasks'][task['id']]['verificationStatus'] = fresh['verificationStatus']
                case['tasks'][task['id']]['executionStatus'] = fresh['executionStatus']
            last = store.get(tasks[-1]['id'])
            if last['verificationStatus'] != 'passed' or tasks[0]['verificationStatus'] != 'failed':
                raise RuntimeError('round_outcomes_not_distinct')
            resume_value = journal.resume_input(wid, run_id, last['payload']['workflow']['nodeExecutionId'])
            if resume_value.get('taskId') != last['id']:
                raise RuntimeError('final_resume_not_round2')
            round0_again = CompletionRelay(sim, journal).deliver(store.get(tasks[0]['id']), PauseBindings(DATA / 'pauses.sqlite').get(tasks[0]['id']))
            if round0_again.get('contextId') != tasks[0]['payload']['workflow']['nodeExecutionId'] or round0_again.get('status') != 'accepted':
                raise RuntimeError('final_late_result_retargeted')
            if journal.resume_input(wid, run_id, last['payload']['workflow']['nodeExecutionId']).get('taskId') != last['id']:
                raise RuntimeError('final_output_overwritten')
            selectors = native_id(wid, 'repair') + '--guard,' + native_id(wid, 'end') + '.data'
            selected = sim._client.get(
                sim.base_url + f'/api/v2/workflows/{wid}/runs/' + case['finalRunId'],
                params={'selectedOutputs': selectors}, headers={'x-api-key': sim._key}, follow_redirects=False,
            )
            selected.raise_for_status()
            native = selected.json()['data']
            guard = native.get('blockOutputs', {}).get(native_id(wid, 'repair') + '--guard', {}).get('result') or {}
            guard_result = guard.get('result') if isinstance(guard, dict) else None
            case['nativeGuardAndEnd'] = {
                'workflowId': native.get('workflowId'), 'runId': native.get('runId'),
                'guardTaskId': guard_result.get('taskId') if isinstance(guard_result, dict) else None,
                'guardNeedsRepair': guard.get('needsRepair') if isinstance(guard, dict) else None,
                'endTaskId': ((native.get('blockOutputs', {}).get(native_id(wid, 'end') + '.data') or {}).get('agentLineResult') or {}).get('taskId'),
            }
            if (case['nativeGuardAndEnd']['endTaskId'] != last['id']
                    or case['nativeGuardAndEnd']['guardTaskId'] != last['id']
                    or case['nativeGuardAndEnd']['guardNeedsRepair'] is not False):
                raise RuntimeError('native_output_not_round2')
            quiet_host(process['pid'])
            with store.connect() as db:
                leases = db.execute('SELECT count(*) FROM workspace_leases').fetchone()[0]
            if leases:
                raise RuntimeError('leases_remain')
            state['finalRunId'] = case['finalRunId']
            state['result'] = 'passed_live'
            persist()
            report = {
                'status': 'passed_live', 'acceptance': 'AC-39', 'workflowId': wid, 'runId': run_id,
                'finalRunId': case['finalRunId'], 'bindingId': binding, 'lateProof': case['lateProof'],
                'rounds': [case['tasks'][task['id']] for task in tasks],
                'nativeGuardAndEnd': case['nativeGuardAndEnd'], 'activeLeaseCount': 0,
                'openedPath': state['openedPath'], 'restrictionsLeftClosed': state['restrictionsLeftClosed'],
                'limitations': [
                    'One existing native role and gpt-6-luna executor',
                    'Late result is the original round-one completion, not a second executor',
                    'Main adapter admission and role configuration were not changed',
                ],
            }
            write(DATA / 'report.json', report)
            print(json.dumps({'status': 'passed_live', 'taskCount': 2, 'lateProof': True}), flush=True)
            return 0
    except Exception as exc:
        state['failure'] = {
            'type': type(exc).__name__,
            'code': str(exc) if type(exc) is RuntimeError else 'probe_failed',
        }
        persist()
        write(DATA / 'failure.json', {'status': 'incomplete', **state['failure']})
        print(json.dumps({'status': 'incomplete', **state['failure']}), flush=True)
        return 1
    finally:
        if gateway is not None:
            gateway.close()
        if host is not None:
            host.close()
        sim.close()


if __name__ == '__main__':
    raise SystemExit(main())
