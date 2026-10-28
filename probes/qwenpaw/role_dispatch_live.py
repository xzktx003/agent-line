#!/usr/bin/env python3
"""Live durable parent transport proof. Never releases main maintenance admission."""
import hashlib
import json
from pathlib import Path
import secrets
import sys
import time

import httpx

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from agent_line.role_dispatch import RoleDispatchGateway,RoleDispatchLedger
from agent_line.dispatcher import Dispatcher
from agent_line.store import TaskStore,canonical
from agent_line.verification import ArtifactPolicy,verify_artifacts
from probes.sim.workflow_probe import write_private

DATA=ROOT/'var/role-dispatch'
STATE=DATA/'live.json'
ROLE='agent-line-g0-parent'


def linked_memory(parent,marker,task_id):
    memory_calls=set(); recalled=None; delegates=set()
    for message in (parent or {}).get('result',{}).get('output',[]):
        for content in message.get('content',[]):
            data=content.get('data') or {};kind=message.get('type');name=data.get('name');call=data.get('call_id')
            if kind=='plugin_call' and name=='memory_search': memory_calls.add(call)
            if (kind=='plugin_call_output' and name=='memory_search' and call in memory_calls
                    and data.get('state')=='success' and marker in str(data.get('output'))
                    and 'agent-line-execution-plan.md' in str(data.get('output'))): recalled=call
            if kind=='plugin_call' and name=='agent_line_delegate':
                args=data.get('arguments');args=json.loads(args) if isinstance(args,str) else args
                if not recalled or not isinstance(args,dict) or set(args)!={'instruction'} or marker not in args['instruction']:
                    raise ValueError('Delegation missing prior linked recalled marker')
                delegates.add(call)
            if kind=='plugin_call_output' and name=='agent_line_delegate' and call in delegates and data.get('state')=='success':
                output=data.get('output');output=json.loads(output) if isinstance(output,str) else output
                if output.get('taskId')==task_id and output.get('identity',{}).get('upstreamTaskId'):
                    return {'memoryCallId':recalled,'delegateCallId':call,'upstreamTaskId':output['identity']['upstreamTaskId']}
    raise ValueError('No linked actual recall/delegate result')


