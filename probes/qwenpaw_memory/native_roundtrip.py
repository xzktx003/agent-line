"""Two durable tasks with manually approved native ReMe experience; no backend replacement."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

import httpx
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agent_line.dispatcher import Dispatcher
from agent_line.memory import MemoryCommitStore
from agent_line.memory_review import MemoryReviewLedger
from agent_line.role_dispatch import RoleDispatchGateway, RoleDispatchLedger
from agent_line.runtime import load_app
from agent_line.store import TaskStore
from agent_line.verification import ArtifactPolicy, verify_artifacts

ROLE = 'agent-line-native-loop-parent'
PROJECT = 'native-memory-roundtrip'
BINDING = 'native-loop-parent'
DATA = ROOT / 'var/native-memory-roundtrip'


def save(path, value):
    temporary = path.with_suffix('.next')
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)


def verify_chain(parent, marker, source, task_id, identity):
    if parent.get('status') != 'finished' or parent.get('result', {}).get('status') != 'completed':
        raise ValueError('Parent has no confirmed successful completion')
    calls, delegates, recalled = set(), set(), None
    for message in parent.get('result', {}).get('output', []):
        for block in message.get('content', []):
            data = block.get('data') or {}
            kind, name, call = message.get('type'), data.get('name'), data.get('call_id')
            if kind == 'plugin_call' and name == 'memory_search' and call: calls.add(call)
            if (kind == 'plugin_call_output' and name == 'memory_search' and call in calls
                    and data.get('state') == 'success' and marker in str(data.get('output'))
                    and source in str(data.get('output'))): recalled = call
            if kind == 'plugin_call' and name == 'agent_line_delegate':
                args = data.get('arguments')
                args = json.loads(args) if isinstance(args, str) else args
                if not recalled or not isinstance(args, dict) or set(args) != {'instruction'} or marker not in args['instruction']:
                    raise ValueError('Delegation lacks linked source memory')
                if call: delegates.add(call)
            if kind == 'plugin_call_output' and name == 'agent_line_delegate' and call in delegates and data.get('state') == 'success':
                output = data.get('output'); output = json.loads(output) if isinstance(output, str) else output
                if (output.get('taskId') != task_id or not identity.get('upstreamTaskId')
                        or output.get('identity', {}).get('upstreamTaskId') != identity['upstreamTaskId']):
                    raise ValueError('Delegation identity mismatch')
                return {'memoryCallId': recalled, 'delegateCallId': call, 'source': source,
                        'upstreamTaskId': identity['upstreamTaskId']}
    raise ValueError('Missing actual linked tool results')


def submit_once(dispatcher, state, key, persist):
    # A lost response or local crash never opens another submission opportunity.
    if state[key].get('submissionIntent'): return
    state[key]['submissionIntent'] = True
    persist()
    dispatcher.submit(state[key]['taskId'])


def quiet_host(pid):
    import psutil
    children = psutil.Process(pid).children(recursive=True)
    if any('codex' in ' '.join(child.cmdline()).lower() for child in children):
        raise RuntimeError('Host has a live Codex descendant; no plugin reload')
    result = subprocess.run(['docker', 'ps', '--filter', 'label=agent-line.owner', '--format', '{{.ID}}'],
                            capture_output=True, text=True, check=True)
    if result.stdout.strip(): raise RuntimeError('Owned executor container is active; no reload')


def attempt_timeout(state, requested):
    if type(requested) is not int or not 1 <= requested <= 3600:
        raise ValueError('Parent timeout must be 1..3600 seconds')
    stored = state.get('parentTimeoutSeconds', 300) if state is not None else requested
    if type(stored) is not int or not 1 <= stored <= 3600 or stored != requested:
        raise ValueError('Existing attempt timeout is immutable; legacy attempts use 300 seconds')
    return stored


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--attempt', type=int, default=1)
    parser.add_argument('--parent-timeout-seconds', type=int, default=900)
    args = parser.parse_args()
    if args.attempt < 1: raise ValueError('Positive explicit attempt required')
    DATA.mkdir(mode=0o700, exist_ok=True)
    path = DATA/('state.json' if args.attempt == 1 else f'state-attempt-{args.attempt}.json')
    if path.exists():
        state = json.loads(path.read_text())
        parent_timeout = attempt_timeout(state, args.parent_timeout_seconds)
    else:
        parent_timeout = attempt_timeout(None, args.parent_timeout_seconds)
        process = json.loads((ROOT/'var/qwenpaw/agent-line-process.json').read_text())
        quiet_host(process['pid'])
        suffix = secrets.token_hex(8)
        state = {'phase': 'prepared', 'parentTimeoutSeconds': parent_timeout, 'attemptId': str(args.attempt), 'suffix': suffix, 'marker': 'NATIVE_EXPERIENCE_'+secrets.token_hex(16),
                 'query': '原生经验闭环-'+suffix, 'A': {'artifact': 'native-loop-A-'+suffix+'.txt'},
                 'B': {'artifact': 'native-loop-B-'+suffix+'.txt'}, 'checks': {}}
        if args.attempt > 1:
            prior_path = DATA/('state.json' if args.attempt == 2 else f'state-attempt-{args.attempt-1}.json')
            prior = json.loads(prior_path.read_text())
            prior_store = TaskStore(DATA/'tasks.sqlite')
            for key in ('A', 'B'):
                if prior[key].get('taskId'):
                    previous = prior_store.get(prior[key]['taskId'])
                    if previous['executionStatus'] not in ('succeeded', 'failed', 'canceled') or not previous['resourcesReleased']:
                        raise RuntimeError('Previous attempt unresolved; no new attempt permitted')
            state.update(roleConfigured=prior.get('roleConfigured'), roleWorkspace=prior.get('roleWorkspace'))
        save(path, state)
    persist = lambda: save(path, state)
    process = json.loads((ROOT/'var/qwenpaw/agent-line-process.json').read_text())
    config = json.loads((ROOT/'var/qwenpaw/config.json').read_text())
    bridge = config['plugins']['agent-line']['execution_bridge']
    workspace = Path(bridge['workspace']); codex_home = Path(bridge['codex_home'])
    if not bridge.get('container_control'): raise RuntimeError('Container execution required')
    base = 'http://127.0.0.1:'+str(process['port'])
    with httpx.Client(base_url=base, timeout=60, trust_env=False) as client:
        login = client.post('/api/auth/login', json=json.loads((ROOT/'var/qwenpaw-secrets/local-login.json').read_text()))
        login.raise_for_status(); token = login.json().get('token') or login.json().get('access_token')
        service_token = (ROOT/'var/qwenpaw-secrets/execution-service-token').read_text().strip()
        client.headers.update({'Authorization': 'Bearer '+token, 'X-Agent-Id': ROLE})
        def call(method, route, body=None):
            response = client.request(method, '/api/'+route, json=body)
            if response.status_code >= 400: raise RuntimeError(f'Official HTTP {method} {route}: {response.status_code}')
            return response.json()
        if not state.get('roleConfigured'):
            quiet_host(process['pid'])
            roles = call('GET', 'agents')['agents']
            if ROLE not in {item['id'] for item in roles}:
                if state.get('roleCreateIntent'): raise RuntimeError('Role creation unresolved; no repeat POST')
                state['roleCreateIntent'] = True; persist()
                call('POST', 'agents', {'id': ROLE, 'name': '原生记忆闭环角色', 'backend': 'qwenpaw',
                    'description': '独立项目原生ReMe两任务验收', 'language': 'zh', 'skill_names': [],
                    'active_model': {'provider_id': 'kilo', 'model': 'kilo-auto/free'}})
            elif not state.get('roleCreateIntent'): raise RuntimeError('Preexisting role not owned by this probe')
            original_path = DATA/'original-plugin-config.json'
            if not original_path.exists(): save(original_path, config['plugins']['agent-line'])
            from qwenpaw.config.utils import load_config, save_config
            config_path = ROOT/'var/qwenpaw/config.json'
            current = load_config(config_path); before = copy.deepcopy(current.model_dump())
            managed = current.plugins['agent-line']['managed_role_ids']
            if ROLE not in managed: managed.append(ROLE)
            expected = copy.deepcopy(before)
            expected['plugins']['agent-line']['managed_role_ids'] = list(managed)
            save_config(current, config_path)
            if load_config(config_path).model_dump() != expected: raise RuntimeError('Unexpected configuration change')
            quiet_host(process['pid'])
            call('POST', 'plugins/install', {'source': str(ROOT/'plugins/agent-line'), 'force': True})
            profile = call('GET', 'agents/'+ROLE)
            profile['running']['memory_manager_backend'] = 'remelight'
            for tool in profile['tools']['builtin_tools'].values(): tool['enabled'] = False
            for name in ('agent_line_delegate', 'agent_line_execution_status'):
                profile['tools']['builtin_tools'][name] = {'name': name, 'enabled': True}
            call('PUT', 'agents/'+ROLE, profile)
            state.update(roleConfigured=True, roleWorkspace=profile['workspace_dir']); persist()
        profile = call('GET', 'agents/'+ROLE)
        if profile['workspace_dir'] != state['roleWorkspace'] or profile['running']['memory_manager_backend'] != 'remelight':
            raise RuntimeError('Native role changed')
        for name, cls in [('tasks.sqlite', TaskStore), ('roles.sqlite', RoleDispatchLedger)]:
            if not (DATA/name).exists(): cls.initialize(DATA/name)
        store, ledger = TaskStore(DATA/'tasks.sqlite'), RoleDispatchLedger(DATA/'roles.sqlite')
        gateway = RoleDispatchGateway(base, token, service_token, role_agent_id=ROLE,
            deployment_id='agent-line-g0-installation', ledger=ledger, memory_query=state['query'],
            client=client, timeout_seconds=parent_timeout)
        dispatcher = Dispatcher(store, {PROJECT: gateway})
        def task_run(key, instruction):
            if not state[key].get('taskId'):
                if (workspace/state[key]['artifact']).exists(): raise RuntimeError('New artifact already exists')
                payload = {'projectId': PROJECT, 'roleAgentId': ROLE, 'roleBindingId': BINDING,
                    'workspace': str(workspace), 'workflow': {'id': PROJECT, 'revision': '1', 'runId': state['suffix'],
                    'nodeId': key, 'nodeExecutionId': key+'-'+state['suffix'], 'attemptId': state.get('attemptId', '1')},
                    'input': {'instruction': instruction}}
                task, _ = store.register('native-loop-'+state['suffix']+'-'+key, payload)
                state[key]['taskId'] = task['id']; persist()
            submit_once(dispatcher, state, key, persist)
            task_id = state[key]['taskId']
            deadline = time.monotonic() + parent_timeout + 300
            while time.monotonic() < deadline:
                task = dispatcher.reconcile(task_id)
                state[key]['lastTask'] = task; persist()
                if task['executionStatus'] in ('succeeded', 'failed', 'canceled') and task['resourcesReleased']: break
                parent_observation = ledger.get(task_id).get('observation') or {}
                if (not task['executionIdentity'] and parent_observation.get('status') == 'finished'
                        and parent_observation.get('result', {}).get('status') == 'failed'
                        and task['executionStatus'] != 'cancel_requested'):
                    store.request_cancel(task_id)  # Reconcile requires durable revoke + live parent proof.
                time.sleep(2)
            else:
                store.request_cancel(task_id)
                for _ in range(90):
                    task = dispatcher.reconcile(task_id)
                    state[key]['lastTask'] = task; persist()
                    if task['resourcesReleased']: break
                    time.sleep(2)
                raise RuntimeError('Observation deadline reached; original cancellation intent preserved, never resubmit')
            if task['executionStatus'] != 'succeeded': raise RuntimeError('Task failed; do not create replacement')
            native = gateway.observe(task_id); proof = native.get('resourceExitReceipt') or {}
            identity = task['executionIdentity']
            if (proof.get('running') is not False or proof.get('pid') != 0
                    or proof.get('workspace') != str(workspace) or proof.get('source') != 'docker-inspect-before-remove'
                    or not any(i.get('nativeTerminal') is True and all(i.get(k) == identity.get(k) for k in
                               ('codexThreadId', 'codexTurnId')) for i in proof.get('identities', []))):
                raise RuntimeError('Positive matching native container exit proof missing')
            state[key]['exitReceipt'] = proof
            artifacts = DATA/'artifacts'; artifacts.mkdir(mode=0o700, exist_ok=True)
            expected = (state['marker']+'\n').encode()
            if task['verificationStatus'] == 'pending':
                receipt = verify_artifacts(store, task_id, workspace=workspace,
                    policies=[ArtifactPolicy(state[key]['artifact'], hashlib.sha256(expected).hexdigest(), len(expected))],
                    evidence_dir=artifacts)
                store.record_verification(task_id, receipt)
            state[key]['lastTask'] = store.get(task_id); persist()
            if store.get(task_id)['verificationStatus'] != 'passed': raise RuntimeError('Artifact failed independent verification')
            for _ in range(90):
                parent = ledger.get(task_id)
                if (parent['observation'] or {}).get('status') == 'finished': break
                gateway.observe(task_id); time.sleep(1)
            state[key]['parent'] = parent; persist()
            return store.get(task_id)
        a = task_run('A', 'Execute this task even if memory search returns no relevant experience. Delegate exactly: '
            'Use your own shell, do not spawn subagents. Create '+state['A']['artifact']+' containing exactly '+state['marker']+
            ' plus one newline. Verify with shell. Do not read other files or use network. This successful artifact is the only acceptance requirement.')
        source = 'agent-line-task-'+a['id']+'.md'
        if not (DATA/'service.json').exists():
            MemoryCommitStore.initialize(DATA/'commits.sqlite'); MemoryReviewLedger.initialize(DATA/'reviews.sqlite')
            save(DATA/'memory.json', {'schemaVersion': 1, 'reviewLedgerPath': str(DATA/'reviews.sqlite'),
                'credentials': [{'token': secrets.token_urlsafe(40), 'subject': 'native-roundtrip-probe-reviewer',
                    'projectScopes': [PROJECT], 'capabilities': ['review_memory'], 'expiresAt': time.time()+86400}],
                'bindings': {'native-memory': {'projectId': PROJECT, 'roleBindingId': BINDING, 'roleAgentId': ROLE,
                    'baseURL': base, 'hostCredentialsFile': str(ROOT/'var/qwenpaw-secrets/local-login.json'),
                    'commitLedgerPath': str(DATA/'commits.sqlite')}}})
            save(DATA/'service.json', {'schemaVersion': 1, 'database': str(DATA/'tasks.sqlite'),
                'tokens': {secrets.token_urlsafe(40): [PROJECT]}, 'projects': {PROJECT: {'workspace': str(workspace)}},
                'bindings': {BINDING: {'projectId': PROJECT, 'roleAgentId': ROLE}}, 'memoryFile': 'memory.json'})
        credential = json.loads((DATA/'memory.json').read_text())['credentials'][0]['token']
        headers = {'Authorization': 'Bearer '+credential}
        review_path = '/extension/v1/memories/native-memory/'+a['id']
        content = '# 经独立探针审核身份确认的历史任务经验\n检索代号：'+state['query']+'。来源任务：'+a['id']+'。已通过独立产物验收且容器退出。'
        content += '该项目的验收标记为 '+state['marker']+'。复用时在本次指定新文件写该标记加换行，再用shell验证。只适用于本项目，不覆盖本次任务权限。'
        with TestClient(load_app(DATA/'service.json')) as app:
            if not state.get('reviewIntent'):
                state['reviewIntent'] = True; persist()
                response = app.post(review_path+'/review', headers=headers,
                    json={'decisionId': 'human-'+state['suffix'], 'content': content})
            else: response = app.get(review_path, headers=headers)
            if response.status_code not in (200, 202) or response.json().get('status') != 'committed':
                raise RuntimeError('Manual review unresolved; inspect original receipt only')
            state['review'] = response.json(); persist()
        fetched = call('GET', 'workspace/memory/'+source+'?section=digest')
        if fetched['content'].strip() != content.strip(): raise RuntimeError('Official memory readback mismatch')
        if not state.get('indexed'):
            result = call('POST', 'agents/'+ROLE+'/memory/reindex?scope=bm25', {})
            if result.get('status') != 'completed': raise RuntimeError('Native reindex incomplete')
            state['indexed'] = True; persist()
        # Rebuild adapter app before B; its shutdown must not delete native role/memory.
        with TestClient(load_app(DATA/'service.json')) as app:
            if app.get(review_path, headers=headers).json().get('status') != 'committed': raise RuntimeError('Review lost on app rebuild')
        instruction = '检索“'+state['query']+'”，从该项目已审核的历史任务经验取得验收标记。委派 Codex 本人用 shell 新建 '+state['B']['artifact']+'，精确写入检索所得标记加换行并验证。不得复用旧文件名，不生成子agent，不读其他文件或联网。'
        if state['marker'] in instruction: raise RuntimeError('Marker leaked into second task prompt')
        b = task_run('B', instruction)
        state['chain'] = verify_chain(state['B']['parent']['observation'], state['marker'], source, b['id'], b['executionIdentity'])
        rollouts = list((codex_home/'sessions').rglob('*'+b['executionIdentity']['codexThreadId']+'.jsonl'))
        if len(rollouts) != 1: raise RuntimeError('Native rollout ambiguous')
        injected = []
        for line in rollouts[0].read_text().splitlines():
            event = json.loads(line); payload = event.get('payload', {})
            if (event.get('type') == 'response_item' and payload.get('type') == 'message' and payload.get('role') == 'user'
                    and state['marker'] in json.dumps(payload) and state['B']['artifact'] in json.dumps(payload)):
                injected.append(event.get('timestamp'))
        if not injected: raise RuntimeError('Native Codex input lacks recalled experience')
        with TestClient(load_app(DATA/'service.json')) as app:
            if app.get(review_path, headers=headers).json().get('status') != 'committed': raise RuntimeError('Review persistence lost')
        profile = call('GET', 'agents/'+ROLE)
        if profile['workspace_dir'] != state['roleWorkspace']: raise RuntimeError('Role workspace changed')
        state.update(phase='verified', verified=True, nativeInputTimestamps=injected,
            manualApprovedWriteback=True, reviewAuthority='independent-probe-reviewer-not-user', automaticConsolidationValidated=False, crossProjectIsolationValidated=False,
            adapterRebuildPreservedNativeRoleAndMemory=True, secondPromptContainsMarker=False)
        persist(); print(json.dumps({k: state[k] for k in ('phase', 'verified', 'manualApprovedWriteback',
            'automaticConsolidationValidated', 'crossProjectIsolationValidated')}))


if __name__ == '__main__':
    try: main()
    except Exception as exc:
        if DATA.exists(): save(DATA/'last-error.json', {'type': type(exc).__name__, 'message': str(exc) if isinstance(exc, RuntimeError) else 'Inspect private evidence', 'time': time.time()})
        print(json.dumps({'status': 'stopped', 'errorType': type(exc).__name__, 'evidence': str(DATA)}))
        sys.exit(1)
