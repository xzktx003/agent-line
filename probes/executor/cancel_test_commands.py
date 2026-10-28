#!/usr/bin/env python3
"""Real Docker/ASGI cancellation; preceding native task evidence is synthetic."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from fastapi.testclient import TestClient
from agent_line.store import TaskStore
from agent_line.service import ServiceConfig,create_app
from agent_line.automatic_verification import AutomaticVerifier
from agent_line.test_executor import DockerTestExecutor
from agent_line.verification_policy import validate_policy
from agent_line.verification import _persist
import agent_line.test_executor as runner

DATA=ROOT/'var/test-cancellation-native'
def save(name,value):_persist(DATA,dict(value,evidenceFile=name))
def inspect(cid):
    result=subprocess.run(['docker','inspect','--type','container',cid],capture_output=True,check=True)
    return json.loads(result.stdout)[0]

def main():
    DATA.mkdir(mode=0o700,exist_ok=True)
    statepath=DATA/'state.json'
    if statepath.exists():
        state=json.loads(statepath.read_text());result={'mode':'observation_only','newWrites':0}
        if state.get('taskId'):result['task']=TaskStore(DATA/'tasks.sqlite').get(state['taskId'])
        print(json.dumps({k:v for k,v in result.items() if k!='task'}));return 0
    state={'phase':'intent','nativeMetadataSynthetic':True,'pid':os.getpid()};save('state.json',state)
    workspace=DATA/'workspace';workspace.mkdir(mode=0o700)
    evidence=DATA/'evidence';evidence.mkdir(mode=0o700)
    keep=workspace/'keep.txt';keep.write_bytes(b'preserve-this-file\n')
    image=json.loads((ROOT/'var/executor/config.json').read_text())['image']
    executor=DockerTestExecutor(evidence,image)
    script="import subprocess,time;from pathlib import Path;subprocess.Popen(['python3','-c','import os,time;os.setsid();time.sleep(90)']);Path('/tmp/started').write_text('started');time.sleep(60)"
    policy=validate_policy(dict(schemaVersion=2,policyId='cancel-native',kind='command_and_artifacts',requireTests=True,
        testEnvironmentId=executor.environment_id,artifacts=[],tests=[
        dict(id='first',argv=['python3','-c',script],timeoutSeconds=100,maxOutputBytes=1024),
        dict(id='second',argv=['python3','-c',"raise SystemExit('MUST NOT RUN')"],timeoutSeconds=5,maxOutputBytes=1024)]))
    store=TaskStore.initialize(DATA/'tasks.sqlite')
    task,_=store.register('explicitly-synthetic-native-task',dict(projectId='probe',roleAgentId='synthetic-role',roleBindingId='probe',
        workspace=str(workspace),verificationPolicy=policy,workflow=dict(id='synthetic-wf',runId='synthetic-run',nodeExecutionId='node',attemptId='one')))
    tid=task['id'];identity=dict(codexThreadId='explicitly-synthetic-thread',codexTurnId='explicitly-synthetic-turn')
    state.update(taskId=tid,phase='synthetic_native_setup',policy=policy);save('state.json',state)
    store.claim(tid,workspace);store.accept(tid,identity)
    store.finish(tid,'succeeded',{},[dict(kind='execution_terminal',executionId=identity['codexTurnId'],confirmed=True,synthetic=True)])
    store.confirm_resources_released(tid,identity['codexTurnId'],dict(source='docker-inspect-before-remove',owner='a'*32,containerId='a'*64,
        workspace=str(workspace),running=False,pid=0,status='exited',finishedAt='synthetic',identities=[identity]))
    token=secrets.token_urlsafe(32)
    config=ServiceConfig(tokens={token:{'probe'}},projects={'probe':{'workspace':str(workspace)}},bindings={},controlled_execution='unknown')
    headers={'Authorization':'Bearer '+token};path='/extension/v1/node-tasks/'+tid
    calls=[];actual=runner._command
    def traced(argv,**kwargs):
        calls.append({'operation':argv[1],'at':time.time()});save('docker-calls.json',{'calls':calls})
        return actual(argv,**kwargs)
    def app():
        current=TaskStore(DATA/'tasks.sqlite');ex=DockerTestExecutor(evidence,image)
        return create_app(current,config,verifier=AutomaticVerifier(current,evidence,test_executor=ex))
    started=None
    deadline=time.monotonic()+110
    with patch.object(runner,'_command',side_effect=traced):
        with TestClient(app()) as client:
            while time.monotonic()<deadline:
                records=[json.loads(p.read_text()) for p in evidence.glob('test-*.json')]
                if records and records[0].get('containerId'):
                    record=records[0];value=inspect(record['containerId'])
                    expected=runner._hash(record['binding'])[:32]
                    assert value['Config']['Labels']['agent-line.test-owner']==expected
                    if value['State']['Running'] and value['State']['Pid']>0:
                        check=subprocess.run(['docker','exec',value['Id'],'/usr/bin/cat','/tmp/started'],capture_output=True)
                        if check.returncode==0 and check.stdout==b'started':started=value;break
                time.sleep(.15)
            assert started is not None,'Original test did not become observably active'
            state.update(phase='running_before_rebuild',containerId=started['Id'],runningState=started['State']);save('state.json',state)
            print(json.dumps({'pid':os.getpid(),'taskId':tid,'phase':state['phase'],'containerId':started['Id']}),flush=True)
        assert inspect(started['Id'])['State']['Running'] is True
        with TestClient(app()) as client:
            original=client.get(path,headers=headers);original.raise_for_status()
            assert original.json()['verificationStatus']=='pending'
            state['phase']='cancel_intent';save('state.json',state)
            response=client.post(path+'/cancel',headers=headers);response.raise_for_status()
            state['cancelResponse']=response.json();save('state.json',state)
            assert response.json()['executionStatus']=='succeeded' and response.json()['verificationCancellationRequested'] is True
            while time.monotonic()<deadline:
                result=client.get(path,headers=headers);result.raise_for_status()
                if result.json()['verificationStatus']=='canceled':break
                time.sleep(.2)
            else:raise RuntimeError('Original cancellation still unresolved')
            final=TaskStore(DATA/'tasks.sqlite').get(tid)
            proof=next(x for x in final['evidence'] if x.get('kind')=='verification_cancellation')
            assert final['executionStatus']=='succeeded' and proof['tests'][0]['disposition']=='exited'
            assert proof['tests'][0]['containerId']==started['Id'] and proof['tests'][1]['disposition']=='not_started'
            records=[json.loads(p.read_text()) for p in evidence.glob('test-*.json')]
            second=next(x for x in records if x['binding']['testId']=='second')
            assert second['createIntent'] is False and 'containerId' not in second
            before={p.name:p.read_bytes() for p in evidence.glob('*.json')};callcount=len(calls)
            client.post(path+'/cancel',headers=headers).raise_for_status()
        with TestClient(app()) as client:
            client.get(path,headers=headers).raise_for_status()
            client.post(path+'/cancel',headers=headers).raise_for_status()
            time.sleep(.3)
        assert len(calls)==callcount
        assert before=={p.name:p.read_bytes() for p in evidence.glob('*.json')}
    with store.connect() as db:assert db.execute('SELECT count(*) FROM workspace_leases').fetchone()[0]==0
    assert keep.read_bytes()==b'preserve-this-file\n'
    active=subprocess.check_output(['docker','ps','-a','--no-trunc','--filter','id='+started['Id'],'--format','{{.ID}}'],text=True)
    assert not active.strip()
    report=dict(status='passed_real_docker_asgi',nativeExecutionMetadata='explicitly synthetic; no model/Codex execution',
        taskId=tid,containerId=started['Id'],cancellation=proof,cancelResponse=state['cancelResponse'],
        appRebuiltWhileContainerRunning=True,repeatDockerCalls=0,evidenceStable=True,workspaceLeaseCount=0,keepFileUnchanged=True,
        originalContainerAbsent=True,mainConfigurationChanged=False,calls=calls)
    save('report.json',report);state['phase']='completed';save('state.json',state)
    print(json.dumps({'status':report['status'],'taskId':tid,'replayDockerCalls':0}),flush=True)
    return 0

if __name__=='__main__':raise SystemExit(main())
