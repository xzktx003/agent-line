#!/usr/bin/env python3
"""Real parent recall -> controlled delegation -> container native turn/exit."""
import json
from pathlib import Path
import secrets
import time
import httpx

ROOT=Path(__file__).resolve().parents[2]
ROLE='agent-line-g0-parent'
TASK='container-parent-memory-001'
SESSION='agent-line-'+TASK
DIRECTORY=ROOT/'var/qwenpaw/container-parent-memory'
DIRECTORY.mkdir(mode=0o700,exist_ok=True)
RECEIPT=DIRECTORY/'receipt.json'
WORKSPACE=ROOT/'var/executor/workspace'
CODEX_HOME=ROOT/'var/executor/codex-home'


def save(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2));path.chmod(0o600)


def linked_calls(parent,marker,source):
    memory_calls=set();recalled=None;delegates={}
    for message in parent.get('result',{}).get('output',[]):
        for content in message.get('content',[]):
            data=content.get('data') or {};kind=message.get('type');name=data.get('name');call=data.get('call_id')
            if kind=='plugin_call' and name=='memory_search':memory_calls.add(call)
            if (kind=='plugin_call_output' and name=='memory_search' and call in memory_calls
                    and data.get('state')=='success' and marker in str(data.get('output')) and source in str(data.get('output'))):
                recalled=call
            if kind=='plugin_call' and name=='agent_line_delegate':
                assert recalled,'Delegation preceded verified recall'
                arguments=data.get('arguments');arguments=json.loads(arguments) if isinstance(arguments,str) else arguments
                assert set(arguments)=={'instruction'} and marker in arguments['instruction']
                delegates[call]=arguments['instruction']
            if kind=='plugin_call_output' and name=='agent_line_delegate' and call in delegates and data.get('state')=='success':
                output=data.get('output');output=json.loads(output) if isinstance(output,str) else output
                assert output['taskId']==TASK and output['identity'].get('upstreamTaskId')
                return {'memoryCallId':recalled,'delegateCallId':call,'instruction':delegates[call],
                        'upstreamTaskId':output['identity']['upstreamTaskId'],'source':source}
    raise AssertionError('Missing linked actual memory/delegate tool calls')


