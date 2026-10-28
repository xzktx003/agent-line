#!/usr/bin/env python3
"""Real mixed Sim/HTTP approval transport; machine results are synthetic."""
import json
from pathlib import Path
import secrets
import subprocess
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from fastapi.testclient import TestClient
from agent_line.adapters.sim import SimClient
from agent_line.adapters.sim_wait import SimWaitClient
from agent_line.sim_authoring import SimAuthoringClient,native_id,compile_document,decode_document
from agent_line.authoring_api import AuthoringAPI,AuthoringAccess
from agent_line.approval_api import ApprovalAPI,ApprovalAccess
from agent_line.approvals import ApprovalLedger,ApprovalCoordinator,TrustedPrincipal
from agent_line.approval_discovery import WorkflowRunLedger,ApprovalDiscovery
from agent_line.publication import PublicationVerifier,PublicationUnverified
from agent_line.sim_journal import SimJournal
from agent_line.store import TaskStore
from agent_line.pause_bindings import PauseBindings
from agent_line.task_discovery import ExternalTaskDiscovery
from agent_line.task_flow_runtime import TaskFlowRuntime
from agent_line.workflows import WorkflowGateway
from agent_line.service import create_app,ServiceConfig
from probes.sim.authoring_probe import write

DATA=ROOT/'var/sim-mixed-approval'
PROJECT='synthetic-mixed-approval'


