"""Official role -> configured launcher -> native tool -> actual container exit."""
import json
from pathlib import Path
import sys
import time
import httpx
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'deploy/executor'))
from control import proof
ROLE='agent-line-container-codex';SESSION='agent-line-container-formal-001'
receipt=ROOT/'var/executor/formal-role-execution.json'
config=json.loads((ROOT/'var/executor/config.json').read_text());workspace=Path(config['workspace'])
state=json.loads((ROOT/'var/qwenpaw/agent-line-process.json').read_text());base=f'http://127.0.0.1:{state["port"]}/api'
with httpx.Client(timeout=60) as client:
    response=client.post(base+'/auth/login',json=json.loads((ROOT/'var/qwenpaw-secrets/local-login.json').read_text()));response.raise_for_status();login=response.json()
    client.headers.update({'Authorization':'Bearer '+(login.get('token') or login['access_token']),'X-Agent-Id':ROLE})
    if not receipt.exists():
        evidence={'roleId':ROLE,'sessionId':SESSION,'phase':'submission_unknown'};receipt.write_text(json.dumps(evidence,indent=2))
        response=client.post(base+'/console/chat/task',json={'session_id':SESSION,'user_id':'agent-line-service','input':[{'role':'user','content':[{'type':'text','text':'Run a shell command to create formal-container-proof.txt with exactly FORMAL_CONTAINER_OK and newline. Verify exact content. Do not inspect any other files.'}]}],'timeout':180});response.raise_for_status()
        evidence.update(upstreamTaskId=response.json()['task_id'],phase='submitted');receipt.write_text(json.dumps(evidence,indent=2))
    else:
        evidence=json.loads(receipt.read_text())
        if not evidence.get('upstreamTaskId'):raise SystemExit('Unknown submission; not retrying')
    for _ in range(100):
        response=client.get(base+'/console/chat/task/'+evidence['upstreamTaskId']);response.raise_for_status();native=response.json()
        if native.get('status')=='finished':break
        time.sleep(2)
    else:raise SystemExit('Unresolved task retained')
    evidence['native']=native
    receipt.write_text(json.dumps(evidence,indent=2))
    assert native.get('result',{}).get('status')=='completed'
    assert (workspace/'formal-container-proof.txt').read_text()=='FORMAL_CONTAINER_OK\n'
    mapping=json.loads((workspace/'harnesses/codex_sessions.json').read_text());thread=mapping[SESSION]
    rollouts=list(Path(config['codex_home']).joinpath('sessions').rglob('*'+thread+'.jsonl'))
    turns=[]
    for line in rollouts[0].read_text().splitlines():
        event=json.loads(line);payload=event.get('payload',{})
        if event.get('type')=='event_msg' and payload.get('type')=='task_started':turns.append(payload['turn_id'])
    assert len(turns)==1
    evidence['exitProof']=proof(thread,turns[0],str(workspace),stop=True)
    evidence.update(artifactVerified=True,verified=evidence['exitProof']['resourcesReleased'])
    receipt.write_text(json.dumps(evidence,indent=2))
    print(json.dumps({'roleId':ROLE,'threadId':thread,'turnId':turns[0],'verified':evidence['verified']}))
