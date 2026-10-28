#!/usr/bin/env python3
"""Real native Codex G0 write probe; at most one submit, never blind resubmit."""
import json
import argparse
from pathlib import Path
import time
import urllib.request
from runtime_preflight import ROOT, base, TOKEN

parser = argparse.ArgumentParser()
parser.add_argument('--host-sandbox-unavailable-probe', action='store_true', help='Explicit second attempt in role workspace; records missing OS sandbox isolation')
args = parser.parse_args()
ROLE = 'agent-line-g0-codex'
SESSION = 'agent-line-g0-native-code-smoke' + ('-host-v2' if args.host_sandbox_unavailable_probe else '')
WORK = ROOT / ('var/qwenpaw/workspaces/' + ROLE if args.host_sandbox_unavailable_probe else 'var/g0-codex-work')
RECEIPT = ROOT / ('var/qwenpaw/g0-codex-smoke-host-v2.json' if args.host_sandbox_unavailable_probe else 'var/qwenpaw/g0-codex-smoke.json')
WORK.mkdir(parents=True,exist_ok=True)

def request(path, body=None, method=None):
    req=urllib.request.Request(base+'/api/'+path,data=json.dumps(body).encode() if body is not None else None,method=method,headers={'Authorization':'Bearer '+TOKEN,'Content-Type':'application/json','X-Agent-Id':ROLE})
    with urllib.request.urlopen(req,timeout=60) as response:return json.load(response)

if not RECEIPT.exists():
    available=request('harnesses/codex/models')['models']
    model=next(m['id'] for m in available if m['id']=='gpt-6.1-sol')
    config=request('agents/'+ROLE)
    assert config['backend']=='codex'
    config['backend_settings']={**config.get('backend_settings',{}),'model':model,'sandbox':'danger-full-access' if args.host_sandbox_unavailable_probe else 'workspace-write','approval_policy':'never'}
    request('agents/'+ROLE,config,'PUT')
    receipt={'state':'submission_unknown','role':ROLE,'session':SESSION,'model':model,'workspace':str(WORK),'os_sandbox_enabled':not args.host_sandbox_unavailable_probe}
    RECEIPT.write_text(json.dumps(receipt,indent=2))
    result=request('console/chat/task',{'session_id':SESSION,'user_id':'agent-line-g0','input':[{'role':'user','content':[{'type':'text','text':'Within the assigned working directory only, create native-codex-proof.txt containing exactly AGENT_LINE_NATIVE_CODEX_OK followed by a newline. Run a shell check to verify its exact contents. Do not inspect other directories or call network tools. Report the file and check result.'}]}],'request_context':{'session_project_dir':str(WORK)},'timeout':180})
    receipt.update(state='submitted',task_id=result['task_id'])
    RECEIPT.write_text(json.dumps(receipt,indent=2))
else:
    receipt=json.loads(RECEIPT.read_text())
    if not receipt.get('task_id'):raise SystemExit('Submission receipt unknown: reconcile manually; not resubmitting')
for _ in range(90):
    status=request('console/chat/task/'+receipt['task_id'])
    if status.get('status')=='finished':
        result=status.get('result',{})
        receipt.update(state='finished',result_status=result.get('status'))
        RECEIPT.write_text(json.dumps(receipt,indent=2))
        assert result.get('status')=='completed', 'Native task did not complete'
        assert (WORK/'native-codex-proof.txt').read_text()=='AGENT_LINE_NATIVE_CODEX_OK\n'
        print(json.dumps({'native_codex_execution':True,'file_contents_verified':True,'role':ROLE,'session':SESSION,'task_id':receipt['task_id'],'model':receipt['model']}))
        break
    time.sleep(2)
else: raise SystemExit('Task remains nonterminal; poll the existing receipt, never resubmit')
