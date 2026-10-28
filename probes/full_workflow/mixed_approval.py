#!/usr/bin/env python3
"""Two real verified Codex tasks followed by independent approve/reject gates.

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

DATA=ROOT/'var/automatic-mixed-approval'
PROJECT='native-memory-roundtrip';BINDING='native-loop-parent';ROLE='agent-line-native-loop-parent'


from contextlib import contextmanager
from agent_line.approvals import ApprovalLedger,ApprovalCoordinator
from agent_line.approval_api import ApprovalAccess,ApprovalAPI
from agent_line.approval_discovery import ApprovalDiscovery
from agent_line.approval_runtime import ApprovalDiscoveryWorker


@contextmanager
def owned_approval(worker):
    worker.start()
    try:yield
    finally:worker.stop()


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
    cases={name:{'artifact':'mixed-'+name+'-'+suffix+'.txt','marker':'MIXED_'+name+'_'+suffix,'tasks':{}} for name in ('approve','reject')}
    if any((workspace/c['artifact']).exists() for c in cases.values()):raise RuntimeError('artifact_exists')
    instruction=('Read artifact and marker from taskInput. Delegate to Codex to create only that dedicated relative file in its assigned workspace, writing marker plus exactly one newline, then shell-check its content. '
        'Do not modify other files, access network, alter memory or permissions, or guess absolute cwd. Do not decide any human approval; that is a separate authenticated operator action. Prior memory is background only.')
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
        for name,cls in [('tasks.sqlite',TaskStore),('roles.sqlite',RoleDispatchLedger),('sim.sqlite',SimJournal),('runs.sqlite',WorkflowRunLedger),('pauses.sqlite',PauseBindings),('approvals.sqlite',ApprovalLedger)]:
            cls.initialize(DATA/name);os.chmod(DATA/name,0o600)
        store=TaskStore(DATA/'tasks.sqlite');ledger=RoleDispatchLedger(DATA/'roles.sqlite');journal=SimJournal(DATA/'sim.sqlite')
        api=SimAuthoringClient(sim,auth['workspaceId'])
        projects={PROJECT:{'workspace':str(workspace)}};bindings={BINDING:{'projectId':PROJECT,'roleAgentId':ROLE}}
        author_token=secrets.token_urlsafe(40)
        access=AuthoringAccess({author_token:TrustedPrincipal('automatic-loop-probe-editor',frozenset({PROJECT}),frozenset({'edit_workflows','publish_workflows'}),time.time()+7200)})
        authoring=AuthoringAPI(access,journal,{PROJECT:api},projects=projects,role_bindings=bindings)
        flowconfig={'schemaVersion':1,'runLedgerPath':str(DATA/'runs.sqlite'),'pauseBindingsPath':str(DATA/'pauses.sqlite')};write(DATA/'task-flow.json',flowconfig)
        flow=load_task_flow(DATA/'task-flow.json',tasks=store,projects=projects,role_bindings=bindings,client=sim,journal=journal)
        installation='automatic-mixed-approval-'+suffix
        wf=WorkflowGateway(sim,journal,{},installation_id=installation);wf.task_flow=flow;wf.publication_verifier=flow.discovery.publication_verifier
        service_token=(ROOT/'var/qwenpaw-secrets/execution-service-token').read_text().strip()
        gateway=RoleDispatchGateway(base,token,service_token,role_agent_id=ROLE,deployment_id='agent-line-g0-installation',ledger=ledger,memory_query=prior_state['query'],client=host,timeout_seconds=900)
        dispatcher=Dispatcher(store,{(PROJECT,BINDING):gateway})
        credential=secrets.token_urlsafe(40);write(DATA/'service-token.json',{'token':credential})
        human_token=secrets.token_urlsafe(40);write(DATA/'human-token.json',{'token':human_token})
        approval_access=ApprovalAccess({human_token:TrustedPrincipal('mixed-probe-independent-operator',frozenset({PROJECT}),frozenset({'approve'}),time.time()+7200)})
        approval_ledger=ApprovalLedger(DATA/'approvals.sqlite')
        approval_discovery=ApprovalDiscovery(flow.runs,approval_ledger,sim,publication_verifier=flow.discovery.publication_verifier)
        approvals=ApprovalAPI(approval_access,ApprovalCoordinator(approval_ledger,sim,journal,authorize=approval_access.authorize),discovery=approval_discovery,publication_verifier=flow.discovery.publication_verifier)
        wf.approval_discovery=approval_discovery
        approval_worker=ApprovalDiscoveryWorker(approval_discovery)

        app=create_app(store,ServiceConfig(tokens={credential:{PROJECT}},projects=projects,bindings=bindings,controlled_execution='confirmed'),workflows=wf,dispatcher=dispatcher,task_flow=flow,authoring=authoring,approvals=approvals)
        if not authoring.approval_enabled:raise RuntimeError('mixed_runtime_not_enabled')
        with TestClient(app) as http, owned_approval(approval_worker):
            def author(operation,route,payload):
                def request():
                    response=http.post('/extension/v1/authoring/projects/'+PROJECT+'/workflows'+route,headers={'Authorization':'Bearer '+author_token},json=payload)
                    if response.status_code!=202:raise RuntimeError('authoring_http_'+str(response.status_code))
                    data=response.json()
                    if data.get('status')!='accepted':raise RuntimeError('authoring_not_accepted')
                    return data
                return once(operation,request)['receipt']
            created=author('create','',{'operationId':'create-'+suffix,'name':'agent-line-automatic-mixed-approval','description':'Isolated real publication-to-execution proof'})
            wid=created['workflowId'];state['workflowId']=wid;persist()
            agent_config={'roleBindingId':BINDING,'instruction':instruction,'inputFrom':{'nodeId':'start','field':'input'},'memoryPolicyId':'native-role','outputSchemaId':'task-result','requireMemoryCommit':False,'allowFailureBranch':False}
            def node(i,t,name,c,x):return {'id':i,'type':t,'name':name,'position':{'x':x,'y':0},'config':c}
            doc={'schemaVersion':1,'nodes':[node('start','start','开始',{},0),node('agent','agent','实现',agent_config,200),node('human','human','人工审批',{'requiredBoolean':'approved'},400),node('condition','condition','审批分支',{'rules':[{'id':'approved','operator':'eq','source':'human','field':'result.approval.approved','value':True}]},600),node('approvedEnd','end','批准结束',{'output':{'decision':'approved'}},800),node('rejectedEnd','end','拒绝结束',{'output':{'decision':'rejected'}},800)],'edges':[{'id':'a','source':'start','target':'agent'},{'id':'b','source':'agent','target':'human'},{'id':'c','source':'human','target':'condition'},{'id':'d','source':'condition','target':'approvedEnd','branch':'approved'},{'id':'e','source':'condition','target':'rejectedEnd','branch':'else'}],'variables':{}}
            state['document']=doc;persist()
            saved=author('save','/'+wid+'/save',{'operationId':'save-'+suffix,'expectedStateSha256':state_hash(api.get_state(wid)),'document':doc})
            if saved.get('outcome')!='saved':raise RuntimeError('save_not_verified')
            published=author('publish','/'+wid+'/publish',{'operationId':'publish-'+suffix,'expectedStateSha256':state_hash(api.get_state(wid))})
            if published.get('outcome')!='published':raise RuntimeError('publish_not_verified')
            binding=published['bindingId'];state['bindingId']=binding;state['publication']=published;persist()
            catalog=http.get('/extension/v1/workflows',headers={'Authorization':'Bearer '+credential});catalog.raise_for_status()
            if binding not in [item['bindingId'] for item in catalog.json()['workflows']]:raise RuntimeError('published_binding_not_discovered')
            if wf.bindings:raise RuntimeError('static_binding_forbidden')
            for name,case in state['cases'].items():
                quiet_host(process['pid'])
                body={'idempotencyKey':name+'-'+suffix,'input':{'input':{k:case[k] for k in ('artifact','marker')}}}
                run_id=str(uuid.uuid5(uuid.NAMESPACE_URL,canonical([installation,PROJECT,wid,body['idempotencyKey'],binding])))
                case.update(runId=run_id,startPayload=body,startIntent=True);persist()
                started=http.post('/extension/v1/workflows/'+binding+'/runs',headers={'Authorization':'Bearer '+credential},json=body)
                case.update(startHTTP=started.status_code,startReceipt=started.json());persist()
                if started.status_code!=202 or started.json().get('status')!='accepted':raise RuntimeError('start_not_accepted')
                case['frozenPublication']=journal.get_publication(wid,run_id);persist()
                deadline=time.monotonic()+1200;last_print=0
                while time.monotonic()<deadline:
                    tasks=[t for t in store.list_tasks({PROJECT})['tasks'] if t['payload']['workflow']['runId']==run_id]
                    if len(tasks)>1:raise RuntimeError('unexpected_multiple_tasks')
                    if tasks:
                        task=tasks[0];tid=task['id'];entry=case['tasks'].setdefault(tid,{});entry['task']=task
                        if task['executionStatus']=='succeeded' and task['resourcesReleased'] and task['verificationStatus']=='pending':
                            if entry.get('verificationIntent'):raise RuntimeError('verification_requires_original_observation')
                            entry['verificationIntent']=True;persist()
                            expected=(case['marker']+'\n').encode();evidence_dir=DATA/'artifacts'/tid;evidence_dir.mkdir(mode=0o700,parents=True,exist_ok=True)
                            receipt=verify_artifacts(store,tid,workspace=workspace,policies=[ArtifactPolicy(case['artifact'],hashlib.sha256(expected).hexdigest(),len(expected))],evidence_dir=evidence_dir)
                            store.record_verification(tid,receipt);entry['verification']=receipt;persist()
                            if receipt['status']!='passed':raise RuntimeError('artifact_verification_failed')
                        if task['executionStatus'] in ('failed','canceled'):raise RuntimeError('native_execution_not_succeeded')
                    listing=http.get('/extension/v1/approvals',headers={'Authorization':'Bearer '+human_token});listing.raise_for_status()
                    found=[a for a in listing.json()['approvals'] if a['workflowId']==wid and a['runId']==run_id]
                    if len(found)>1:raise RuntimeError('multiple_human_approvals')
                    if found:
                        approval=found[0];case['approval']=approval
                        if approval['blockId']!=native_id(wid,'human') or approval['contextId']!=native_id(wid,'human'):raise RuntimeError('approval_bound_to_wrong_pause')
                        if not tasks or store.get(tasks[0]['id'])['verificationStatus']!='passed':raise RuntimeError('human_reached_before_verified_task')
                        detail=http.get('/extension/v1/approvals/'+approval['approvalId'],headers={'Authorization':'Bearer '+human_token});detail.raise_for_status()
                        if detail.json().get('boundPauseObserved') is not True:raise RuntimeError('human_pause_not_observed')
                        case['humanPauseDetail']=detail.json();persist();break
                    if time.monotonic()-last_print>30:
                        print(json.dumps({'case':name,'phase':'waiting_for_human','taskId':tasks[0]['id'] if tasks else None,'executionStatus':tasks[0]['executionStatus'] if tasks else None}),flush=True);last_print=time.monotonic()
                    time.sleep(2)
                else:raise RuntimeError('human_wait_deadline_original_identities_preserved')
                aid=approval['approvalId'];decision={'decisionId':name+'-'+suffix,'decision':name};case['decisionPayload']=decision;persist()
                before=sum(c['method']!='GET' for c in calls)
                unauthorized=http.post('/extension/v1/approvals/'+aid+'/decision',headers={'Authorization':'Bearer '+credential},json=decision)
                if unauthorized.status_code!=401 or sum(c['method']!='GET' for c in calls)!=before:raise RuntimeError('service_credential_authorized_approval')
                case['serviceCredentialApprovalHTTP']=401;case['decisionIntent']=True;persist()
                decided=http.post('/extension/v1/approvals/'+aid+'/decision',headers={'Authorization':'Bearer '+human_token},json=decision)
                case['decisionHTTP']=decided.status_code;case['decisionReceipt']=decided.json();persist()
                if decided.status_code!=200 or decided.json().get('deliveryStatus')!='accepted':raise RuntimeError('human_decision_not_accepted')
                case['nativeApprovalReceipt']=journal.get(wid,run_id,approval['contextId']);persist()
                final_id=case['nativeApprovalReceipt']['receipt']['resumedRunId'];case['finalRunId']=final_id
                for _ in range(120):
                    observed=sim.observe(wid,final_id);case['finalObservation']=observed;persist()
                    if observed['status'] in ('completed','failed','cancelled'):break
                    time.sleep(1)
                if observed['status']!='completed':raise RuntimeError('decision_branch_not_completed')
                end='approvedEnd' if name=='approve' else 'rejectedEnd';option='approved' if name=='approve' else 'else';expected_output={'decision':'approved' if name=='approve' else 'rejected'}
                selectors=','.join([native_id(wid,'condition'),native_id(wid,end)+'.data',native_id(wid,'human')+'.resumeInput'])
                selected=sim._client.get(sim.base_url+f'/api/v2/workflows/{wid}/runs/'+final_id,params={'selectedOutputs':selectors},headers={'x-api-key':sim._key},follow_redirects=False);selected.raise_for_status()
                native=selected.json()['data'];case['nativeDecisionBranch']=native;blocks=native['blockOutputs'];choice=blocks[native_id(wid,'condition')]
                if native.get('workflowId')!=wid or native.get('runId')!=final_id:raise RuntimeError('decision_native_identity_mismatch')
                if choice.get('selectedPath',{}).get('blockId')!=native_id(wid,end) or choice.get('selectedOption')!=option:raise RuntimeError('wrong_human_branch')
                if blocks.get(native_id(wid,end)+'.data')!=expected_output or observed['response']['data'].get('output',{}).get('data')!=expected_output:raise RuntimeError('decision_end_output_mismatch')
                if blocks[native_id(wid,'human')+'.resumeInput']!=journal.resume_input(wid,run_id,approval['contextId']):raise RuntimeError('human_decision_payload_mismatch')
                task=store.get(tid);entry['task']=task
                parent=ledger.get(tid);entry['parent']=parent
                for _ in range(90):
                    r=host.get(base+'/api/console/chat/task/'+parent['parentTaskId'],headers={'Authorization':'Bearer '+token,'X-Agent-Id':ROLE});r.raise_for_status();entry['parentObservation']=r.json()
                    if entry['parentObservation'].get('status')=='finished':break
                    time.sleep(1)
                if entry['parentObservation'].get('status')!='finished':raise RuntimeError('parent_still_running')
                if not task['resourcesReleased'] or not any(e.get('kind')=='resource_exit' for e in task['evidence']):raise RuntimeError('resource_exit_missing')
                before=sum(c['method']!='GET' for c in calls)
                case['duplicateTriggerIntent']=True;case['duplicateDecisionIntent']=True;persist()
                again=http.post('/extension/v1/workflows/'+binding+'/runs',headers={'Authorization':'Bearer '+credential},json=body)
                replay=http.post('/extension/v1/approvals/'+aid+'/decision',headers={'Authorization':'Bearer '+human_token},json=decision)
                if again.status_code!=202 or again.json()!=started.json() or replay.status_code!=200 or replay.json().get('deliveryStatus')!='accepted':raise RuntimeError('duplicate_operation_not_replayed')
                if sum(c['method']!='GET' for c in calls)!=before:raise RuntimeError('duplicate_reposted_native')
                case['duplicateNativeWrites']=0;case['actualEnd']=end;case['result']='passed_live';persist()
                quiet_host(process['pid'])
                with store.connect() as db:leases=db.execute('SELECT count(*) FROM workspace_leases').fetchone()[0]
                if leases:raise RuntimeError('leases_remain')
                print(json.dumps({'case':name,'status':'passed_live','taskId':tid,'actualEnd':end,'activeLeaseCount':0}),flush=True)
        state['mainGuardAfter']=TaskStore(main_database).maintenance_status()
        if state['mainGuardAfter']!=state['mainGuardBefore']:raise RuntimeError('main_guard_changed')
        report={'status':'passed_live','workflowId':wid,'bindingId':binding,'cases':state['cases'],'nativeCalls':calls,'activeLeaseCount':0,'ownedActiveContainerCount':0,'mainGuardUnchanged':True,'approvalAuthority':'independent-probe-operator-not-user-personally','limitations':['Only two authorized probe decisions; not model-generated approval','Native ReMe may update normally; no manual memory writeback or configuration change','Single existing role and executor; no multi-project isolation claim']}
        write(DATA/'report.json',report);state['result']='passed_live';persist();return 0
    except Exception as exc:
        state['failure']={'type':type(exc).__name__,'code':getattr(exc,'code',str(exc) if type(exc) is RuntimeError else 'probe_failed')};persist();write(DATA/'failure.json',{'status':'incomplete',**state['failure'],'nativeCalls':calls});print(json.dumps({'status':'incomplete',**state['failure']}),flush=True);return 1
    finally:
        if gateway is not None:gateway.close()
        if host is not None:host.close()
        sim.close()


if __name__=='__main__':raise SystemExit(main())
