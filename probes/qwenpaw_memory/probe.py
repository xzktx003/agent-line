"""Live QwenPaw memory probe, official HTTP write and native role tool recall."""
import argparse
import json
import os
from pathlib import Path
import secrets
import time
import httpx

ROOT = Path(__file__).resolve().parents[2]
ROLE = 'agent-line-memory-probe'
EVIDENCE = ROOT / 'var/qwenpaw/memory-probe.json'


def save(value):
    with os.fdopen(os.open(EVIDENCE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)


def verify_recall(result, marker):
    calls = {}
    matched = []
    final_text = []
    for message in result.get('result', {}).get('output', []):
        for content in message.get('content', []):
            data = content.get('data', {})
            if message.get('type') == 'plugin_call' and data.get('name') == 'memory_search':
                calls[data.get('call_id')] = data.get('arguments')
            if (message.get('type') == 'plugin_call_output' and data.get('name') == 'memory_search'
                    and data.get('call_id') in calls and data.get('state') == 'success'
                    and marker in str(data.get('output')) and 'agent-line-probe.md' in str(data.get('output'))):
                matched.append({'callId': data['call_id'], 'arguments': calls[data['call_id']], 'source': 'digest/agent-line-probe.md'})
            if message.get('type') == 'message' and message.get('role') == 'assistant':
                final_text.append(content.get('text', ''))
    return {'nativeMemorySearchEvidence': matched,
            'markerInFinalAnswer': marker in '\n'.join(final_text),
            'verified': bool(matched) and marker in '\n'.join(final_text)
                        and result.get('result', {}).get('status') == 'completed'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['seed', 'recall', 'after-restart', 'poll'])
    parser.add_argument('--session', default='before')
    args = parser.parse_args()
    process = json.loads((ROOT / 'var/qwenpaw/agent-line-process.json').read_text())
    base = f'http://127.0.0.1:{process["port"]}'
    with httpx.Client(base_url=base, timeout=180) as client:
        auth = client.post('/api/auth/login', json=json.loads((ROOT / 'var/qwenpaw-secrets/local-login.json').read_text()))
        auth.raise_for_status()
        token = auth.json().get('token') or auth.json().get('access_token')
        client.headers.update({'Authorization': 'Bearer '+token, 'X-Agent-Id': ROLE})
        def call(method, path, body=None):
            r = client.request(method, '/api/'+path, json=body)
            if r.status_code >= 400:
                raise RuntimeError(f'{method} {path}: HTTP {r.status_code}')
            return r.json()
        evidence = json.loads(EVIDENCE.read_text()) if EVIDENCE.exists() else {'role': ROLE, 'marker': 'MEMORY_VALUE_'+secrets.token_hex(12), 'sessions': {}}
        if args.action == 'seed':
            if evidence.get('writeReadbackConfirmed'):
                print('Existing probe seed preserved; use recall or after-restart')
                return
            listing = call('GET', 'agents')['agents']
            if ROLE not in {r['id'] for r in listing}:
                call('POST', 'agents', {'id': ROLE, 'name': '隔离记忆验收角色', 'description': '仅用于公开API写入和真实memory_search验收', 'backend': 'qwenpaw', 'language': 'zh', 'skill_names': [], 'active_model': {'provider_id':'kilo', 'model':'kilo-auto/free'}})
            evidence['initialPid'] = process['pid']
            save(evidence)
            text = '# Agent Line isolated memory probe\n\nThe project codename is cobalt-orchard. Its verified acceptance marker is '+evidence['marker']+'.\nThis is non-secret synthetic test data, scoped only to this probe role.\n'
            response = call('PUT', 'workspace/memory/agent-line-probe.md?section=digest', {'content': text})
            if response.get('written') is not True:
                raise RuntimeError('Official memory write not confirmed')
            read = call('GET', 'workspace/memory/agent-line-probe.md?section=digest')
            if read.get('content','').strip() != text.strip():
                raise RuntimeError('Official memory readback mismatch')
            evidence['writeApi'] = 'PUT /api/workspace/memory/agent-line-probe.md?section=digest'
            evidence['writeReadbackConfirmed'] = True
            evidence['reindex'] = call('POST', 'agents/'+ROLE+'/memory/reindex?scope=bm25')
            save(evidence)
            print('Official memory write/readback/reindex completed; native recall not yet proven')
            return
        if args.action == 'after-restart':
            if process['pid'] == evidence.get('initialPid'):
                raise RuntimeError('Service PID unchanged; restart unproven')
            content = call('GET', 'workspace/memory/agent-line-probe.md?section=digest')
            if evidence['marker'] not in content.get('content',''):
                raise RuntimeError('Persisted memory missing after restart')
            evidence['afterRestartPid'] = process['pid']
            evidence['memoryReadbackAfterRestart'] = True
            save(evidence)
        session_name = args.session
        record = evidence['sessions'].get(session_name)
        if not record:
            if args.action == 'poll':
                raise RuntimeError('No existing task to poll')
            record = {'sessionId':'memory-probe-'+secrets.token_hex(10), 'state':'submission_unknown', 'servicePid':process['pid']}
            evidence['sessions'][session_name] = record
            save(evidence)
            response = call('POST','console/chat/task',{'session_id': record['sessionId'], 'user_id':'agent-line-memory-g0', 'input':[{'role':'user','content':[{'type':'text','text':'这是只读记忆验收。请先调用 memory_search 查询 cobalt-orchard 的已验证 acceptance marker。只返回检索中找到的 marker，并注明来源。禁止猜测；找不到就明确说找不到。不要修改文件、不要运行命令、不要调用网络。'}]}], 'timeout':150})
            record.update(state='submitted',taskId=response['task_id'])
            save(evidence)
        if not record.get('taskId'):
            raise RuntimeError('Submission unknown; do not resubmit')
        for _ in range(50):
            result = call('GET', 'console/chat/task/'+record['taskId'])
            record['observed'] = result
            save(evidence)
            if result.get('status') == 'finished':
                serialized = json.dumps(result, ensure_ascii=False)
                record['markerFoundInResult'] = evidence['marker'] in serialized
                record['memorySearchMentioned'] = 'memory_search' in serialized
                record.update(verify_recall(result, evidence['marker']))
                record['state'] = 'finished'
                save(evidence)
                print(json.dumps({'session':session_name,'finished':True,'resultStatus':result.get('result',{}).get('status'),'markerFoundInResult':record['markerFoundInResult'],'memorySearchMentioned':record['memorySearchMentioned'],'verifiedNativeToolAndAnswer':record['verified']},ensure_ascii=False))
                if not record['verified']:
                    raise RuntimeError('Native tool output and final-answer proof incomplete')
                return
            time.sleep(2)
        raise RuntimeError('Task remains live; use poll on same session, never resubmit')


if __name__ == '__main__':
    main()
