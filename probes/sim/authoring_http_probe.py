#!/usr/bin/env python3
"""Real HTTP API via TestClient, real Sim transport, isolated durable journals."""
import json
import os
from pathlib import Path
import secrets
import sys
import time
import uuid
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from fastapi.testclient import TestClient
from agent_line.adapters.sim import SimClient
from agent_line.runtime import load_app
from agent_line.store import TaskStore
from agent_line.sim_journal import SimJournal
from probes.sim.authoring_probe import write


def main():
    directory=ROOT/'var/sim-authoring-http';directory.mkdir(mode=0o700,exist_ok=True)
    workspace=directory/'workspace';workspace.mkdir(mode=0o700,exist_ok=True)
    statepath=directory/'state.json';configpath=directory/'service.json'
    native_auth=ROOT/'var/sim/probe-auth.json'
    if native_auth.stat().st_mode & 0o077:raise RuntimeError('credential_permissions')
    auth=json.loads(native_auth.read_text());project='authoring-http-probe'
    if not configpath.exists():
        # Explicit initialization, never overwrite an existing journal/database.
        if (directory/'tasks.sqlite').exists() or (directory/'sim.sqlite').exists():raise RuntimeError('partial_initialization')
        TaskStore.initialize(directory/'tasks.sqlite');SimJournal.initialize(directory/'sim.sqlite')
        human=secrets.token_urlsafe(40);service=secrets.token_urlsafe(40)
        write(directory/'authoring.json',{'schemaVersion':1,'credentials':[{'token':human,'subject':'authoring-http-probe','projectScopes':[project],'capabilities':['edit_workflows','publish_workflows'],'expiresAt':time.time()+86400*7}],'workspaces':{project:auth['workspaceId']}})
        write(configpath,{'schemaVersion':1,'database':'tasks.sqlite','tokens':{service:[project]},'projects':{project:{'workspace':str(workspace),'name':'隔离搭建验收'}},'bindings':{},'authoringFile':'authoring.json','sim':{'authFile':str(native_auth),'journal':'sim.sqlite','installationId':'authoring-http-probe','bindings':{}}})
        for name in ('tasks.sqlite','sim.sqlite'):os.chmod(directory/name,0o600)
    credential=json.loads((directory/'authoring.json').read_text())['credentials'][0]['token']
    state=json.loads(statepath.read_text()) if statepath.exists() else {'requests':{},'receipts':{}}
    calls=[];sessions=[];invocation=uuid.uuid4().hex
    class CountedSim(SimClient):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs);sessions.append(self)
            self._client.event_hooks['request'].append(lambda r:calls.append({'method':r.method,'path':r.url.path}))
    base='/extension/v1/authoring/projects/'+project
    headers={'Authorization':'Bearer '+credential}
    def read(http,path):
        response=http.get(path,headers=headers)
        if response.status_code!=200:raise RuntimeError('http_read_failed')
        return response.json()
    def submit(http,key,path,body):
        if key not in state['requests']:
            state['requests'][key]={'path':path,'body':body,'intent':True};write(statepath,state)
        else:
            old=state['requests'][key]
            if old['path']!=path or old['body']!=body:raise RuntimeError('request_identity_changed')
            if key not in state['receipts']:
                observed=read(http,base+'/operations/'+body['operationId']+'?kind='+key)
                state.setdefault('uncertainObservations',{})[key]=observed;write(statepath,state)
                if observed.get('status')!='accepted':raise RuntimeError('prior_intent_unconfirmed')
                state['receipts'][key]=observed;write(statepath,state)
                return observed
        response=http.post(path,headers=headers,json=body)
        if response.status_code!=202:raise RuntimeError('http_write_receipt_failed')
        data=response.json()
        state['receipts'][key]=data;write(statepath,state)
        if data.get('status')!='accepted':raise RuntimeError('outcome_not_accepted')
        return data
    def require(value,code):
        if not value:raise RuntimeError(code)
    def node(i,kind,name,config,x,y=0):return {'id':i,'type':kind,'name':name,'position':{'x':x,'y':y},'config':config}
    doc={'schemaVersion':1,'nodes':[node('start','start','开始',{},0),node('condition','condition','条件',{'rules':[{'id':'yes','operator':'eq','field':'input.answer','value':True}]},250),node('yes','end','通过',{'output':{'结果':'通过'}},500),node('no','end','未通过',{'output':{'结果':'未通过'}},500,250)],'edges':[{'id':'a','source':'start','target':'condition'},{'id':'b','source':'condition','target':'yes','branch':'yes'},{'id':'c','source':'condition','target':'no','branch':'else'}],'variables':{}}
    try:
        with patch('agent_line.adapters.sim.SimClient',CountedSim):
            with TestClient(load_app(configpath)) as http:
                projects=read(http,'/extension/v1/authoring/projects')
                require(projects['canPublish'] is True,'publish_permission_missing')
                create_body=state['requests'].get('create',{}).get('body') or {'operationId':'create-'+uuid.uuid4().hex,'name':'agent-line-authoring-http-probe','description':'独立 HTTP 中文搭建验收'}
                created=submit(http,'create',base+'/workflows',create_body)
                require(created['receipt']['outcome']=='created','create_failed')
                wid=created['receipt']['workflowId'];state['workflowId']=wid;write(statepath,state)
                path=base+'/workflows/'+wid
                detail=read(http,path)
                if 'save' not in state['requests']:
                    state['emptyDetail']=detail;write(statepath,state)
                    require(detail['draftEmpty'] is True and detail['document'] is None,'new_draft_not_empty')
                save_body=state['requests'].get('save',{}).get('body') or {'operationId':'save-'+uuid.uuid4().hex,'expectedStateSha256':detail['stateSha256'],'document':doc}
                saved=submit(http,'save',path+'/save',save_body)
                require(saved['receipt']['outcome']=='saved','save_not_verified')
                detail=read(http,path);require(detail['document']==doc and detail['editable'] is True,'roundtrip_changed')
                publish_body=state['requests'].get('publish',{}).get('body') or {'operationId':'publish-'+uuid.uuid4().hex,'expectedStateSha256':detail['stateSha256']}
                published=submit(http,'publish',path+'/publish',publish_body)
                require(published['receipt']['outcome']=='published','publication_not_verified')
                state['detail']=read(http,path);write(statepath,state)
            first_calls=list(calls);before=len(calls)
            with TestClient(load_app(configpath)) as http:
                restarted=read(http,path)
                require(restarted['workflowId']==wid and restarted['document']==doc,'restart_changed')
                for kind in ('create','save','publish'):
                    request=state['requests'][kind];original=state['receipts'][kind]
                    replay=submit(http,kind,request['path'],request['body'])
                    require(replay==original,'replay_receipt_changed')
                    op=read(http,base+'/operations/'+request['body']['operationId']+'?kind='+kind)
                    require(op['receipt']==original['receipt'] and op['status']=='accepted','operation_changed')
                listing=read(http,base+'/workflows')
                require(any(w['id']==wid for w in listing['workflows']),'ownership_missing')
            replay_calls=calls[before:]
            require(not any(c['method']!='GET' for c in replay_calls),'replay_wrote_upstream')
        require(all(s._client.is_closed for s in sessions),'transport_not_closed')
        report={'status':'passed_live','workflowId':wid,'published':published['receipt'],'document':doc,'firstSessionNativeCalls':first_calls,'reopenedSessionNativeCalls':replay_calls,'nativeWritesThisInvocation':sum(c['method']!='GET' for c in calls),'reopenedNativeWrites':0,'allTransportsClosed':True,'httpShapes':{'projects':projects,'create':created,'saved':saved,'detail':restarted,'publish':published,'list':listing},'scope':'real Sim and real load_app HTTP via TestClient; no listener, execution or browser'}
        write(directory/('report-'+invocation+'.json'),report)
        write(directory/'report.json',report)
        print(json.dumps({'status':'passed_live','nativeWritesThisInvocation':report['nativeWritesThisInvocation'],'reopenedNativeWrites':0}))
        return 0
    except Exception as exc:
        write(directory/'failure.json',{'status':'incomplete','type':type(exc).__name__,'code':getattr(exc,'code',str(exc) if type(exc) is RuntimeError else 'probe_check_failed'),'nativeCalls':calls})
        print(json.dumps({'status':'incomplete','type':type(exc).__name__,'code':getattr(exc,'code',str(exc) if type(exc) is RuntimeError else 'probe_check_failed')}))
        return 1
    finally:
        for session in sessions:session.close()


if __name__=='__main__':raise SystemExit(main())