def main():
    DATA.mkdir(mode=0o700,exist_ok=True);statepath=DATA/'state.json';credentialpath=DATA/'credentials.json'
    authpath=ROOT/'var/sim/probe-auth.json'
    if authpath.stat().st_mode & 0o077:raise RuntimeError('credential_permissions')
    auth=json.loads(authpath.read_text());sim=SimClient(auth['baseUrl'],auth['apiKey']);calls=[]
    sim._client.event_hooks['request'].append(lambda r:calls.append({'method':r.method,'path':r.url.path}))
    existed=statepath.exists();state=json.loads(statepath.read_text()) if existed else {'operations':{},'runs':{}}
    def persist():write(statepath,state)
    def once(key,payload,fn):
        prior=state['operations'].get(key)
        if prior:
            if prior['payload']!=payload or prior['status']!='received':raise RuntimeError('prior_intent_no_resubmit')
            return prior['result']
        state['operations'][key]={'status':'intent','payload':payload};persist()
        result=fn();state['operations'][key].update(status='received',result=result);persist();return result
    def post(http,key,path,body,token,expected):
        def submit():
            response=http.post(path,json=body,headers={'Authorization':'Bearer '+token})
            data={'statusCode':response.status_code,'body':response.json()}
            if response.status_code!=expected:
                state.setdefault('httpRejections',{})[key]=data;persist();raise RuntimeError('http_status_'+str(response.status_code))
            return data
        return once(key,{'path':path,'body':body},submit)['body']
    def get(http,path,token):
        r=http.get(path,headers={'Authorization':'Bearer '+token})
        if r.status_code!=200:raise RuntimeError('http_get_'+str(r.status_code))
        return r.json()
    def poll(wid,rid,context=None):
        for _ in range(100):
            observed=sim.observe(wid,rid);data=observed['response']['data']
            if context and (data.get('paused') or {}).get('contextId')==context:return data
            if not context and data['status'] in ('completed','failed','cancelled'):return data
            if context and data['status'] in ('failed','completed'):raise RuntimeError('unexpected_terminal')
            time.sleep(.2)
        raise RuntimeError('same_identity_still_pending')
    try:
        if existed and '--continue-known' not in sys.argv:
            result={name:{rid:sim.observe(state['workflowId'],rid) for rid in dict.fromkeys([run['runId']]+run.get('children',[]))} for name,run in state['runs'].items()}
            write(DATA/'observation.json',{'mode':'readonly','runs':result,'nativeCalls':calls,'nativeWrites':0,'priorResult':state.get('result')})
            print(json.dumps({'mode':'readonly','nativeWrites':0,'priorResult':state.get('result')}));return 0
        check=subprocess.run(['docker','ps','--filter','label=com.docker.compose.project=agent-line','--format','{{.Names}} {{.Status}}'],capture_output=True,text=True,check=True).stdout
        if 'agent-line-simstudio-1' not in check or 'healthy' not in check:raise RuntimeError('sim_health_unconfirmed')
        state['processCheck']=check;persist()
        if not credentialpath.exists():
            if any(DATA.glob('*.sqlite')):raise RuntimeError('partial_initialization')
            for name,cls in [('tasks',TaskStore),('journal',SimJournal),('runs',WorkflowRunLedger),('pauses',PauseBindings),('approvals',ApprovalLedger)]:cls.initialize(DATA/(name+'.sqlite'))
            write(credentialpath,{key:secrets.token_urlsafe(40) for key in ('service','editor','human','expired')})
        credentials=json.loads(credentialpath.read_text());tasks=TaskStore(DATA/'tasks.sqlite');journal=SimJournal(DATA/'journal.sqlite');runs=WorkflowRunLedger(DATA/'runs.sqlite');pauses=PauseBindings(DATA/'pauses.sqlite');ledger=ApprovalLedger(DATA/'approvals.sqlite')
        principal=lambda subject,cap,expiry:TrustedPrincipal(subject,frozenset({PROJECT}),frozenset(cap),expiry)
        access=ApprovalAccess({credentials['human']:principal('synthetic-reviewer',['approve'],time.time()+86400),credentials['expired']:principal('expired-reviewer',['approve'],time.time()-10)})
        edits=AuthoringAccess({credentials['editor']:principal('synthetic-editor',['edit_workflows','publish_workflows'],time.time()+86400)})
        projects={PROJECT:{'workspace':str(DATA)}};roles={'synthetic-role':{'projectId':PROJECT,'roleAgentId':'unbound-synthetic-no-executor'}}
        verifier=PublicationVerifier(sim);discovery=ApprovalDiscovery(runs,ledger,sim,publication_verifier=verifier)
        api=ApprovalAPI(access,ApprovalCoordinator(ledger,sim,journal,authorize=access.authorize),discovery=discovery,publication_verifier=verifier)
        author=AuthoringAPI(edits,journal,{PROJECT:SimAuthoringClient(sim,auth['workspaceId'])},projects=projects,role_bindings=roles)
        machine=ExternalTaskDiscovery(runs,tasks,pauses,sim,SimWaitClient(sim),publication_verifier=verifier,projects=projects,role_bindings=roles,journal=journal)
        flow=TaskFlowRuntime(runs,pauses,machine,()) # no execution/discovery worker; synthetic results only
        gateway=WorkflowGateway(sim,journal,{},installation_id='synthetic-mixed-probe');gateway.task_flow=flow;gateway.approval_discovery=discovery;gateway.publication_verifier=verifier
        app=create_app(tasks,ServiceConfig({credentials['service']:{PROJECT}},projects,roles),workflows=gateway,approvals=api,authoring=author,task_flow=flow)
        if not author.approval_enabled:raise RuntimeError('assembled_approval_capability_unavailable')
        def node(i,t,c):return {'id':i,'type':t,'name':i,'position':{'x':0,'y':0},'config':c}
        cfg={'roleBindingId':'synthetic-role','instruction':'只验证synthetic结果，不调用模型。','inputFrom':{'nodeId':'start','field':'input'},'memoryPolicyId':'native-role','outputSchemaId':'task-result','requireMemoryCommit':False,'allowFailureBranch':True}
        doc={'schemaVersion':1,'nodes':[node('start','start',{}),node('loop','loop',{'body':'agent','maxIterations':3}),node('agent','agent',cfg),node('human','human',{'requiredBoolean':'approved'}),node('condition','condition',{'rules':[{'id':'approve','source':'human','field':'result.approval.approved','operator':'eq','value':True}]}),node('yes','end',{'output':{'syntheticProbeDecision':'approved'}}),node('no','end',{'output':{'syntheticProbeDecision':'rejected'}})],'edges':[{'id':'a','source':'start','target':'loop'},{'id':'b','source':'loop','target':'agent','branch':'body'},{'id':'c','source':'loop','target':'human','branch':'done'},{'id':'d','source':'human','target':'condition'},{'id':'e','source':'condition','target':'yes','branch':'approve'},{'id':'f','source':'condition','target':'no','branch':'else'}],'variables':{}}
        base='/extension/v1/authoring/projects/'+PROJECT
        with TestClient(app) as http:
            created=post(http,'create',base+'/workflows',{'operationId':'mixed-create','name':'agent-line-synthetic-mixed-approval','description':'Synthetic machine results; real human approval HTTP'},credentials['editor'],202)
            wid=created['receipt']['workflowId'];state['workflowId']=wid;state['document']=doc;persist();path=base+'/workflows/'+wid
            detail=get(http,path,credentials['editor'])
            savebody=state['operations'].get('save',{}).get('payload',{}).get('body') or {'operationId':'mixed-save','document':doc,'expectedStateSha256':detail['stateSha256']}
            saved=post(http,'save',path+'/save',savebody,credentials['editor'],202)
            if saved['receipt']['outcome']!='saved':raise RuntimeError('save_not_verified')
            detail=get(http,path,credentials['editor'])
            pubbody=state['operations'].get('publish',{}).get('payload',{}).get('body') or {'operationId':'mixed-publish','expectedStateSha256':detail['stateSha256']}
            published=post(http,'publish',path+'/publish',pubbody,credentials['editor'],202)['receipt']
            if published['outcome']!='published':raise RuntimeError('publication_not_verified')
            state['publication']=published;persist()
            version=author.clients[PROJECT].get_version(wid,published['revision'])
            if compile_document(decode_document(version['state']),wid)!=compile_document(doc,wid):raise RuntimeError('published_graph_mismatch')
            binding=published['bindingId'];catalog=get(http,'/extension/v1/workflows',credentials['service'])
            if binding not in [x['bindingId'] for x in catalog['workflows']]:raise RuntimeError('catalog_missing')
            for choice,rounds in [('approve',2),('reject',1)]:
                started=post(http,choice+'-start','/extension/v1/workflows/'+binding+'/runs',{'idempotencyKey':'synthetic-'+choice,'input':{'input':{}}},credentials['service'],202)
                rid=started['runId'];run=state['runs'].setdefault(choice,{'runId':rid,'children':[],'pauses':[]});persist()
                frozen=runs.get(wid,rid)
                for iteration in range(rounds):
                    context=native_id(wid,'agent')+'_loop'+str(iteration);key=choice+'-machine-'+str(iteration)
                    if key in state['operations']:
                        if state['operations'][key]['status']!='received':raise RuntimeError('unknown_machine_receipt')
                        continue
                    paused=poll(wid,rid,context);verifier.verify_run(frozen)
                    resolved=SimWaitClient(sim).read_input(wid,rid,native_id(wid,'agent'),context_id=context)
                    if resolved['input']!={'input':{},'iteration':iteration}:raise RuntimeError('machine_input_mismatch')
                    discovery.tick()
                    if any(row['binding'].run_id==rid for row in ledger.list_approvals({PROJECT})['approvals']):raise RuntimeError('machine_pause_became_approval')
                    synthetic={'executionStatus':'succeeded','verificationStatus':'failed' if iteration+1<rounds else 'passed','output':{'synthetic':True,'round':iteration}}
                    receipt=once(key,{'runId':rid,'contextId':context,'input':synthetic},lambda:journal.resume(sim,wid,rid,context,synthetic))
                    if receipt['status']!='accepted':raise RuntimeError('machine_resume_unconfirmed')
                    run['children'].append(receipt['receipt']['resumedRunId']);run['pauses'].append({'contextId':context,'input':resolved['input'],'nativePaused':paused['paused'],'publicationVerified':True});persist()
                    print(json.dumps({'case':choice,'syntheticRound':iteration,'accepted':True}),flush=True)
                if choice+'-decision' not in state['operations']:
                    poll(wid,rid,native_id(wid,'human'));verifier.verify_run(frozen);discovery.tick()
                    rows=[row for row in ledger.list_approvals({PROJECT})['approvals'] if row['binding'].run_id==rid]
                    if len(rows)!=1:raise RuntimeError('human_registration_missing')
                    b=rows[0]['binding'];run['approvalId']=b.approval_id;run['approvalExpiresAt']=b.expires_at;persist()
                    decisionpath='/extension/v1/approvals/'+b.approval_id+'/decision';body={'decisionId':'synthetic-'+choice,'decision':choice}
                    before=len(calls)
                    post(http,choice+'-service-denied',decisionpath,body,credentials['service'],401)
                    post(http,choice+'-expired-denied',decisionpath,body,credentials['expired'],403)
                    if any(x['method']!='GET' for x in calls[before:]):raise RuntimeError('denied_decision_native_write')
                    post(http,choice+'-decision',decisionpath,body,credentials['human'],200)
                b=ledger.get(run['approvalId'])['binding'];entry=journal.get(wid,rid,b.context_id)
                if entry['status']!='accepted':raise RuntimeError('approval_delivery_unknown')
                child=entry['receipt']['resumedRunId']
                if child not in run['children']:run['children'].append(child);persist()
                final=poll(wid,child);run['final']=final;run['parentFinal']=sim.observe(wid,rid)
                try:run['finalChildPublicationVerified']=verifier.verify_run({**frozen,'runId':child})
                except PublicationUnverified:run['finalChildPublicationVerified']=False
                persist()
                expected='approved' if choice=='approve' else 'rejected';end=native_id(wid,'yes' if choice=='approve' else 'no');condition=native_id(wid,'condition')
                response=sim._client.get(sim.base_url+'/api/v2/workflows/'+wid+'/runs/'+child,params={'selectedOutputs':condition+','+end+'.data'},headers={'x-api-key':sim._key},follow_redirects=False)
                if response.status_code!=200:raise RuntimeError('selected_output_unavailable')
                outputs=response.json()['data'];run['selectedOutputs']=outputs;persist()
                if final['status']!='completed' or final['output']['data']!={'syntheticProbeDecision':expected}:raise RuntimeError('decision_result_mismatch')
                if outputs['blockOutputs'][condition]['selectedPath']['blockId']!=end or outputs['blockOutputs'][end+'.data']!=final['output']['data']:raise RuntimeError('native_branch_mismatch')
                before=len(calls);record_before=ledger.get(b.approval_id)
                post(http,choice+'-decision-replay','/extension/v1/approvals/'+b.approval_id+'/decision',{'decisionId':'synthetic-'+choice,'decision':choice},credentials['human'],200)
                if any(x['method']!='GET' for x in calls[before:]) or ledger.get(b.approval_id)!=record_before:raise RuntimeError('decision_replay_changed')
                run['replayNativeWrites']=0;run['serviceDenied']=True;run['expiredCredentialDenied']=True;persist()
                print(json.dumps({'case':choice,'status':'completed','decisionReplayWrites':0}),flush=True)
        with tasks.connect() as db:
            if db.execute('SELECT count(*) FROM tasks').fetchone()[0]!=0:raise RuntimeError('unexpected_model_task')
        state['result']='passed_live';persist()
        report={'status':'passed_live','workflowId':wid,'revision':published['revision'],'nativeCalls':calls,'nativeWrites':sum(x['method']!='GET' for x in calls),'syntheticMachineResults':True,'taskCount':0,'runs':state['runs']}
        write(DATA/'report.json',report);print(json.dumps({'status':'passed_live','nativeWrites':report['nativeWrites'],'taskCount':0}));return 0
    except Exception as exc:
        report={'status':'incomplete','code':str(exc) if isinstance(exc,RuntimeError) else type(exc).__name__,'nativeCalls':calls}
        write(DATA/('failure-'+uuid.uuid4().hex+'.json'),report);print(json.dumps({'status':'incomplete','code':report['code']}));return 1
    finally:sim.close()


if __name__=='__main__':raise SystemExit(main())
