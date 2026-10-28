"""Real persisted cancellation; never resubmit an existing receipt."""
import json
import sys
import time
from pathlib import Path
import httpx
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from agent_line.store import TaskStore
from agent_line.dispatcher import Dispatcher
from agent_line.adapters.qwenpaw import QwenPawExecutionGateway
state=ROOT/'var/cancellation';state.mkdir(exist_ok=True)
receipt=state/'receipt.json'
database=state/'tasks.sqlite'
if not database.exists():TaskStore.initialize(database)
store=TaskStore(database)
process=json.loads((ROOT/'var/qwenpaw/agent-line-process.json').read_text())
base=f'http://127.0.0.1:{process["port"]}'
with httpx.Client(timeout=30) as client:
    response=client.post(base+'/api/auth/login',json=json.loads((ROOT/'var/qwenpaw-secrets/local-login.json').read_text()))
    response.raise_for_status();login=response.json()
gateway=QwenPawExecutionGateway(base,login.get('token') or login.get('access_token'),(ROOT/'var/qwenpaw-secrets/execution-service-token').read_text().strip())
dispatcher=Dispatcher(store,{'local-validation':gateway})
workspace=ROOT/'var/qwenpaw/workspaces/agent-line-g0-codex'
first=workspace/'cancel-probe-stage1.txt';second=workspace/'cancel-probe-stage2.txt'
try:
    if not receipt.exists():
        assert not first.exists() and not second.exists(),'Probe files already exist'
        payload={'projectId':'local-validation','roleAgentId':'agent-line-g0-parent','roleBindingId':'g0-cancel-probe','workspace':str(workspace),'workflow':{'id':'g0-cancel','revision':'1','runId':'cancel-live-001','nodeId':'codex','nodeExecutionId':'cancel-001','attemptId':'1'},'input':{'instruction':'Run exactly one shell command in the assigned directory: printf STAGE1 > cancel-probe-stage1.txt; sleep 45; printf STAGE2 > cancel-probe-stage2.txt . Wait for it; do not run anything else or inspect other files. This is an authorized cancellation test. The filenames are literal, do not append punctuation.'}}
        task,_=store.register('cancel-live-001',payload)
        evidence={'taskId':task['id'],'phase':'registered','secondStageDelaySeconds':45}
        receipt.write_text(json.dumps(evidence,indent=2))
        dispatcher.submit(task['id'])
    else:evidence=json.loads(receipt.read_text())
    task_id=evidence['taskId']
    if not evidence.get('cancelRequestedAt'):
        for _ in range(180):
            current=dispatcher.reconcile(task_id)
            if first.exists():break
            if current['executionStatus'] in ('succeeded','failed','canceled'):raise RuntimeError('Native ended before cancellable stage')
            time.sleep(1)
        else:raise RuntimeError('No stage1; retain existing task identity')
        assert not second.exists(),'Already too late to cancel'
        evidence.update(phase='cancel_requested',stage1Observed=True,cancelRequestedAt=time.time())
        receipt.write_text(json.dumps(evidence,indent=2))
    store.request_cancel(task_id)
    for _ in range(120):
        current=dispatcher.reconcile(task_id)
        if current['executionStatus'] in ('succeeded','failed','canceled') and current['resourcesReleased']:break
        time.sleep(1)
    evidence.update(task=current,bridge=gateway.observe(task_id))
    # Wait beyond the original sleep even if an upstream stop acknowledges early.
    while time.time()<evidence['cancelRequestedAt']+48:time.sleep(1)
    identity=current['executionIdentity']
    rollouts=list((ROOT/'var/codex-g0/sessions').rglob('*'+identity['codexThreadId']+'.jsonl'))
    aborted=[]
    for rollout in rollouts:
        for line in rollout.read_text().splitlines():
            event=json.loads(line)
            payload=event.get('payload',{})
            if event.get('type')=='event_msg' and payload.get('type')=='turn_aborted' and payload.get('turn_id')==identity['codexTurnId']:
                aborted.append({'path':str(rollout.relative_to(ROOT)), 'event':event})
    evidence['nativeAbortEvidence']=aborted
    evidence['fileEvidence']={path.name:{'mtime':path.stat().st_mtime,'content':path.read_text()} for path in (first,second) if path.exists()}
    evidence['secondStageAbsent']=not second.exists()
    evidence['verified']=current['executionStatus']=='canceled' and current['resourcesReleased'] and evidence['secondStageAbsent'] and bool(aborted)
    receipt.write_text(json.dumps(evidence,indent=2))
    print(json.dumps({'taskId':task_id,'status':current['executionStatus'],'resourcesReleased':current['resourcesReleased'],'secondStageAbsent':evidence['secondStageAbsent'],'verified':evidence['verified']}))
    assert evidence['verified'],'Cancellation not proven; preserve native evidence'
finally:gateway.close()