state=json.loads((ROOT/'var/qwenpaw/agent-line-process.json').read_text())
base=f'http://127.0.0.1:{state["port"]}/api'
with httpx.Client(timeout=120) as client:
    response=client.post(base+'/auth/login',json=json.loads((ROOT/'var/qwenpaw-secrets/local-login.json').read_text()));response.raise_for_status();login=response.json()
    client.headers.update({'Authorization':'Bearer '+(login.get('token') or login['access_token']),
                           'X-Agent-Id':ROLE,'X-Agent-Line-Service-Token':(ROOT/'var/qwenpaw-secrets/execution-service-token').read_text().strip()})
    def request(path,body=None,method=None):
        response=client.request(method or ('POST' if body is not None else 'GET'),base+'/'+path,json=body)
        response.raise_for_status();return response.json()
    if not RECEIPT.exists():
        suffix=secrets.token_hex(8)
        evidence={'taskId':TASK,'roleId':ROLE,'roleSessionId':SESSION,'marker':'CONTAINER_RECALLED_'+secrets.token_hex(16),
                  'query':'青松容器验收-'+suffix,'source':'agent-line-container-memory-'+suffix+'.md',
                  'artifact':'parent-container-'+suffix+'.txt','phase':'prepared',
                  'memoryWriteDecision':'Authorized isolated verification through official workspace memory PUT; separate named digest, no overwrite of existing evidence.'}
        save(RECEIPT,evidence)
    else:evidence=json.loads(RECEIPT.read_text())
    if evidence['phase']=='prepared':
        assert not (WORKSPACE/evidence['artifact']).exists()
        memory='# 容器委派独立验收\n任务代号：'+evidence['query']+'。唯一标记：'+evidence['marker']+'。执行要求：在固定执行角色工作目录用shell创建 '+evidence['artifact']+'，文件内容精确为唯一标记加换行，随后读取验证。不要检查其他文件，不访问网络。'
        request('workspace/memory/'+evidence['source']+'?section=digest',{'content':memory},'PUT')
        request('agents/'+ROLE+'/memory/reindex?scope=bm25',{},'POST')
        request('agent-line/delegations/bind',{'taskId':TASK,'roleAgentId':ROLE,'roleSessionId':SESSION})
        evidence['phase']='submission_unknown';save(RECEIPT,evidence)
        prompt='这是隔离容器委派验收。先调用 memory_search 检索任务代号“'+evidence['query']+'”，从实际结果获得唯一标记及完整文件要求，再调用 agent_line_delegate 完整传递要求和标记。不要自行编造标记，不调用shell/文件工具，不做首次启动引导。可以通过 agent_line_execution_status 查询；未完成就如实报告已提交。'
        assert evidence['marker'] not in prompt
        evidence['parentPromptContainsMarker']=False
        result=request('console/chat/task',{'session_id':SESSION,'user_id':'agent-line-service','input':[{'role':'user','content':[{'type':'text','text':prompt}]}],'timeout':180})
        evidence.update(parentTaskId=result['task_id'],phase='submitted');save(RECEIPT,evidence)
    if not evidence.get('parentTaskId'):raise SystemExit('Submission unknown: preserve identity; no resubmit')
    for _ in range(120):
        parent=request('console/chat/task/'+evidence['parentTaskId'])
        if parent.get('status')=='finished':break
        time.sleep(2)
    else:raise SystemExit('Parent still unresolved; retain receipt')
    save(DIRECTORY/'parent-native-result.json',parent)
    assert parent.get('result',{}).get('status')=='completed'
    chain=linked_calls(parent,evidence['marker'],evidence['source'])
    evidence['parentChain']=chain;save(RECEIPT,evidence)
    for _ in range(120):
        native=request('agent-line/executions/'+TASK)
        if native.get('terminalConfirmed') and native.get('resourcesReleased'):break
        time.sleep(2)
    else:raise SystemExit('Native resources unresolved; retain identity')
    evidence['native']=native;save(RECEIPT,evidence)
    identity=native['identity'];exit_receipt=native.get('resourceExitReceipt') or {}
    assert identity['upstreamTaskId']==chain['upstreamTaskId'] and native['status']=='succeeded'
    assert exit_receipt['workspace']==str(WORKSPACE) and exit_receipt['source']=='docker-inspect-before-remove'
    assert exit_receipt['running'] is False and exit_receipt['pid']==0 and exit_receipt['finishedAt']
    assert exit_receipt['owner'] and len(exit_receipt['containerId'])==64
    assert any(item['codexThreadId']==identity['codexThreadId'] and item['codexTurnId']==identity['codexTurnId'] for item in exit_receipt['identities'])
    rollout=list((CODEX_HOME/'sessions').rglob('*'+identity['codexThreadId']+'.jsonl'))
    assert len(rollout)==1
    injected=[]
    for line in rollout[0].read_text().splitlines():
        event=json.loads(line);payload=event.get('payload',{})
        if event.get('type')=='response_item' and payload.get('type')=='message' and payload.get('role')=='user':
            if evidence['marker'] in json.dumps(payload) and evidence['artifact'] in json.dumps(payload):injected.append(event.get('timestamp'))
    assert injected,'Marker absent from native user input'
    assert (WORKSPACE/evidence['artifact']).read_text()==evidence['marker']+'\n'
    evidence.update(phase='verified',nativeUserInjectionTimestamps=injected,artifactVerified=True,verified=True)
    save(RECEIPT,evidence)
    print(json.dumps({'taskId':TASK,'memoryDelegateLinked':True,'nativeInputVerified':True,'artifactVerified':True,'containerExitVerified':True,'identity':identity}))
