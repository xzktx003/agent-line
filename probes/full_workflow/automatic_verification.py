#!/usr/bin/env python3
"""Real automatic artifact verification with a durable-receipt recovery window.

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
from agent_line.sim_authoring import SimAuthoringClient,native_id
from agent_line.sim_journal import SimJournal
from agent_line.store import TaskStore,canonical
from agent_line.task_flow_runtime import load_task_flow
from agent_line.automatic_verification import AutomaticVerifier
from agent_line.store import Conflict
from agent_line.workflows import WorkflowGateway
from agent_line.authoring_api import AuthoringAPI,AuthoringAccess,state_hash
from agent_line.approvals import TrustedPrincipal
from probes.qwenpaw_memory.native_roundtrip import quiet_host
from probes.sim.authoring_probe import write

DATA=ROOT/'var/automatic-verification-flow'
PROJECT='native-memory-roundtrip';BINDING='native-loop-parent';ROLE='agent-line-native-loop-parent'


def main():
    DATA.mkdir(mode=0o700,exist_ok=True);path=DATA/'state.json'
    auth=json.loads((ROOT/'var/sim/probe-auth.json').read_text());sim=SimClient(auth['baseUrl'],auth['apiKey'])
    calls=[];sim._client.event_hooks['request'].append(lambda r:calls.append({'method':r.method,'path':r.url.path}))
    if path.exists():
        state=json.loads(path.read_text())
        result={'mode':'observation_only','newSubmissions':0}
        result['runs']={name:sim.observe(state['workflowId'],case.get('finalRunId',case['runId'])) for name,case in state.get('cases',{}).items() if case.get('runId')}
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
    suffix=uuid.uuid4().hex[:16]
    artifact='automatic-verification-'+suffix+'.txt'
    correct='AUTO_CORRECT_'+suffix;wrong='AUTO_WRONG_'+suffix
    cases={name:{'artifact':artifact,'marker':correct if name=='pass' else wrong,'tasks':{}} for name in ('pass','fail')}
    if (workspace/artifact).exists():raise RuntimeError('artifact_exists')
    instruction=('Read artifact and marker from taskInput. Delegate to Codex to write only that relative file in its assigned workspace, replacing its content with marker plus exactly one newline, then shell-check its content. '
        'This intentionally tests externally configured byte verification; do not substitute other markers from memory, do not guess or alter verifier policy. Do not modify other files, network, memory or permissions; do not guess absolute cwd.')
    main_config_path=ROOT/'var/adapter/service.json';main_config=json.loads(main_config_path.read_text())
    main_database=Path(main_config['database']);main_database=main_database if main_database.is_absolute() else main_config_path.parent/main_database
    main_guard=TaskStore(main_database).maintenance_status()
    state={'steps':{},'cases':cases,'suffix':suffix,'instruction':instruction,'createdAt':time.time(),'roleAgentId':ROLE,'projectId':PROJECT,'roleBindingId':BINDING,'parentTimeoutSeconds':900,'mainGuardBefore':main_guard}
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
        projects={PROJECT:{'workspace':str(workspace)}};bindings={BINDING:{'projectId':PROJECT,'roleAgentId':ROLE,'verificationPolicy':{'schemaVersion':1,'policyId':'automatic-bytes-'+suffix,'kind':'artifact_sha256','requireTests':False,'artifacts':[{'path':artifact,'sha256':hashlib.sha256((correct+'\n').encode()).hexdigest(),'exactSize':len((correct+'\n').encode()),'maxBytes':1024}]}}}
        author_token=secrets.token_urlsafe(40)
        access=AuthoringAccess({author_token:TrustedPrincipal('automatic-loop-probe-editor',frozenset({PROJECT}),frozenset({'edit_workflows','publish_workflows'}),time.time()+7200)})
        authoring=AuthoringAPI(access,journal,{PROJECT:api},projects=projects,role_bindings=bindings)
        flowconfig={'schemaVersion':1,'runLedgerPath':str(DATA/'runs.sqlite'),'pauseBindingsPath':str(DATA/'pauses.sqlite')};write(DATA/'task-flow.json',flowconfig)
        installation='automatic-verification-flow-'+suffix
        service_token=(ROOT/'var/qwenpaw-secrets/execution-service-token').read_text().strip()
        gateway=RoleDispatchGateway(base,token,service_token,role_agent_id=ROLE,deployment_id='agent-line-g0-installation',ledger=ledger,memory_query=prior_state['query'],client=host,timeout_seconds=900)
        dispatcher=Dispatcher(store,{(PROJECT,BINDING):gateway})
        credential=secrets.token_urlsafe(40);write(DATA/'service-token.json',{'token':credential})
        evidence_dir=DATA/'verification-evidence';evidence_dir.mkdir(mode=0o700)
        fault={'calls':0}
        def refuse_record(*args,**kwargs):
            fault['calls']+=1
            raise Conflict('Probe injected record-commit interruption')
        store.record_verification=refuse_record
        def build_app(current_store):
            runtime=load_task_flow(DATA/'task-flow.json',tasks=current_store,projects=projects,role_bindings=bindings,client=sim,journal=journal)
            workflow=WorkflowGateway(sim,journal,{},installation_id=installation)
            workflow.task_flow=runtime;workflow.publication_verifier=runtime.discovery.publication_verifier
            verifier=AutomaticVerifier(current_store,evidence_dir)
            current_dispatcher=Dispatcher(current_store,{(PROJECT,BINDING):gateway})
            return create_app(current_store,ServiceConfig(tokens={credential:{PROJECT}},projects=projects,bindings=bindings,controlled_execution='confirmed'),workflows=workflow,dispatcher=current_dispatcher,task_flow=runtime,authoring=authoring,verifier=verifier)
        headers={'Authorization':'Bearer '+credential}
        app=build_app(store);http=TestClient(app);http.__enter__()
        try:
            def author(operation,route,payload):
                def request():
                    response=http.post('/extension/v1/authoring/projects/'+PROJECT+'/workflows'+route,headers={'Authorization':'Bearer '+author_token},json=payload)
                    if response.status_code!=202:raise RuntimeError('authoring_http_'+str(response.status_code))
                    value=response.json()
                    if value.get('status')!='accepted':raise RuntimeError('authoring_not_accepted')
                    return value
                return once(operation,request)['receipt']
            created=author('create','',{'operationId':'create-'+suffix,'name':'agent-line-automatic-verification','description':'Real background byte verification and receipt recovery'})
            wid=created['workflowId'];state['workflowId']=wid;persist()
            agent_config={'roleBindingId':BINDING,'instruction':instruction,'inputFrom':{'nodeId':'start','field':'input'},'memoryPolicyId':'native-role','outputSchemaId':'task-result','requireMemoryCommit':False,'allowFailureBranch':True}
            def node(i,t,c,x):return {'id':i,'type':t,'name':i,'position':{'x':x,'y':0},'config':c}
            doc={'schemaVersion':1,'nodes':[node('start','start',{},0),node('agent','agent',agent_config,200),node('condition','condition',{'rules':[{'id':'verified','operator':'eq','source':'agent','field':'result.verificationStatus','value':'passed'}]},400),node('passEnd','end',{'resultFrom':'agent'},600),node('failEnd','end',{'resultFrom':'agent'},600)],'edges':[{'id':'a','source':'start','target':'agent'},{'id':'b','source':'agent','target':'condition'},{'id':'c','source':'condition','target':'passEnd','branch':'verified'},{'id':'d','source':'condition','target':'failEnd','branch':'else'}],'variables':{}}
            state['document']=doc;state['verificationPolicy']=bindings[BINDING]['verificationPolicy'];persist()
            saved=author('save','/'+wid+'/save',{'operationId':'save-'+suffix,'expectedStateSha256':state_hash(api.get_state(wid)),'document':doc})
            if saved.get('outcome')!='saved':raise RuntimeError('save_not_verified')
            published=author('publish','/'+wid+'/publish',{'operationId':'publish-'+suffix,'expectedStateSha256':state_hash(api.get_state(wid))})
            if published.get('outcome')!='published':raise RuntimeError('publish_not_verified')
            binding=published['bindingId'];state.update(bindingId=binding,publication=published);persist()
            for name,case in cases.items():
                quiet_host(process['pid'])
                body={'idempotencyKey':name+'-'+suffix,'input':{'input':{'case':name,'artifact':artifact,'marker':case['marker']}}}
                run_id=str(uuid.uuid5(uuid.NAMESPACE_URL,canonical([installation,PROJECT,wid,body['idempotencyKey'],binding])))
                route='/extension/v1/workflows/'+binding+'/runs/'+run_id
                case.update(runId=run_id,startPayload=body,startIntent=True);persist()
                started=http.post('/extension/v1/workflows/'+binding+'/runs',headers=headers,json=body)
                case.update(startHTTP=started.status_code,startReceipt=started.json());persist()
                if started.status_code!=202 or started.json().get('status')!='accepted':raise RuntimeError('start_not_accepted')
                case['frozenPublication']=journal.get_publication(wid,run_id);persist()
                deadline=time.monotonic()+1200;last_print=0
                while time.monotonic()<deadline:
                    tasks=[t for t in store.list_tasks({PROJECT})['tasks'] if t['payload']['workflow']['runId']==run_id]
                    if len(tasks)>1:raise RuntimeError('unexpected_multiple_tasks')
                    if tasks:
                        task=tasks[0];tid=task['id'];case['tasks'][tid]=task
                        if task['executionStatus'] in ('failed','canceled'):raise RuntimeError('native_execution_failed')
                        if name=='pass' and not case.get('restarted'):
                            files=list(evidence_dir.glob('verification-*.json'))
                            if files and fault['calls'] and task['resourcesReleased']:
                                if len(files)!=1 or task['verificationStatus']!='pending':raise RuntimeError('fault_window_not_preserved')
                                raw=files[0].read_bytes();receipt=json.loads(raw)
                                if receipt['taskId']!=tid or store.verification_lease(tid)!=str(workspace):raise RuntimeError('fault_window_ownership_mismatch')
                                if journal.resume_summary(wid,run_id) is not None:raise RuntimeError('unverified_task_resumed')
                                native=sim.observe(wid,run_id)
                                if native['status']!='paused':raise RuntimeError('fault_window_not_paused')
                                case['faultWindow']={'task':task,'receipt':receipt,'evidenceFile':str(files[0]),'bytesSha256':hashlib.sha256(raw).hexdigest(),'recordRefusals':fault['calls'],'native':native,'leaseOwner':tid};persist()
                                http.__exit__(None,None,None)
                                store=TaskStore(DATA/'tasks.sqlite')
                                app=build_app(store);http=TestClient(app);http.__enter__()
                                case['restarted']=True;persist()
                                print(json.dumps({'case':name,'phase':'receipt_recovery_new_app','taskId':tid}),flush=True)
                        current=store.get(tid)
                        expected='passed' if name=='pass' else 'failed'
                        if current['verificationStatus']!= 'pending':
                            if current['verificationStatus']!=expected:raise RuntimeError('unexpected_verification_result')
                            case['tasks'][tid]=current
                            if name=='pass':
                                raw=Path(case['faultWindow']['evidenceFile']).read_bytes()
                                entry=next(e for e in current['evidence'] if e.get('kind')=='verification')
                                if hashlib.sha256(raw).hexdigest()!=case['faultWindow']['bytesSha256'] or entry!=case['faultWindow']['receipt']:raise RuntimeError('receipt_was_recomputed')
                                case['receiptReusedExactly']=True
                            result=http.get(route,headers=headers);result.raise_for_status();observed=result.json();child=observed.get('resumption',{})
                            if child.get('execution',{}).get('status')=='completed':
                                case['finalRunId']=child['runId'];case['finalObservation']=observed;persist();break
                    if time.monotonic()-last_print>30:
                        print(json.dumps({'case':name,'phase':'background_execution_and_verification','taskCount':len(tasks),'taskId':tasks[0]['id'] if tasks else None,'verificationStatus':tasks[0]['verificationStatus'] if tasks else None}),flush=True);last_print=time.monotonic();persist()
                    time.sleep(1)
                else:raise RuntimeError('original_execution_observation_timeout')
                end='passEnd' if name=='pass' else 'failEnd'
                selectors=','.join([native_id(wid,'condition'),native_id(wid,end)+'.data'])
                response=sim._client.get(sim.base_url+f'/api/v2/workflows/{wid}/runs/'+case['finalRunId'],params={'selectedOutputs':selectors},headers={'x-api-key':sim._key},follow_redirects=False);response.raise_for_status()
                native=response.json()['data'];case['actualBranch']=native;blocks=native['blockOutputs']
                selected=blocks[native_id(wid,'condition')]['selectedPath']['blockId']
                result=blocks[native_id(wid,end)+'.data']['agentLineResult']
                if selected!=native_id(wid,end) or result['taskId']!=tid or result['verificationStatus']!=expected or result['output']!=current['output']:raise RuntimeError('actual_native_branch_mismatch')
                parent=ledger.get(tid);case['parent']=parent
                for _ in range(90):
                    response=host.get(base+'/api/console/chat/task/'+parent['parentTaskId'],headers={'Authorization':'Bearer '+token,'X-Agent-Id':ROLE});response.raise_for_status();case['parentObservation']=response.json()
                    if response.json().get('status')=='finished':break
                    time.sleep(1)
                if case['parentObservation'].get('status')!='finished':raise RuntimeError('parent_not_finished')
                if not current['resourcesReleased'] or not any(e.get('kind')=='resource_exit' for e in current['evidence']):raise RuntimeError('resource_exit_missing')
                before=sum(c['method']!='GET' for c in calls);case['duplicateIntent']=True;persist()
                replay=http.post('/extension/v1/workflows/'+binding+'/runs',headers=headers,json=body)
                if replay.status_code!=202 or sum(c['method']!='GET' for c in calls)!=before:raise RuntimeError('duplicate_native_write')
                case['duplicateNativeWrites']=0
                quiet_host(process['pid'])
                with store.connect() as db:leases=db.execute('SELECT count(*) FROM workspace_leases').fetchone()[0]
                if leases:raise RuntimeError('leases_remain')
                case.update(result='passed_live',businessVerified=name=='pass');persist()
                print(json.dumps({'case':name,'status':'passed_live','verificationStatus':expected,'taskId':tid,'activeLeaseCount':0}),flush=True)
        finally:http.__exit__(None,None,None)
        state['mainGuardAfter']=TaskStore(main_database).maintenance_status()
        if state['mainGuardAfter']!=state['mainGuardBefore']:raise RuntimeError('main_guard_changed')
        write(DATA/'report.json',{'status':'passed_live','cases':cases,'nativeCalls':calls,'mainGuardUnchanged':True,'activeLeaseCount':0,'ownedActiveContainerCount':0,'receiptRecoveryValidated':True,'automaticArtifactVerificationValidated':True,'testExecutionValidated':False,'limitations':['Byte policies only, not general test-suite execution','Native ReMe may update normally; no manual writeback/configuration change']})
        state['result']='passed_live';persist();return 0
    except Exception as exc:
        state['failure']={'type':type(exc).__name__,'code':str(exc) if type(exc) is RuntimeError else 'probe_failed'};persist();write(DATA/'failure.json',{'status':'incomplete',**state['failure'],'nativeCalls':calls});print(json.dumps({'status':'incomplete',**state['failure']}),flush=True);return 1
    finally:
        if gateway is not None:gateway.close()
        if host is not None:host.close()
        sim.close()


if __name__=='__main__':raise SystemExit(main())
