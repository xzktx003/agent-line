#!/usr/bin/env python3
"""Read-only preparation for the isolated native approval slice. Never starts a model.

The --execute path is only for an explicitly opened model window. Existing state
is observation-only; static readiness is not an E2E pass marker.
"""
import argparse
import ast
import json
import os
from pathlib import Path
import re
import stat

ROOT = Path(__file__).resolve().parents[2]
PARENT = 'agent-line-permission-parent'
TARGET = 'agent-line-permission-codex'


def role_payloads(workspace, launcher, model):
    """Official POST /api/agents bodies; no credentials or inherited settings."""
    if not isinstance(model, str) or not model.strip():
        raise ValueError('fixed_model_required')
    return [
        {'id': PARENT, 'name': '隔离审批绑定角色', 'backend': 'qwenpaw',
         'language': 'zh', 'skill_names': [],
         'active_model': {'provider_id':'permission-metadata','model':model}},
        {'id': TARGET, 'name': '隔离原生权限执行角色', 'backend': 'codex',
         'workspace_dir': str(workspace), 'language': 'zh', 'skill_names': [],
         'backend_settings': {'binary': str(launcher), 'model': model,
                              'sandbox': 'workspace-write', 'approval_policy': 'on-request'}},
    ]


def instruction(nonce):
    if not re.fullmatch('[0-9a-f]{32}', nonce):
        raise ValueError('fixed_nonce_required')
    return ('这是原生权限审批探针。只调用一次 shell 工具，显式使用 '
            'sandbox_permissions=require_escalated 请求人工权限；命令为 '
            f"python3 -c \"print('{nonce}')\"。"
            '等待原生审批，不以其他工具或路径绕过。被拒绝就结束，不重试。'
            '不要读取凭据、访问网络、修改工作区或任何其他文件。')


def private_json(path):
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError('private_path_symlink')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('private_file_required')
    return json.loads(path.read_text())


def validation_directory(root, retirement_validation=False):
    return root / ('var/native-permissions-retirement' if retirement_validation
                   else 'var/native-permissions-attempt2')


def prior_retirement_proof(root):
    """New source validation is separate from the unresolved historical task."""
    import hashlib
    proof = private_json(root / 'var/pinned-parallel-role-flow/root-check.json')
    if (proof.get('status') != 'passed_real_static_parallel_read_only'
            or proof.get('taskCount') != 2 or proof.get('activeLeaseCount') != 0
            or proof.get('ownedActiveContainerCount') != 0
            or proof.get('mainNineConfigHashesUnchanged') is not True):
        raise ValueError('real_runtime_retirement_proof_required')
    old = root / 'var/native-permissions-attempt2'
    state = private_json(old / 'state.json')
    audit = private_json(old / 'root-final-check.json')
    if (state.get('hostStopped') is not True or state.get('status') != 'incomplete'
            or audit.get('status') != 'partial_approve_reject_unresolved'
            or audit.get('temporaryHostPidAbsent') is not True
            or audit.get('taskResubmissions') != 0):
        raise ValueError('legacy_unknown_task_must_remain_preserved')
    files = ['state.json', 'tasks.sqlite', 'roles.sqlite', 'receipts.sqlite', 'root-final-check.json']
    return {str((old / name).relative_to(root)): hashlib.sha256((old / name).read_bytes()).hexdigest()
            for name in files}


def inspect(root=ROOT, retirement_validation=False):
    """Only source/lock/state reads. Do not load plugins, Docker, or credentials."""
    lock = json.loads((root / 'upstreams.lock.json').read_text())
    revision = lock['repositories']['qwenpaw']['commit']
    if not re.fullmatch('[0-9a-f]{40}', revision):
        raise ValueError('pinned_revision_invalid')
    files = ['probes/upgrade/qwenpaw_runtime.py', 'deploy/executor/launcher.py',
             'deploy/executor/control.py', 'agent_line/native_approval_api.py',
             'plugins/agent-line/execution_bridge.py']
    for name in files:
        ast.parse((root / name).read_text(), filename=name)
    candidate = root / 'var/upgrade-candidates' / ('qwenpaw-' + revision)
    marker = candidate.with_name(candidate.name + '.state.json')
    candidate_ready = False
    if marker.exists():
        value = json.loads(marker.read_text())
        candidate_ready = (value.get('status') == 'ready' and value.get('commit') == revision
                           and value.get('repository') == 'qwenpaw'
                           and value.get('remote') == lock['repositories']['qwenpaw']['remote']
                           and value.get('path') == str(candidate.relative_to(root)))
    state_path = validation_directory(root, retirement_validation) / 'state.json'
    existing = private_json(state_path) if state_path.exists() else None
    return {'status': 'static_preparation_only', 'modelInvocations': 0,
            'hostStarts': 0, 'writes': 0, 'candidateMarkerReady': candidate_ready,
            'existingState': existing is not None,
            'repeatMode': 'observe_only', 'liveRunnerReady': True,
            'liveValidated': False, 'pending': ['retirement_bootstrap_window' if retirement_validation
                                               else 'attempt2_bootstrap_window']}



