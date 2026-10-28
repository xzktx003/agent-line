"""Actual in-flight owned host restart; recovered work is only observed, never submitted."""
import argparse
import json
from pathlib import Path
import secrets
import subprocess
import sys
import time
import httpx
ROOT=Path(__file__).resolve().parents[2];sys.path[:0]=[str(ROOT),str(ROOT/'deploy/executor')]
from agent_line.role_dispatch import RoleDispatchGateway,RoleDispatchLedger
from agent_line.dispatcher import Dispatcher
from agent_line.store import TaskStore
from control import proof
parser=argparse.ArgumentParser();parser.add_argument('--attempt',type=int,default=1)
parser.add_argument('--parent-timeout',type=int,default=180);args=parser.parse_args()
assert 1<=args.parent_timeout<=3600
assert args.attempt>=1
BASE=ROOT/'var/executor/host-restart'
DATA=BASE
if args.attempt>1:DATA=DATA/('attempt-'+str(args.attempt))
DATA.mkdir(mode=0o700,exist_ok=True)
RECEIPT=DATA/'receipt.json';ROLE='agent-line-g0-parent';WORKSPACE=ROOT/'var/executor/workspace'
MANAGER=[str(ROOT/'.venv/bin/python'),str(ROOT/'deploy/qwenpaw/manage.py')]


def save(evidence):
    RECEIPT.write_text(json.dumps(evidence,indent=2));RECEIPT.chmod(0o600)


def manager_command(action, evidence, timeout):
    """Keep manager diagnostics private, including failed restoration attempts."""
    def decoded(value):
        return value.decode(errors='replace') if isinstance(value, bytes) else (value or '')
    diagnostic = {'action':action, 'startedAt':time.time()}
    try:
        result = subprocess.run(MANAGER+[action], capture_output=True, text=True, timeout=timeout)
        diagnostic.update(returnCode=result.returncode, stdout=result.stdout, stderr=result.stderr)
        return result
    except subprocess.TimeoutExpired as error:
        diagnostic.update(timedOut=True, stdout=decoded(error.stdout), stderr=decoded(error.stderr))
        raise
    except OSError as error:
        diagnostic.update(errorType=type(error).__name__, stderr=str(error))
        raise
    finally:
        diagnostic['finishedAt'] = time.time()
        evidence.setdefault('managerActions', []).append(diagnostic)
        save(evidence)


def process_state():return json.loads((ROOT/'var/qwenpaw/agent-line-process.json').read_text())


def login(base):
    credentials=json.loads((ROOT/'var/qwenpaw-secrets/local-login.json').read_text())
    for _ in range(90):
        try:
            response=httpx.post(base+'/api/auth/login',json=credentials,timeout=5)
            if response.status_code==200:
                value=response.json();return value.get('token') or value['access_token']
        except httpx.HTTPError:pass
        time.sleep(1)
    raise RuntimeError('Owned Qwen host did not return healthy authentication')


