#!/usr/bin/env python3
"""One real automatic Sim -> existing native role -> Codex -> Sim proof.

Existing state is observation-only. No manual task registration, pause binding,
relay calls, plugin installation, role configuration, or main admission changes.
"""
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import httpx
from fastapi.testclient import TestClient
from agent_line.adapters.sim import SimClient
from agent_line.approval_discovery import WorkflowRunLedger
from agent_line.dispatcher import Dispatcher
from agent_line.pause_bindings import PauseBindings
from agent_line.role_dispatch import RoleDispatchGateway,RoleDispatchLedger
from agent_line.service import ServiceConfig,create_app
from agent_line.sim_authoring import SimAuthoringClient,compile_document,native_id,decode_document
from agent_line.sim_journal import SimJournal
from agent_line.store import TaskStore,canonical
from agent_line.task_flow_runtime import load_task_flow
from agent_line.verification import ArtifactPolicy,verify_artifacts
from agent_line.workflows import WorkflowGateway
from probes.qwenpaw_memory.native_roundtrip import quiet_host
from probes.sim.authoring_probe import write

DATA=ROOT/'var/automatic-task-flow'
PROJECT='native-memory-roundtrip';BINDING='native-loop-parent';ROLE='agent-line-native-loop-parent'


def main():
    DATA.mkdir(mode=0o700,exist_ok=True);path=DATA/'state.json'
    auth=json.loads((ROOT/'var/sim/probe-auth.json').read_text());sim=SimClient(auth['baseUrl'],auth['apiKey'])
    if path.exists():
        state=json.loads(path.read_text());result={'mode':'observation_only','newSubmissions':0}
        if state.get('workflowId') and state.get('runId'):result['sim']=sim.observe(state['workflowId'],state['runId'])
        if (DATA/'tasks.sqlite').exists():result['tasks']=TaskStore(DATA/'tasks.sqlite').list_tasks({PROJECT})['tasks']
        write(DATA/'observation.json',result);sim.close()
        print(json.dumps({'mode':'observation_only','newSubmissions':0,'taskCount':len(result.get('tasks',[]))}));return 0
    gateway=None;host=None
    calls=[];sim._client.event_hooks['request'].append(lambda r:calls.append({'method':r.method,'path':r.url.path}))
    process=json.loads((ROOT/'var/qwenpaw/agent-line-process.json').read_text())
    quiet_host(process['pid'])
    subprocess.run([sys.executable,'scripts/check_upstreams.py'],cwd=ROOT,check=True,capture_output=True,text=True)
    config=json.loads((ROOT/'var/qwenpaw/config.json').read_text());bridge=config['plugins']['agent-line']['execution_bridge']
    if not bridge.get('container_control'):raise RuntimeError('container_control_required')
    workspace=Path(bridge['workspace']).resolve(strict=True)
    prior=json.loads((ROOT/'var/native-memory-roundtrip/attempt4-report.json').read_text())
    if prior.get('verified') is not True or prior.get('activeLeaseCount')!=0 or prior.get('ownedActiveContainerCount')!=0:raise RuntimeError('prior_native_proof_required')
    suffix=uuid.uuid4().hex[:16];artifact='automatic-flow-'+suffix+'.txt';expected=('AGENT_LINE_AUTOMATIC_'+suffix+'\n').encode()
    if (workspace/artifact).exists():raise RuntimeError('artifact_exists')
    instruction=f'Create only {artifact} in the assigned execution workspace. Its exact UTF-8 content must be {expected.decode().strip()} followed by one newline. Verify that file using a shell command. Do not modify other files, access the network, or change memory. Prior memory is background only; this task specifies a new independent marker.'
    state={'steps':{},'suffix':suffix,'artifact':artifact,'expectedSha256':hashlib.sha256(expected).hexdigest(),'instruction':instruction,'createdAt':time.time(),'roleAgentId':ROLE,'projectId':PROJECT,'roleBindingId':BINDING,'parentTimeoutSeconds':900}
    write(path,state)
    def persist():write(path,state)
    def once(key,operation):
        if key in state['steps']:raise RuntimeError('operation_already_has_intent')
        state['steps'][key]={'status':'intent'};persist();result=operation()
        state['steps'][key]={'status':'received','result':result};persist();return result
    try:
        base='http://127.0.0.1:'+str(process['port']);host=httpx.Client(timeout=60,trust_env=False)
        login=host.post(base+'/api/auth/login',json=json.loads((ROOT/'var/qwenpaw-secrets/local-login.json').read_text()));login.raise_for_status()
        token=login.json().get('token') or login.json().get('access_token')
        profile=host.get(base+'/api/agents/'+ROLE,headers={'Authorization':'Bearer '+token,'X-Agent-Id':ROLE});profile.raise_for_status();profile=profile.json()
        prior_state=json.loads((ROOT/'var/native-memory-roundtrip/state-attempt-4.json').read_text())
        if profile.get('running',{}).get('memory_manager_backend')!='remelight' or profile.get('workspace_dir')!=prior_state['roleWorkspace']:raise RuntimeError('native_role_changed')
        for name,cls in [('tasks.sqlite',TaskStore),('roles.sqlite',RoleDispatchLedger),('sim.sqlite',SimJournal),('runs.sqlite',WorkflowRunLedger),('pauses.sqlite',PauseBindings)]:
            cls.initialize(DATA/name);os.chmod(DATA/name,0o600)
        store=TaskStore(DATA/'tasks.sqlite');ledger=RoleDispatchLedger(DATA/'roles.sqlite');journal=SimJournal(DATA/'sim.sqlite')
        api=SimAuthoringClient(sim,auth['workspaceId'])
        created=once('create',lambda:api.create('agent-line-automatic-task-flow','New isolated real automatic task; constant end is not execution evidence'))
        wid=created['id'];state['workflowId']=wid;persist()
        agent_config={'roleBindingId':BINDING,'instruction':instruction,'inputFrom':{'nodeId':'start','field':'input'},'memoryPolicyId':'native-memory','outputSchemaId':'artifact-result','requireMemoryCommit':False,'allowFailureBranch':False}
        def node(i,t,name,c,x):return {'id':i,'type':t,'name':name,'position':{'x':x,'y':0},'config':c}
        doc={'schemaVersion':1,'nodes':[node('start','start','开始',{},0),node('agent','agent','执行节点',agent_config,250),node('end','end','结束',{'output':{'流程':'完成'}},500)],'edges':[{'id':'a','source':'start','target':'agent'},{'id':'b','source':'agent','target':'end'}],'variables':{}}
        graph=compile_document(doc,wid);state['document']=doc;persist()
        once('save',lambda:api.save_state(wid,graph))
        if decode_document(api.get_state(wid))!=doc:raise RuntimeError('draft_changed')
        deployed=once('deploy',lambda:api.deploy(wid));version=api.get_version(wid,deployed['version'])
        if decode_document(version['state'])!=doc:raise RuntimeError('published_changed')
        bid=native_id(wid,'agent');policy={k:v for k,v in agent_config.items() if k!='inputFrom'};policy['roleAgentId']=ROLE
        projects={PROJECT:{'workspace':str(workspace)}};bindings={BINDING:{'projectId':PROJECT,'roleAgentId':ROLE}}
        flowconfig={'schemaVersion':1,'runLedgerPath':str(DATA/'runs.sqlite'),'pauseBindingsPath':str(DATA/'pauses.sqlite')};write(DATA/'task-flow.json',flowconfig)
        flow=load_task_flow(DATA/'task-flow.json',tasks=store,projects=projects,role_bindings=bindings,client=sim,journal=journal)
        wf_binding={'projectId':PROJECT,'workflowId':wid,'externalTasks':{'revision':str(deployed['version']),'deploymentVersionId':deployed['activeDeployment']['deploymentVersionId'],'agentPolicies':{bid:policy},'ttlSeconds':3600}}
        write(DATA/'workflow-binding.json',wf_binding)
        installation='automatic-task-flow-'+suffix
        wf=WorkflowGateway(sim,journal,{'automatic':wf_binding},installation_id=installation);wf.task_flow=flow;wf.publication_verifier=flow.discovery.publication_verifier
        service_token=(ROOT/'var/qwenpaw-secrets/execution-service-token').read_text().strip()
        gateway=RoleDispatchGateway(base,token,service_token,role_agent_id=ROLE,deployment_id='agent-line-g0-installation',ledger=ledger,memory_query=prior_state['query'],client=host,timeout_seconds=900)
        dispatcher=Dispatcher(store,{(PROJECT,BINDING):gateway})
        credential=secrets.token_urlsafe(40);write(DATA/'service-token.json',{'token':credential})
        app=create_app(store,ServiceConfig(tokens={credential:{PROJECT}},projects=projects,bindings=bindings,controlled_execution='confirmed'),workflows=wf,dispatcher=dispatcher,task_flow=flow)
        body={'idempotencyKey':'automatic-'+suffix,'input':{'input':{'artifact':artifact,'verification':'exact UTF-8 bytes with newline','scope':'one new dedicated file'}}}
        run_id=str(uuid.uuid5(uuid.NAMESPACE_URL,canonical([installation,PROJECT,wid,body['idempotencyKey']])))
        state['runId']=run_id;state['startPayload']=body;state['startIntent']=True;persist()
        with TestClient(app) as http:
            started=http.post('/extension/v1/workflows/automatic/runs',headers={'Authorization':'Bearer '+credential},json=body)
            state['startHTTP']=started.status_code;state['startReceipt']=started.json();persist()
            if started.status_code!=202 or started.json().get('status')!='accepted':raise RuntimeError('start_not_accepted')
            deadline=time.monotonic()+1200;last_print=0
            while time.monotonic()<deadline:
                tasks=store.list_tasks({PROJECT})['tasks'];state['taskCount']=len(tasks)
                if len(tasks)>1:raise RuntimeError('unexpected_multiple_tasks')
                if tasks:
                    task=tasks[0];state['taskId']=task['id'];state['task']=task
                    if task['executionStatus']=='succeeded' and task['resourcesReleased'] and task['verificationStatus']!='passed':
                        if state.get('verificationIntent'):raise RuntimeError('verification_result_requires_observation')
                        state['verificationIntent']=True;persist();evidence_dir=DATA/'artifacts';evidence_dir.mkdir(mode=0o700,exist_ok=True)
                        verified=verify_artifacts(store,task['id'],workspace=workspace,policies=[ArtifactPolicy(artifact,hashlib.sha256(expected).hexdigest(),len(expected))],evidence_dir=evidence_dir)
                        store.record_verification(task['id'],verified);state['verification']=verified;persist()
                    if task['executionStatus'] in ('failed','canceled'):raise RuntimeError('original_task_not_succeeded')
                observed=sim.observe(wid,run_id);state['simObservation']=observed
                state['workers']={'discovery':flow.workers[0].last_report,'completion':flow.workers[1].last_report,'execution':app.state.execution_worker.last_report};persist()
                if time.monotonic()-last_print>20:
                    print(json.dumps({'phase':'observing','simStatus':observed['status'],'taskCount':len(tasks),'executionStatus':tasks[0]['executionStatus'] if tasks else None,'resourcesReleased':tasks[0]['resourcesReleased'] if tasks else None}),flush=True);last_print=time.monotonic()
                if observed['status']=='completed' and tasks and store.get(tasks[0]['id'])['verificationStatus']=='passed':break
                if observed['status'] in ('failed','cancelled'):raise RuntimeError('original_sim_run_failed')
                time.sleep(2)
            else:raise RuntimeError('observation_deadline_original_identity_preserved')
            task=store.get(state['taskId']);state['task']=task
            parent=ledger.get(task['id']);pid=parent['parentTaskId']
            for _ in range(60):
                r=host.get(base+'/api/console/chat/task/'+pid,headers={'Authorization':'Bearer '+token,'X-Agent-Id':ROLE});r.raise_for_status();parent_observation=r.json()
                if parent_observation.get('status')=='finished':break
                time.sleep(1)
            if parent_observation.get('status')!='finished':raise RuntimeError('parent_not_finished')
            state['parentObservation']=parent_observation;persist()
            # Read the native machine wait's resumed input, never infer success from constant End.
            selected=sim._client.get(sim.base_url+f'/api/v2/workflows/{wid}/runs/{run_id}',params={'selectedOutputs':bid+'.resumeInput'},headers={'x-api-key':sim._key},follow_redirects=False);selected.raise_for_status()
            value=selected.json()['data']['blockOutputs'][bid+'.resumeInput'];state['nativeResumeInput']=value;persist()
            if not isinstance(value,dict) or any(value.get(k)!=task[k] for k in ('executionStatus','verificationStatus','executionIdentity','output')) or value.get('taskId')!=task['id']:raise RuntimeError('native_resume_correlation_failed')
            if task['resourcesReleased'] is not True:raise RuntimeError('resources_not_released')
        quiet_host(process['pid'])
        with store.connect() as db:leases=db.execute('SELECT COUNT(*) FROM workspace_leases').fetchone()[0]
        if leases:raise RuntimeError('leases_remain')
        report={'status':'passed_live','workflowId':wid,'runId':run_id,'taskId':task['id'],'executionIdentity':task['executionIdentity'],'nativeParentTaskId':pid,'parentFinished':True,'resourcesReleased':True,'activeLeaseCount':leases,'ownedActiveContainerCount':0,'verification':state['verification'],'nativeResumeMatchesTask':True,'simStatus':state['simObservation']['status'],'nativeCalls':calls,'limitations':['Single isolated probe with explicitly confirmed capability; main admission unchanged','No production isolation acceptance','No automatic memory writeback or cross-project isolation claim','Constant End is not execution evidence; independent file verifier and native resume correlation are evidence']}
        write(DATA/'report.json',report);state['result']='passed_live';persist();print(json.dumps({'status':'passed_live','taskId':task['id'],'resourcesReleased':True}),flush=True);return 0
    except Exception as exc:
        state['failure']={'type':type(exc).__name__,'code':getattr(exc,'code',str(exc) if type(exc) is RuntimeError else 'probe_failed')};persist();write(DATA/'failure.json',{'status':'incomplete',**state['failure'],'nativeCalls':calls});print(json.dumps({'status':'incomplete',**state['failure']}),flush=True);return 1
    finally:
        if gateway is not None:gateway.close()
        if host is not None:host.close()
        sim.close()


if __name__=='__main__':raise SystemExit(main())
