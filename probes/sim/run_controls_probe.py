#!/usr/bin/env python3
"""Real run admission controls and native cancellation; no models or task workers."""
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

DATA=ROOT/'var/sim-run-controls'
PROJECT='native-run-controls'


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
        if existed:
            result={name:{rid:sim.observe(state['workflowId'],rid) for rid in dict.fromkeys([run['runId']]+run.get('children',[]))} for name,run in state['runs'].items()}
            if any(c['method']!='GET' for c in calls):raise RuntimeError('readonly_write_detected')
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
        def build_app():
            gateway=WorkflowGateway(sim,journal,{},installation_id='run-controls-probe')
            gateway.approval_discovery=discovery;gateway.publication_verifier=verifier
            return create_app(tasks,ServiceConfig({credentials['service']:{PROJECT}},projects,{}),workflows=gateway,approvals=api,authoring=author)
        app=build_app()
        if not author.approval_enabled:raise RuntimeError('assembled_approval_capability_unavailable')
        def node(i,t,c):return {'id':i,'type':t,'name':i,'position':{'x':0,'y':0},'config':c}
        doc={'schemaVersion':1,'nodes':[node('start','start',{}),node('human','human',{'requiredBoolean':'approved'}),node('condition','condition',{'rules':[{'id':'approve','source':'human','field':'result.approval.approved','operator':'eq','value':True}]}),node('yes','end',{'output':{'decision':'approved'}}),node('no','end',{'output':{'decision':'rejected'}})],'edges':[{'id':'a','source':'start','target':'human'},{'id':'b','source':'human','target':'condition'},{'id':'c','source':'condition','target':'yes','branch':'approve'},{'id':'d','source':'condition','target':'no','branch':'else'}],'variables':{}}
        base='/extension/v1/authoring/projects/'+PROJECT
        with TestClient(app) as http:
            created=post(http,'create',base+'/workflows',{'operationId':'controls-create','name':'agent-line-native-run-controls','description':'No model; real run controls and human approval'},credentials['editor'],202)
            wid=created['receipt']['workflowId'];state['workflowId']=wid;state['document']=doc;persist();path=base+'/workflows/'+wid
            detail=get(http,path,credentials['editor'])
            savebody=state['operations'].get('save',{}).get('payload',{}).get('body') or {'operationId':'controls-save','document':doc,'expectedStateSha256':detail['stateSha256']}
            saved=post(http,'save',path+'/save',savebody,credentials['editor'],202)
            if saved['receipt']['outcome']!='saved':raise RuntimeError('save_not_verified')
            detail=get(http,path,credentials['editor'])
            pubbody=state['operations'].get('publish',{}).get('payload',{}).get('body') or {'operationId':'controls-publish','expectedStateSha256':detail['stateSha256']}
            published=post(http,'publish',path+'/publish',pubbody,credentials['editor'],202)['receipt']
            if published['outcome']!='published':raise RuntimeError('publication_not_verified')
            state['publication']=published;persist()
            version=author.clients[PROJECT].get_version(wid,published['revision'])
            if compile_document(decode_document(version['state']),wid)!=compile_document(doc,wid):raise RuntimeError('published_graph_mismatch')
            binding=published['bindingId'];catalog=get(http,'/extension/v1/workflows',credentials['service'])
            if binding not in [x['bindingId'] for x in catalog['workflows']]:raise RuntimeError('catalog_missing')
            for choice in ('continue','stop'):
                started=post(http,choice+'-start','/extension/v1/workflows/'+binding+'/runs',{'idempotencyKey':'controls-'+choice,'input':{'input':{}}},credentials['service'],202)
                rid=started['runId'];run=state['runs'].setdefault(choice,{'runId':rid,'children':[]});persist()
                poll(wid,rid,native_id(wid,'human'));verifier.verify_run(runs.get(wid,rid));discovery.tick()
                rows=[row for row in ledger.list_approvals({PROJECT})['approvals'] if row['binding'].run_id==rid]
                if len(rows)!=1:raise RuntimeError('human_registration_missing')
                aid=rows[0]['binding'].approval_id;run['approvalId']=aid;persist()
                controlpath='/extension/v1/workflows/'+binding+'/runs/'+rid+'/control'
                runpath='/extension/v1/workflows/'+binding+'/runs/'+rid
                run['pause']=post(http,choice+'-pause',controlpath,{'operationId':choice+'-pause','action':'pause','expectedVersion':0,'activeExecution':'continue'},credentials['service'],200);persist()
                before=len(calls)
                post(http,choice+'-denied','/extension/v1/approvals/'+aid+'/decision',{'decisionId':choice+'-approve','decision':'approve'},credentials['human'],409)
                if ledger.get(aid)['decision'] is not None or any(x['method']!='GET' for x in calls[before:]):raise RuntimeError('paused_decision_side_effect')
                run['pausedDecisionDenied']=True;persist()
                print(json.dumps({'case':choice,'paused':True,'approvalDenied':True}),flush=True)
                # New app object over the same durable stores. No background workers exist.
                with TestClient(build_app()) as reopened:
                    run['reopenedPause']=get(reopened,runpath,credentials['service'])
                    if run['reopenedPause']['control']['state']!='paused':raise RuntimeError('pause_not_persistent')
                    actionbody={'operationId':choice+'-action','action':choice,'expectedVersion':1}
                    run['action']=post(reopened,choice+'-action',controlpath,actionbody,credentials['service'],200);persist()
                    if choice=='continue':
                        post(reopened,'approve','/extension/v1/approvals/'+aid+'/decision',{'decisionId':'continue-approve','decision':'approve'},credentials['human'],200)
                        receipt=journal.get(wid,rid,native_id(wid,'human'))
                        if receipt['status']!='accepted':raise RuntimeError('resume_unknown')
                        child=receipt['receipt']['resumedRunId'];run['children'].append(child);persist()
                        run['final']=poll(wid,child)
                        if run['final']['status']!='completed' or run['final']['output']['data']!={'decision':'approved'}:raise RuntimeError('approval_not_completed')
                    else:
                        run['cancelReceipt']=journal.get(wid,rid,'run-cancel')
                        if run['cancelReceipt']['status']!='accepted' or run['cancelReceipt']['receipt']['verifiedStopped'] is not False:raise RuntimeError('cancel_receipt_invalid')
                        run['final']=poll(wid,rid)
                        if run['final']['status']!='cancelled':raise RuntimeError('native_cancel_not_confirmed')
                        before=len(calls)
                        with TestClient(build_app()) as again:
                            run['reopenedStop']=get(again,runpath,credentials['service'])
                            if run['reopenedStop']['control']['state']!='stop_requested':raise RuntimeError('stop_not_persistent')
                            post(again,'stop-replay',controlpath,actionbody,credentials['service'],200)
                        if any(x['method']!='GET' for x in calls[before:]):raise RuntimeError('stop_replay_native_write')
                        run['stopReplayNativeWrites']=0
                    persist()
                    print(json.dumps({'case':choice,'nativeStatus':run['final']['status']}),flush=True)
        with tasks.connect() as db:
            if db.execute('SELECT count(*) FROM tasks').fetchone()[0]!=0:raise RuntimeError('unexpected_model_task')
        state['result']='passed_live';persist()
        report={'status':'passed_live','workflowId':wid,'revision':published['revision'],'nativeCalls':calls,'nativeWrites':sum(x['method']!='GET' for x in calls),'modelsCalled':False,'taskCount':0,'runs':state['runs']}
        write(DATA/'report.json',report);print(json.dumps({'status':'passed_live','nativeWrites':report['nativeWrites'],'taskCount':0}));return 0
    except Exception as exc:
        report={'status':'incomplete','code':str(exc) if isinstance(exc,RuntimeError) else type(exc).__name__,'nativeCalls':calls}
        write(DATA/('failure-'+uuid.uuid4().hex+'.json'),report);print(json.dumps({'status':'incomplete','code':report['code']}));return 1
    finally:sim.close()


