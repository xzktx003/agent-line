#!/usr/bin/env python3
"""Real HTTP bridge submission, idempotency, native identity and artifact probe."""
import json
import argparse
from pathlib import Path
import time
import urllib.request
import urllib.error
from runtime_preflight import ROOT, base, TOKEN
parser=argparse.ArgumentParser()
parser.add_argument('--task-id',default='g0-bridge-native-001')
TASK=parser.parse_args().task_id
assert TASK.replace('-', '').isalnum()
RECEIPT=ROOT/('var/qwenpaw/g0-execution-bridge-smoke.json' if TASK=='g0-bridge-native-001' else 'var/qwenpaw/'+TASK+'.json')
FILENAME='bridge-proof.txt' if TASK=='g0-bridge-native-001' else 'bridge-proof-'+TASK+'.txt'
SERVICE_TOKEN=(ROOT/'var/qwenpaw-secrets/execution-service-token').read_text().strip()
WORK=ROOT/'var/qwenpaw/workspaces/agent-line-g0-codex'
body={'taskId':TASK,'roleSessionId':'agent-line-'+TASK,'instruction':f'Create {FILENAME} in your assigned working directory with exact content AGENT_LINE_BRIDGE_OK and newline. Verify exact contents with a shell command. Do not read other files or call network tools.'}
def request(path, body=None, valid=True):
    req=urllib.request.Request(base+'/api/agent-line/'+path,data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json','Authorization':'Bearer '+TOKEN,'X-Agent-Line-Service-Token':SERVICE_TOKEN if valid else 'incorrect'})
    try:
        with urllib.request.urlopen(req,timeout=60) as response:return response.status,json.load(response)
    except urllib.error.HTTPError as error:return error.code,{}
assert request('executions/'+TASK,valid=False)[0]==403
if not RECEIPT.exists():
    RECEIPT.write_text(json.dumps({'taskId':TASK,'submission':'unknown'}))
    code,first=request('executions',body)
    assert code==202, f'Submit HTTP {code}'
    code,duplicate=request('executions',body)
    assert code==202
    if first['identity'].get('upstreamTaskId'):
        assert first['identity']['upstreamTaskId']==duplicate['identity']['upstreamTaskId']
    assert request('executions',{**body,'instruction':'changed'})[0]==409
for _ in range(100):
    code,result=request('executions/'+TASK)
    assert code==200, f'Query HTTP {code}; never automatically resubmit'
    RECEIPT.write_text(json.dumps({'taskId':TASK,**result},ensure_ascii=False,indent=2))
    if result['terminalConfirmed']:
        assert result['status']=='succeeded', 'Native execution failed'
        assert result['identity']['codexThreadId'] and result['identity']['codexTurnId']
        assert result['resourcesReleased'], 'Native resource exit not confirmed'
        assert (WORK/FILENAME).read_text()=='AGENT_LINE_BRIDGE_OK\n'
        print(json.dumps({'bridge_execution':True,'identity':result['identity'],'terminalConfirmed':True,'resourcesReleased':True,'artifact_verified':True}))
        break
    time.sleep(2)
else: raise SystemExit('Nonterminal or uncertain; existing receipt retained without resubmit')
