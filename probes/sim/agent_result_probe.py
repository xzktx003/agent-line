#!/usr/bin/env python3
"""New no-model Sim result branches; supplied machine results are not Codex proof."""
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from agent_line.adapters.sim import SimClient
from agent_line.adapters.sim_wait import SimWaitClient
from agent_line.sim_authoring import SimAuthoringClient,compile_document,decode_document,native_id
from probes.sim.authoring_probe import write

DATA=ROOT/'var/sim-agent-result'


def main():
    DATA.mkdir(mode=0o700,exist_ok=True);path=DATA/'state.json';authpath=ROOT/'var/sim/probe-auth.json'
    if authpath.stat().st_mode & 0o077:raise RuntimeError('credential_permissions')
    auth=json.loads(authpath.read_text());sim=SimClient(auth['baseUrl'],auth['apiKey']);api=SimAuthoringClient(sim,auth['workspaceId'])
    calls=[];sim._client.event_hooks['request'].append(lambda request:calls.append({'method':request.method,'path':request.url.path}))
    if path.exists() and '--continue-known' not in sys.argv:
        try:
            old=json.loads(path.read_text());observed={}
            for name,run in old.get('runs',{}).items():
                if old.get('workflowId'):
                    observed[name]={'parent':sim.observe(old['workflowId'],run['runId'])}
                    if run.get('resumedRunId'):observed[name]['resumed']=sim.observe(old['workflowId'],run['resumedRunId'])
            report={'mode':'observation_only','nativeWritesThisInvocation':0,'nativeCalls':calls,'observations':observed,'priorResult':old.get('result','incomplete')}
            write(DATA/'observation.json',report);print(json.dumps({'mode':'observation_only','nativeWritesThisInvocation':0,'runCount':len(observed),'priorResult':report['priorResult']}));return 0
        finally:sim.close()
    state=json.loads(path.read_text()) if path.exists() else {'steps':{},'runs':{},'workspaceId':auth['workspaceId'],'createdAt':time.time()};write(path,state)
    def persist():write(path,state)
    def once(key,payload,operation):
        if key in state['steps']:
            previous=state['steps'][key]
            if previous['status']!='received' or previous['payload']!=payload:raise RuntimeError('existing_intent_no_resubmission')
            return previous['result']
        state['steps'][key]={'status':'intent','payload':payload};persist()
        result=operation();state['steps'][key].update(status='received',result=result);persist();return result
    def poll(wid,rid,expected):
        for _ in range(60):
            result=sim.observe(wid,rid)
            if result['status'] in expected:return result
            time.sleep(0.5)
        raise RuntimeError('observation_pending_original_identity_only')
    def get_outputs(wid,rid,selectors):
        response=sim._client.get(sim.base_url+f'/api/v2/workflows/{wid}/runs/{rid}',params={'selectedOutputs':','.join(selectors)},headers={'x-api-key':sim._key},follow_redirects=False)
        if response.status_code!=200:raise RuntimeError('output_read_unavailable')
        data=response.json()['data']
        if data.get('workflowId')!=wid or data.get('runId')!=rid:raise RuntimeError('run_identity_mismatch')
        return data
    try:
        processes=subprocess.run(['docker','ps','--filter','label=com.docker.compose.project=agent-line','--format','{{.Names}} {{.Status}}'],capture_output=True,text=True,check=True).stdout.splitlines()
        if not any('agent-line-simstudio-1' in item and 'healthy' in item for item in processes):raise RuntimeError('sim_not_healthy')
        state['processCheck']=processes;api.list_workflows(limit=1)
        create={'name':'agent-line-agent-result-probe','description':'No-model if/else-if/else and dynamic result proof; no Codex task'}
        created=once('create',create,lambda:api.create(**create));wid=created['id'];state['workflowId']=wid;persist()
        config={'roleBindingId':'probe-only-unbound','instruction':'本图只验证Sim结果分支，不执行模型。','inputFrom':{'nodeId':'start','field':'input'},'memoryPolicyId':'none','outputSchemaId':'probe-result','requireMemoryCommit':False,'allowFailureBranch':False}
        def node(i,kind,name,c,x,y=0):return {'id':i,'type':kind,'name':name,'position':{'x':x,'y':y},'config':c}
        doc={'schemaVersion':1,'nodes':[node('start','start','开始',{},0),node('agent','agent','机器等待',config,200),node('condition','condition','结果判断',{'rules':[{'id':'passed','operator':'eq','source':'agent','field':'result.verificationStatus','value':'passed'},{'id':'review','operator':'eq','source':'agent','field':'result.output.needsReview','value':True}]},400),node('yes','end','通过结果',{'resultFrom':'agent'},650),node('review','end','复核结果',{'resultFrom':'agent'},650,200),node('otherwise','end','其他结果',{'resultFrom':'agent'},650,400)],'edges':[{'id':'a','source':'start','target':'agent'},{'id':'b','source':'agent','target':'condition'},{'id':'c','source':'condition','target':'yes','branch':'passed'},{'id':'d','source':'condition','target':'review','branch':'review'},{'id':'e','source':'condition','target':'otherwise','branch':'else'}],'variables':{}}
        state['document']=doc;graph=compile_document(doc,wid);persist()
        once('save',graph,lambda:api.save_state(wid,graph));deployed=once('publish',{},lambda:api.deploy(wid));state['deployment']=deployed;persist()
        published=api.get_version(wid,deployed['version']);write(DATA/'published.json',published)
        if decode_document(published['state'])!=doc or published['id']!=deployed['activeDeployment']['deploymentVersionId']:raise RuntimeError('published_graph_mismatch')
        cases=[('if',{'verificationStatus':'passed','output':{'needsReview':False,'nested':{'值':1}}},'yes',None),('else_if',{'verificationStatus':'failed','output':{'needsReview':True,'nested':{'值':2}}},'review',None),('else',{'verificationStatus':'failed','output':{'needsReview':False,'nested':{'值':3}}},'otherwise',None),('missing_status',{'output':{'needsReview':False}},None,'condition_field_missing'),('missing_output_field',{'verificationStatus':'failed','output':{}},None,'condition_field_missing'),('wrong_type',{'verificationStatus':True,'output':{'needsReview':False}},None,'condition_type_mismatch')]
        wait_id=native_id(wid,'agent')
        for name,supplied,end,error in cases:
            run=state['runs'].get(name) or {'runId':str(uuid.uuid4()),'input':{'input':{'case':name,'purpose':'no-model-machine-result'}},'suppliedResult':supplied,'expectedEnd':end,'expectedError':error};state['runs'][name]=run;persist()
            rid=run['runId'];once(name+'-execute',{'runId':rid,'input':run['input']},lambda:sim.execute(wid,rid,run['input']))
            if name+'-resume' not in state['steps']:
                paused=poll(wid,rid,{'paused','failed','completed'});run['pause']=paused;persist()
                resolved=SimWaitClient(sim).read_input(wid,rid,wait_id)
                if resolved['input']!=run['input']['input']:raise RuntimeError('wait_input_mismatch')
                run['resolvedInput']=resolved;persist()
                once(name+'-resume',{'runId':rid,'contextId':resolved['run']['contextId'],'input':supplied},lambda:sim.resume(wid,rid,resolved['run']['contextId'],supplied))
            elif state['steps'][name+'-resume']['status']!='received':raise RuntimeError('existing_intent_no_resubmission')
            resumed_id=state['steps'][name+'-resume']['result']['resumedRunId']
            run['resumedRunId']=resumed_id;persist()
            final=poll(wid,resumed_id,{'completed','failed','cancelled'});run['final']=final
            run['parentFinal']=sim.observe(wid,rid);persist()
            if end:
                output=get_outputs(wid,resumed_id,[native_id(wid,'condition'),native_id(wid,end)+'.data'])
                run['nativeEndOutputs']=output['blockOutputs'];persist()
                expected={'agentLineResult':supplied}
                if final['status']!='completed' or final['response']['data'].get('output',{}).get('data')!=expected:raise RuntimeError('dynamic_result_mismatch')
                blocks=output['blockOutputs'];condition=blocks.get(native_id(wid,'condition'),{})
                if blocks.get(native_id(wid,end)+'.data')!=expected or condition.get('selectedPath',{}).get('blockId')!=native_id(wid,end):raise RuntimeError('branch_identity_mismatch')
                if condition.get('selectedOption')!= {'yes':'passed','review':'review','otherwise':'else'}[end]:raise RuntimeError('branch_option_mismatch')
            else:
                if final['status']!='failed' or error not in final['response']['data'].get('error',{}).get('message',''):raise RuntimeError('invalid_result_not_failed')
                if final['response']['data'].get('output') is not None:raise RuntimeError('invalid_result_reached_end')
                run['nativeEndOutputs']={};persist()
            print(json.dumps({'case':name,'status':final['status'],'branchVerified':end is not None,'failureVerified':error is not None}),flush=True)
        state['result']='passed_live';persist()
        report={'status':'passed_live','workflowId':wid,'version':deployed['version'],'deploymentVersionId':published['id'],'nativeWritesThisInvocation':sum(c['method']!='GET' for c in calls),'nativeCalls':calls,'cumulativeAcknowledgedWrites':len([v for v in state['steps'].values() if v['status']=='received']),'runs':{name:{'runId':r['runId'],'resumedRunId':r['resumedRunId'],'parentStatus':r['parentFinal']['status'],'status':r['final']['status'],'expectedEnd':r['expectedEnd'],'expectedError':r['expectedError'],'endOutputs':r['nativeEndOutputs']} for name,r in state['runs'].items()},'scope':'supplied machine-result data; native Sim execution only; not Codex/verification evidence'}
        write(DATA/'report.json',report);print(json.dumps({'status':'passed_live','nativeWritesThisInvocation':report['nativeWritesThisInvocation']}),flush=True);return 0
    except Exception as exc:
        failure={'status':'incomplete','type':type(exc).__name__,'code':getattr(exc,'code',str(exc) if type(exc) is RuntimeError else 'probe_failed'),'nativeCalls':calls};write(DATA/'failure.json',failure);print(json.dumps({k:failure[k] for k in ('status','type','code')}),flush=True);return 1
    finally:sim.close()


if __name__=='__main__':raise SystemExit(main())
