#!/usr/bin/env python3
"""One live pinned-role workflow with automatic artifact verification.

Existing state is observation-only. No manual task registration, pause binding,
relay calls, plugin installation, role configuration, or main admission changes.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
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

DATA=ROOT/'var/pinned-role-flow'
PROJECT='native-memory-roundtrip';BINDING='native-loop-parent';ROLE='agent-line-native-loop-parent'


def probe_directory(root, *, test_commands=False, reuse_known_template=False, test_failure=False,
                    test_cancel=False, publication_capabilities=False):
    if publication_capabilities and (not test_commands or test_failure or test_cancel):
        raise ValueError('Publication capabilities require test commands without failure or cancellation')
    if test_cancel and (test_failure or not (test_commands and reuse_known_template)):
        raise ValueError('Cancel case requires test commands and known template without failure mode')
    if test_failure and not (test_commands and reuse_known_template):
        raise ValueError('Failure case requires test commands and known template')
    if reuse_known_template and not test_commands:
        raise ValueError('Known template mode requires test commands')
    relative='var/pinned-role-flow'
    if test_commands:
        relative='var/pinned-test-flow-known-template' if reuse_known_template else 'var/pinned-test-flow'
    if test_failure:
        relative='var/pinned-test-failure-known-template'
    if test_cancel:
        relative='var/pinned-test-cancel-known-template'
    if publication_capabilities:
        relative='var/publication-ready-known-template' if reuse_known_template else 'var/publication-ready-flow'
    return Path(root)/relative


def publication_counts(store, ledger):
    with store.connect() as db:
        counts={name:db.execute('SELECT count(*) FROM '+name).fetchone()[0]
                for name in ('tasks','events','workspace_leases')}
    with ledger.connect() as db:
        counts['deliveries']=db.execute('SELECT count(*) FROM deliveries').fetchone()[0]
    return counts


def historical_sources(root, *, test_cancel=False, test_failure=False, publication_capabilities=False):
    root=Path(root)
    original=root/('var/pinned-test-failure-known-template' if test_cancel else 'var/pinned-test-flow-known-template' if test_failure else 'var/pinned-role-flow')
    sources=([root/'var/pinned-role-flow',root/'var/pinned-test-flow-known-template',root/'var/pinned-test-failure-known-template']
             if publication_capabilities else [root/'var/pinned-role-flow',root/'var/pinned-test-flow-known-template',original]
             if test_cancel else [root/'var/pinned-role-flow',original] if test_failure else [original])
    return original,sources


def checked_historical_parents(sources,sim):
    allowed_parents=set()
    for source in sources:
        proof_state=json.loads((source/'state.json').read_text())
        proof_report=json.loads((source/'report.json').read_text())
        root_check=json.loads((source/'root-check.json').read_text())
        if (proof_state.get('result')!='passed_live' or proof_report.get('status')!='passed_live'
                or root_check.get('status')!='passed_read_only'
                or proof_state.get('workflowId')!='fc628016-3198-4921-ae23-ad3e35c3fd51'
                or root_check.get('workflowId')!=proof_state['workflowId']):
            raise RuntimeError('root_checked_template_proof_required')
        for case in proof_state['cases'].values():
            if (case.get('result')!='passed_live' or root_check.get('rootRunId')!=case['runId']
                    or root_check.get('childRunId')!=case['finalRunId']
                    or not any(c.get('runId')==case['runId'] and c.get('finalRunId')==case['finalRunId']
                               for c in proof_report['cases'].values())):
                raise RuntimeError('root_checked_run_identity_mismatch')
            if sim.observe(proof_state['workflowId'],case['finalRunId'])['status']!='completed':
                raise RuntimeError('root_checked_child_not_completed')
            allowed_parents.add(case['runId'])
    return allowed_parents


def check_known_history(history,workflow,allowed_parents):
    if not isinstance(history,list):raise RuntimeError('known_template_history_unavailable')
    for run in history:
        if run.get('workflowId')!=workflow:raise RuntimeError('known_template_history_identity_mismatch')
        if run.get('status') not in ('completed','failed','cancelled'):
            if run.get('runId') not in allowed_parents or run.get('status')!='paused':
                raise RuntimeError('known_template_has_unresolved_run')


def candidate_settings(root, directory, *, workspace, base, bindings, image, memory_query,
                       author_token, runner_token, sim_workspace, installation):
    """Persist the same candidate for later normal startup; grants no readiness."""
    root,directory=Path(root),Path(directory)
    projects={PROJECT:{'workspace':str(workspace)}}
    service={'schemaVersion':1,'database':str(directory/'tasks.sqlite'),
        'tokens':{runner_token:[PROJECT]},'projects':projects,'bindings':bindings,
        'executionFile':'execution.json','authoringFile':'authoring.json','taskFlowFile':'task-flow.json',
        'verification':{'evidenceDirectory':str(directory/'verification-evidence'),'testExecutor':{'imageId':image}},
        'sim':{'authFile':str(root/'var/sim/probe-auth.json'),'journal':'sim.sqlite',
               'installationId':installation,'bindings':{}}}
    execution={'schemaVersion':1,'gateways':[{'projectId':PROJECT,'roleBindingId':BINDING,
        'roleAgentId':ROLE,'workspace':str(workspace),'installationId':'agent-line-g0-installation',
        'ledgerPath':str(directory/'roles.sqlite'),'baseURL':base,
        'hostCredentialsFile':str(root/'var/qwenpaw-secrets/local-login.json'),
        'serviceTokenFile':str(root/'var/qwenpaw-secrets/execution-service-token'),
        'memoryQuery':memory_query,'parentTimeoutSeconds':900,'requestTimeoutSeconds':60}]}
    authoring={'schemaVersion':1,'credentials':[{'token':author_token,'subject':'automatic-loop-probe-editor',
        'projectScopes':[PROJECT],'capabilities':['edit_workflows','publish_workflows'],
        'expiresAt':time.time()+7200}],'workspaces':{PROJECT:sim_workspace}}
    return {'service.json':service,'execution.json':execution,'authoring.json':authoring}


def main(*, test_commands=False, reuse_known_template=False, test_failure=False, test_cancel=False,
         publication_capabilities=False):
    global DATA
    DATA=probe_directory(ROOT,test_commands=test_commands,reuse_known_template=reuse_known_template,
                         test_failure=test_failure,test_cancel=test_cancel,
                         publication_capabilities=publication_capabilities)
    if publication_capabilities and DATA.exists() and not (DATA/'state.json').exists():
        raise RuntimeError('existing_publication_directory_requires_reconciliation')
    DATA.mkdir(mode=0o700,exist_ok=True);path=DATA/'state.json'
    auth=json.loads((ROOT/'var/sim/probe-auth.json').read_text());sim=SimClient(auth['baseUrl'],auth['apiKey'])
    calls=[];responses=[]
    sim._client.event_hooks['request'].append(lambda r:calls.append({'method':r.method,'path':r.url.path}))
    sim._client.event_hooks['response'].append(lambda r:responses.append({'method':r.request.method,'path':r.request.url.path,'statusCode':r.status_code}))
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
    artifact=('pinned-add-'+suffix+'.py') if test_commands else ('pinned-role-'+suffix+'.txt')
    correct='def add(left, right):\n    return left + right' if test_commands else 'PINNED_ROLE_'+suffix
    if test_failure:
        correct='def add(left, right):'+chr(10)+'    return left - right'
    cases={'test-cancel' if test_cancel else 'test-failure' if test_failure else 'pass':{'artifact':artifact,'marker':correct,'tasks':{}}}
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
        known=None
        if reuse_known_template:
            original,proof_sources=historical_sources(ROOT,test_cancel=test_cancel,test_failure=test_failure,
                                                     publication_capabilities=publication_capabilities)
            allowed_parents=checked_historical_parents(proof_sources,sim)
            state['allowedHistoricalParents']=sorted(allowed_parents);persist()
            previous=json.loads((original/'state.json').read_text())
            report=json.loads((original/'report.json').read_text())
            if previous.get('result')!='passed_live' or report.get('status')!='passed_live' or previous.get('workflowId')!='fc628016-3198-4921-ae23-ad3e35c3fd51':
                raise RuntimeError('known_template_proof_missing')
            for case in previous['cases'].values():
                terminal=sim.observe(previous['workflowId'],case['finalRunId'])
                if terminal['status'] not in ('completed','failed','cancelled'):raise RuntimeError('known_template_history_not_terminal')
            known=previous
            source=original/'sim.sqlite'
            destination=DATA/'sim.sqlite'
            fd=os.open(destination,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
            with sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True) as src, sqlite3.connect(destination) as dst:
                src.backup(dst)
            state['reusedTemplate']={'workflowId':known['workflowId'],'journalSource':str(source),
                'sourcePublication':known['publication'],'unusedUnknownCreateState':str(ROOT/'var/pinned-test-flow/state.json')}
            if publication_capabilities:
                state['reusedTemplate'].update(preserved409CreateState=str(ROOT/'var/publication-ready-flow/state.json'),
                                               newDraftCreate=False,retriesPreservedCreate=False)
            persist()
        for name,cls in [('tasks.sqlite',TaskStore),('roles.sqlite',RoleDispatchLedger),('sim.sqlite',SimJournal),('runs.sqlite',WorkflowRunLedger),('pauses.sqlite',PauseBindings)]:
            if not (reuse_known_template and name=='sim.sqlite'):
                cls.initialize(DATA/name)
            os.chmod(DATA/name,0o600)
        store=TaskStore(DATA/'tasks.sqlite');ledger=RoleDispatchLedger(DATA/'roles.sqlite');journal=SimJournal(DATA/'sim.sqlite')
        api=SimAuthoringClient(sim,auth['workspaceId'])
        service_token=(ROOT/'var/qwenpaw-secrets/execution-service-token').read_text().strip()
        response=host.get(base+'/api/agent-line/execution-binding',headers={'Authorization':'Bearer '+token,'X-Agent-Line-Service-Token':service_token})
        response.raise_for_status();descriptor=response.json()
        from agent_line.role_revision import validate_role_revision
        role_revision=validate_role_revision(descriptor.get('roleRevisions',{}).get(ROLE))
        if descriptor.get('workspace')!=str(workspace) or ROLE not in descriptor.get('managedRoleIds',[]):raise RuntimeError('descriptor_binding_mismatch')
        state['roleRevision']=role_revision;persist()
        projects={PROJECT:{'workspace':str(workspace)}};bindings={BINDING:{'projectId':PROJECT,'roleAgentId':ROLE,'roleRevision':role_revision,'verificationPolicy':{'schemaVersion':1,'policyId':'automatic-bytes-'+suffix,'kind':'artifact_sha256','requireTests':False,'artifacts':[{'path':artifact,'sha256':hashlib.sha256((correct+'\n').encode()).hexdigest(),'exactSize':len((correct+'\n').encode()),'maxBytes':1024}]}}}
        evidence_dir=DATA/'verification-evidence';evidence_dir.mkdir(mode=0o700)
        test_executor=None
        if test_commands:
            from agent_line.test_executor import DockerTestExecutor
            image=json.loads((ROOT/'var/executor/config.json').read_text())['image']
            test_executor=DockerTestExecutor(evidence_dir,image)
            code=("import runpy, unittest\nadd = runpy.run_path("+repr(artifact)+")['add']\n"
                  "class AddTests(unittest.TestCase):\n"
                  "    def test_positive(self): self.assertEqual(add(2, 3), 5)\n"
                  "    def test_negative(self): self.assertEqual(add(-2, 3), 1)\n"
                  "    def test_zero(self): self.assertEqual(add(0, 0), 0)\n"
                  "unittest.main(verbosity=2)\n")
            bindings[BINDING]['verificationPolicy'].update(schemaVersion=2,kind='command_and_artifacts',requireTests=True,
                testEnvironmentId=test_executor.environment_id,
                tests=[{'id':'addition-unittests','argv':['python3','-c',code],'timeoutSeconds':60,'maxOutputBytes':65536}])
            if test_cancel:
                slow_code="from pathlib import Path; import time; Path('/tmp/agent-line-test-started').write_text('started'); time.sleep(60)\n"+code
                bindings[BINDING]['verificationPolicy']['tests']=[
                    {'id':'active-addition-unittests','argv':['python3','-c',slow_code],'timeoutSeconds':120,'maxOutputBytes':65536},
                    {'id':'must-not-start','argv':['python3','-c','raise SystemExit(0)'],'timeoutSeconds':30,'maxOutputBytes':1024}]
            state['testEnvironmentId']=test_executor.environment_id;persist()
        author_token=secrets.token_urlsafe(40)
        access=AuthoringAccess({author_token:TrustedPrincipal('automatic-loop-probe-editor',frozenset({PROJECT}),frozenset({'edit_workflows','publish_workflows'}),time.time()+7200)})
        authoring=AuthoringAPI(access,journal,{PROJECT:api},projects=projects,role_bindings=bindings)
        flowconfig={'schemaVersion':1,'runLedgerPath':str(DATA/'runs.sqlite'),'pauseBindingsPath':str(DATA/'pauses.sqlite')};write(DATA/'task-flow.json',flowconfig)
        installation=('pinned-test-flow-' if test_commands else 'pinned-role-flow-')+suffix
        service_token=(ROOT/'var/qwenpaw-secrets/execution-service-token').read_text().strip()
        gateway=RoleDispatchGateway(base,token,service_token,role_agent_id=ROLE,deployment_id='agent-line-g0-installation',ledger=ledger,memory_query=prior_state['query'],client=host,timeout_seconds=900)
        dispatcher=Dispatcher(store,{(PROJECT,BINDING):gateway})
        credential=secrets.token_urlsafe(40);write(DATA/'service-token.json',{'token':credential})
        if publication_capabilities:
            candidate=candidate_settings(ROOT,DATA,workspace=workspace,base=base,bindings=bindings,image=image,
                memory_query=prior_state['query'],author_token=author_token,runner_token=credential,
                sim_workspace=auth['workspaceId'],installation=installation)
            for filename,settings in candidate.items():write(DATA/filename,settings)
            state['publicationCapabilitiesMode']=True;persist()
        def build_app(current_store):
            runtime=load_task_flow(DATA/'task-flow.json',tasks=current_store,projects=projects,role_bindings=bindings,client=sim,journal=journal)
            workflow=WorkflowGateway(sim,journal,{},installation_id=installation)
            workflow.task_flow=runtime;workflow.publication_verifier=runtime.discovery.publication_verifier
            verifier=AutomaticVerifier(current_store,evidence_dir,test_executor=test_executor)
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
            if known is None:
                created=author('create','',{'operationId':'create-'+suffix,
                    'name':'agent-line-publication-ready-'+suffix if publication_capabilities else 'agent-line-pinned-role',
                    'description':'One real pinned role task with automatic byte verification'})
                wid=created['workflowId']
            else:
                from agent_line.sim_authoring import compile_document,decode_document
                wid=known['workflowId']
                detail=api.get_detail(wid)
                actual=api.get_state(wid)
                if detail.get('id')!=wid or compile_document(decode_document(actual),wid)!=compile_document(known['document'],wid):
                    raise RuntimeError('known_template_draft_changed')
                # The old completion proof is insufficient if another run was started later.
                response=sim._client.get(sim.base_url+'/api/v2/workflows/'+wid+'/runs',
                    params={'limit':100},headers={'x-api-key':sim._key},follow_redirects=False)
                response.raise_for_status();listing=response.json()
                if listing.get('nextCursor'):raise RuntimeError('known_template_history_requires_more_pages')
                history=listing.get('data')
                # Parent pause rows can remain paused after a completed resume child;
                # allow only the exact previously proved parent and child identities.
                check_known_history(history,wid,allowed_parents)
                state['knownTemplateHistory']=history;persist()
            state['workflowId']=wid;persist()
            agent_config={'roleBindingId':BINDING,'instruction':instruction,'inputFrom':{'nodeId':'start','field':'input'},'memoryPolicyId':'native-role','outputSchemaId':'task-result','requireMemoryCommit':False,'allowFailureBranch':True}
            def node(i,t,c,x):return {'id':i,'type':t,'name':i,'position':{'x':x,'y':0},'config':c}
            doc={'schemaVersion':1,'nodes':[node('start','start',{},0),node('agent','agent',agent_config,200),node('condition','condition',{'rules':[{'id':'verified','operator':'eq','source':'agent','field':'result.verificationStatus','value':'passed'}]},400),node('passEnd','end',{'resultFrom':'agent'},600),node('failEnd','end',{'resultFrom':'agent'},600)],'edges':[{'id':'a','source':'start','target':'agent'},{'id':'b','source':'agent','target':'condition'},{'id':'c','source':'condition','target':'passEnd','branch':'verified'},{'id':'d','source':'condition','target':'failEnd','branch':'else'}],'variables':{}}
            state['document']=doc;state['verificationPolicy']=bindings[BINDING]['verificationPolicy'];persist()
            saved=author('save','/'+wid+'/save',{'operationId':'save-'+suffix,'expectedStateSha256':state_hash(api.get_state(wid)),'document':doc})
            if saved.get('outcome')!='saved':raise RuntimeError('save_not_verified')
            if publication_capabilities:
                publication_before=publication_counts(store,ledger)
                state['publicationCapabilityCheck']={'before':publication_before};persist()
                if any(publication_before.values()):raise RuntimeError('publication_has_prior_task_writes')
                publication_native=[]
                def publication_request(request):
                    publication_native.append({'method':request.method,'path':request.url.path})
                host.event_hooks['request'].append(publication_request)
            try:
                published=author('publish','/'+wid+'/publish',{'operationId':'publish-'+suffix,'expectedStateSha256':state_hash(api.get_state(wid))})
            finally:
                if publication_capabilities:
                    host.event_hooks['request'].remove(publication_request)
                    state['publicationCapabilityCheck'].update(after=publication_counts(store,ledger),
                                                               nativeRequests=publication_native)
                    persist()
            if publication_capabilities:
                evidence=state['publicationCapabilityCheck']
                if (evidence['after']!=publication_before
                        or publication_native!=[{'method':'GET','path':'/api/agent-line/execution-binding'}]):
                    raise RuntimeError('publication_preflight_read_only_proof_missing')
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
                        current=store.get(tid)
                        expected='canceled' if test_cancel else 'passed' if name=='pass' else 'failed'
                        if test_cancel and current['executionStatus']=='succeeded' and current['resourcesReleased'] and 'stopIntent' not in case:
                            jobs=[json.loads(f.read_text()) for f in evidence_dir.glob('test-*.json')]
                            jobs=[j for j in jobs if j.get('binding',{}).get('taskId')==tid and j.get('binding',{}).get('testId')=='active-addition-unittests']
                            if jobs and jobs[0].get('containerId'):
                                job=jobs[0];container=job['containerId'];live=test_executor._inspect(container)
                                if live is not None and live['State']['Running'] is True:
                                    test_executor._matches(live,job['binding'],job['name'],test_executor._argv(job['binding']['test']))
                                    observed_marker=subprocess.run(['docker','exec',container,'/usr/bin/python3','-c',"from pathlib import Path; print(Path('/tmp/agent-line-test-started').read_text())"],capture_output=True,text=True,timeout=10)
                                    if observed_marker.returncode==0 and observed_marker.stdout.strip()=='started':
                                        case['activeTestObservation']={'containerId':container,'binding':job['binding'],'state':live['State'],'marker':'started'}
                                        control_body={'operationId':'stop-active-test-'+suffix,'action':'stop','expectedVersion':0}
                                        case['stopIntent']={'path':route+'/control','body':control_body};persist()
                                        stopped=http.post(route+'/control',headers=headers,json=control_body)
                                        case['stopReceipt']={'httpStatus':stopped.status_code,'body':stopped.json()};persist()
                                        if stopped.status_code!=200:raise RuntimeError('stop_not_acknowledged')
                                        print(json.dumps({'case':name,'phase':'stop_requested','taskId':tid,'containerId':container}),flush=True)
                        if current['verificationStatus']!= 'pending':
                            if current['verificationStatus']!=expected:raise RuntimeError('unexpected_verification_result')
                            case['tasks'][tid]=current
                            result=http.get(route,headers=headers);result.raise_for_status();observed=result.json();child=observed.get('resumption',{})
                            if test_cancel:
                                native_stop=sim.observe(wid,run_id)
                                if observed.get('control',{}).get('state')=='stop_requested' and native_stop['status']=='cancelled':
                                    if journal.resume_summary(wid,run_id) is not None:raise RuntimeError('cancel_must_not_resume')
                                    case['finalObservation']=observed;case['nativeCanceled']=native_stop;persist();break
                            if child.get('execution',{}).get('status')=='completed':
                                case['finalRunId']=child['runId'];case['finalObservation']=observed;persist();break
                    if time.monotonic()-last_print>30:
                        print(json.dumps({'case':name,'phase':'background_execution_and_verification','taskCount':len(tasks),'taskId':tasks[0]['id'] if tasks else None,'verificationStatus':tasks[0]['verificationStatus'] if tasks else None}),flush=True);last_print=time.monotonic();persist()
                    time.sleep(1)
                else:raise RuntimeError('original_execution_observation_timeout')
                if not test_cancel:
                    end='passEnd' if name=='pass' else 'failEnd'
                    selectors=','.join([native_id(wid,'condition'),native_id(wid,end)+'.data'])
                    response=sim._client.get(sim.base_url+f'/api/v2/workflows/{wid}/runs/'+case['finalRunId'],params={'selectedOutputs':selectors},headers={'x-api-key':sim._key},follow_redirects=False);response.raise_for_status()
                    native=response.json()['data'];case['actualBranch']=native;blocks=native['blockOutputs']
                    selected=blocks[native_id(wid,'condition')]['selectedPath']['blockId']
                    result=blocks[native_id(wid,end)+'.data']['agentLineResult']
                    if selected!=native_id(wid,end) or result['taskId']!=tid or result['verificationStatus']!=expected or result['output']!=current['output']:raise RuntimeError('actual_native_branch_mismatch')
                    if test_commands:
                        verification=next(e for e in current['evidence'] if e.get('kind')=='verification')
                        tests=[check for check in verification['checks'] if check.get('kind')=='test_command']
                        wanted_status='failed' if test_failure else 'passed';wanted_exit=1 if test_failure else 0
                        if len(tests)!=1 or tests[0]['status']!=wanted_status or tests[0]['exitCode']!=wanted_exit:raise RuntimeError('test_command_result_mismatch')
                        artifact_checks=[check for check in verification['checks'] if check.get('kind')=='artifact_sha256']
                        if len(artifact_checks)!=1 or artifact_checks[0]['status']!='passed':raise RuntimeError('declared_artifact_not_matched')
                        if test_failure and current['executionStatus']!='succeeded':raise RuntimeError('test_failure_not_independent')
                        if 'Ran 3 tests' not in tests[0]['stderr']+tests[0]['stdout']:raise RuntimeError('actual_unittest_evidence_missing')
                        case['testChecks']=tests
                        jobs=[json.loads(file.read_text()) for file in evidence_dir.glob('test-*.json')]
                        jobs=[job for job in jobs if job.get('binding',{}).get('taskId')==tid]
                        if len(jobs)!=1 or jobs[0].get('removed') is not True or jobs[0].get('check')!=tests[0]:
                            raise RuntimeError('test_executor_cleanup_unconfirmed')
                        case['testJobs']=jobs
                else:
                    jobs=[json.loads(f.read_text()) for f in evidence_dir.glob('test-*.json')]
                    jobs={j['binding']['testId']:j for j in jobs if j.get('binding',{}).get('taskId')==tid}
                    first=jobs.get('active-addition-unittests',{});second=jobs.get('must-not-start',{})
                    if (len(jobs)!=2 or first.get('removed') is not True
                            or first.get('cancellation',{}).get('disposition')!='exited'
                            or second.get('createIntent') is not False or second.get('startIntent') is not False
                            or second.get('containerId') is not None
                            or second.get('cancellation',{}).get('disposition')!='not_started'):
                        raise RuntimeError('canceled_test_cleanup_not_proven')
                    case['testJobs']=jobs
                    if any(c['method']=='POST' and c['path'].endswith('/resume') for c in calls):raise RuntimeError('unexpected_resume_after_cancel')
                    actual=(workspace/artifact).read_bytes()
                    if hashlib.sha256(actual).hexdigest()!=bindings[BINDING]['verificationPolicy']['artifacts'][0]['sha256']:raise RuntimeError('cancel_artifact_hash_mismatch')
                    case['actualArtifactSha256']=hashlib.sha256(actual).hexdigest()
                parent=ledger.get(tid);case['parent']=parent
                run_snapshot=WorkflowRunLedger(DATA/'runs.sqlite').get(wid,run_id)
                native_wait=native_id(wid,'agent')
                from agent_line.verification_policy import policy_hash
                frozen_policies=[published['externalTasks']['agentPolicies'][native_wait]['verificationPolicy'],run_snapshot['agentPolicies'][native_wait]['verificationPolicy'],current['payload']['verificationPolicy']]
                if any(policy_hash(v)!=policy_hash(bindings[BINDING]['verificationPolicy']) for v in frozen_policies):raise RuntimeError('frozen_test_policy_mismatch')
                case['frozenTestPolicySha256']=policy_hash(frozen_policies[0])
                frozen_pins={'publication':published['externalTasks']['agentPolicies'][native_wait]['roleRevision'],
                    'run':run_snapshot['agentPolicies'][native_wait]['roleRevision'],
                    'task':current['payload']['roleRevision'],'parent':parent['payload']['roleRevision']}
                receipt_db=Path(bridge['receipt_db']).resolve(strict=True)
                with sqlite3.connect(receipt_db.as_uri()+'?mode=ro',uri=True) as db:
                    row=db.execute('SELECT role_id,session_id,role_revision FROM role_bindings WHERE task_id=?',(tid,)).fetchone()
                if not row or row[0]!=ROLE or row[1]!='agent-line-'+tid:raise RuntimeError('plugin_binding_identity_mismatch')
                frozen_pins['pluginBinding']=row[2]
                if any(value!=role_revision for value in frozen_pins.values()):raise RuntimeError('frozen_role_revision_mismatch')
                case['frozenRoleRevisions']=frozen_pins;persist()
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
        write(DATA/'report.json',{'status':'passed_live','cases':cases,'nativeCalls':calls,'mainGuardUnchanged':True,'activeLeaseCount':0,'ownedActiveContainerCount':0,'roleRevisionFrozenEndToEnd':True,'automaticArtifactVerificationValidated':True,'testExecutionValidated':test_commands,
            **({'publicationCapabilityCheck':state['publicationCapabilityCheck']} if publication_capabilities else {}),
            'limitations':['Only three frozen addition unittest cases; not full product acceptance' if test_commands else 'Byte policies only, not general test-suite execution','Native ReMe may update normally; no manual writeback/configuration change','One active independent test cancellation' if test_cancel else 'One intentional independent test failure' if test_failure else 'One positive pinned task; no new fault matrix']})
        state['result']='passed_live';persist();return 0
    except Exception as exc:
        state['failure']={'type':type(exc).__name__,'code':str(exc) if type(exc) is RuntimeError else 'probe_failed'};persist();write(DATA/'failure.json',{'status':'incomplete',**state['failure'],'nativeCalls':calls,'nativeResponses':responses});print(json.dumps({'status':'incomplete',**state['failure']}),flush=True);return 1
    finally:
        if gateway is not None:gateway.close()
        if host is not None:host.close()
        sim.close()


def arguments(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--test-commands',action='store_true')
    parser.add_argument('--reuse-known-template',action='store_true')
    parser.add_argument('--test-failure',action='store_true')
    parser.add_argument('--test-cancel',action='store_true')
    parser.add_argument('--publication-capabilities',action='store_true',
                        help='Verify current publication gate with a fresh schema-two real workflow')
    options=vars(parser.parse_args(argv))
    try:
        probe_directory(ROOT,**options)
    except ValueError as error:
        parser.error(str(error))
    return options


if __name__=='__main__':
    raise SystemExit(main(**arguments()))