def save(path, value):
    temporary = path.with_suffix('.next')
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


def run_live(*, bootstrap_only=False, resume_pre_submit=False, retirement_validation=False):
    """Explicit one-shot execution only; any existing state prohibits new writes."""
    import importlib.util
    import hashlib
    import secrets
    import signal
    import socket
    import subprocess
    import sys
    import time
    import httpx
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    sys.path.insert(0, str(ROOT))
    from agent_line.store import TaskStore
    from agent_line.role_dispatch import RoleDispatchGateway, RoleDispatchLedger
    from agent_line.dispatcher import Dispatcher
    from agent_line.approvals import TrustedPrincipal
    from agent_line.approval_api import ApprovalAccess
    from agent_line.native_approval_api import NativeApprovalAPI

    data = validation_directory(ROOT, retirement_validation)
    previous = private_json(data / 'state.json') if (data / 'state.json').exists() else None
    legacy_hashes = prior_retirement_proof(ROOT) if retirement_validation else None
    if previous and retirement_validation and previous.get('preservedLegacyEvidence') != legacy_hashes:
        raise ValueError('legacy_evidence_changed')
    if previous is None and not bootstrap_only:
        raise ValueError('bootstrap_required_before_execution')
    if previous is None:
        first=private_json(ROOT/'var/native-permissions/state.json')
        recovery=private_json(ROOT/'var/native-permissions/root-check.json')
        if recovery.get('status') != 'restored_exact_original_no_execution':
            raise ValueError('first_attempt_recovery_unverified')
        if (first.get('modelInvocations') != 0 or first.get('hostStopped') is not True
                or first.get('cases') or any(k.endswith(('_execute','_decision')) for k in first.get('operations',{}))):
            raise ValueError('first_attempt_not_proven_unexecuted')
    if previous and previous.get('status')=='bootstrap_ready' and (
            previous.get('baselineUnchanged') is not True or previous.get('hostStopped') is not True):
        raise ValueError('bootstrap_not_safely_completed')
    pre_submit_recovery = bool(resume_pre_submit and previous and not bootstrap_only and previous.get('status')=='incomplete'
        and previous.get('modelInvocations')==0 and previous.get('cases')=={}
        and previous.get('hostStopped') is True and previous.get('baselineUnchanged') is True
        and previous.get('failureType')=='HTTPStatusError'
        and all(v.get('status')=='received' for v in previous.get('operations',{}).values())
        and not any(k.endswith(('_bind','_execute','_decision')) for k in previous.get('operations',{})))
    if pre_submit_recovery:
        root_proof=private_json(data/'root-bootstrap-check.json')
        if root_proof.get('status')!='bootstrap_verified_no_model':
            raise ValueError('bootstrap_root_proof_missing')
    if previous and (bootstrap_only or (previous.get('status') != 'bootstrap_ready' and not pre_submit_recovery)):
        return {'status': 'existing_identity_observe_only', 'writes': 0,
                'state': private_json(data / 'state.json').get('status')}
    for path in (data, data / 'isolated-host', data / 'secrets', data/'deploy/executor',
                 data/'var/executor/workspace', data/'var/executor/launches'):
        if path.is_symlink(): raise ValueError('symlink_rejected')
        path.mkdir(mode=0o700, parents=True, exist_ok=True); path.chmod(0o700)
    state = previous or {'status': 'intent', 'cases': {}, 'operations': {}, 'host': None}
    if retirement_validation and previous is None:
        state.update(purpose='post_fix_native_permission_validation', preservedLegacyEvidence=legacy_hashes)
    if previous is None: save(data / 'state.json', state)
    if pre_submit_recovery:
        save(data/'pre-submit-interruption.json',previous)
        state['preSubmitRecovery']={'priorFailureType':previous['failureType'],'priorFailureCode':previous['failureCode'],'priorHost':previous['host']}
        state.setdefault('preSubmitRecoveryHistory',[]).append(dict(state['preSubmitRecovery']))
        state['status']='pre_submit_recovery'
        save(data/'state.json',state)
    def persist(): save(data / 'state.json', state)
    def once(key, callback):
        if key in state['operations']:
            old = state['operations'][key]
            if old['status'] == 'received': return old.get('result')
            raise RuntimeError('unknown_operation_not_replayed')
        state['operations'][key] = {'status': 'intent'}; persist()
        result = callback()
        state['operations'][key].update(status='received',result=result); persist()
        return result
    env = {k: os.environ[k] for k in ('PATH','HOME','LANG','LC_ALL','TZ','SSL_CERT_FILE','SSL_CERT_DIR') if k in os.environ}
    env.update(QWENPAW_WORKING_DIR=str(data / 'isolated-host'), QWENPAW_SECRET_DIR=str(data / 'secrets'),
               QWENPAW_AUTH_ENABLED='true', QWENPAW_DISABLE_KEYRING='true',
               QWENPAW_KEYRING_ACCOUNT='agent-line-' + data.name, PYTHONNOUSERSITE='1',
               PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(ROOT / 'upstream/qwenpaw/src'))
    python = str(ROOT / '.venv/bin/python')
    process = None
    def local(args, environment=env):
        with (data / 'bootstrap.log').open('ab') as log:
            (data / 'bootstrap.log').chmod(0o600)
            subprocess.run([python, '-B', *args], env=environment, cwd=ROOT,
                           stdout=log, stderr=log, check=True, timeout=120)
    try:
        once('upstream_check', lambda: local(['scripts/check_upstreams.py']))
        # Read only the configured target's fixed model through the official loader.
        from probes.qwenpaw_memory.native_roundtrip import quiet_host
        main_process = private_json(ROOT / 'var/qwenpaw/agent-line-process.json')
        quiet_host(main_process['pid'])
        main_service_path = ROOT / 'var/adapter/service.json'
        main_service = private_json(main_service_path)
        main_database = Path(main_service['database'])
        if not main_database.is_absolute(): main_database = main_service_path.parent / main_database
        main_store = TaskStore(main_database)
        state['mainGuardBefore'] = main_store.maintenance_status(); persist()
        with main_store.connect() as db:
            if db.execute('SELECT count(*) FROM workspace_leases').fetchone()[0]:
                raise RuntimeError('main_workspace_lease_active')
        main = private_json(ROOT / 'var/qwenpaw/config.json')
        baseline_paths=[ROOT/'var/qwenpaw/config.json',ROOT/'var/executor/config.json',main_service_path,
                        ROOT/'deploy/executor/launcher.py',ROOT/'deploy/executor/control.py',ROOT/'deploy/executor/lifecycle.py',ROOT/'plugins/agent-line/execution_bridge.py']
        baseline_paths += [Path(x['workspace_dir'])/'agent.json' for x in main['agents']['profiles'].values()]
        baseline={str(x.relative_to(ROOT)) if x.is_relative_to(ROOT) else str(x):hashlib.sha256(x.read_bytes()).hexdigest() for x in baseline_paths}
        if previous and state.get('baseline') != baseline: raise RuntimeError('baseline_changed')
        state['baseline']=baseline;state['modelInvocations']=0;persist()
        original = main['plugins']['agent-line']['execution_bridge']
        executor = private_json(ROOT / 'var/executor/config.json')
        workspace = str((data/'var/executor/workspace').resolve(strict=True))
        if not previous and (Path(workspace)/'agent.json').exists():
            raise RuntimeError('isolated_workspace_not_empty')
        if Path(workspace) == Path(executor['workspace']).resolve(strict=True):
            raise RuntimeError('shared_workspace_role_config_collision')
        copied_sources = {}
        for name in ('launcher.py','control.py','lifecycle.py'):
            source = ROOT/'deploy/executor'/name; destination = data/'deploy/executor'/name
            content = source.read_bytes()
            if not previous:
                fd = os.open(destination, os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW, 0o700)
                with os.fdopen(fd,'wb') as stream:
                    stream.write(content);stream.flush();os.fsync(stream.fileno())
            if destination.is_symlink() or destination.read_bytes()!=content:
                raise RuntimeError('executor_copy_mismatch')
            copied_sources[name]=hashlib.sha256(content).hexdigest()
        state['executorSourceHashes']=copied_sources
        isolated_config={**executor,'workspace':workspace}
        config_path=data/'var/executor/config.json'
        if not previous: save(config_path,isolated_config)
        elif private_json(config_path)!=isolated_config: raise RuntimeError('isolated_executor_config_changed')
        state['executorConfigSha256']=hashlib.sha256(config_path.read_bytes()).hexdigest();persist()
        main_env = {**env, 'QWENPAW_WORKING_DIR': str(ROOT / 'var/qwenpaw')}
        model = subprocess.check_output([python, '-B', '-c',
            'from qwenpaw.config.utils import load_agent_config; import sys; print(load_agent_config(sys.argv[1]).backend_settings["model"])',
            original['target_agent_id']], env=main_env, text=True, timeout=15).strip()
        # Exact existing container activity check before reusing shared executor resources.
        active = subprocess.check_output(['docker','ps','-q','--filter','label=agent-line.owner'], text=True, timeout=15).strip()
        if active: raise RuntimeError('owned_container_active')
        login = private_json(data/'secrets/login.json') if previous else {'username':'native-permissions-probe','password':secrets.token_urlsafe(32)}
        if not previous: save(data / 'secrets/login.json', login)
        service_token = (data/'secrets/bridge-token').read_text() if previous else secrets.token_urlsafe(48)
        human_token = private_json(data/'secrets/human.json')['token'] if previous else secrets.token_urlsafe(48)
        token_path = data / 'secrets/bridge-token'
        if not previous:
            fd = os.open(token_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'w') as stream: stream.write(service_token)
            save(data / 'secrets/human.json', {'token':human_token})
        once('host_init', lambda: local(['-m','qwenpaw','init','--defaults','--accept-security']))
        spec = importlib.util.spec_from_file_location('permission_bridge', ROOT / 'plugins/agent-line/execution_bridge.py')
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        if not previous: module.ReceiptLedger.initialize(data / 'receipts.sqlite')
        bridge = {'workspace':workspace, 'codex_home':executor['codex_home'],
                  'container_control':str(data/'deploy/executor/control.py'),
                  'container_launcher':str(data/'deploy/executor/launcher.py'),
                  'receipt_db':str(data/'receipts.sqlite'), 'service_token_file':str(token_path),
                  'target_agent_id':TARGET,'timeout_seconds':240}
        if not previous: save(data / 'bootstrap.json', {'bridge':bridge,'login':login,'model':model})
        bootstrap = '''import json,os
from pathlib import Path
from qwenpaw.config.utils import load_config,save_config
from qwenpaw.app.auth import register_user,authenticate
v=json.loads((Path(os.environ['QWENPAW_WORKING_DIR']).parent/'bootstrap.json').read_text())
c=load_config();c.security.allow_no_auth_hosts=[]
c.plugins['agent-line']={'managed_role_ids':['agent-line-permission-parent'],'codex_target_agent_id':'agent-line-permission-codex','execution_bridge':v['bridge']}
save_config(c)
assert register_user(**v['login']);assert authenticate(**v['login'])
import asyncio
from qwenpaw.providers.provider_manager import ProviderManager
from qwenpaw.providers.provider import ProviderInfo
asyncio.run(ProviderManager.get_instance().add_custom_provider(ProviderInfo(
    id='permission-metadata',name='No parent inference',base_url='http://127.0.0.1:9/v1',
    enabled=True,models=[{'id':v['model'],'name':v['model']}])) )
'''
        once('host_config', lambda: local(['-c',bootstrap]))
        once('plugin_install', lambda: local(['-m','qwenpaw','plugin','install',str(ROOT/'plugins/agent-line'),'--force']))
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0)); port = sock.getsockname()[1]
        state['hostStartIntent'] = {'port':port}; persist()
        with (data/'host.log').open('ab') as log:
            (data/'host.log').chmod(0o600)
            process = subprocess.Popen([python,'-B','-m','qwenpaw','app','--host','127.0.0.1','--port',str(port)],
                env=env,cwd=ROOT,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
        proc = Path('/proc') / str(process.pid)
        state['host'] = {'pid':process.pid,'startTicks':proc.joinpath('stat').read_text().split()[21],'port':port}
        persist()
        base = f'http://127.0.0.1:{port}'
        with httpx.Client(base_url=base,timeout=30,follow_redirects=False) as host:
            for _ in range(90):
                if process.poll() is not None: raise RuntimeError('isolated_host_exited')
                try:
                    response = host.get('/api/agent-line/health')
                    if response.status_code == 401: break
                    if response.status_code == 200: raise RuntimeError('authentication_bypassed')
                except httpx.TransportError: pass
                time.sleep(1)
            else: raise RuntimeError('host_start_deadline')
            def post(path, body):
                response = host.post(path,json=body); response.raise_for_status(); return response.json()
            auth = post('/api/auth/login',login)  # Auth may refresh; never starts native work.
            host.headers['Authorization'] = 'Bearer ' + (auth.get('token') or auth['access_token'])
            for body in role_payloads(workspace, bridge['container_launcher'], model):
                once('create_' + body['id'], lambda body=body: post('/api/agents',body))
            host.headers['X-Agent-Line-Service-Token'] = service_token
            # Global authentication starts before general plugins (official phase 2).
            # Poll only GET; a 401 health response cannot establish route readiness.
            descriptor_deadline=time.monotonic()+120
            while True:
                descriptor = host.get('/api/agent-line/execution-binding')
                if descriptor.status_code not in (404,503): break
                if (process.poll() is not None or time.monotonic()>=descriptor_deadline
                        or (Path('/proc')/str(process.pid)/'stat').read_text().split()[21]!=state['host']['startTicks']):
                    raise RuntimeError('descriptor_readiness_timeout')
                time.sleep(2)
            descriptor.raise_for_status()
            descriptor = descriptor.json(); revision = descriptor['roleRevisions'][PARENT]
            if descriptor['ownedContainer'] is not True: raise RuntimeError('container_binding_missing')
            state['descriptor'] = descriptor; persist()
            if bootstrap_only:
                state['status']='bootstrap_ready';persist()
                return {'status':'bootstrap_finished_check_persisted_state','modelInvocations':0}
            state['status']='execution_intent';persist()
            store = TaskStore.initialize(data/'tasks.sqlite')
            ledger = RoleDispatchLedger.initialize(data/'roles.sqlite')
            gateway = RoleDispatchGateway(base,host.headers['Authorization'][7:],service_token,
                role_agent_id=PARENT,deployment_id='native-permissions',ledger=ledger,
                memory_query='原生权限审批隔离探针，不调用父模型',client=host)
            dispatcher = Dispatcher(store, {'native-permissions':gateway})
            access = ApprovalAccess({human_token:TrustedPrincipal('probe-human',frozenset({'native-permissions'}),frozenset({'approve'}),time.time()+1800)})
            access.reject_service_tokens([service_token])
            app = FastAPI(); app.include_router(NativeApprovalAPI(store,dispatcher,access,service_tokens=(service_token,)).router())
            with TestClient(app) as outer:
                for decision in ('approve','reject'):
                    nonce = secrets.token_hex(16)
                    task,_ = store.register('native-permission-'+decision, {'projectId':'native-permissions',
                        'roleAgentId':PARENT,'roleRevision':revision,'workspace':workspace,
                        'workflow':{'id':'native-permissions','runId':decision,'nodeExecutionId':decision,'attemptId':'1'},
                        'input':{'instruction':instruction(nonce)}})
                    tid=task['id']; case={'taskId':tid,'nonce':nonce,'roleRevision':revision,'decision':decision}
                    state['cases'][decision]=case; persist()
                    session='agent-line-'+tid
                    ledger.claim(tid,{'roleAgentId':PARENT,'deploymentId':'native-permissions','roleSessionId':session,'directNativeSlice':True})
                    once(decision+'_bind',lambda:post('/api/agent-line/delegations/bind',{'taskId':tid,'roleAgentId':PARENT,'roleRevision':revision,'roleSessionId':session}))
                    if not store.claim(tid,workspace): raise RuntimeError('workspace_lease_unavailable')
                    state['modelInvocations']+=1;persist()
                    once(decision+'_execute',lambda:post('/api/agent-line/executions',{'taskId':tid,'roleSessionId':session,'instruction':instruction(nonce)}))
                    url='/extension/v1/node-tasks/'+tid+'/execution-approvals'
                    deadline=time.monotonic()+300; decided=False
                    while time.monotonic()<deadline:
                        task=dispatcher.reconcile(tid); case['task']=task; persist()
                        if task['executionStatus'] in ('succeeded','failed','canceled') and task['resourcesReleased'] is True: break
                        if not decided and set(task['executionIdentity']) == {'upstreamTaskId','codexThreadId','codexTurnId'}:
                            response=outer.get(url,headers={'Authorization':'Bearer '+human_token})
                            if response.status_code==200:
                                waiting=[x for x in response.json()['approvals'] if x['status']=='waiting' and x['bound'] is True]
                                if len(waiting)>1: raise RuntimeError('multiple_approval_requests')
                                if waiting:
                                    record=waiting[0];case['approval']=record;persist()
                                    body={'decisionId':'permission-'+decision,'decision':decision,'expectedIdentity':task['executionIdentity']}
                                    def deliver():
                                        response=outer.post(url+'/'+record['requestId']+'/decision',json=body,headers={'Authorization':'Bearer '+human_token})
                                        response.raise_for_status();return response.json()
                                    case['decisionReceipt']=once(decision+'_decision',deliver);decided=True;persist()
                        time.sleep(2)
                    else: raise RuntimeError('native_deadline_original_identity_retained')
                    if not decided: raise RuntimeError('native_approval_not_observed')
                    identity=task['executionIdentity']; files=list((Path(bridge['codex_home'])/'sessions').rglob('*'+identity['codexThreadId']+'.jsonl'))
                    if len(files)!=1: raise RuntimeError('rollout_not_unique')
                    rows=[json.loads(line) for line in files[0].read_text().splitlines()]
                    item_id=case['approval']['providerItemId']; events=[]
                    for row in rows:
                        event=row.get('payload',{}); item=event.get('item',{})
                        if event.get('turn_id')==identity['codexTurnId'] and isinstance(item,dict) and item.get('id')==item_id:
                            events.append(item)
                    save(data/(decision+'-native-items.json'),events)
                    if decision=='approve':
                        passed=any(x.get('status')=='completed' and (x.get('exit_code')==0 or x.get('exitCode')==0) and any(isinstance(x.get(k),str) and nonce in x[k].splitlines() for k in ('stdout','aggregated_output','output')) for x in events)
                    else:
                        passed=any(x.get('status') in ('declined','rejected') for x in events)
                        if not passed and retirement_validation:
                            from probes.qwenpaw.permission_evidence import rejected_native_evidence
                            launches=[private_json(file) for file in (data/'var/executor/launches').glob('*.json')]
                            launches=[value for value in launches if any(
                                item.get('codexThreadId')==identity['codexThreadId']
                                and item.get('codexTurnId')==identity['codexTurnId']
                                for item in value.get('identities',[]))]
                            if len(launches)!=1: raise RuntimeError('original_launcher_not_unique')
                            case['nativeRejectionEvidence']=rejected_native_evidence(
                                rows,identity,case['approval'],case['decisionReceipt'],launches[0],nonce)
                            passed=True
                    case['nativeItemEvidenceVerified']=passed;persist()
                    if not passed: raise RuntimeError('native_item_evidence_incomplete')
                    parent = host.get('/api/console/chat/task/'+identity['upstreamTaskId'])
                    parent.raise_for_status();case['nativeTargetTaskStatus']=parent.json().get('status')
                    if case['nativeTargetTaskStatus'] != 'finished': raise RuntimeError('native_target_not_finished')
                    with store.connect() as db:
                        case['remainingLeases']=db.execute('SELECT count(*) FROM workspace_leases').fetchone()[0]
                    if case['remainingLeases']: raise RuntimeError('probe_lease_remains')
                    if retirement_validation:
                        _, receipt = module.ReceiptLedger(data/'receipts.sqlite').get(tid)
                        exit_receipt = receipt.get('resourceExitReceipt') or {}
                        expected = {'schemaVersion':1,'source':'qwenpaw-harness-runtime-stop',
                            'taskId':tid,'targetAgentId':TARGET,'workspace':workspace,'identity':identity,
                            'owner':exit_receipt.get('owner'),'containerId':exit_receipt.get('containerId'),
                            'state':'stopped'}
                        if (receipt.get('resourcesReleased') is not True
                                or receipt.get('identity') != identity or receipt.get('runtimeRetirement') != expected):
                            raise RuntimeError('original_runtime_retirement_not_proven')
                        control = subprocess.check_output([python, bridge['container_control'], 'observe',
                            '--thread',identity['codexThreadId'],'--turn',identity['codexTurnId'],
                            '--workspace',workspace],text=True,timeout=25)
                        control = json.loads(control)
                        if (control.get('resourcesReleased') is not True
                                or control.get('exitReceipt') != exit_receipt
                                or control.get('launcherExitProof',{}).get('state') not in ('absent','pid_reused')):
                            raise RuntimeError('original_launcher_exit_not_proven')
                        case['resourceExitProof']=control;case['runtimeRetirement']=expected;persist()
                    quiet_host(process.pid);persist()
            if retirement_validation:
                for key in ('upstreamTaskId','codexThreadId','codexTurnId'):
                    if len({case['task']['executionIdentity'][key] for case in state['cases'].values()}) != 2:
                        raise RuntimeError('independent_native_execution_identity_required')
                if len({case['resourceExitProof']['containerId'] for case in state['cases'].values()}) != 2:
                    raise RuntimeError('independent_native_container_required')
            state['mainGuardAfter']=main_store.maintenance_status()
            if state['mainGuardAfter'] != state['mainGuardBefore']: raise RuntimeError('main_guard_changed')
            state['status']='passed_direct_native_slice';persist()
    except Exception as error:
        state['status']='incomplete';state['failureType']=type(error).__name__
        state['failureCode']=str(error) if re.fullmatch('[a-z_]+',str(error)) else 'inspect_private_evidence'
        persist()
    finally:
        if process is not None and process.poll() is None:
            saved=state.get('host');proc=Path('/proc')/str(process.pid)
            current_env=dict(x.split(b'=',1) for x in proc.joinpath('environ').read_bytes().split(b'\0') if b'=' in x)
            if (saved and proc.joinpath('stat').read_text().split()[21]==saved['startTicks']
                    and current_env.get(b'QWENPAW_WORKING_DIR')==env['QWENPAW_WORKING_DIR'].encode()
                    and proc.stat().st_uid==os.getuid()):
                process.send_signal(signal.SIGTERM)
                try:process.wait(timeout=45)
                except subprocess.TimeoutExpired:state['hostStopUnknown']=True
        state['hostStopped']=process is None or process.poll() is not None
        if 'baseline' in state:
            state['baselineUnchanged']=all(hashlib.sha256((ROOT/filename).read_bytes()).hexdigest()==digest for filename,digest in state['baseline'].items())
            if not state['baselineUnchanged']: state['status']='baseline_changed_blocked'
        if 'main_store' in locals(): state['mainGuardAfter']=main_store.maintenance_status()
        if legacy_hashes is not None:
            state['legacyEvidenceUnchanged']=all(hashlib.sha256((ROOT/filename).read_bytes()).hexdigest()==digest
                                                for filename,digest in legacy_hashes.items())
            if not state['legacyEvidenceUnchanged']: state['status']='legacy_evidence_changed_blocked'
        persist()
    return {'status':state['status'],'hostStopped':state['hostStopped'],'stateFile':str((data/'state.json').relative_to(ROOT))}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check',action='store_true')
    parser.add_argument('--bootstrap-only',action='store_true',help='Official isolated host setup and descriptor only; no model submission')
    parser.add_argument('--resume-pre-submit',action='store_true',help='Explicit proven zero-task pre-submit host recovery only')
    parser.add_argument('--execute',action='store_true',help='Explicit isolated native model window; never retries existing state')
    parser.add_argument('--retirement-validation',action='store_true',help='Separate post-fix validation; never reuses historical task or workspace')
    args=parser.parse_args()
    if args.execute and args.bootstrap_only: parser.error('choose one execution phase')
    if args.resume_pre_submit and not args.execute: parser.error('pre-submit recovery requires --execute')
    try: result=run_live(bootstrap_only=args.bootstrap_only,resume_pre_submit=args.resume_pre_submit,
                        retirement_validation=args.retirement_validation) if (args.execute or args.bootstrap_only) else inspect(retirement_validation=args.retirement_validation)
    except (OSError,ValueError,KeyError,TypeError,SyntaxError):
        print(json.dumps({'status':'preflight_failed','liveRunnerReady':False}));return 1
    print(json.dumps(result,ensure_ascii=False));return 0


if __name__=='__main__':
    raise SystemExit(main())
