#!/usr/bin/env python3
"""A dedicated no-model Human machine-wait; never touches other human pauses."""
import json
from pathlib import Path
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from agent_line.adapters.sim import SimClient
from agent_line.adapters.sim_wait import SimWaitClient
from probes.sim.authoring_probe import write
from probes.sim.workflow_probe import block,edge,response


def main():
    directory=ROOT/'var/sim-agent-wait';directory.mkdir(mode=0o700,exist_ok=True)
    configpath=ROOT/'var/sim/probe-auth.json'
    if configpath.stat().st_mode & 0o077:raise RuntimeError('credential_permissions')
    config=json.loads(configpath.read_text());statepath=directory/'state-v2.json'
    state=json.loads(statepath.read_text()) if statepath.exists() else {'steps':{},'runId':str(uuid.uuid4()),'prefix':'agentwait_'+uuid.uuid4().hex,'workspaceId':config['workspaceId']}
    if state['workspaceId']!=config['workspaceId']:raise RuntimeError('workspace_changed')
    if not statepath.exists() and (directory/'state.json').exists():
        old=json.loads((directory/'state.json').read_text())
        state['steps']['create']=old['steps']['create']
        state['previousRunId']=old['runId']
    write(statepath,state);sim=SimClient(config['baseUrl'],config['apiKey']);calls=[]
    sim._client.event_hooks['request'].append(lambda r:calls.append({'method':r.method,'path':r.url.path}))
    def request(method,path,body):
        r=sim._client.request(method,sim.base_url+path,headers={'x-api-key':sim._key},json=body,follow_redirects=False)
        if r.status_code not in (200,201):raise RuntimeError('native_receipt_unconfirmed')
        payload=r.json()
        if not isinstance(payload,dict) or 'data' not in payload:raise RuntimeError('native_envelope_invalid')
        return payload['data']
    def once(key,operation):
        old=state['steps'].get(key)
        if old:
            if old['status']=='received':return old['result']
            raise RuntimeError('prior_intent_requires_observation')
        state['steps'][key]={'status':'intent'};write(statepath,state)
        result=operation();state['steps'][key]={'status':'received','result':result};write(statepath,state)
        return result
    def observe_until(terminal):
        for _ in range(60):
            data=sim.observe(state['workflowId'],state['runId']);state['lastObservation']=data;write(statepath,state)
            if data['status'] in terminal:return data
            time.sleep(0.5)
        raise RuntimeError('observation_pending')
    try:
        created=once('create',lambda:request('POST','/api/v2/workflows',{'workspaceId':config['workspaceId'],'name':'agent-line-agent-wait-probe','description':'No-model machine wait input and resume proof; not a human approval'}))
        state['workflowId']=created['id'];wid=state['workflowId'];base='/api/v2/workflows/'+wid
        start=block(state['prefix']+'_start','start_trigger',{'runMetadata':('switch',True)});start['name']='开始'
        wait=block(state['prefix']+'_wait','human_in_the_loop_v2',{'inputFormat':('input-format',[{'id':'taskInput','name':'taskInput','type':'object','value':'<开始.input>'}]),'notification':('tool-input',[]),'builderData':('response-format',[])},250);wait['name']='执行等待'
        wait2=block(state['prefix']+'_wait2','human_in_the_loop_v2',{'inputFormat':('input-format',[{'id':'taskInput','name':'taskInput','type':'object','value':'<执行等待.resumeInput.output>'}]),'notification':('tool-input',[]),'builderData':('response-format',[])},500);wait2['name']='后续等待'
        done=response(state['prefix']+'_end',{});done['name']='结束'
        done['subBlocks']['data']['value']='{"result": <后续等待.resumeInput.output>}'
        graph={'blocks':{b['id']:b for b in (start,wait,wait2,done)},'edges':[edge(start['id'],wait['id']),edge(wait['id'],wait2['id']),edge(wait2['id'],done['id'])],'variables':{}}
        state['waitBlockId']=wait['id'];state['graph']=graph;write(statepath,state)
        once('save',lambda:request('PUT',base+'/state',graph))
        once('publish',lambda:request('POST',base+'/deploy',{}))
        inputs={'instruction':'仅验证暂停输入，不调用模型','nested':{'flags':[True,False,None],'count':3,'object':{'name':'中文'}},'items':[{'id':1},{'id':2}]}
        state['expectedInput']=inputs;write(statepath,state)
        if 'execute' not in state['steps']:
            once('execute',lambda:sim.execute(wid,state['runId'],{'input':inputs}))
        # An existing execute intent is never re-sent; the same run is observed.
        if 'resume' not in state['steps']:
            observed=observe_until({'paused','failed','completed'})
            if observed['status']!='paused':raise RuntimeError('expected_pause_missing')
            read=SimWaitClient(sim).read_input(wid,state['runId'],wait['id'])
            state['readInput']=read;write(statepath,state)
            if read['input']!=inputs:raise RuntimeError('parsed_input_mismatch')
            fixed={'output':{'confirmed':True,'message':'固定无模型结果','nested':{'value':7}}}
            state['resumeInput']=fixed;write(statepath,state)
            once('resume',lambda:sim.resume(wid,state['runId'],read['run']['contextId'],fixed))
        if 'resume2' not in state['steps']:
            # Poll for the second context; an accepted first resume may still be queued.
            for _ in range(60):
                observed=sim.observe(wid,state['runId'])
                pause=observed.get('response',{}).get('data',{}).get('paused') or {}
                if pause.get('blockedOnBlockId')==wait2['id']:break
                time.sleep(0.5)
            else:raise RuntimeError('second_pause_missing')
            read2=SimWaitClient(sim).read_input(wid,state['runId'],wait2['id'])
            state['readInput2']=read2;write(statepath,state)
            if read2['input']!=state['resumeInput']['output']:raise RuntimeError('second_input_mismatch')
            fixed2={'output':{'done':True,'来自':'第二节点','nested':{'count':2}}}
            state['resumeInput2']=fixed2;write(statepath,state)
            once('resume2',lambda:sim.resume(wid,state['runId'],read2['run']['contextId'],fixed2))
        final=observe_until({'completed','failed','cancelled'})
        if final['status']!='completed' or final['response']['data'].get('output',{}).get('data')!={'result':state['resumeInput2']['output']}:raise RuntimeError('resume_output_mismatch')
        report={'status':'passed_live','workflowId':wid,'runId':state['runId'],'blockId':wait['id'],'contextId':state['readInput']['run']['contextId'],'inputEqualsExpected':state['readInput']['input']==inputs,'secondInputEqualsPreviousOutput':state['readInput2']['input']==state['resumeInput']['output'],'secondPauseResumedCount':state['readInput2']['run']['resumedCount'],'finalOutput':final['response']['data']['output'],'nativeWritesThisInvocation':sum(c['method']!='GET' for c in calls),'nativeCalls':calls,'scope':'public API selectedOutputs, dedicated no-model machine-wait, two fixed sequential resumes; no human decision or model task'}
        write(directory/('report-'+uuid.uuid4().hex+'.json'),report);write(directory/'report.json',report)
        print(json.dumps({'status':'passed_live','nativeWritesThisInvocation':report['nativeWritesThisInvocation']}));return 0
    except Exception as exc:
        report={'status':'incomplete','type':type(exc).__name__,'code':getattr(exc,'code',str(exc) if type(exc) is RuntimeError else 'probe_check_failed'),'nativeCalls':calls}
        write(directory/'failure.json',report);print(json.dumps({k:report[k] for k in ('status','type','code')}));return 1
    finally:sim.close()


if __name__=='__main__':raise SystemExit(main())