def recover_stop_window():
    """Explicit new case; any existing recovery state switches to read-only."""
    import threading
    from agent_line.approval_runtime import ApprovalDiscoveryWorker
    statepath=DATA/'state-stop-recovery.json'
    original=json.loads((DATA/'state.json').read_text())
    auth=json.loads((ROOT/'var/sim/probe-auth.json').read_text())
    credentials=json.loads((DATA/'credentials.json').read_text())
    sim=SimClient(auth['baseUrl'],auth['apiKey']);calls=[]
    sim._client.event_hooks['request'].append(lambda r:calls.append({'method':r.method,'path':r.url.path,'thread':threading.current_thread().name}))
    prior=statepath.exists();state=json.loads(statepath.read_text()) if prior else {'steps':{},'workflowId':original['workflowId']}
    def persist():write(statepath,state)
    def once(key,payload,fn):
        if key in state['steps']:raise RuntimeError('original_operation_must_not_be_reposted')
        state['steps'][key]={'status':'intent','payload':payload};persist()
        result=fn();state['steps'][key].update(status='received',result=result);persist();return result
    try:
        wid=state['workflowId']
        if prior:
            observed=sim.observe(wid,state['runId']) if state.get('runId') else None
            if any(c['method']!='GET' for c in calls):raise RuntimeError('readonly_write')
            write(DATA/'stop-recovery-observation.json',{'observed':observed,'nativeCalls':calls,'nativeWrites':0})
            print(json.dumps({'mode':'readonly-recovery','nativeWrites':0,'priorResult':state.get('result')}));return 0
        tasks=TaskStore(DATA/'tasks.sqlite');journal=SimJournal(DATA/'journal.sqlite')
        runs=WorkflowRunLedger(DATA/'runs.sqlite');ledger=ApprovalLedger(DATA/'approvals.sqlite')
        verifier=PublicationVerifier(sim)
        projects={PROJECT:{'workspace':str(DATA)}}
        principal=TrustedPrincipal('recovery-editor',frozenset({PROJECT}),frozenset({'edit_workflows','publish_workflows'}),time.time()+86400)
        def build():
            discovery=ApprovalDiscovery(runs,ledger,sim,publication_verifier=verifier)
            access=ApprovalAccess({credentials['human']:TrustedPrincipal('recovery-reviewer',frozenset({PROJECT}),frozenset({'approve'}),time.time()+86400)})
            api=ApprovalAPI(access,ApprovalCoordinator(ledger,sim,journal,authorize=access.authorize),discovery=discovery,publication_verifier=verifier)
            author=AuthoringAPI(AuthoringAccess({credentials['editor']:principal}),journal,{PROJECT:SimAuthoringClient(sim,auth['workspaceId'])},projects=projects,role_bindings={})
            gateway=WorkflowGateway(sim,journal,{},installation_id='run-controls-probe')
            gateway.approval_discovery=discovery;gateway.publication_verifier=verifier
            app=create_app(tasks,ServiceConfig({credentials['service']:{PROJECT}},projects,{}),workflows=gateway,approvals=api,authoring=author)
            return app,discovery
        app,discovery=build();binding=original['publication']['bindingId']
        with TestClient(app) as http:
            body={'idempotencyKey':'stop-window-recovery','input':{'input':{}}}
            path='/extension/v1/workflows/'+binding+'/runs'
            def start():
                response=http.post(path,json=body,headers={'Authorization':'Bearer '+credentials['service']})
                if response.status_code!=202:raise RuntimeError('start_http_'+str(response.status_code))
                return response.json()
            receipt=once('start',{'path':path,'body':body},start)
            rid=receipt['runId'];state['runId']=rid;persist()
            for _ in range(100):
                observed=sim.observe(wid,rid)
                data=observed.get('response',{}).get('data',{})
                if data.get('status')=='paused':break
                time.sleep(.2)
            else:raise RuntimeError('pause_not_observed')
            if verifier.verify_run(runs.get(wid,rid)) is not True:raise RuntimeError('publication_unverified')
            payload=dict(project_id=PROJECT,workflow_id=wid,run_id=rid,action='stop',operation_id='crash-window-stop',expected_version=0)
            state['stop']=once('stop-local-only',payload,lambda:app.state.run_control.apply(**payload))
            try:journal.get(wid,rid,'run-cancel')
            except KeyError:state['cancelIntentAbsentBeforeRestart']=True
            else:raise RuntimeError('unexpected_cancel_intent')
            state['nativeBeforeRestart']=sim.observe(wid,rid)
            if state['nativeBeforeRestart']['status']!='paused':raise RuntimeError('not_paused_before_restart')
            persist()
        # Rebuild normal API wiring, then start the real production discovery owner.
        rebuilt,new_discovery=build();worker=ApprovalDiscoveryWorker(new_discovery,interval=.1)
        before=len(calls)
        with TestClient(rebuilt):
            once('worker-start',{'worker':'ApprovalDiscoveryWorker'},lambda:(worker.start() or {'started':True}))
            try:
                for _ in range(150):
                    observed=sim.observe(wid,rid)
                    if observed['status']=='cancelled':break
                    time.sleep(.2)
                else:raise RuntimeError('recovery_cancel_not_observed')
            finally:worker.stop()
        state['final']=observed;state['cancelReceipt']=journal.get(wid,rid,'run-cancel')
        writes=[c for c in calls[before:] if c['method']!='GET']
        if len(writes)!=1 or writes[0]['path']!='/api/v2/workflows/'+wid+'/runs/'+rid+'/cancel' or writes[0]['thread']!='agent-line-approval-discovery':raise RuntimeError('recovery_write_provenance_mismatch')
        if state['cancelReceipt']['status']!='accepted' or state['cancelReceipt']['receipt']['verifiedStopped'] is not False:raise RuntimeError('receipt_mismatch')
        with tasks.connect() as db:
            if db.execute('SELECT count(*) FROM tasks').fetchone()[0]!=0:raise RuntimeError('unexpected_task')
        state.update(result='passed_live',nativeCalls=calls,nativeWrites=sum(c['method']!='GET' for c in calls),taskCount=0,workerAlive=worker.thread.is_alive());persist()
        print(json.dumps({'status':'passed_live','nativeWrites':state['nativeWrites'],'cancelSourceThread':writes[0]['thread'],'taskCount':0}));return 0
    except Exception as exc:
        write(DATA/'stop-recovery-failure.json',{'code':type(exc).__name__,'nativeCalls':calls})
        print(json.dumps({'status':'incomplete','code':type(exc).__name__}));return 1
    finally:sim.close()


if __name__=='__main__':raise SystemExit(recover_stop_window() if '--recover-stop-window' in sys.argv else main())
