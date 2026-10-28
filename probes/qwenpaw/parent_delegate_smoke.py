#!/usr/bin/env python3
"""Real native parent memory recall -> controlled tool -> native Codex proof."""
import json
import secrets
import time
import urllib.request
from runtime_preflight import ROOT, base, TOKEN
ROLE='agent-line-g0-parent'
TASK='g0-parent-delegate-001'
SESSION='agent-line-'+TASK
RECEIPT=ROOT/'var/qwenpaw/g0-parent-delegate.json'
SERVICE_TOKEN=(ROOT/'var/qwenpaw-secrets/execution-service-token').read_text().strip()

def request(path, body=None, method=None):
    req=urllib.request.Request(base+'/api/'+path,data=json.dumps(body).encode() if body is not None else None,method=method,headers={'Authorization':'Bearer '+TOKEN,'Content-Type':'application/json','X-Agent-Id':ROLE,'X-Agent-Line-Service-Token':SERVICE_TOKEN})
    with urllib.request.urlopen(req,timeout=120) as response:return json.load(response)

def verify_parent_chain(parent, marker, task_id):
    """Require linked actual tool results; narrative claims do not count."""
    recall_calls = set()
    recalled = None
    delegate_calls = set()
    for message in parent.get('result', {}).get('output', []):
        for content in message.get('content', []):
            data = content.get('data') or {}
            kind, name, call_id = message.get('type'), data.get('name'), data.get('call_id')
            if kind == 'plugin_call' and name == 'memory_search':
                recall_calls.add(call_id)
            if (kind == 'plugin_call_output' and name == 'memory_search'
                    and call_id in recall_calls and data.get('state') == 'success'
                    and marker in str(data.get('output'))
                    and 'agent-line-execution-plan.md' in str(data.get('output'))):
                recalled = call_id
            if kind == 'plugin_call' and name == 'agent_line_delegate':
                assert recalled, 'Delegate preceded verified memory result'
                arguments = data.get('arguments')
                arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
                assert set(arguments) == {'instruction'}, 'Unexpected model identity arguments'
                assert marker in arguments['instruction'], 'Recall marker missing in delegation'
                delegate_calls.add(call_id)
            if (kind == 'plugin_call_output' and name == 'agent_line_delegate'
                    and call_id in delegate_calls and data.get('state') == 'success'):
                output = data.get('output')
                output = json.loads(output) if isinstance(output, str) else output
                assert output['taskId'] == task_id
                assert output['identity'].get('upstreamTaskId')
                return {'memoryCallId': recalled, 'delegateCallId': call_id,
                        'upstreamTaskId': output['identity']['upstreamTaskId']}
    raise AssertionError('Missing linked recall and delegate tool evidence')

if not RECEIPT.exists():
    marker='AGENT_LINE_RECALLED_'+secrets.token_hex(8)
    config=request('agents/'+ROLE)
    config['active_model']={'provider_id':'kilo','model':'kilo-auto/free'}
    for tool in config['tools']['builtin_tools'].values():tool['enabled']=False
    for name in ('agent_line_delegate','agent_line_execution_status'):
        config['tools']['builtin_tools'][name]={'name':name,'enabled':True}
    request('agents/'+ROLE,config,'PUT')
    request('workspace/memory/agent-line-execution-plan.md?section=digest',{'content':'# Agent Line 受控委派验收\n任务代号：紫竹流水线。唯一验收内容：'+marker+'。执行要求：在固定 Codex 角色工作目录创建 parent-chain-proof.txt，文件仅含唯一验收内容和换行，并执行 shell 检查。此记忆只用于隔离验收。'},'PUT')
    request('agents/'+ROLE+'/memory/reindex?scope=bm25',{},'POST')
    request('agent-line/delegations/bind',{'taskId':TASK,'roleAgentId':ROLE,'roleSessionId':SESSION})
    evidence={'taskId':TASK,'role':ROLE,'session':SESSION,'marker':marker,'parentSubmission':'unknown'}
    RECEIPT.write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
    result=request('console/chat/task',{'session_id':SESSION,'user_id':'agent-line-service','input':[{'role':'user','content':[{'type':'text','text':'这是受控验收任务，请直接执行，不做首次启动引导。先调用 memory_search 查询“紫竹流水线 唯一验收内容”，必须从检索结果取得唯一验收内容，不能自行编造。然后调用 agent_line_delegate，将检索到的文件要求和唯一内容完整交给固定执行角色。不要自己写文件或执行shell。提交后可用 agent_line_execution_status 查询真实状态；若尚未完成可如实报告已提交。禁止调用其他工具。'}]}],'timeout':180})
    evidence.update(parentSubmission='submitted',parentTaskId=result['task_id'])
    RECEIPT.write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
else:
    evidence=json.loads(RECEIPT.read_text())
    if not evidence.get('parentTaskId'):raise SystemExit('Parent submission unknown; do not resubmit')
for _ in range(100):
    parent=request('console/chat/task/'+evidence['parentTaskId'])
    if parent.get('status')=='finished':
        (RECEIPT.parent/'g0-parent-delegate-parent-result.json').write_text(json.dumps(parent,ensure_ascii=False,indent=2))
        assert parent.get('result',{}).get('status')=='completed','Native parent failed'
        break
    time.sleep(2)
else:raise SystemExit('Parent still running; retain existing receipt')
chain = verify_parent_chain(parent, evidence['marker'], TASK)
for _ in range(100):
    native=request('agent-line/executions/'+TASK)
    if native['terminalConfirmed'] and native['resourcesReleased']:
        assert native['status']=='succeeded'
        path=ROOT/'var/qwenpaw/workspaces/agent-line-g0-codex/parent-chain-proof.txt'
        assert path.read_text()==evidence['marker']+'\n'
        assert native['identity']['upstreamTaskId'] == chain['upstreamTaskId']
        evidence.update(native=native,artifactVerified=True,parentChain=chain)
        RECEIPT.write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
        print(json.dumps({'parent_completed':True,'linked_recall_delegate_verified':True,'artifact_contains_recalled_marker':True,'native_identity':native['identity']}))
        break
    time.sleep(2)
else:raise SystemExit('Native task still nonterminal; do not resubmit')
