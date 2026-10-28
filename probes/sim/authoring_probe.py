#!/usr/bin/env python3
"""Isolated, restart-safe public API authoring verification; never replay intents."""
import json
import os
from pathlib import Path
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from agent_line.adapters.sim import SimClient
from agent_line.sim_authoring import SimAuthoringClient, compile_document, decode_document


def write(path,value):
    temporary=path.with_suffix('.tmp')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as out:
        json.dump(value,out,ensure_ascii=False,indent=2);out.flush();os.fsync(out.fileno())
    os.replace(temporary,path)
    fd=os.open(path.parent,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)


def main():
    directory=ROOT/'var/sim-authoring-probe';directory.mkdir(mode=0o700,exist_ok=True)
    configpath=ROOT/'var/sim/probe-auth.json'
    if configpath.stat().st_mode & 0o077:raise RuntimeError('credential_permissions')
    config=json.loads(configpath.read_text());path=directory/'state.json'
    state=json.loads(path.read_text()) if path.exists() else {'workspaceId':config['workspaceId'],'steps':{},'runs':{}}
    if state['workspaceId']!=config['workspaceId']:raise RuntimeError('workspace_changed')
    sim=SimClient(config['baseUrl'],config['apiKey']);api=SimAuthoringClient(sim,config['workspaceId'])
    writes=0
    def once(key,operation):
        nonlocal writes
        old=state['steps'].get(key)
        if old:
            if old['status']=='received':return old['result']
            raise RuntimeError('prior_intent_requires_reconciliation')
        state['steps'][key]={'status':'intent'};write(path,state)
        result=operation();writes+=1
        state['steps'][key]={'status':'received','result':result};write(path,state)
        return result
    try:
        # Authenticated read-only health/access check before first create.
        api.list_workflows(limit=1)
        created=once('create',lambda:api.create('agent-line-authoring-probe','隔离中文搭建验收：无模型、无角色、无记忆操作'))
        wid=created['id'];state['workflowId']=wid;write(path,state)
        def node(i,t,c,x,y=0):return {'id':i,'type':t,'name':{'start':'开始','check':'条件','yes':'通过','no':'未通过'}[i],'position':{'x':x,'y':y},'config':c}
        doc={'schemaVersion':1,'nodes':[node('start','start',{},0),node('check','condition',{'rules':[{'id':'yes','operator':'eq','field':'input.answer','value':True}]},250),node('yes','end',{'output':{'branch':'yes'}},500),node('no','end',{'output':{'branch':'no'}},500,200)],'edges':[{'id':'a','source':'start','target':'check'},{'id':'b','source':'check','target':'yes','branch':'yes'},{'id':'c','source':'check','target':'no','branch':'else'}],'variables':{}}
        graph=compile_document(doc,wid);write(directory/'compiled.json',graph)
        once('save-name-reference-v2',lambda:api.save_state(wid,graph))
        native=api.get_state(wid);write(directory/'readback.json',native)
        decoded=decode_document(native)
        if decoded!=doc:raise RuntimeError('roundtrip_changed')
        deployment=once('deploy-name-reference-v2',lambda:api.deploy(wid))
        versions=api.versions(wid);write(directory/'versions.json',versions)
        version=api.get_version(wid,deployment['version'])
        write(directory/'published.json',version)
        published=version['state']
        if decode_document(published)!=doc:raise RuntimeError('published_graph_changed')
        expected_id=deployment['activeDeployment']['deploymentVersionId']
        if version.get('id')!=expected_id:raise RuntimeError('published_identity_mismatch')
        for label,value in [('yes',{'answer':True}),('no',{'answer':False}),('missing',{}),('wrong_type',{'answer':'true'})]:
            run=state['runs'].setdefault('v2-'+label,{'runId':str(uuid.uuid4())});write(path,state)
            if not run.get('intent'):
                run['intent']=True;write(path,state)
                run['receipt']=sim.execute(wid,run['runId'],{'input':value});writes+=1;write(path,state)
            for _ in range(60):
                observed=sim.observe(wid,run['runId']);run['observed']=observed;write(path,state)
                data=observed['response']['data']
                if data.get('status') in ('completed','failed','cancelled'):break
                time.sleep(0.5)
            if label in ('yes','no'):
                if data.get('status')!='completed' or data.get('output',{}).get('data')!={'branch':label}:raise RuntimeError('branch_not_verified')
            elif data.get('status')!='failed':raise RuntimeError('invalid_input_did_not_fail')
            log=api._request('GET','/'+wid+'/runs/'+run['runId'])['data']
            run['nativeStatus']=log['status'];write(path,state)
        report={'status':'passed_live','workflowId':wid,'deploymentVersionId':expected_id,'version':deployment['version'],'roundtrip':True,'publishedGraphVerified':True,'writesThisInvocation':writes,'runs':{k:{'runId':v['runId'],'status':v['nativeStatus']} for k,v in state['runs'].items() if k.startswith('v2-')},'scope':'public API only; no browser or production admission changes'}
        write(directory/'report.json',report)
        print(json.dumps({'status':'passed_live','writesThisInvocation':writes}))
    except Exception as exc:
        write(directory/'failure.json',{'status':'incomplete','errorType':type(exc).__name__,'code':getattr(exc,'code','probe_check_failed'),'writesThisInvocation':writes})
        print(json.dumps({'status':'incomplete','errorType':type(exc).__name__,'code':getattr(exc,'code','probe_check_failed')}))
        return 1
    finally:sim.close()
    return 0


if __name__=='__main__':raise SystemExit(main())
