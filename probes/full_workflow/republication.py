"""Actual main old paused run -> publish new version -> old/new Codex executions.

New --execute alone may create one workflow, publish twice, start twice and
approve its two own test gates. Existing state always observes original IDs;
no replayed mutations, alternate stores, role changes or service restarts.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

import httpx

from agent_line.adapters.sim import SimClient
from agent_line.approval_discovery import WorkflowRunLedger
from agent_line.authoring_api import state_hash
from agent_line.role_dispatch import RoleDispatchLedger
from agent_line.published_workflows import graph_hash
from agent_line.sim_authoring import SimAuthoringClient, compile_document, native_id
from agent_line.sim_journal import SimJournal
from agent_line.store import TaskStore, canonical
from probes.full_workflow.parallel_roles import check_exit
from probes.qwenpaw_memory.native_roundtrip import quiet_host, save

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT/'var/republication-20261022'
BASE = 'http://127.0.0.1:3193'
PROJECT = 'native-memory-roundtrip'
SOURCE = '1b2a278a-f28f-4bb2-b637-f79d045449fc'


def require(condition, code):
    if not condition:
        raise RuntimeError(code)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def original_rows(database, ids=None):
    with sqlite3.connect(Path(database).as_uri()+'?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(row) for row in db.execute('SELECT * FROM tasks ORDER BY id')]
    return [row for row in rows if ids is None or row['id'] in ids]


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def verify_execution_log(log, workflow_id, run_id, publication, output, definition):
    """The durable root log owns deployment, snapshot and final output.

    A resume child has an execution identity but may have no separate /logs row.
    Its absence cannot weaken the positive checks on the original root record.
    """
    require(isinstance(log,dict) and log.get('runId')==run_id and log.get('workflowId')==workflow_id,
            'native_log_identity_mismatch')
    require(log.get('deploymentVersionId')==publication['deploymentVersionId'],'actual_native_version_changed')
    require(log.get('status')=='completed','native_root_not_completed')
    require(definition.get('id')==publication['deploymentVersionId']
            and type(definition.get('version')) is int and definition['version']==publication['revision'],
            'native_definition_identity_mismatch')
    published=definition['state']
    require(published.get('variables')=={},'unsupported_probe_variables')
    require(graph_hash(published,workflow_id)==publication['graphSha256'],'published_snapshot_changed')
    actual=log.get('workflowState')
    fields=('blocks','edges','loops','parallels')
    require(isinstance(actual,dict) and set(actual)==set(fields),'native_log_graph_incomplete')
    # Official log projection omits variables and inserts null edge handles.
    # Compare every other graph field exactly; do not discard unknown metadata.
    def graph(state):
        result={key:copy.deepcopy(state[key]) for key in fields}
        require(isinstance(result['edges'],list),'native_log_edges_invalid')
        for edge in result['edges']:
            require(isinstance(edge,dict),'native_log_edges_invalid')
            for key in ('sourceHandle','targetHandle'):
                if edge.get(key) is None:edge.pop(key,None)
        return result
    require(graph(actual)==graph(published),'actual_native_graph_changed')
    require(isinstance(log.get('finalOutput'),dict) and log['finalOutput'].get('data')==output,
            'native_root_final_output_mismatch')
    return True


def load():
    cfg = json.loads((ROOT/'var/adapter/service.json').read_text())
    author = json.loads(Path(cfg['authoringFile']).read_text())
    human = json.loads(Path(cfg['approvalFile']).read_text())
    execution = json.loads(Path(cfg['executionFile']).read_text())['gateways'][0]
    task_flow = json.loads(Path(cfg['taskFlowFile']).read_text())
    sim_auth = json.loads(Path(cfg['sim']['authFile']).read_text())
    sim = SimClient(sim_auth['baseUrl'], sim_auth['apiKey'], client=httpx.Client(timeout=20, trust_env=False))
    native = SimAuthoringClient(sim, author['workspaces'][PROJECT])
    def credential(config):
        return next(c['token'] for c in config['credentials']
                    if PROJECT in c['projectScopes'] and c['expiresAt'] > time.time())
    tokens = dict(editor=credential(author), human=credential(human),
                  service=next(k for k, scopes in cfg['tokens'].items() if PROJECT in scopes))
    return cfg, execution, task_flow, native, sim, tokens


def document(agent_config, tag, allow_failure):
    def node(i, kind, name, config, x, y=100):
        return dict(id=i,type=kind,name=name,config=config,position=dict(x=x,y=y))
    agent = copy.deepcopy(agent_config)
    agent['instruction'] = 'Workflow template snapshot: '+tag+'. Keep the following original task requirements.\n'+agent['instruction']
    agent['allowFailureBranch'] = allow_failure
    agent['inputFrom'] = dict(nodeId='start',field='input')
    nodes = [node('start','start','开始',{},0),
             node('approval','human','发布中等待确认',{'requiredBoolean':'approved'},190),
             node('agent','agent','原生角色执行',agent,380),
             node('condition','condition','测试验收分支',{'rules':[dict(id='passed',operator='eq',
                   source='agent',field='result.verificationStatus',value='passed')]},570),
             node('success','end','验收通过',{'output':dict(templateMarker=tag,allowFailureBranch=allow_failure)},770,10),
             node('failure','end','验收失败',{'output':dict(templateMarker=tag,failure=True)},770,220)]
    edges = [dict(id='a',source='start',target='approval'),dict(id='b',source='approval',target='agent'),
             dict(id='c',source='agent',target='condition'),dict(id='d',source='condition',target='success',branch='passed'),
             dict(id='e',source='condition',target='failure',branch='else')]
    return dict(schemaVersion=1,nodes=nodes,edges=edges,variables={})


class Probe:
    def __init__(self, state, cfg, execution, task_flow, native, sim, tokens):
        self.state, self.cfg, self.execution = state, cfg, execution
        self.native, self.sim, self.tokens = native, sim, tokens
        self.http = httpx.Client(timeout=60, trust_env=False)
        self.tasks = TaskStore(Path(cfg['database']))
        self.roles = RoleDispatchLedger(Path(execution['ledgerPath']))
        self.runs = WorkflowRunLedger(Path(task_flow['runLedgerPath']))
        self.journal = SimJournal(Path(cfg['sim']['journal']))

    def persist(self):
        save(DATA/'state.json',self.state)

    def get(self, path, authority='service', **kwargs):
        response = self.http.get(BASE+path,headers={'Authorization':'Bearer '+self.tokens[authority]},**kwargs)
        require(response.status_code==200,'public_get_unavailable')
        return response.json()

    def once(self, key, path, body, authority):
        require(key not in self.state['steps'],'existing_mutation_intent_must_only_be_observed')
        entry = dict(status='intent',path=path,body=body,authority=authority,at=time.time())
        self.state['steps'][key] = entry
        self.persist()
        response = self.http.post(BASE+path,json=body,headers={'Authorization':'Bearer '+self.tokens[authority]})
        entry.update(status='received',httpStatus=response.status_code,response=response.json())
        self.persist()
        require(response.status_code in (200,202),'public_mutation_not_accepted')
        return entry['response']

    def author(self, key, suffix, body):
        operation = self.once(key,'/extension/v1/authoring/projects/'+PROJECT+'/workflows'+suffix,
                              dict(operationId=key+'-'+self.state['suffix'],**body),'editor')
        require(operation['status']=='accepted','authoring_outcome_unknown')
        return operation['receipt']

    def task_for(self, run):
        items = self.tasks.list_tasks({PROJECT},limit=500)['tasks']
        tasks = [item for item in items if item['payload']['workflow']['id']==self.state['workflowId']
                 and item['payload']['workflow']['runId']==run['runId']]
        require(len(tasks)<=1,'multiple_tasks_for_original_run')
        return tasks[0] if tasks else None

    def wait_approval(self, run, seconds=180):
        end = time.monotonic()+seconds
        while time.monotonic()<end:
            listing = self.get('/extension/v1/approvals',authority='human',params={'limit':500})
            found = [item for item in listing['approvals'] if item['workflowId']==self.state['workflowId']
                     and item['runId']==run['runId']]
            require(len(found)<=1,'multiple_own_approvals')
            if found:
                detail = self.get('/extension/v1/approvals/'+found[0]['approvalId'],authority='human')
                if detail.get('boundPauseObserved') is True:
                    require(detail['blockId']==native_id(self.state['workflowId'],'approval'),'wrong_human_pause')
                    require(self.task_for(run) is None,'machine_started_before_human_approval')
                    run['approval']=detail
                    self.persist()
                    return
            time.sleep(1)
        raise RuntimeError('original_human_pause_not_observed')

    def start(self, name, publication, *, idempotency_suffix=''):
        body = dict(idempotencyKey='template-republication-'+self.state['suffix']+idempotency_suffix,
                    input={'input':{'purpose':'原运行版本冻结，两个版本使用同一输入'}})
        result = self.once('start-'+name,'/extension/v1/workflows/'+publication['bindingId']+'/runs',body,'service')
        require(result['status']=='accepted','start_outcome_unknown')
        run = dict(runId=result['runId'],bindingId=publication['bindingId'],publication=publication)
        self.state['runs'][name] = run
        self.persist()
        self.wait_approval(run)
        return run

    def approve(self, name, run):
        decision = self.once('approve-'+name,'/extension/v1/approvals/'+run['approval']['approvalId']+'/decision',
                             dict(decisionId='republication-'+name+'-'+self.state['suffix'],decision='approve'),'human')
        require(decision.get('deliveryStatus')=='accepted','approval_delivery_unknown')
        run['decision']=decision
        self.persist()

    def wait_complete(self, name, run):
        end, last = time.monotonic()+1200, 0
        while time.monotonic()<end:
            task = self.task_for(run)
            if task:
                run['taskId']=task['id']
                run['lastTaskStatus']={key:task[key] for key in ('executionStatus','verificationStatus','resourcesReleased')}
                self.persist()
                require(task['executionStatus'] not in ('failed','canceled'),'original_task_failed')
            public = self.get('/extension/v1/workflows/'+run['bindingId']+'/runs/'+run['runId'])
            run['lastPublicRun']=public
            self.persist()
            actual = public.get('resumption',{}).get('execution',public['execution'])
            if task and task['executionStatus']=='succeeded' and task['verificationStatus']=='passed' and task['resourcesReleased'] and actual['status']=='completed':
                return self.verify_run(name,run,task,actual)
            if time.monotonic()-last>30:
                print(json.dumps(dict(phase='observing_original_run',case=name,runId=run['runId'],
                                     taskId=task['id'] if task else None,taskStatus=task['executionStatus'] if task else None,
                                     simStatus=actual['status'])),flush=True)
                last=time.monotonic()
            time.sleep(2)
        raise RuntimeError('original_run_observation_deadline')

    def verify_run(self, name, run, task, actual):
        wid = self.state['workflowId']
        expected_doc = self.state['documents'][name]
        pub = run['publication']
        frozen = self.runs.get(wid,run['runId'])
        public_snapshot = self.journal.get_publication(wid,run['runId'])
        require(public_snapshot['publication']['deploymentVersionId']==pub['deploymentVersionId'],'frozen_publication_changed')
        require(frozen['deploymentVersionId']==pub['deploymentVersionId'] and frozen['revision']==str(pub['revision']),'frozen_run_version_changed')
        policy = frozen['agentPolicies'][native_id(wid,'agent')]
        expected_agent = next(node for node in expected_doc['nodes'] if node['type']=='agent')['config']
        require(policy['instruction']==expected_agent['instruction'] and policy['allowFailureBranch']==expected_agent['allowFailureBranch'],'frozen_node_policy_changed')
        require(task['payload']['input']['instruction']==policy['instruction'],'task_used_other_template')
        require(task['payload']['roleRevision']==policy['roleRevision']==self.cfg['bindings']['native-loop-parent']['roleRevision'],'role_pin_changed')
        require(task['payload']['verificationPolicy']==policy['verificationPolicy'],'verification_policy_changed')
        output = actual['response']['data'].get('output',{}).get('data')
        expected_output=next(node for node in expected_doc['nodes'] if node['id']=='success')['config']['output']
        require(output==expected_output,'wrong_actual_template_end')
        final_id=actual['response']['data']['runId']
        selectors=native_id(wid,'condition')+','+native_id(wid,'success')+'.data'
        response=self.sim._client.get(self.sim.base_url+f'/api/v2/workflows/{wid}/runs/{final_id}',
                      params={'selectedOutputs':selectors},headers={'x-api-key':self.sim._key})
        require(response.status_code==200,'actual_end_unavailable')
        selected=response.json()['data']
        require(selected.get('workflowId')==wid and selected.get('runId')==final_id
                and selected.get('status')=='completed','actual_end_identity_mismatch')
        blocks=selected['blockOutputs']
        require(blocks[native_id(wid,'condition')]['selectedPath']['blockId']==native_id(wid,'success')
                and blocks[native_id(wid,'success')+'.data']==expected_output,'actual_success_branch_unproven')
        log=self.sim._client.get(self.sim.base_url+'/api/v2/logs/'+run['runId'],
                params={'includeWorkflowState':'true'},headers={'x-api-key':self.sim._key})
        require(log.status_code==200,'native_root_log_unavailable')
        root_log=log.json()['data']
        definition=self.native.get_version(wid,pub['revision'])
        verify_execution_log(root_log,wid,run['runId'],pub,expected_output,definition)
        save(DATA/(name+'-native-definition.json'),definition)
        save(DATA/f'{name}-native-log-0.json',root_log)
        child=self.sim._client.get(self.sim.base_url+'/api/v2/logs/'+final_id,
                params={'includeWorkflowState':'true'},headers={'x-api-key':self.sim._key})
        require(child.status_code in (200,404),'native_child_log_unavailable')
        if child.status_code==200:
            child_log=child.json()['data']
            require(child_log.get('deploymentVersionId')==pub['deploymentVersionId'],'native_child_version_mismatch')
            save(DATA/f'{name}-native-log-1.json',child_log)
        save(DATA/(name+'-child-log-observation.json'),dict(runId=final_id,httpStatus=child.status_code,
             rootLogAuthoritative=True,rootGraphAndVersionAndFinalOutputVerified=True))
        bridge=json.loads((ROOT/'var/qwenpaw/config.json').read_text())['plugins']['agent-line']['execution_bridge']
        check_exit(task,bridge)
        parent=self.roles.get(task['id'])
        token=json.loads((DATA/'host-token.json').read_text())['token']
        deadline=time.monotonic()+240
        while time.monotonic()<deadline:
            observed=self.http.get(self.execution['baseURL']+'/api/console/chat/task/'+parent['parentTaskId'],
                         headers={'Authorization':'Bearer '+token,'X-Agent-Id':'agent-line-native-loop-parent'})
            require(observed.status_code==200,'original_parent_unavailable')
            parent_observation=observed.json()
            if parent_observation.get('status')=='finished':break
            time.sleep(2)
        else:raise RuntimeError('original_parent_not_finished')
        require(parent_observation.get('result',{}).get('status')=='completed','original_parent_not_completed')
        save(DATA/(name+'-parent.json'),parent_observation)
        save(DATA/(name+'-task.json'),task)
        save(DATA/(name+'-frozen-run.json'),frozen)
        save(DATA/(name+'-actual-end.json'),response.json())
        run.update(result='passed_actual',finalRunId=final_id,actualEndOutput=output,parentFinished=True,
                   frozenNodePolicyMatches=True,independentVerification='passed',resourcesReleased=True)
        self.persist()
        print(json.dumps(dict(case=name,result=run['result'],taskId=task['id'],nativeParentFinished=True)),flush=True)

    def observe(self):
        result={'mode':'observe_original_only','newSubmissions':0,'runs':{}}
        for name,run in self.state.get('runs',{}).items():
            public=self.get('/extension/v1/workflows/'+run['bindingId']+'/runs/'+run['runId'])
            task=self.task_for(run)
            result['runs'][name]=dict(runId=run['runId'],taskId=task['id'] if task else None,
                 taskStatus=task['executionStatus'] if task else None,public=public)
        save(DATA/'observation.json',result)
        print(json.dumps({'mode':result['mode'],'newSubmissions':0,
                          'tasks':{name:run['taskStatus'] for name,run in result['runs'].items()}}),flush=True)

    def close(self):
        self.http.close()
        self.sim._client.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute',action='store_true')
    args=parser.parse_args()
    cfg,execution,flow,native,sim,tokens=load()
    state_path=DATA/'state.json'
    if state_path.exists():
        probe=Probe(json.loads(state_path.read_text()),cfg,execution,flow,native,sim,tokens)
        try:probe.observe()
        finally:probe.close()
        return
    if not args.execute:
        sim._client.close()
        print(json.dumps(dict(mode='not_started',newSubmissions=0)))
        return
    DATA.mkdir(mode=0o700,exist_ok=False)
    state={'phase':'prepared','suffix':uuid.uuid4().hex[:16],'steps':{},'runs':{},'documents':{},'createdAt':time.time()}
    probe=Probe(state,cfg,execution,flow,native,sim,tokens)
    try:
        health=probe.get('/extension/v1/health')
        require(health['readyForExecution'] is True,'main_not_ready')
        with probe.tasks.connect() as db:require(db.execute('SELECT count(*) FROM workspace_leases').fetchone()[0]==0,'main_not_idle')
        host=json.loads((ROOT/'var/qwenpaw/agent-line-process.json').read_text())
        quiet_host(host['pid'])
        baseline=json.loads((ROOT/'var/inflight-adapter-restart-20261022/runtime/state.json').read_text())
        state['configurationHashes']={path:sha(path) for path in baseline['configurationHashes']}
        before=original_rows(cfg['database'])
        state.update(oldTaskIds=[row['id'] for row in before],oldTaskRowsSha256=fingerprint(before),host=host)
        source=probe.get('/extension/v1/authoring/projects/'+PROJECT+'/workflows/'+SOURCE,'editor')
        agent=next(node for node in source['document']['nodes'] if node['type']=='agent')['config']
        for name,allow_failure in (('v1',False),('v2',True)):
            state['documents'][name]=document(agent,'SNAPSHOT_'+name.upper()+'_'+state['suffix'],allow_failure)
            compile_document(state['documents'][name],'preflight-snapshot')
        probe.persist()
        login=probe.http.post(execution['baseURL']+'/api/auth/login',
                              json=json.loads(Path(execution['hostCredentialsFile']).read_text()))
        require(login.status_code==200,'host_auth_unavailable')
        token=login.json().get('token') or login.json().get('access_token')
        require(isinstance(token,str) and bool(token),'host_auth_invalid')
        save(DATA/'host-token.json',{'token':token})
        created=probe.author('create','',dict(name='运行版本冻结验收-'+state['suffix'],description='自有测试流程：旧运行保持v1，新运行使用v2，不修改角色或原生记忆'))
        require(created['outcome']=='created','create_not_confirmed')
        wid=created['workflowId'];state['workflowId']=wid;probe.persist()
        def publish(name):
            saved=probe.author('save-'+name,'/'+wid+'/save',dict(expectedStateSha256=state_hash(native.get_state(wid)),document=state['documents'][name]))
            require(saved['outcome']=='saved','save_not_confirmed')
            publication=probe.author('publish-'+name,'/'+wid+'/publish',dict(expectedStateSha256=state_hash(native.get_state(wid))))
            require(publication['outcome']=='published','publish_not_confirmed')
            state[name+'Publication']=publication;probe.persist()
            return publication
        first=publish('v1')
        old=probe.start('v1',first)
        state['phase']='old_run_waiting_before_new_publication';probe.persist()
        second=publish('v2')
        require(native.get_version(wid,first['revision'])['isActive'] is False,'old_version_still_active')
        require(native.get_version(wid,second['revision'])['isActive'] is True,'new_version_not_active')
        state['phase']='v2_published_while_v1_waiting';probe.persist()
        probe.approve('v1',old)
        probe.wait_complete('v1',old)
        new=probe.start('v2',second)
        probe.approve('v2',new)
        probe.wait_complete('v2',new)
        require(fingerprint(original_rows(cfg['database'],state['oldTaskIds']))==state['oldTaskRowsSha256'],'old_task_rows_changed')
        require(all(sha(path)==value for path,value in state['configurationHashes'].items()),'configuration_changed')
        with probe.tasks.connect() as db:require(db.execute('SELECT count(*) FROM workspace_leases').fetchone()[0]==0,'leases_remain')
        quiet_host(host['pid'])
        state['phase']='passed_actual_republication';probe.persist()
        save(DATA/'report.json',dict(status=state['phase'],workflowId=wid,runs=state['runs'],
             oldTaskRowsUnchanged=True,configurationUnchanged=True,activeLeaseCount=0,
             roleAgentId='agent-line-native-loop-parent',nativeMemoryBackendUnchanged=True,
             independentHumanCredentialUsedForOwnedTestGates=True,mainServiceRestarted=False,
             limits=['controlled deployed human pauses only','not arbitrary historical-version start or external-editor atomic pin',
                     'no native role configuration change or transparent restart continuation','not complete PRD acceptance']))
        print(json.dumps(dict(phase=state['phase'],oldVersionContinued=True,newRunUsedNewVersion=True)),flush=True)
    except Exception as error:
        state['errorType']=type(error).__name__
        state['errorCode']=str(error) if type(error) is RuntimeError else 'probe_failed'
        probe.persist()
        print(json.dumps(dict(phase=state['phase'],status='incomplete_observe_original',errorType=state['errorType'],code=state['errorCode'])),flush=True)
        raise SystemExit(1)
    finally:
        probe.close()


if __name__=='__main__':
    main()