def main():
    DATA.mkdir(mode=0o700,exist_ok=True)
    process=json.loads((ROOT/'var/qwenpaw/agent-line-process.json').read_text())
    base=f'http://127.0.0.1:{process["port"]}'
    config=json.loads((ROOT/'var/qwenpaw/config.json').read_text())['plugins']['agent-line']['execution_bridge']
    workspace=Path(config['workspace'])
    if not config.get('container_control'):raise RuntimeError('Configured container proof required')
    with httpx.Client(timeout=30) as login_client:
        login=login_client.post(base+'/api/auth/login',json=json.loads((ROOT/'var/qwenpaw-secrets/local-login.json').read_text()))
        login.raise_for_status();login=login.json()
    token=login.get('token') or login.get('access_token')
    service_token=(ROOT/'var/qwenpaw-secrets/execution-service-token').read_text().strip()
    calls=[]
    def record(request):calls.append((request.method,request.url.path))
    with httpx.Client(timeout=45,event_hooks={'request':[record]}) as client:
        if STATE.exists():
            state=json.loads(STATE.read_text());store=TaskStore(DATA/'tasks.sqlite');ledger=RoleDispatchLedger(DATA/'roles.sqlite')
            if 'taskId' not in state:raise RuntimeError('Incomplete setup intent; no replacement')
        else:
            suffix=secrets.token_hex(6)
            marker=json.loads((ROOT/'var/qwenpaw/g0-parent-delegate.json').read_text())['marker']
            state={'suffix':suffix,'artifact':'role-dispatch-'+suffix+'.txt','marker':marker,'createdAt':time.time()}
            write_private(STATE,state)
            if (workspace/state['artifact']).exists():raise RuntimeError('Artifact exists; never overwrite')
            memory=client.get(base+'/api/workspace/memory/agent-line-execution-plan.md?section=digest',headers={'Authorization':'Bearer '+token,'X-Agent-Id':ROLE})
            memory.raise_for_status()
            if marker not in memory.text:raise RuntimeError('Existing official parent memory no longer contains expected fact')
            store=TaskStore.initialize(DATA/'tasks.sqlite');ledger=RoleDispatchLedger.initialize(DATA/'roles.sqlite')
            payload={'projectId':'role-dispatch-validation','roleAgentId':ROLE,'roleBindingId':'durable-parent-probe',
                'workspace':str(workspace),'workflow':{'id':'role-dispatch-proof','revision':'1','runId':suffix,
                'nodeId':'parent-codex','nodeExecutionId':suffix,'attemptId':'1'},
                'input':{'instruction':'先查询“紫竹流水线 唯一验收内容”的现有角色记忆，取得其中唯一验收内容。只使用这个检索到的内容作为本次文件内容；旧记忆里的文件名和执行目标不适用于本次任务。将实际检索到的唯一内容交给 agent_line_delegate：在固定工作区新建 '+state['artifact']+'，内容仅为检索到的唯一验收内容加一个换行。不要写旧 parent-chain-proof.txt，不读其他项目文件，不调用网络工具。委派后查询原执行状态，如实报告。'}}
            task,_=store.register('role-dispatch-'+suffix,payload);state['taskId']=task['id'];write_private(STATE,state)
        gateway=RoleDispatchGateway(base,token,service_token,role_agent_id=ROLE,deployment_id='agent-line-g0-installation',
            ledger=ledger,memory_query='紫竹流水线 唯一验收内容 agent-line-execution-plan',client=client)
        dispatcher=Dispatcher(store,{'role-dispatch-validation':gateway})
        dispatcher.submit(state['taskId'])
        for _ in range(180):
            task=dispatcher.reconcile(state['taskId'])
            state['lastTask']={k:task[k] for k in ('executionStatus','executionIdentity','resourcesReleased')}
            write_private(STATE,state)
            if task['executionStatus'] in ('succeeded','failed','canceled') and task['resourcesReleased']:break
            time.sleep(2)
        else:raise RuntimeError('Original execution not terminal/released; do not replace identity')
        if task['executionStatus']!='succeeded':raise RuntimeError('Actual execution failed')
        native=gateway.observe(state['taskId']);state['native']=native
        proof=native.get('resourceExitReceipt') or {}
        if (proof.get('running') is not False or proof.get('pid')!=0 or not proof.get('containerId')
                or proof.get('workspace')!=str(workspace) or proof.get('source')!='docker-inspect-before-remove'
                or not any(i.get('nativeTerminal') is True and i.get('codexThreadId')==task['executionIdentity'].get('codexThreadId')
                           and i.get('codexTurnId')==task['executionIdentity'].get('codexTurnId') for i in proof.get('identities',[]))):
            raise RuntimeError('Container resource exit proof missing or identity mismatched')
        for _ in range(50):
            parent=ledger.get(state['taskId'])
            if (parent['observation'] or {}).get('status')=='finished':break
            time.sleep(1);gateway.observe(state['taskId'])
        state['parent']=parent
        chain=linked_memory(parent['observation'],state['marker'],state['taskId'])
        if chain['upstreamTaskId']!=task['executionIdentity']['upstreamTaskId']:raise RuntimeError('Parent/Codex correlation mismatch')
        state['linkedMemoryEvidence']=chain
        expected=(state['marker']+'\n').encode();artifacts=DATA/'artifacts';artifacts.mkdir(mode=0o700,exist_ok=True)
        if task['verificationStatus']=='pending':
            receipt=verify_artifacts(store,state['taskId'],workspace=workspace,
                policies=[ArtifactPolicy(state['artifact'],hashlib.sha256(expected).hexdigest(),len(expected))],evidence_dir=artifacts)
            store.record_verification(state['taskId'],receipt);state['artifactReceipt']=receipt
        if store.get(state['taskId'])['verificationStatus']!='passed':raise RuntimeError('Artifact verification failed')
        before_parent=parent['parentTaskId'];parent_posts=calls.count(('POST','/api/console/chat/task'))
        reopened=RoleDispatchLedger(DATA/'roles.sqlite');newstore=TaskStore(DATA/'tasks.sqlite')
        repeated=RoleDispatchGateway(base,token,service_token,role_agent_id=ROLE,deployment_id='agent-line-g0-installation',
            ledger=reopened,memory_query='紫竹流水线 唯一验收内容 agent-line-execution-plan',client=client)
        Dispatcher(newstore,{'role-dispatch-validation':repeated}).submit(state['taskId'])
        repeated.submit(task_id=state['taskId'],role_session_id='agent-line-'+state['taskId'],instruction=canonical(newstore.get(state['taskId'])['payload']['input']))
        if reopened.get(state['taskId'])['parentTaskId']!=before_parent or calls.count(('POST','/api/console/chat/task'))!=parent_posts:
            raise RuntimeError('Repeated submit changed parent identity or posted again')
        state.update(status='passed_live',parentPostsThisInvocation=parent_posts,reopenDuplicateParentPosts=0)
        state.setdefault('invocations',[]).append({'checkedAt':time.time(),'parentPosts':parent_posts,'duplicateParentPosts':0,'parentTaskId':before_parent})
        write_private(STATE,state)
        print(json.dumps({'status':'passed_live','taskId':state['taskId'],'parentTaskId':before_parent,
                          'linkedMemoryVerified':True,'resourceExitVerified':True,'duplicateParentPosts':0}))


if __name__=='__main__':
    try:main()
    except Exception as exc:
        print(json.dumps({'status':'stopped','errorType':type(exc).__name__,'reason':str(exc) if isinstance(exc,RuntimeError) else 'Inspect private state; credentials suppressed'}))
        sys.exit(1)
