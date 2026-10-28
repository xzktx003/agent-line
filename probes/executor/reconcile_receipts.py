"""Reconcile existing identities only and prove resource evidence survives reopen."""
import json
from pathlib import Path
import sys
import httpx
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from agent_line.store import TaskStore
from agent_line.dispatcher import Dispatcher
from agent_line.adapters.qwenpaw import QwenPawExecutionGateway
state=ROOT/'var/executor/bridge-probes';database=state/'tasks.sqlite'
process=json.loads((ROOT/'var/qwenpaw/agent-line-process.json').read_text());base=f'http://127.0.0.1:{process["port"]}'
with httpx.Client(timeout=30) as client:
    response=client.post(base+'/api/auth/login',json=json.loads((ROOT/'var/qwenpaw-secrets/local-login.json').read_text()));response.raise_for_status();login=response.json()
gateway=QwenPawExecutionGateway(base,login.get('token') or login['access_token'],(ROOT/'var/qwenpaw-secrets/execution-service-token').read_text().strip())
try:
    dispatcher=Dispatcher(TaskStore(database),{'local-validation':gateway})
    results=[]
    for mode in ('success','cancel'):
        path=state/(mode+'.json');evidence=json.loads(path.read_text());task_id=evidence['taskId']
        before=dispatcher.store.get(task_id)
        dispatcher.reconcile(task_id)
        reopened=TaskStore(database).get(task_id)
        exits=[item for item in reopened['evidence'] if item.get('kind')=='resource_exit']
        assert len(exits)==1 and exits[0]['receipt']==evidence['bridge']['resourceExitReceipt']
        assert reopened['executionIdentity']==before['executionIdentity'] and reopened['resourcesReleased']
        evidence['task']=reopened;evidence['resourceExitPersistedAfterReopen']=True
        path.write_text(json.dumps(evidence,indent=2));results.append({'taskId':task_id,'resourceExitPersistedAfterReopen':True})
    print(json.dumps(results))
finally:gateway.close()
