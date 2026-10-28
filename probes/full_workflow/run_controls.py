#!/usr/bin/env python3
"""Two bounded real Codex cases for persistent pause/continue and stop.

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
from agent_line.verification import ArtifactPolicy,verify_artifacts
from agent_line.workflows import WorkflowGateway
from agent_line.authoring_api import AuthoringAPI,AuthoringAccess,state_hash
from agent_line.approvals import TrustedPrincipal
from probes.qwenpaw_memory.native_roundtrip import quiet_host
from probes.sim.authoring_probe import write

DATA=ROOT/'var/automatic-run-controls'
PROJECT='native-memory-roundtrip';BINDING='native-loop-parent';ROLE='agent-line-native-loop-parent'


def main():
    DATA.mkdir(mode=0o700,exist_ok=True);path=DATA/'state.json'
    auth=json.loads((ROOT/'var/sim/probe-auth.json').read_text());sim=SimClient(auth['baseUrl'],auth['apiKey'])
    calls=[];sim._client.event_hooks['request'].append(lambda r:calls.append({'method':r.method,'path':r.url.path}))
    recovering=path.exists() and '--finish-remaining' in sys.argv
    if path.exists() and not recovering:
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
    previous=json.loads(path.read_text()) if recovering else None
    suffix=previous['suffix'] if recovering else uuid.uuid4().hex[:16]
    cases={name:{'artifact':'control-'+name+'-'+suffix+'.txt','marker':'CONTROL_'+name+'_'+suffix,'tasks':{}} for name in ('pause_continue','stop')}
    if not recovering and any((workspace/c['artifact']).exists() for c in cases.values()):raise RuntimeError('artifact_exists')
    instruction=('Read artifact and marker from taskInput. Codex must itself execute a foreground shell command in its assigned cwd: write marker plus newline to artifact + ".started", sleep exactly 20 seconds, then write marker plus newline to artifact and shell-check it. '
        'The sleep must run in the foreground and complete before final file writing, never background or delegate a child agent. Only these two dedicated relative files may be changed. '
        'No network, memory changes, permission changes or absolute cwd guesses. This bounded wait tests external run control; do not shorten it.')
    main_config_path=ROOT/'var/adapter/service.json';main_config=json.loads(main_config_path.read_text())
    main_database=Path(main_config['database']);main_database=main_database if main_database.is_absolute() else main_config_path.parent/main_database
    main_guard=TaskStore(main_database).maintenance_status()
    state={'steps':{},'cases':cases,'suffix':suffix,'instruction':instruction,'createdAt':time.time(),'roleAgentId':ROLE,'projectId':PROJECT,'roleBindingId':BINDING,'parentTimeoutSeconds':900,'mainGuardBefore':main_guard}
    if recovering:
        state=previous;cases=state['cases'];state.setdefault('recovery',[]).append({'intent':'finish_original_pause_and_unstarted_stop','time':time.time()})
    write(path,state)
    def persist():write(path,state)
    def once(key,operation):
        if key in state['steps']:
            if recovering and state['steps'][key]['status']=='received':return state['steps'][key]['result']
            raise RuntimeError('operation_already_has_intent')
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
            if not recovering:cls.initialize(DATA/name);os.chmod(DATA/name,0o600)
        store=TaskStore(DATA/'tasks.sqlite');ledger=RoleDispatchLedger(DATA/'roles.sqlite');journal=SimJournal(DATA/'sim.sqlite')
        api=SimAuthoringClient(sim,auth['workspaceId'])
        projects={PROJECT:{'workspace':str(workspace)}};bindings={BINDING:{'projectId':PROJECT,'roleAgentId':ROLE}}
        author_token=secrets.token_urlsafe(40)
        access=AuthoringAccess({author_token:TrustedPrincipal('run-controls-probe-editor',frozenset({PROJECT}),frozenset({'edit_workflows','publish_workflows'}),time.time()+7200)})
        authoring=AuthoringAPI(access,journal,{PROJECT:api},projects=projects,role_bindings=bindings)
        flowconfig={'schemaVersion':1,'runLedgerPath':str(DATA/'runs.sqlite'),'pauseBindingsPath':str(DATA/'pauses.sqlite')};write(DATA/'task-flow.json',flowconfig)
        installation='automatic-run-controls-'+suffix
        service_token=(ROOT/'var/qwenpaw-secrets/execution-service-token').read_text().strip()
        gateway=RoleDispatchGateway(base,token,service_token,role_agent_id=ROLE,deployment_id='agent-line-g0-installation',ledger=ledger,memory_query=prior_state['query'],client=host,timeout_seconds=900)
        dispatcher=Dispatcher(store,{(PROJECT,BINDING):gateway})
        credential=secrets.token_urlsafe(40);write(DATA/'service-token.json',{'token':credential})
        def build_app():
            runtime=load_task_flow(DATA/'task-flow.json',tasks=store,projects=projects,role_bindings=bindings,client=sim,journal=journal)
            workflow=WorkflowGateway(sim,journal,{},installation_id=installation)
            workflow.task_flow=runtime;workflow.publication_verifier=runtime.discovery.publication_verifier
            application=create_app(store,ServiceConfig(tokens={credential:{PROJECT}},projects=projects,bindings=bindings,controlled_execution='confirmed'),workflows=workflow,dispatcher=dispatcher,task_flow=runtime,authoring=authoring)
            return application,workflow
        headers={'Authorization':'Bearer '+credential}
        app,wf=build_app()
        http=TestClient(app);http.__enter__()
        try:
            def author(operation,route,payload):
                def request():
                    response=http.post('/extension/v1/authoring/projects/'+PROJECT+'/workflows'+route,headers={'Authorization':'Bearer '+author_token},json=payload)
                    if response.status_code!=202:raise RuntimeError('authoring_http_'+str(response.status_code))
                    value=response.json()
                    if value.get('status')!='accepted':raise RuntimeError('authoring_not_accepted')
                    return value
                return once(operation,request)['receipt']
            created=author('create','',{'operationId':'create-'+suffix,'name':'agent-line-run-controls','description':'Isolated real execution run control proof'})
            wid=created['workflowId'];state['workflowId']=wid;persist()
            agent_config={'roleBindingId':BINDING,'instruction':instruction,'inputFrom':{'nodeId':'start','field':'input'},'memoryPolicyId':'native-role','outputSchemaId':'task-result','requireMemoryCommit':False,'allowFailureBranch':False}
            def node(i,t,c,x):return {'id':i,'type':t,'name':i,'position':{'x':x,'y':0},'config':c}
            # Both cases share one graph. A second controlled task would be unsafe
            # for pause_continue's one-task bound, so its successor is an End.
            doc={'schemaVersion':1,'nodes':[node('start','start',{},0),node('agent','agent',agent_config,200),node('end','end',{'resultFrom':'agent'},400)],'edges':[{'id':'a','source':'start','target':'agent'},{'id':'b','source':'agent','target':'end'}],'variables':{}}
            state['document']=doc;persist()
            saved=author('save','/'+wid+'/save',{'operationId':'save-'+suffix,'expectedStateSha256':state_hash(api.get_state(wid)),'document':doc})
            if saved.get('outcome')!='saved':raise RuntimeError('save_not_verified')
            published=author('publish','/'+wid+'/publish',{'operationId':'publish-'+suffix,'expectedStateSha256':state_hash(api.get_state(wid))})
            if published.get('outcome')!='published':raise RuntimeError('publish_not_verified')
            binding=published['bindingId'];state.update(bindingId=binding,publication=published);persist()
            for name,case in cases.items():
                quiet_host(process['pid'])
                if recovering and case.get('startIntent'):
                    if name!='pause_continue' or not case.get('finalRunId') or not case.get('actualEnd'):
                        raise RuntimeError('existing_submission_observation_only')
                    tid=next(iter(case['tasks']));task=store.get(tid)
                    result=case['actualEnd'].get('data',{}).get('blockOutputs',{}).get(native_id(wid,'end')+'.data',{}).get('agentLineResult',{})
                    if result.get('taskId')!=tid or result.get('verificationStatus')!='passed' or not task['resourcesReleased']:
                        raise RuntimeError('recovery_original_result_unconfirmed')
                    case['tasks'][tid]=task;parent=ledger.get(tid);case['parent']=parent
                    response=host.get(base+'/api/console/chat/task/'+parent['parentTaskId'],headers={'Authorization':'Bearer '+token,'X-Agent-Id':ROLE});response.raise_for_status();case['parentObservation']=response.json()
                    if response.json().get('status')!='finished':raise RuntimeError('recovery_parent_not_finished')
                    before=sum(c['method']!='GET' for c in calls)
                    case['duplicateIntent']=True;persist()
                    again=http.post('/extension/v1/workflows/'+binding+'/runs',headers=headers,json=case['startPayload'])
                    duplicate=http.post('/extension/v1/workflows/'+binding+'/runs/'+case['runId']+'/control',headers=headers,json=case['pauseIntent'])
                    if again.status_code!=202 or duplicate.status_code!=200 or sum(c['method']!='GET' for c in calls)!=before:raise RuntimeError('recovery_replay_failed')
                    case.update(result='passed_live',replayNativeWrites=0,queryParserCorrection=True);persist()
                    print(json.dumps({'case':name,'phase':'original_completed_result_rechecked','taskId':tid,'newNativeWrites':0}),flush=True)
                    continue
                body={'idempotencyKey':name+'-'+suffix,'input':{'input':{k:case[k] for k in ('artifact','marker')}}}
                run_id=str(uuid.uuid5(uuid.NAMESPACE_URL,canonical([installation,PROJECT,wid,body['idempotencyKey'],binding])))
                route='/extension/v1/workflows/'+binding+'/runs/'+run_id
                case.update(runId=run_id,startPayload=body,startIntent=True);persist()
                started=http.post('/extension/v1/workflows/'+binding+'/runs',headers=headers,json=body)
                case.update(startHTTP=started.status_code,startReceipt=started.json());persist()
                if started.status_code!=202 or started.json().get('status')!='accepted':raise RuntimeError('start_not_accepted')
                deadline=time.monotonic()+1200;last_print=0
                def tasks_for_run():return [t for t in store.list_tasks({PROJECT})['tasks'] if t['payload']['workflow']['runId']==run_id]
                def control(action,version):
                    payload={'operationId':name+'-'+action+'-'+suffix,'action':action,'expectedVersion':version}
                    if action=='pause':payload['activeExecution']='continue'
                    case[action+'Intent']=payload;persist()
                    response=http.post(route+'/control',headers=headers,json=payload)
                    case[action+'Response']={'status':response.status_code,'body':response.json()};persist()
                    if response.status_code!=200:raise RuntimeError('control_'+action+'_http_'+str(response.status_code))
                    return payload
                while time.monotonic()<deadline:
                    tasks=tasks_for_run()
                    if len(tasks)>1:raise RuntimeError('unexpected_successor_task')
                    if tasks:
                        task=tasks[0];tid=task['id'];case['tasks'][tid]=task;identity=task['executionIdentity']
                        if all(identity.get(k) for k in ('codexThreadId','codexTurnId')) and (workspace/(case['artifact']+'.started')).exists():
                            observed=subprocess.run([sys.executable,bridge['container_control'],'observe','--thread',identity['codexThreadId'],'--turn',identity['codexTurnId'],'--workspace',str(workspace)],capture_output=True,text=True,check=True)
                            proof=json.loads(observed.stdout)
                            inspect=subprocess.run(['docker','inspect','--format','{{json .State}}',proof['containerId']],capture_output=True,text=True,check=True)
                            container=json.loads(inspect.stdout)
                            if proof['resourcesReleased'] is False and container.get('Running') is True and task['resourcesReleased'] is False:
                                case['activeProof']={'task':task,'controlProof':proof,'containerState':container,'startedContentMatches':(workspace/(case['artifact']+'.started')).read_text()==case['marker']+'\n'};persist()
                                if not case['activeProof']['startedContentMatches']:raise RuntimeError('started_content_mismatch')
                                control('pause' if name=='pause_continue' else 'stop',0)
                                print(json.dumps({'case':name,'phase':'controlled_active_codex','taskId':tid,'identity':identity}),flush=True)
                                break
                        if task['executionStatus'] in ('succeeded','failed','canceled'):raise RuntimeError('missed_live_execution_window')
                    if time.monotonic()-last_print>30:
                        print(json.dumps({'case':name,'phase':'waiting_live_codex','taskCount':len(tasks),'taskId':tasks[0]['id'] if tasks else None}),flush=True);last_print=time.monotonic();persist()
                    time.sleep(1)
                else:raise RuntimeError('live_execution_observation_timeout')
                # Pausing must leave the same native execution alive, not cancel it.
                if name=='pause_continue':
                    after=store.get(tid)
                    proof=json.loads(subprocess.run([sys.executable,bridge['container_control'],'observe','--thread',identity['codexThreadId'],'--turn',identity['codexTurnId'],'--workspace',str(workspace)],capture_output=True,text=True,check=True).stdout)
                    case['afterPause']={'task':after,'containerProof':proof};persist()
                    if after['cancellationRequested'] or proof['resourcesReleased'] or after['executionIdentity']!=identity:raise RuntimeError('pause_changed_native_execution')
                while time.monotonic()<deadline:
                    task=store.get(tid);case['tasks'][tid]=task;persist()
                    if task['resourcesReleased'] and task['executionStatus'] in ('succeeded','failed','canceled'):break
                    if time.monotonic()-last_print>30:
                        print(json.dumps({'case':name,'phase':'observing_original_task','taskId':tid,'status':task['executionStatus'],'resourcesReleased':task['resourcesReleased']}),flush=True);last_print=time.monotonic()
                    time.sleep(1)
                else:raise RuntimeError('original_execution_terminal_unknown')
                if not any(e.get('kind')=='resource_exit' for e in task['evidence']):raise RuntimeError('positive_resource_exit_required')
                if name=='pause_continue':
                    if task['executionStatus']!='succeeded':raise RuntimeError('paused_task_not_succeeded')
                    expected=(case['marker']+'\n').encode();evidence_dir=DATA/'artifacts'/tid;evidence_dir.mkdir(mode=0o700,parents=True,exist_ok=True)
                    case['verificationIntent']=True;persist()
                    receipt=verify_artifacts(store,tid,workspace=workspace,policies=[ArtifactPolicy(case['artifact'],hashlib.sha256(expected).hexdigest(),len(expected))],evidence_dir=evidence_dir)
                    store.record_verification(tid,receipt);case['verification']=receipt;persist()
                    if receipt['status']!='passed':raise RuntimeError('artifact_failed')
                    # Let background completion attempt the paused gate, then drain.
                    time.sleep(3)
                    if journal.resume_summary(wid,run_id) is not None:raise RuntimeError('paused_run_resumed')
                    case['pausedNative']=sim.observe(wid,run_id)
                    if case['pausedNative'].get('response',{}).get('data',{}).get('status')!='paused':raise RuntimeError('native_machine_wait_changed')
                    http.__exit__(None,None,None)
                    app,wf=build_app();http=TestClient(app);http.__enter__()
                    reopened=http.get(route,headers=headers);reopened.raise_for_status();case['reopened']=reopened.json();persist()
                    if reopened.json().get('control',{}).get('state')!='paused' or len(tasks_for_run())!=1:raise RuntimeError('pause_not_persistent')
                    control('continue',1)
                    for _ in range(120):
                        response=http.get(route,headers=headers);response.raise_for_status();result=response.json()
                        child=result.get('resumption',{});execution=child.get('execution',{}).get('response',{}).get('data',{})
                        if execution.get('status')=='completed':
                            case['finalRunId']=child['runId'];case['finalObservation']=result;break
                        time.sleep(1)
                    else:raise RuntimeError('continued_run_did_not_complete')
                    selected=sim._client.get(sim.base_url+f'/api/v2/workflows/{wid}/runs/'+case['finalRunId'],params={'selectedOutputs':native_id(wid,'end')+'.data'},headers={'x-api-key':sim._key},follow_redirects=False);selected.raise_for_status()
                    actual=selected.json();case['actualEnd']=actual
                    end=actual.get('data',{}).get('blockOutputs',{}).get(native_id(wid,'end')+'.data',{})
                    if end.get('agentLineResult',{}).get('taskId')!=tid:raise RuntimeError('end_task_identity_mismatch')
                else:
                    case['stoppedNative']=sim.observe(wid,run_id)
                    if journal.resume_summary(wid,run_id) is not None:raise RuntimeError('stopped_run_resumed')
                    if len(tasks_for_run())!=1:raise RuntimeError('stop_admitted_successor')
                    case['finalArtifactExists']=(workspace/case['artifact']).exists()
                parent=ledger.get(tid);case['parent']=parent
                for _ in range(90):
                    observation=host.get(base+'/api/console/chat/task/'+parent['parentTaskId'],headers={'Authorization':'Bearer '+token,'X-Agent-Id':ROLE});observation.raise_for_status();case['parentObservation']=observation.json()
                    if observation.json().get('status')=='finished':break
                    time.sleep(1)
                if case['parentObservation'].get('status')!='finished':raise RuntimeError('parent_not_finished')
                before=sum(c['method']!='GET' for c in calls);case['duplicateIntent']=True;persist()
                replay=http.post('/extension/v1/workflows/'+binding+'/runs',headers=headers,json=body)
                operation=case['pauseIntent'] if name=='pause_continue' else case['stopIntent']
                replay_control=http.post(route+'/control',headers=headers,json=operation)
                if replay.status_code!=202 or replay_control.status_code!=200:raise RuntimeError('replay_failed')
                if sum(c['method']!='GET' for c in calls)!=before:raise RuntimeError('replay_native_write')
                case['replayNativeWrites']=0;case['tasks'][tid]=store.get(tid)
                quiet_host(process['pid'])
                with store.connect() as db:leases=db.execute('SELECT count(*) FROM workspace_leases').fetchone()[0]
                if leases:raise RuntimeError('leases_remain')
                case['result']='passed_live';persist()
                print(json.dumps({'case':name,'status':'passed_live','taskId':tid,'activeLeaseCount':0}),flush=True)
        finally:http.__exit__(None,None,None)
        state['mainGuardAfter']=TaskStore(main_database).maintenance_status()
        if state['mainGuardAfter']!=state['mainGuardBefore']:raise RuntimeError('main_guard_changed')
        write(DATA/'report.json',{'status':'passed_live','cases':cases,'nativeCalls':calls,'mainGuardUnchanged':True,'activeLeaseCount':0,'ownedActiveContainerCount':0,'limitations':['No second Agent node in this graph; stop proves no result resume to End','Native ReMe automatic memory may update normally; no manual memory/config change']})
        state['result']='passed_live';persist();return 0
    except Exception as exc:
        state['failure']={'type':type(exc).__name__,'code':str(exc) if type(exc) is RuntimeError else 'probe_failed'};persist();write(DATA/'failure.json',{'status':'incomplete',**state['failure'],'nativeCalls':calls});print(json.dumps({'status':'incomplete',**state['failure']}),flush=True);return 1
    finally:
        if gateway is not None:gateway.close()
        if host is not None:host.close()
        sim.close()


if __name__=='__main__':raise SystemExit(main())