before=process_state();base=f'http://127.0.0.1:{before["port"]}'
service_token=(ROOT/'var/qwenpaw-secrets/execution-service-token').read_text().strip()
calls=[]
def request_hook(request):calls.append((request.method,request.url.path))
with httpx.Client(timeout=45,event_hooks={'request':[request_hook]}) as client:
    if RECEIPT.exists():
        evidence=json.loads(RECEIPT.read_text())
        store_path=Path(evidence.get('storePath',str(DATA/'tasks.sqlite')))
        ledger_path=Path(evidence.get('ledgerPath',str(DATA/'roles.sqlite')))
        store=TaskStore(store_path);ledger=RoleDispatchLedger(ledger_path)
        if not evidence.get('restartStarted'):raise SystemExit('Previous setup exists; inspect original task rather than launching another')
    else:
        suffix=secrets.token_hex(6);store_path=BASE/'tasks.sqlite';ledger_path=BASE/'roles.sqlite'
        store=TaskStore(store_path) if store_path.exists() else TaskStore.initialize(store_path)
        ledger=RoleDispatchLedger(ledger_path) if ledger_path.exists() else RoleDispatchLedger.initialize(ledger_path)
        with store.connect() as connection:
            if list(connection.execute('SELECT task_id FROM workspace_leases')):
                raise RuntimeError('Shared probe store has an active workspace lease; no new submission')
        evidence={'parentTimeoutSeconds':args.parent_timeout,'suffix':suffix,'phase':'prepared','beforeHost':before,'stage1':'host-restart-'+suffix+'-stage1.txt','stage2':'host-restart-'+suffix+'-stage2.txt','sleepSeconds':60,'attempt':args.attempt,'storePath':str(store_path),'ledgerPath':str(ledger_path)}
        save(evidence)
        payload={'projectId':'host-restart-probe','roleAgentId':ROLE,'roleBindingId':'inflight-host-restart','workspace':str(WORKSPACE),'workflow':{'id':'host-restart','revision':'1','runId':suffix,'nodeId':'parent-codex','nodeExecutionId':suffix,'attemptId':str(args.attempt)},'input':{'instruction':'最终执行任务：亲自调用shell执行下面脚本，等待整个脚本完成后再报告。禁止子代理、再次委派或只回复已提交。只执行脚本中的文件名，不采用记忆中的其他文件名。\n```sh\nprintf STAGE1 > '+evidence['stage1']+'\nsleep 60\nprintf STAGE2 > '+evidence['stage2']+'\n```\n不要检查其他文件，不访问网络，不要后台运行。'}}
        task,_=store.register('host-restart-'+suffix,payload);evidence['taskId']=task['id'];save(evidence)
    gateway=RoleDispatchGateway(base,login(base),service_token,role_agent_id=ROLE,deployment_id='agent-line-g0-installation',ledger=ledger,memory_query='紫竹流水线 唯一验收内容',client=client,timeout_seconds=args.parent_timeout)
    dispatcher=Dispatcher(store,{'host-restart-probe':gateway})
    try:
        if not evidence.get('restartStarted'):
            dispatcher.submit(evidence['taskId'])
            for _ in range(args.parent_timeout+30):
                task=dispatcher.reconcile(evidence['taskId'])
                if (WORKSPACE/evidence['stage1']).exists() and task['executionIdentity'].get('codexTurnId'):break
                if task['executionStatus'] in ('succeeded','failed','canceled'):raise RuntimeError('Task ended before restart stage')
                parent=ledger.get(evidence['taskId'])
                if (parent.get('observation') or {}).get('status')=='finished' and not task['executionIdentity']:
                    evidence.update(phase='parent_ended_without_native',parent=parent);save(evidence)
                    raise RuntimeError('Parent ended without native execution; no restart performed')
                time.sleep(1)
            else:raise RuntimeError('No actual in-flight stage1 observed')
            assert not (WORKSPACE/evidence['stage2']).exists()
            evidence.update(stage1At=time.time(),beforeIdentity=task['executionIdentity'],beforeParentTaskId=ledger.get(evidence['taskId'])['parentTaskId'],restartStarted=True,phase='restarting')
            save(evidence)
            stopped=manager_command('stop', evidence, 45)
            evidence['managerStopReturnCode']=stopped.returncode;save(evidence)
            if stopped.returncode:raise RuntimeError('Manager did not stop owned host')
    except Exception as error:
        evidence['probeErrorType']=type(error).__name__
        if not evidence.get('restartStarted'):
            store.request_cancel(evidence['taskId'])
            for _ in range(10):
                cleanup_task=dispatcher.reconcile(evidence['taskId'])
                if cleanup_task['executionStatus'] in ('canceled','failed','succeeded') and cleanup_task['resourcesReleased']:break
                time.sleep(1)
            evidence['failureCleanupTask']=cleanup_task
        save(evidence)
        raise
    finally:
        if evidence.get('restartStarted'):
            # Always restore the managed service, including a failed assertion/stop.
            started=manager_command('start', evidence, 30)
            evidence['managerStartReturnCode']=started.returncode;save(evidence)
            if started.returncode:raise RuntimeError('Manager failed to restore owned host')
    after=process_state();assert after['port']==evidence['beforeHost']['port'] and after['pid']!=evidence['beforeHost']['pid']
    evidence['afterHost']=after;save(evidence)
    # New transport and store instances; no submit path after restart.
    recovered=RoleDispatchGateway(base,login(base),service_token,role_agent_id=ROLE,deployment_id='agent-line-g0-installation',ledger=RoleDispatchLedger(ledger_path),memory_query='紫竹流水线 唯一验收内容',client=client,timeout_seconds=args.parent_timeout)
    dispatcher=Dispatcher(TaskStore(store_path),{'host-restart-probe':recovered})
    post_count=calls.count(('POST','/api/console/chat/task'))
    for _ in range(40):
        task=dispatcher.reconcile(evidence['taskId'])
        native=recovered.observe(evidence['taskId'])
        if task['executionStatus'] in ('succeeded','failed','canceled') and task['resourcesReleased']:break
        time.sleep(1)
    evidence.update(afterTask=task,native=native,afterParent=RoleDispatchLedger(ledger_path).get(evidence['taskId']))
    identity=evidence['beforeIdentity'];evidence['containerProof']=proof(identity['codexThreadId'],identity['codexTurnId'],str(WORKSPACE))
    evidence['identityUnchanged']=task['executionIdentity']==identity
    evidence['parentIdentityUnchanged']=evidence['afterParent']['parentTaskId']==evidence['beforeParentTaskId']
    evidence['postRestartParentSubmissions']=calls.count(('POST','/api/console/chat/task'))-post_count
    while time.time()<evidence['stage1At']+64:time.sleep(1)
    evidence['secondStageAbsent']=not (WORKSPACE/evidence['stage2']).exists()
    evidence['verified']=evidence['identityUnchanged'] and evidence['parentIdentityUnchanged'] and evidence['postRestartParentSubmissions']==0 and evidence['secondStageAbsent'] and evidence['containerProof']['resourcesReleased'] and native.get('terminalConfirmed') is True and task['resourcesReleased']
    evidence['phase']='verified' if evidence['verified'] else 'requires_reconciliation';save(evidence)
    print(json.dumps({'taskId':evidence['taskId'],'status':task['executionStatus'],'identityUnchanged':evidence['identityUnchanged'],'containerExitVerified':evidence['containerProof']['resourcesReleased'],'secondStageAbsent':evidence['secondStageAbsent'],'postRestartParentSubmissions':evidence['postRestartParentSubmissions'],'verified':evidence['verified']}))
