#!/usr/bin/env python3
"""Real Sim, no-model human approvals through the extension HTTP boundary."""
import json
import os
from pathlib import Path
import secrets
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from fastapi.testclient import TestClient
from probes.sim.workflow_probe import Probe, block, edge, response, write_private
from agent_line.adapters.sim import SimClient
from agent_line.approvals import ApprovalBinding, ApprovalCoordinator, ApprovalLedger, TrustedPrincipal
from agent_line.approval_api import ApprovalAccess, ApprovalAPI
from agent_line.service import create_app, ServiceConfig
from agent_line.sim_journal import SimJournal
from agent_line.store import TaskStore


class CountedSim(SimClient):
    def __init__(self,*args):super().__init__(*args);self.resume_calls=0
    def resume(self,*args):self.resume_calls+=1;return super().resume(*args)


def main(directory=None):
    directory=directory or ROOT/'var'/'approvals-live'
    directory.mkdir(mode=0o700,exist_ok=True)
    probe=Probe(ROOT/'var/sim/probe-auth.json',directory/'state.json')
    state=probe.state
    if 'prefix' not in state:
        state.update(prefix='approval-'+uuid.uuid4().hex[:12],expiresAt=time.time()+86400)
        probe.save()
    prefix=state['prefix']; project='approval-validation'
    authpath=directory/'credentials.json'
    if not authpath.exists():write_private(authpath,{'human':secrets.token_urlsafe(32),'other':secrets.token_urlsafe(32),'service':secrets.token_urlsafe(32)})
    auth=json.loads(authpath.read_text())
    created=probe.once('create','POST','/api/v2/workflows',{'workspaceId':probe.workspace,'name':'Agent Line 人工审批 HTTP 验收','description':'Isolated no-model no-notification approve/reject graph'})
    wid=created['id'];base='/api/v2/workflows/'+wid
    start=block(prefix+'-start','start_trigger');start['name']='start'
    human=block(prefix+'-approval','human_in_the_loop_v2',{'inputFormat':('input-format',[{'id':'approval','name':'approval','type':'object','value':{}}]),'notification':('tool-input',[]),'builderData':('response-format',[])},250);human['name']='approval'
    condition=block(prefix+'-condition','condition',{'conditions':('condition-input',json.dumps([{'id':'yes','title':'if','value':'<approval.resumeInput.approval.approved> === true'},{'id':'no','title':'else','value':''}]))},500);condition['name']='decision'
    approved=response(prefix+'-approved',{'decision':'approved'});rejected=response(prefix+'-rejected',{'decision':'rejected'},200)
    blocks=[start,human,condition,approved,rejected]
    graph={'blocks':{b['id']:b for b in blocks},'edges':[edge(start['id'],human['id']),edge(human['id'],condition['id']),edge(condition['id'],approved['id'],'condition-yes'),edge(condition['id'],rejected['id'],'condition-no')],'loops':{},'parallels':{},'variables':{}}
    saved=probe.once('state','PUT',base+'/state',graph)
    if saved.get('warnings'):raise RuntimeError('Graph normalization warnings; inspect private state')
    deployed=probe.once('deploy','POST',base+'/deploy',{})
    state['workflowId']=wid;probe.save()
    revision=deployed.get('version') or deployed.get('versionId') or deployed.get('deploymentId')
    if revision is None:
        raise RuntimeError('Deployment revision unavailable; inspect deployment response keys')
    state['revision']=str(revision);probe.save()
    paths={name:directory/(name+'.sqlite') for name in ('approvals','journal','tasks')}
    ledger=ApprovalLedger(paths['approvals']) if paths['approvals'].exists() else ApprovalLedger.initialize(paths['approvals'])
    journal=SimJournal(paths['journal']) if paths['journal'].exists() else SimJournal.initialize(paths['journal'])
    tasks=TaskStore(paths['tasks']) if paths['tasks'].exists() else TaskStore.initialize(paths['tasks'])
    config=json.loads((ROOT/'var/sim/probe-auth.json').read_text())
    sim=CountedSim(config['baseUrl'],config['apiKey'])
    access=ApprovalAccess({auth['human']:TrustedPrincipal('approval-probe-human',frozenset({project}),frozenset({'approve'}),state['expiresAt']),auth['other']:TrustedPrincipal('approval-probe-other',frozenset({'unrelated-project'}),frozenset({'approve'}),state['expiresAt'])})
    api=ApprovalAPI(access,ApprovalCoordinator(ledger,sim,journal,authorize=access.authorize))
    app=create_app(tasks,ServiceConfig(tokens={auth['service']:{project}},projects={project:{'workspace':str(directory)}},bindings={}),approvals=api)
    results=[]
    try:
        with TestClient(app) as client:
            for choice in ('approve','reject'):
                run=state['runs'].setdefault(choice,{'workflowId':wid,'runId':str(uuid.uuid4()),'approvalId':prefix+'-'+choice,'decisionId':prefix+'-decision-'+choice})
                probe.save()
                probe.once(choice+'-execute','POST',base+'/execute',{'input':{}},{'X-Run-Id':run['runId']})
                observed=probe.observe(choice)
                if 'binding' not in run:
                    pause=observed.get('paused',{})
                    if observed.get('status')!='paused' or pause.get('pauseKind')!='human':raise RuntimeError('Expected exact live human pause')
                    run['binding']={'approval_id':run['approvalId'],'project_id':project,'workflow_id':wid,'run_id':run['runId'],'block_id':pause['blockedOnBlockId'],'context_id':pause['contextId'],'revision':state['revision'],'activation_id':run['runId'],'attempt_id':'one','purpose':'human_approval','expires_at':state['expiresAt']}
                    probe.save()
                ledger.bind(ApprovalBinding(**run['binding']))
                path='/extension/v1/approvals/'+run['approvalId'];headers={'Authorization':'Bearer '+auth['human']}
                if client.get(path,headers={'Authorization':'Bearer '+auth['other']}).status_code!=404:raise RuntimeError('Cross-project disclosure')
                if client.post(path+'/decision',headers={'Authorization':'Bearer '+auth['other']},json={'decisionId':'unauthorized','decision':'approve'}).status_code!=404:raise RuntimeError('Cross-project mutation allowed')
                before=sim.resume_calls
                if not run.get('decisionRequested'):
                    run['decisionRequested']=True;probe.save()
                    body={'decisionId':run['decisionId'],'decision':choice}
                    decision=client.post(path+'/decision',headers=headers,json=body)
                    run['decisionHTTP']=decision.status_code;run['decisionResponse']=decision.json();probe.save()
                    if decision.status_code!=200:raise RuntimeError('Decision HTTP failed; observe original ID only')
                    replay=client.post(path+'/decision',headers=headers,json=body)
                    if replay.status_code!=200 or sim.resume_calls-before!=1:raise RuntimeError('Decision replay was not exactly-once delivery')
                for _ in range(20):
                    observed=probe.observe(choice)
                    if observed.get('status')=='completed':break
                    time.sleep(0.5)
                expected='approved' if choice=='approve' else 'rejected'
                if observed.get('status')!='completed' or observed.get('output',{}).get('data')!={'decision':expected}:raise RuntimeError('Original run did not complete expected approval branch')
                detail=client.get(path,headers=headers)
                if detail.status_code!=200:raise RuntimeError('Approval status query failed')
                results.append({'choice':choice,'approvalId':run['approvalId'],'runId':run['runId'],'completedOutput':observed['output'],'apiStatus':detail.json(),'additionalResumeCalls':sim.resume_calls-before})
        report={'status':'passed_live','workflowId':wid,'revision':state['revision'],'deployment':deployed,'results':results,'totalResumeCallsThisInvocation':sim.resume_calls,'scope':'real Sim through ApprovalAPI TestClient; no browser verification'}
        report_path=directory/'report.json'
        previous=json.loads(report_path.read_text()) if report_path.exists() else None
        history=(previous.get('invocations', []) or [previous]) if previous else []
        report['invocations']=history+[{'time':time.time(),'totalResumeCallsThisInvocation':sim.resume_calls,'results':results}]
        write_private(report_path,report)
        print(json.dumps({'status':report['status'],'workflowId':wid,'resumeCalls':sim.resume_calls,'report':str(directory/'report.json')}))
    finally:sim.close();probe.client.close()


if __name__=='__main__':main()
