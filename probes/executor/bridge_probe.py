"""Dispatcher/Gateway/plugin/official role/container positive exit and cancellation."""
import argparse
import json
from pathlib import Path
import sys
import time
import httpx
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from agent_line.store import TaskStore
from agent_line.dispatcher import Dispatcher
from agent_line.adapters.qwenpaw import QwenPawExecutionGateway
parser=argparse.ArgumentParser();parser.add_argument('mode',choices=['success','cancel']);args=parser.parse_args()
state=ROOT/'var/executor/bridge-probes';state.mkdir(exist_ok=True)
receipt=state/(args.mode+'.json');database=state/'tasks.sqlite'
if not database.exists():TaskStore.initialize(database)
store=TaskStore(database)
process=json.loads((ROOT/'var/qwenpaw/agent-line-process.json').read_text());base=f'http://127.0.0.1:{process["port"]}'
with httpx.Client(timeout=30) as client:
    response=client.post(base+'/api/auth/login',json=json.loads((ROOT/'var/qwenpaw-secrets/local-login.json').read_text()));response.raise_for_status();login=response.json()
gateway=QwenPawExecutionGateway(base,login.get('token') or login['access_token'],(ROOT/'var/qwenpaw-secrets/execution-service-token').read_text().strip())
dispatcher=Dispatcher(store,{'local-validation':gateway});workspace=ROOT/'var/executor/workspace'
first=workspace/'bridge-cancel-stage1.txt';second=workspace/'bridge-cancel-stage2.txt'
try:
    if not receipt.exists():
        if args.mode=='success':
            instruction='Create bridge-container-proof.txt with exactly BRIDGE_CONTAINER_OK and newline using a shell command. Verify content. Do not inspect other files.'
            assert not (workspace/'bridge-container-proof.txt').exists()
        else:
            instruction='Run exactly one shell command: printf STAGE1 > bridge-cancel-stage1.txt; sleep 35; printf STAGE2 > bridge-cancel-stage2.txt . Wait for it. Do nothing else. Authorized cancellation test.'
            assert not first.exists() and not second.exists()
        key='container-bridge-'+args.mode+'-001'
        payload={'projectId':'local-validation','roleAgentId':'agent-line-g0-parent','roleBindingId':'container-bridge-probe','workspace':str(workspace),'workflow':{'id':'g0-container','revision':'1','runId':key,'nodeId':'codex','nodeExecutionId':key,'attemptId':'1'},'input':{'instruction':instruction}}
        task,_=store.register(key,payload);evidence={'taskId':task['id'],'mode':args.mode,'phase':'registered'};receipt.write_text(json.dumps(evidence,indent=2))
        dispatcher.submit(task['id'])
    else:evidence=json.loads(receipt.read_text())
    task_id=evidence['taskId']
    if args.mode=='cancel':
        if not evidence.get('cancelRequestedAt'):
            for _ in range(180):
                current=dispatcher.reconcile(task_id)
                if first.exists():break
                if current['executionStatus'] in ('succeeded','failed','canceled'):raise RuntimeError('Native ended before cancel stage')
                time.sleep(1)
            else:raise RuntimeError('No stage1; preserve existing identity')
            assert not second.exists()
            evidence['cancelRequestedAt']=time.time();receipt.write_text(json.dumps(evidence,indent=2))
        store.request_cancel(task_id)
    for _ in range(180):
        current=dispatcher.reconcile(task_id)
        if current['executionStatus'] in ('succeeded','failed','canceled') and current['resourcesReleased']:break
        time.sleep(1)
    bridge=gateway.observe(task_id);evidence.update(task=current,bridge=bridge)
    expected='succeeded' if args.mode=='success' else 'canceled'
    evidence['artifactVerified']=(workspace/'bridge-container-proof.txt').read_text()=='BRIDGE_CONTAINER_OK\n' if args.mode=='success' and (workspace/'bridge-container-proof.txt').exists() else False
    if args.mode=='cancel':
        while time.time()<evidence['cancelRequestedAt']+38:time.sleep(1)
        evidence['secondStageAbsent']=not second.exists()
        evidence['artifactVerified']=evidence['secondStageAbsent']
    evidence['verified']=current['executionStatus']==expected and current['resourcesReleased'] and bool(bridge.get('resourceExitReceipt')) and evidence['artifactVerified']
    receipt.write_text(json.dumps(evidence,indent=2))
    print(json.dumps({'taskId':task_id,'mode':args.mode,'status':current['executionStatus'],'resourcesReleased':current['resourcesReleased'],'artifactVerified':evidence['artifactVerified'],'verified':evidence['verified']}))
    assert evidence['verified'],'Container bridge proof failed; retain task identity'
finally:gateway.close()
