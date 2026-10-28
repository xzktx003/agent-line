"""Owned G0 delivery through the production store, dispatcher and HTTP gateway.

May be rerun: registration is idempotent and prior claims are only observed.
The configured native role currently uses G0 sandbox settings, not production
isolation. This probe does not enable public task admission.
"""
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

process=json.loads((ROOT/'var/qwenpaw/agent-line-process.json').read_text())
base=f'http://127.0.0.1:{process["port"]}'
with httpx.Client(timeout=30) as client:
    response=client.post(base+'/api/auth/login',json=json.loads((ROOT/'var/qwenpaw-secrets/local-login.json').read_text()))
    response.raise_for_status()
    token=response.json().get('token') or response.json().get('access_token')
service_token=(ROOT/'var/qwenpaw-secrets/execution-service-token').read_text().strip()
gateway=QwenPawExecutionGateway(base,token,service_token)
store=TaskStore(ROOT/'var/adapter/tasks.sqlite')
workspace=ROOT/'var/qwenpaw/workspaces/agent-line-g0-codex'
payload={'projectId':'local-validation','roleAgentId':'agent-line-g0-parent','roleBindingId':'g0-dispatcher-probe',
         'workspace':str(workspace),'workflow':{'id':'g0','revision':'1','runId':'dispatcher-live-001',
         'nodeId':'codex','nodeExecutionId':'codex-001','attemptId':'1'},
         'input':{'instruction':'Create dispatcher-proof.txt in your assigned working directory with exact content AGENT_LINE_DISPATCHER_OK and newline. Verify its exact content using a shell command. Do not read other files or call network tools.'}}
task,_=store.register('dispatcher-live-001',payload)
dispatcher=Dispatcher(store,{'local-validation':gateway})
try:
    dispatcher.submit(task['id'])
    for _ in range(90):
        current=dispatcher.reconcile(task['id'])
        if current['executionStatus'] in ('succeeded','failed','canceled') and current['resourcesReleased']:
            break
        time.sleep(2)
    else:
        raise RuntimeError('Existing task remains unresolved; preserve identity and observe later')
    assert current['executionStatus']=='succeeded'
    assert (workspace/'dispatcher-proof.txt').read_text()=='AGENT_LINE_DISPATCHER_OK\n'
    assert current['verificationStatus'] in ('pending', 'passed')
    if current['verificationStatus'] == 'passed':
        assert any(e.get('kind') == 'verification' and e.get('taskId') == current['id']
                   and e.get('executionIdentity') == current['executionIdentity'] for e in current['evidence'])
    proof={'taskId':task['id'],'executionIdentity':current['executionIdentity'],
           'status':current['executionStatus'],'resourcesReleased':current['resourcesReleased'],
           'artifactVerified':True,'scope':'G0 dispatcher transport; production isolation and parent memory loop not proven'}
    target=ROOT/'var/adapter/dispatcher-live.json'
    target.write_text(json.dumps(proof,indent=2)); target.chmod(0o600)
    print(json.dumps(proof))
finally:
    gateway.close()
