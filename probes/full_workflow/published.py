#!/usr/bin/env python3
"""One real published catalog -> Sim -> native role -> Codex -> dynamic End proof.

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
from agent_line.authoring_api import AuthoringAPI,AuthoringAccess,state_hash
from agent_line.approvals import TrustedPrincipal
from probes.qwenpaw_memory.native_roundtrip import quiet_host
from probes.sim.authoring_probe import write

DATA=ROOT/'var/published-task-flow'
PROJECT='native-memory-roundtrip';BINDING='native-loop-parent';ROLE='agent-line-native-loop-parent'


def finalize(state,sim,calls):
    """Read-only recovery of the original run after a probe-query failure."""
    wid,rid=state['workflowId'],state['runId']
    store=TaskStore(DATA/'tasks.sqlite');task=store.get(state['taskId'])
    if task['executionStatus']!='succeeded' or task['verificationStatus']!='passed' or task['resourcesReleased'] is not True:
        raise RuntimeError('original_task_not_verified_and_released')
    if not any(e.get('kind')=='resource_exit' for e in task['evidence']):raise RuntimeError('resource_exit_proof_missing')
    journal=SimJournal(DATA/'sim.sqlite')
    frozen=journal.get_publication(wid,rid)
    if frozen!=state['frozenPublication'] or frozen['bindingId']!=state['bindingId']:raise RuntimeError('frozen_publication_changed')
    config=json.loads((ROOT/'var/qwenpaw/config.json').read_text())
    workspace=Path(config['plugins']['agent-line']['execution_bridge']['workspace']).resolve(strict=True)
    if hashlib.sha256((workspace/state['artifact']).read_bytes()).hexdigest()!=state['expectedSha256']:raise RuntimeError('artifact_changed')
    condition=native_id(wid,'condition');end=native_id(wid,'successEnd')
    response=sim._client.get(sim.base_url+f'/api/v2/workflows/{wid}/runs/{rid}',params={'selectedOutputs':condition+','+end+'.data'},headers={'x-api-key':sim._key},follow_redirects=False)
    response.raise_for_status();data=response.json()['data']
    if data.get('workflowId')!=wid or data.get('runId')!=rid:raise RuntimeError('result_identity_mismatch')
    blocks=data['blockOutputs'];choice=blocks[condition]
    if choice.get('selectedPath',{}).get('blockId')!=end or choice.get('selectedOption')!='passed':raise RuntimeError('actual_success_branch_unproven')
    value=blocks[end+'.data']['agentLineResult']
    if value.get('taskId')!=task['id'] or any(value.get(k)!=task[k] for k in ('executionStatus','verificationStatus','executionIdentity','output')):raise RuntimeError('dynamic_result_identity_mismatch')
    observed=sim.observe(wid,rid)
    if observed['status']!='completed' or observed['response']['data'].get('output',{}).get('data')!={'agentLineResult':value}:raise RuntimeError('final_result_mismatch')
    if state['parentObservation'].get('status')!='finished':raise RuntimeError('parent_not_finished')
    process=json.loads((ROOT/'var/qwenpaw/agent-line-process.json').read_text());quiet_host(process['pid'])
    with store.connect() as db:leases=db.execute('SELECT count(*) FROM workspace_leases').fetchone()[0]
    if leases:raise RuntimeError('leases_remain')
    if any(c['method']!='GET' for c in calls):raise RuntimeError('finalize_must_not_mutate')
    write(DATA/'finalize-evidence.json',{'nativeEndEvidence':data,'task':task,'simObservation':observed,'nativeCalls':calls,'nativeWritesThisInvocation':0})
    report={'status':'passed_live','workflowId':wid,'runId':rid,'bindingId':state['bindingId'],'taskId':task['id'],'executionIdentity':task['executionIdentity'],'nativeParentTaskId':RoleDispatchLedger(DATA/'roles.sqlite').get(task['id'])['parentTaskId'],'parentFinished':True,'resourcesReleased':True,'activeLeaseCount':leases,'ownedActiveContainerCount':0,'verification':state['verification'],'nativeResumeMatchesTask':True,'frozenPublication':frozen,'actualBranch':'successEnd','dynamicFinalResultMatchesTask':True,'simStatus':'completed','finalizeNativeWrites':0,'initialProbeFailurePreserved':state['failure'],'limitations':['Single isolated confirmed probe; main admission unchanged','No automatic memory writeback or cross-project isolation claim','Original run completed; initial probe requested an unexecuted End and got HTTP400; same-run read-only finalize corrected query']}
    write(DATA/'report.json',report)
    print(json.dumps({'status':'passed_live','taskId':task['id'],'actualBranch':'successEnd','resourcesReleased':True,'finalizeNativeWrites':0}),flush=True)


def main():
    DATA.mkdir(mode=0o700,exist_ok=True);path=DATA/'state.json'
    auth=json.loads((ROOT/'var/sim/probe-auth.json').read_text());sim=SimClient(auth['baseUrl'],auth['apiKey'])
    calls=[];sim._client.event_hooks['request'].append(lambda r:calls.append({'method':r.method,'path':r.url.path}))
    if path.exists():
        state=json.loads(path.read_text())
        if '--finalize' in sys.argv:
            try:finalize(state,sim,calls)
            finally:sim.close()
            return 0
        result={'mode':'observation_only','newSubmissions':0}
        if state.get('workflowId') and state.get('runId'):result['sim']=sim.observe(state['workflowId'],state['runId'])
        if (DATA/'tasks.sqlite').exists():result['tasks']=TaskStore(DATA/'tasks.sqlite').list_tasks({PROJECT})['tasks']
        result['nativeCalls']=calls;result['nativeWritesThisInvocation']=sum(c['method']!='GET' for c in calls)
        if result['nativeWritesThisInvocation']:raise RuntimeError('observation_must_not_write')
        write(DATA/'observation.json',result);sim.close()
        print(json.dumps({'mode':'observation_only','newSubmissions':0,'taskCount':len(result.get('tasks',[]))}));return 0
    gateway=None;host=None
    process=json.loads((ROOT/'var/qwenpaw/agent-line-process.json').read_text())
    quiet_host(process['pid'])
    subprocess.run([sys.executable,'scripts/check_upstreams.py'],cwd=ROOT,check=True,capture_output=True,text=True)
    config=json.loads((ROOT/'var/qwenpaw/config.json').read_text());bridge=config['plugins']['agent-line']['execution_bridge']
    if not bridge.get('container_control'):raise RuntimeError('container_control_required')
    workspace=Path(bridge['workspace']).resolve(strict=True)
    prior=json.loads((ROOT/'var/native-memory-roundtrip/attempt4-report.json').read_text())
    if prior.get('verified') is not True or prior.get('activeLeaseCount')!=0 or prior.get('ownedActiveContainerCount')!=0:raise RuntimeError('prior_native_proof_required')
    suffix=uuid.uuid4().hex[:16];artifact='published-flow-'+suffix+'.txt';expected=('AGENT_LINE_PUBLISHED_'+suffix+'\n').encode()
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
        projects={PROJECT:{'workspace':str(workspace)}};bindings={BINDING:{'projectId':PROJECT,'roleAgentId':ROLE}}
        author_token=secrets.token_urlsafe(40)
        access=AuthoringAccess({author_token:TrustedPrincipal('published-flow-probe-editor',frozenset({PROJECT}),frozenset({'edit_workflows','publish_workflows'}),time.time()+7200)})
        authoring=AuthoringAPI(access,journal,{PROJECT:api},projects=projects,role_bindings=bindings)
        flowconfig={'schemaVersion':1,'runLedgerPath':str(DATA/'runs.sqlite'),'pauseBindingsPath':str(DATA/'pauses.sqlite')};write(DATA/'task-flow.json',flowconfig)
        flow=load_task_flow(DATA/'task-flow.json',tasks=store,projects=projects,role_bindings=bindings,client=sim,journal=journal)
        installation='published-task-flow-'+suffix
        wf=WorkflowGateway(sim,journal,{},installation_id=installation);wf.task_flow=flow;wf.publication_verifier=flow.discovery.publication_verifier
        service_token=(ROOT/'var/qwenpaw-secrets/execution-service-token').read_text().strip()
        gateway=RoleDispatchGateway(base,token,service_token,role_agent_id=ROLE,deployment_id='agent-line-g0-installation',ledger=ledger,memory_query=prior_state['query'],client=host,timeout_seconds=900)
        dispatcher=Dispatcher(store,{(PROJECT,BINDING):gateway})
        credential=secrets.token_urlsafe(40);write(DATA/'service-token.json',{'token':credential})
        app=create_app(store,ServiceConfig(tokens={credential:{PROJECT}},projects=projects,bindings=bindings,controlled_execution='confirmed'),workflows=wf,dispatcher=dispatcher,task_flow=flow,authoring=authoring)
        body={'idempotencyKey':'automatic-'+suffix,'input':{'input':{'artifact':artifact,'verification':'exact UTF-8 bytes with newline','scope':'one new dedicated file'}}}
        with TestClient(app) as http:
            def author(operation,route,payload):
                def request():
                    response=http.post('/extension/v1/authoring/projects/'+PROJECT+'/workflows'+route,headers={'Authorization':'Bearer '+author_token},json=payload)
                    if response.status_code!=202:raise RuntimeError('authoring_http_'+str(response.status_code))
                    data=response.json()
                    if data.get('status')!='accepted':raise RuntimeError('authoring_not_accepted')
                    return data
                return once(operation,request)['receipt']
            created=author('create','',{'operationId':'create-'+suffix,'name':'agent-line-published-task-flow','description':'Isolated real publication-to-execution proof'})
            wid=created['workflowId'];state['workflowId']=wid;persist()
            agent_config={'roleBindingId':BINDING,'instruction':instruction,'inputFrom':{'nodeId':'start','field':'input'},'memoryPolicyId':'native-role','outputSchemaId':'task-result','requireMemoryCommit':False,'allowFailureBranch':False}
            def node(i,t,name,c,x):return {'id':i,'type':t,'name':name,'position':{'x':x,'y':0},'config':c}
            doc={'schemaVersion':1,'nodes':[node('start','start','开始',{},0),node('agent','agent','执行节点',agent_config,200),node('condition','condition','验收分支',{'rules':[{'id':'passed','operator':'eq','source':'agent','field':'result.verificationStatus','value':'passed'}]},400),node('successEnd','end','验收通过',{'resultFrom':'agent'},600),node('failureEnd','end','验收失败',{'resultFrom':'agent'},600)],'edges':[{'id':'a','source':'start','target':'agent'},{'id':'b','source':'agent','target':'condition'},{'id':'c','source':'condition','target':'successEnd','branch':'passed'},{'id':'d','source':'condition','target':'failureEnd','branch':'else'}],'variables':{}}
            state['document']=doc;persist()
            saved=author('save','/'+wid+'/save',{'operationId':'save-'+suffix,'expectedStateSha256':state_hash(api.get_state(wid)),'document':doc})
            if saved.get('outcome')!='saved':raise RuntimeError('save_not_verified')
            published=author('publish','/'+wid+'/publish',{'operationId':'publish-'+suffix,'expectedStateSha256':state_hash(api.get_state(wid))})
            if published.get('outcome')!='published':raise RuntimeError('publish_not_verified')
            binding=published['bindingId'];state['bindingId']=binding;state['publication']=published;persist()
            catalog=http.get('/extension/v1/workflows',headers={'Authorization':'Bearer '+credential});catalog.raise_for_status()
            if binding not in [item['bindingId'] for item in catalog.json()['workflows']]:raise RuntimeError('published_binding_not_discovered')
            if wf.bindings:raise RuntimeError('static_binding_forbidden')
            bid=native_id(wid,'agent')
            run_id=str(uuid.uuid5(uuid.NAMESPACE_URL,canonical([installation,PROJECT,wid,body['idempotencyKey'],binding])))
            state['runId']=run_id;state['startPayload']=body;state['startIntent']=True;persist()
            started=http.post('/extension/v1/workflows/'+binding+'/runs',headers={'Authorization':'Bearer '+credential},json=body)
            state['startHTTP']=started.status_code;state['startReceipt']=started.json();persist()
            if started.status_code!=202 or started.json().get('status')!='accepted':raise RuntimeError('start_not_accepted')
            state['frozenPublication']=journal.get_publication(wid,run_id);persist()
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
                if time.monotonic()-last_print>30:
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
            selectors=native_id(wid,'condition')+','+native_id(wid,'successEnd')+'.data'
            ends=sim._client.get(sim.base_url+f'/api/v2/workflows/{wid}/runs/{run_id}',params={'selectedOutputs':selectors},headers={'x-api-key':sim._key},follow_redirects=False);ends.raise_for_status()
            end_data=ends.json()['data'];state['nativeEndEvidence']=end_data;persist()
            if end_data.get('workflowId')!=wid or end_data.get('runId')!=run_id:raise RuntimeError('end_identity_mismatch')
            expected_end={'agentLineResult':value}
            blocks=end_data.get('blockOutputs',{});choice=blocks.get(native_id(wid,'condition'),{})
            if blocks.get(native_id(wid,'successEnd')+'.data')!=expected_end or choice.get('selectedPath',{}).get('blockId')!=native_id(wid,'successEnd') or choice.get('selectedOption')!='passed':raise RuntimeError('success_branch_not_proven')
            if state['simObservation']['response']['data'].get('output',{}).get('data')!=expected_end:raise RuntimeError('dynamic_final_output_mismatch')

        quiet_host(process['pid'])
        with store.connect() as db:leases=db.execute('SELECT COUNT(*) FROM workspace_leases').fetchone()[0]
        if leases:raise RuntimeError('leases_remain')
        report={'status':'passed_live','workflowId':wid,'runId':run_id,'taskId':task['id'],'executionIdentity':task['executionIdentity'],'nativeParentTaskId':pid,'parentFinished':True,'resourcesReleased':True,'activeLeaseCount':leases,'ownedActiveContainerCount':0,'verification':state['verification'],'nativeResumeMatchesTask':True,'bindingId':binding,'frozenPublication':state['frozenPublication'],'actualBranch':'successEnd','dynamicFinalResultMatchesTask':True,'simStatus':state['simObservation']['status'],'nativeCalls':calls,'limitations':['Single isolated probe with explicitly confirmed capability; main admission unchanged','No production isolation acceptance','No automatic memory writeback or cross-project isolation claim','Dynamic End and selectedOutputs prove actual branch; independent file verifier proves artifact']}
        write(DATA/'report.json',report);state['result']='passed_live';persist();print(json.dumps({'status':'passed_live','taskId':task['id'],'resourcesReleased':True}),flush=True);return 0
    except Exception as exc:
        state['failure']={'type':type(exc).__name__,'code':getattr(exc,'code',str(exc) if type(exc) is RuntimeError else 'probe_failed')};persist();write(DATA/'failure.json',{'status':'incomplete',**state['failure'],'nativeCalls':calls});print(json.dumps({'status':'incomplete',**state['failure']}),flush=True);return 1
    finally:
        if gateway is not None:gateway.close()
        if host is not None:host.close()
        sim.close()


if __name__=='__main__':raise SystemExit(main())
