"""One gpt-6-luna tool turn through the existing owned Docker boundary.

Uses the installed official QwenPaw RPC client, not a new Agent or task binding.
An existing state never sends another request or resumes the turn.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import time
from qwenpaw.harnesses.codex.app_server import CodexAppServerClient
from probes.qwenpaw_memory.native_roundtrip import save

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'deploy/executor'))
from control import proof

OUT=ROOT/'var/luna-current-executor-20261022'
MODEL='gpt-6-luna'
ARTIFACT='luna-current-executor-proof-20261022.txt'
MARKER=b'LUNA_CURRENT_EXECUTOR_OK\n'


def configuration_hashes():
    paths=[ROOT/'var/executor/config.json', ROOT/'var/qwenpaw/config.json', ROOT/'var/adapter/service.json']
    native=json.loads(paths[1].read_text())
    paths += [Path(profile['workspace_dir'])/'agent.json' for profile in native['agents']['profiles'].values()]
    return {str(path):hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def observe():
    path=OUT/'state.json'
    if not path.exists():
        print(json.dumps({'mode':'not_started','model':MODEL,'newSubmissions':0})); return
    state=json.loads(path.read_text())
    report={key:state.get(key) for key in ('phase','modelRequested','modelEcho','nativeTerminal','artifactVerified','successfulCommandEvents')}
    report.update(mode='existing_turn_observation',newSubmissions=0)
    if state.get('threadId') and state.get('turnId'):
        config=json.loads((ROOT/'var/executor/config.json').read_text())
        workspace=Path(config['workspace'])
        artifact=workspace/ARTIFACT
        report['currentArtifactVerified']=(artifact.is_file()
            and artifact.stat().st_size==len(MARKER) and artifact.read_bytes()==MARKER)
        rollouts=list((Path(config['codex_home'])/'sessions').rglob('*'+state['threadId']+'.jsonl'))
        contexts=[];terminals=[]
        if len(rollouts)==1:
            for line in rollouts[0].read_text().splitlines():
                event=json.loads(line);payload=event.get('payload',{})
                if payload.get('turn_id')!=state['turnId']:
                    continue
                if event.get('type')=='turn_context':
                    contexts.append(payload.get('model'))
                if event.get('type')=='event_msg' and payload.get('type')=='task_complete':
                    terminals.append(payload)
        report['nativeRolloutIdentityVerified']=(bool(contexts) and all(model==MODEL for model in contexts)
            and bool(terminals))
        exit_proof=proof(state['threadId'],state['turnId'],workspace)
        save(OUT/'exit-proof.json',exit_proof)
        report['resourcesReleased']=exit_proof['resourcesReleased']
    save(OUT/'observation.json',report)
    print(json.dumps(report,ensure_ascii=False))


async def execute():
    if (OUT/'state.json').exists():
        observe(); return
    OUT.mkdir(mode=0o700,exist_ok=True)
    config=json.loads((ROOT/'var/executor/config.json').read_text())
    workspace=Path(config['workspace']).resolve(strict=True)
    assert workspace.is_relative_to(ROOT)
    artifact=workspace/ARTIFACT
    assert not artifact.exists(), 'Existing artifact must not be overwritten'
    original_hashes=configuration_hashes()
    state={'phase':'preparing','modelRequested':MODEL,'startedAt':time.time(),'successfulCommandEvents':0,
           'modelFallbackAllowed':False,'businessRoleConfigurationChanged':False}
    def persist(): save(OUT/'state.json',state)
    persist()
    client=CodexAppServerClient(binary=str(ROOT/'deploy/executor/launcher.py'))
    try:
        await asyncio.wait_for(client.start(),30)
        state['phase']='thread_request_intent';persist()
        response=await asyncio.wait_for(client.request('thread/start',{'cwd':str(workspace),
            'approvalPolicy':'never','sandbox':'danger-full-access','model':MODEL}),30)
        state['threadId']=response['thread']['id'];state['modelEcho']=response.get('model')
        assert state['modelEcho']==MODEL,'Native app-server did not confirm exact requested model'
        queue=client.subscribe()
        state['phase']='turn_request_intent';persist()
        response=await asyncio.wait_for(client.request('turn/start',{'threadId':state['threadId'],
            'cwd':str(workspace),'input':[{'type':'text','text':
                'Use the shell tool exactly for this bounded verification. Create the NEW file '+ARTIFACT+
                ' in the current working directory with exactly LUNA_CURRENT_EXECUTOR_OK followed by newline. '
                'Use printf or Python, then read back only that new file. Do not inspect existing project files, '
                'instructions, history, credentials, configuration, skills or network services. Do not change anything else. '
                'Report completion only after the command succeeds.'}],
            'approvalPolicy':'never','sandboxPolicy':{'type':'dangerFullAccess'}}),30)
        state['turnId']=response['turn']['id'];state['phase']='observing';persist()
        deadline=time.monotonic()+240
        while time.monotonic()<deadline:
            try:
                event=await asyncio.wait_for(queue.get(),min(15,max(0.01,deadline-time.monotonic())))
            except asyncio.TimeoutError:
                state['lastObservedAt']=time.time();persist();continue
            method=event.get('method');params=event.get('params',{})
            if method=='item/completed':
                item=params.get('item',{})
                if item.get('type')=='commandExecution' and item.get('exitCode')==0:
                    state['successfulCommandEvents']+=1;persist()
            if method=='turn/completed':
                state['nativeTerminal']=params['turn']['status'];break
            if method=='error':
                state['nativeErrorObserved']=True;persist()
        state['artifactVerified']=artifact.is_file() and artifact.stat().st_size==len(MARKER) and artifact.read_bytes()==MARKER
        state['phase']='passed' if (state.get('nativeTerminal')=='completed' and state['artifactVerified']
            and state['successfulCommandEvents']>0) else 'incomplete'
        persist()
    except Exception as error:
        state['phase']='failed_or_unknown';state['failureType']=type(error).__name__;persist()
    finally:
        await client.stop()
        state['clientStopped']=True
        state['configurationHashesUnchanged']=configuration_hashes()==original_hashes
        persist()
    assert state['configurationHashesUnchanged']
    observe()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--execute',action='store_true');args=parser.parse_args()
    if args.execute: asyncio.run(execute())
    else: observe()
