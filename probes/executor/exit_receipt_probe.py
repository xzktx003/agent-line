"""Native turn cancellation plus positive owned-container exit proof."""
import asyncio
import json
from pathlib import Path
import sys
import time
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'deploy/executor'))
from control import proof
from qwenpaw.harnesses.codex.app_server import CodexAppServerClient
config=json.loads((ROOT/'var/executor/config.json').read_text());workspace=Path(config['workspace'])
receipt=ROOT/'var/executor/container-cancel-evidence.json'
if receipt.exists():print(receipt.read_text());raise SystemExit('Existing receipt retained; do not blindly resubmit')
first=workspace/'container-cancel-stage1.txt';second=workspace/'container-cancel-stage2.txt'
assert not first.exists() and not second.exists()
evidence={'phase':'not_submitted'}
async def main():
    client=CodexAppServerClient(binary=str(ROOT/'deploy/executor/launcher.py'))
    try:
        await client.start()
        thread=await client.request('thread/start',{'cwd':str(workspace),'approvalPolicy':'never','sandbox':'danger-full-access','model':'gpt-6.1-sol'})
        thread_id=thread['thread']['id'];evidence.update(threadId=thread_id,phase='submission_unknown');receipt.write_text(json.dumps(evidence,indent=2))
        turn=await client.request('turn/start',{'threadId':thread_id,'cwd':str(workspace),'input':[{'type':'text','text':'Run exactly one shell command: printf STAGE1 > container-cancel-stage1.txt; sleep 35; printf STAGE2 > container-cancel-stage2.txt . Wait for it. Do nothing else. This is an authorized cancellation test.'}],'approvalPolicy':'never','sandboxPolicy':{'type':'dangerFullAccess'}})
        turn_id=turn['turn']['id'];evidence.update(turnId=turn_id,phase='submitted');receipt.write_text(json.dumps(evidence,indent=2))
        for _ in range(150):
            if first.exists():break
            await asyncio.sleep(1)
        else:raise RuntimeError('Native stage1 not observed')
        assert not second.exists()
        evidence['stage1At']=time.time()
        await client.request('turn/interrupt',{'threadId':thread_id,'turnId':turn_id})
        await asyncio.sleep(.5)
        evidence['exitProof']=await asyncio.to_thread(proof,thread_id,turn_id,str(workspace),stop=True)
        evidence['phase']='owned_container_exited';receipt.write_text(json.dumps(evidence,indent=2))
        await asyncio.sleep(37)
        evidence['secondStageAbsent']=not second.exists()
        evidence['verified']=evidence['exitProof']['resourcesReleased'] and evidence['secondStageAbsent']
        assert evidence['verified']
    finally:
        await client.stop()
        receipt.write_text(json.dumps(evidence,indent=2))
asyncio.run(main())
print(json.dumps(evidence))
