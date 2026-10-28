"""Verify actual container filesystem boundary and official native app-server."""
import asyncio
import argparse
import sys
import importlib.util
import json
from pathlib import Path
import subprocess
from qwenpaw.harnesses.codex.app_server import CodexAppServerClient
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'deploy/executor'))
spec=importlib.util.spec_from_file_location('launcher',ROOT/'deploy/executor/launcher.py')
launcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(launcher)
config=json.loads(launcher.CONFIG.read_text())
workspace=Path(config['workspace'])
parser=argparse.ArgumentParser()
parser.add_argument('--new-attempt', action='store_true')
args=parser.parse_args()
evidence_path=ROOT/'var/executor/evidence.json'
if evidence_path.exists():
    previous=json.loads(evidence_path.read_text())
    if not args.new_attempt:
        print(json.dumps(previous));raise SystemExit(0 if previous.get('artifactVerified') else 1)
    if not previous.get('nativeTerminal'):
        raise SystemExit('Previous native outcome unknown; reconcile rather than resubmit')
    evidence_path.rename(evidence_path.with_name('evidence-'+previous.get('turnId','prior')+'.json'))
script='''set -eu
printf 'container-write-ok\\n' > boundary-proof.txt
! touch /etc/agent-line-escape 2>/dev/null
! touch /root/agent-line-escape 2>/dev/null
! test -e /var/run/docker.sock
! test -e /data01/home/zukang.xu/.codex/auth.json
! test -e /data01/home/zukang.xu/workspace/ai_agent/agentteams/agent_line/var/qwenpaw-secrets/local-login.json
! ln -s /etc /tmp/agent-line-escape-link 2>/dev/null || ! touch /tmp/agent-line-escape-link/agent-line-escape 2>/dev/null
printf 'boundary-denials-ok\\n'
'''
result=subprocess.run(launcher.command(['-c',script],probe=True),capture_output=True,text=True)
evidence={'filesystemExitCode':result.returncode,'filesystemOutput':result.stdout,'filesystemError':result.stderr}
assert result.returncode==0,evidence
assert (workspace/'boundary-proof.txt').read_text()=='container-write-ok\n'
async def main():
    (workspace/'native-container-proof.txt').unlink(missing_ok=True)
    client=CodexAppServerClient(binary=str(ROOT/'deploy/executor/launcher.py'))
    try:
        await asyncio.wait_for(client.start(),30)
        evidence['officialQwenPawClientInitialized']=True
        result=await client.request('thread/start',{'cwd':str(workspace),'approvalPolicy':'never','sandbox':'danger-full-access','model':'gpt-6.1-sol'})
        thread=result['thread']['id']
        evidence['threadId']=thread
        queue=client.subscribe()
        turn=await client.request('turn/start',{'threadId':thread,'cwd':str(workspace),'input':[{'type':'text','text':'Run shell to create native-container-proof.txt with exactly CONTAINER_NATIVE_OK plus newline. Then prove shell cannot write /etc/agent-line-escape and /root/agent-line-escape, cannot see /var/run/docker.sock. Do not inspect any credentials. Report results.'}],'approvalPolicy':'never','sandboxPolicy':{'type':'dangerFullAccess'}})
        evidence['turnId']=turn['turn']['id']
        (ROOT/'var/executor/evidence.json').write_text(json.dumps(evidence,indent=2))
        while True:
            event=await asyncio.wait_for(queue.get(),180)
            if event.get('method')=='error':
                evidence.setdefault('errors',[]).append(event.get('params'))
            if event.get('method')=='turn/completed':
                evidence['terminalDetails']=event['params']['turn']
                evidence['nativeTerminal']=event['params']['turn']['status'];break
        evidence['artifactVerified']=(workspace/'native-container-proof.txt').exists() and (workspace/'native-container-proof.txt').read_text()=='CONTAINER_NATIVE_OK\n'
        assert evidence['nativeTerminal']=='completed' and evidence['artifactVerified']
    finally:
        await client.stop()
        (ROOT/'var/executor/evidence.json').write_text(json.dumps(evidence,indent=2))
asyncio.run(main())
print(json.dumps(evidence))
