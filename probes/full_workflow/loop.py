#!/usr/bin/env python3
"""Two bounded native loop cases through published catalog and real Codex.

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

DATA=ROOT/'var/automatic-loop-flow'
PROJECT='native-memory-roundtrip';BINDING='native-loop-parent';ROLE='agent-line-native-loop-parent'


def feedback_proof(parent,task,codex_home):
    previous=task['payload']['input']['repair']['previousResult']
    if previous is None:return None
    checks=[c for e in previous['evidence'] if e.get('kind')=='verification' for c in e.get('checks',[]) if c.get('status')=='failed']
    if not checks:raise RuntimeError('previous_failed_check_missing')
    check=checks[0];required=[previous['taskId'],check['reason'],check['actualSha256']]
    calls={};matched=None
    for message in parent.get('result',{}).get('output',[]):
        for block in message.get('content',[]):
            data=block.get('data') or {};call=data.get('call_id')
            if message.get('type')=='plugin_call' and data.get('name')=='agent_line_delegate':
                args=data.get('arguments');args=json.loads(args) if isinstance(args,str) else args
                if isinstance(args,dict) and all(value in args.get('instruction','') for value in required):calls[call]=args['instruction']
            if message.get('type')=='plugin_call_output' and data.get('name')=='agent_line_delegate' and call in calls and data.get('state')=='success':
                out=data.get('output');out=json.loads(out) if isinstance(out,str) else out
                if isinstance(out,dict) and out.get('taskId')==task['id'] and out.get('identity',{}).get('upstreamTaskId')==task['executionIdentity'].get('upstreamTaskId'):matched=call
    if matched is None:raise RuntimeError('real_delegation_missing_previous_failure_evidence')
    rollouts=list((codex_home/'sessions').rglob('*'+task['executionIdentity']['codexThreadId']+'.jsonl'))
    if len(rollouts)!=1:raise RuntimeError('rollout_ambiguous')
    inputs=[]
    for line in rollouts[0].read_text().splitlines():
        event=json.loads(line);payload=event.get('payload',{})
        if event.get('type')=='response_item' and payload.get('type')=='message' and payload.get('role')=='user' and all(value in json.dumps(payload) for value in required):inputs.append(event)
    if not inputs:raise RuntimeError('native_codex_input_missing_previous_failure_evidence')
    return {'previousTaskId':previous['taskId'],'failedCheck':check,'delegateCallId':matched,'delegateInstruction':calls[matched],'nativeUserInputs':inputs}


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
    guard_report=json.loads((ROOT/'var/sim-finite-loop-guard/report.json').read_text())
    if guard_report.get('status')!='passed_live':raise RuntimeError('native_guard_live_proof_required')
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
    cases={name:{'artifact':'loop-'+name+'-'+suffix+'.txt','correctMarker':'LOOP_CORRECT_'+name+'_'+suffix,'wrongMarker':'LOOP_WRONG_'+name+'_'+suffix,'expectedRounds':rounds,'tasks':{}} for name,rounds in [('early_success',2),('exhausted',3)]}
    if any((workspace/c['artifact']).exists() for c in cases.values()):raise RuntimeError('artifact_exists')
    instruction=('This is an authorized bounded loop test. Read case, artifact, correctMarker and wrongMarker from the nested taskInput.input data; read the zero-based iteration from repair.iteration. '
        'Delegate actual file work to Codex. For case early_success at iteration 0 write wrongMarker plus one newline; at iteration 1 use repair.previousResult verification failure to correct the file to correctMarker plus one newline. '
        'For case exhausted at every iteration 0,1,2 intentionally write wrongMarker plus one newline, even though prior validation failed. This deliberately tests the bounded failure branch. '
        'Only write the specified dedicated relative artifact in the execution workspace, using a shell command and checking exact content. Never modify other files, network, permissions or memory. '
        'Do not call coordination tools from Codex. Do not guess an absolute cwd. Do not claim independent verification: the external checker decides. In every nonzero iteration the delegated instruction MUST include the exact repair.previousResult.taskId and copy the previous failed artifact check reason and actualSha256 as evidence, even for exhausted mode.')
    state={'steps':{},'cases':cases,'suffix':suffix,'instruction':instruction,'createdAt':time.time(),'roleAgentId':ROLE,'projectId':PROJECT,'roleBindingId':BINDING,'parentTimeoutSeconds':900}
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
        access=AuthoringAccess({author_token:TrustedPrincipal('automatic-loop-probe-editor',frozenset({PROJECT}),frozenset({'edit_workflows','publish_workflows'}),time.time()+7200)})
        authoring=AuthoringAPI(access,journal,{PROJECT:api},projects=projects,role_bindings=bindings)
        flowconfig={'schemaVersion':1,'runLedgerPath':str(DATA/'runs.sqlite'),'pauseBindingsPath':str(DATA/'pauses.sqlite')};write(DATA/'task-flow.json',flowconfig)
        flow=load_task_flow(DATA/'task-flow.json',tasks=store,projects=projects,role_bindings=bindings,client=sim,journal=journal)
        installation='automatic-loop-flow-'+suffix
        wf=WorkflowGateway(sim,journal,{},installation_id=installation);wf.task_flow=flow;wf.publication_verifier=flow.discovery.publication_verifier
        service_token=(ROOT/'var/qwenpaw-secrets/execution-service-token').read_text().strip()
        gateway=RoleDispatchGateway(base,token,service_token,role_agent_id=ROLE,deployment_id='agent-line-g0-installation',ledger=ledger,memory_query=prior_state['query'],client=host,timeout_seconds=900)
        dispatcher=Dispatcher(store,{(PROJECT,BINDING):gateway})
        credential=secrets.token_urlsafe(40);write(DATA/'service-token.json',{'token':credential})
        app=create_app(store,ServiceConfig(tokens={credential:{PROJECT}},projects=projects,bindings=bindings,controlled_execution='confirmed'),workflows=wf,dispatcher=dispatcher,task_flow=flow,authoring=authoring)
        with TestClient(app) as http:
            def author(operation,route,payload):
                def request():
                    response=http.post('/extension/v1/authoring/projects/'+PROJECT+'/workflows'+route,headers={'Authorization':'Bearer '+author_token},json=payload)
                    if response.status_code!=202:raise RuntimeError('authoring_http_'+str(response.status_code))
                    data=response.json()
                    if data.get('status')!='accepted':raise RuntimeError('authoring_not_accepted')
                    return data
                return once(operation,request)['receipt']
            created=author('create','',{'operationId':'create-'+suffix,'name':'agent-line-automatic-loop-flow','description':'Isolated real publication-to-execution proof'})
            wid=created['workflowId'];state['workflowId']=wid;persist()
            agent_config={'roleBindingId':BINDING,'instruction':instruction,'inputFrom':{'nodeId':'start','field':'input'},'memoryPolicyId':'native-role','outputSchemaId':'task-result','requireMemoryCommit':False,'allowFailureBranch':True}
            def node(i,t,name,c,x):return {'id':i,'type':t,'name':name,'position':{'x':x,'y':0},'config':c}
            doc={'schemaVersion':1,'nodes':[node('start','start','开始',{},0),node('repair','loop','有界修复',{'body':'agent','maxIterations':3},200),node('agent','agent','执行节点',agent_config,50),node('end','end','最终任务结果',{'resultFrom':'agent'},750)],'edges':[{'id':'a','source':'start','target':'repair'},{'id':'b','source':'repair','target':'agent','branch':'body'},{'id':'c','source':'repair','target':'end','branch':'done'}],'variables':{}}
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
            for name,case in state['cases'].items():
                quiet_host(process['pid'])
                body={'idempotencyKey':name+'-'+suffix,'input':{'input':{'case':name,**{k:case[k] for k in ('artifact','correctMarker','wrongMarker')}}}}
                run_id=str(uuid.uuid5(uuid.NAMESPACE_URL,canonical([installation,PROJECT,wid,body['idempotencyKey'],binding])))
                case.update(runId=run_id,startPayload=body,startIntent=True);persist()
                started=http.post('/extension/v1/workflows/'+binding+'/runs',headers={'Authorization':'Bearer '+credential},json=body)
                case.update(startHTTP=started.status_code,startReceipt=started.json());persist()
                if started.status_code!=202 or started.json().get('status')!='accepted':raise RuntimeError('start_not_accepted')
                case['frozenPublication']=journal.get_publication(wid,run_id);persist()
                deadline=time.monotonic()+3600;last_print=0
                while time.monotonic()<deadline:
                    tasks=[t for t in store.list_tasks({PROJECT})['tasks'] if t['payload']['workflow']['runId']==run_id]
                    tasks.sort(key=lambda t:int(t['payload']['workflow']['attemptId']))
                    if len(tasks)>case['expectedRounds']:raise RuntimeError('unexpected_extra_loop_task')
                    for task in tasks:
                        tid=task['id'];iteration=int(task['payload']['workflow']['attemptId'])-1
                        entry=case['tasks'].setdefault(tid,{});entry['task']=task
                        if task['payload']['workflow']['nodeExecutionId']!=bid+'_loop'+str(iteration):raise RuntimeError('activation_identity_mismatch')
                        if iteration:
                            previous=tasks[iteration-1]
                            feedback=task['payload']['input']['repair']['previousResult']
                            if feedback.get('taskId')!=previous['id'] or feedback.get('verificationStatus')!='failed':raise RuntimeError('prior_feedback_missing')
                            entry['previousResumeReceipt']=journal.get(wid,run_id,bid+'_loop'+str(iteration-1))
                        if task['executionStatus']=='succeeded' and task['resourcesReleased'] and task['verificationStatus']=='pending':
                            if entry.get('verificationIntent'):raise RuntimeError('verification_requires_original_observation')
                            entry['verificationIntent']=True;persist()
                            expected=(case['correctMarker']+'\n').encode()
                            evidence_dir=DATA/'artifacts'/tid;evidence_dir.mkdir(mode=0o700,parents=True,exist_ok=True)
                            receipt=verify_artifacts(store,tid,workspace=workspace,policies=[ArtifactPolicy(case['artifact'],hashlib.sha256(expected).hexdigest(),len(expected))],evidence_dir=evidence_dir)
                            expected_status='passed' if name=='early_success' and iteration==1 else 'failed'
                            actual=(workspace/case['artifact']).read_bytes()
                            wanted=(case['correctMarker'] if expected_status=='passed' else case['wrongMarker'])+'\n'
                            if actual!=wanted.encode() or receipt['status']!=expected_status:raise RuntimeError('controlled_artifact_outcome_mismatch')
                            store.record_verification(tid,receipt);entry['verification']=receipt;persist()
                        if task['executionStatus'] in ('failed','canceled'):raise RuntimeError('native_execution_not_succeeded')
                    observed=http.get('/extension/v1/workflows/'+binding+'/runs/'+run_id,headers={'Authorization':'Bearer '+credential});observed.raise_for_status()
                    view=observed.json();case['gatewayObservation']=view
                    latest=view.get('resumption',{});execution=latest.get('execution',view['execution'])
                    case['workers']={'discovery':flow.workers[0].last_report,'completion':flow.workers[1].last_report,'execution':app.state.execution_worker.last_report};persist()
                    if time.monotonic()-last_print>30:
                        print(json.dumps({'case':name,'phase':'observing','taskCount':len(tasks),'latestExecutionStatus':tasks[-1]['executionStatus'] if tasks else None,'simStatus':execution.get('status')}),flush=True);last_print=time.monotonic()
                    if execution.get('status')=='completed' and len(tasks)==case['expectedRounds']:
                        case['finalRunId']=latest.get('runId',run_id);break
                    if execution.get('status') in ('failed','cancelled'):raise RuntimeError('native_loop_run_failed')
                    time.sleep(2)
                else:raise RuntimeError('observation_deadline_original_identities_preserved')
                for task in tasks:
                    tid=task['id'];task=store.get(tid);entry=case['tasks'][tid];entry['task']=task
                    if not task['resourcesReleased'] or not any(e.get('kind')=='resource_exit' for e in task['evidence']):raise RuntimeError('resource_exit_missing')
                    parent=ledger.get(tid);entry['parent']=parent
                    for _ in range(90):
                        r=host.get(base+'/api/console/chat/task/'+parent['parentTaskId'],headers={'Authorization':'Bearer '+token,'X-Agent-Id':ROLE});r.raise_for_status();entry['parentObservation']=r.json()
                        if entry['parentObservation'].get('status')=='finished':break
                        time.sleep(1)
                    if entry['parentObservation'].get('status')!='finished':raise RuntimeError('parent_still_running')
                    entry['feedbackProof']=feedback_proof(entry['parentObservation'],task,Path(bridge['codex_home']))
                    entry['resumeReceipt']=journal.get(wid,run_id,task['payload']['workflow']['nodeExecutionId']);persist()
                last=store.get(tasks[-1]['id']);resume_value=journal.resume_input(wid,run_id,last['payload']['workflow']['nodeExecutionId'])
                selectors=native_id(wid,'repair')+'--guard,'+native_id(wid,'end')+'.data'
                selected=sim._client.get(sim.base_url+f'/api/v2/workflows/{wid}/runs/'+case['finalRunId'],params={'selectedOutputs':selectors},headers={'x-api-key':sim._key},follow_redirects=False);selected.raise_for_status()
                native=selected.json()['data'];case['nativeGuardAndEnd']=native
                if native.get('workflowId')!=wid or native.get('runId')!=case['finalRunId']:raise RuntimeError('final_native_identity_mismatch')
                expected_end={'agentLineResult':resume_value}
                guard=native.get('blockOutputs',{}).get(native_id(wid,'repair')+'--guard',{}).get('result',{})
                if guard.get('result')!=resume_value or guard.get('needsRepair') is not (name=='exhausted'):raise RuntimeError('native_guard_result_mismatch')
                if native.get('blockOutputs',{}).get(native_id(wid,'end')+'.data')!=expected_end:raise RuntimeError('dynamic_last_result_mismatch')
                if execution.get('response',{}).get('data',{}).get('output',{}).get('data')!=expected_end:raise RuntimeError('final_output_mismatch')
                case.update(result='passed_live',businessVerified=name=='early_success');persist()
                quiet_host(process['pid'])
                with store.connect() as db:leases=db.execute('SELECT count(*) FROM workspace_leases').fetchone()[0]
                if leases:raise RuntimeError('leases_remain')
                print(json.dumps({'case':name,'status':'passed_live','taskCount':len(tasks),'businessVerified':name=='early_success','activeLeaseCount':0}),flush=True)
        report={'status':'passed_live','workflowId':wid,'bindingId':binding,'cases':state['cases'],'nativeCalls':calls,'activeLeaseCount':0,'ownedActiveContainerCount':0,'limitations':['Single existing native role/executor; main admission unchanged','exhausted is bounded failure, not business success','No automatic memory writeback or multi-project isolation claim']}
        write(DATA/'report.json',report);state['result']='passed_live';persist();return 0
    except Exception as exc:
        state['failure']={'type':type(exc).__name__,'code':getattr(exc,'code',str(exc) if type(exc) is RuntimeError else 'probe_failed')};persist();write(DATA/'failure.json',{'status':'incomplete',**state['failure'],'nativeCalls':calls});print(json.dumps({'status':'incomplete',**state['failure']}),flush=True);return 1
    finally:
        if gateway is not None:gateway.close()
        if host is not None:host.close()
        sim.close()


if __name__=='__main__':raise SystemExit(main())
