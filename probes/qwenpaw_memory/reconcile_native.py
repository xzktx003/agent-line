"""One opt-in real memory write with a lost receipt; replays only observe.

Uses the existing native role, commit/review ledgers and finished coding task.
Only the acceptance transport drops replies; QwenPaw remains unmodified.
"""
import argparse
import hashlib
import json
from pathlib import Path
import time

import httpx
from fastapi.testclient import TestClient

from agent_line.memory_runtime import load_memory_api
from agent_line.service import ServiceConfig, create_app
from agent_line.store import TaskStore, canonical
from probes.qwenpaw_memory.native_roundtrip import save

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / 'var/memory-reconciliation-20261022'


def execution_hash(task):
    keys = ('payload', 'executionStatus', 'executionIdentity', 'verificationStatus', 'output', 'evidence', 'resourcesReleased')
    return hashlib.sha256(canonical({key: task[key] for key in keys}).encode()).hexdigest()


class LostReceipt(httpx.BaseTransport):
    def __init__(self, url, state, persist):
        self.target, self.state, self.persist = url, state, persist
        self.transport = httpx.HTTPTransport(retries=0)

    def handle_request(self, request):
        assert str(request.url).split('?', 1)[0] == self.target
        if request.method == 'PUT':
            assert not self.state.get('nativePUTIntent'), 'original write must never be retried'
            self.state['nativePUTIntent'] = True
            self.persist()
            response = self.transport.handle_request(request)
            response.read()
            assert response.status_code == 200, 'native memory write not confirmed by transport'
            response.close()
            self.state.update(nativePUTs=1, nativePUTResponseDropped=True)
            self.persist()
            raise httpx.ReadTimeout('Acceptance-only loss after native write completed', request=request)
        assert request.method == 'GET'
        if self.state.get('nativePUTIntent'):
            self.state['initialReadbackDropped'] = self.state.get('initialReadbackDropped', 0) + 1
            self.persist()
            raise httpx.ReadTimeout('Acceptance-only readback loss', request=request)
        self.state['prewriteGETs'] = self.state.get('prewriteGETs', 0) + 1
        self.persist()
        response = self.transport.handle_request(request)
        assert response.status_code == 404, 'test memory already exists; do not overwrite'
        return response

    def close(self):
        self.transport.close()


def main(execute=False):
    path = DATA / 'native-receipt.json'
    config = json.loads((DATA / 'service.json').read_text())
    memory = json.loads((DATA / 'memory.json').read_text())
    store = TaskStore(Path(config['database']))
    prepared = None
    if path.exists():
        state = json.loads(path.read_text())
        task = store.get(state['taskId'])
        assert execution_hash(task) == state['executionSha256']
        if execute and state['phase'] == 'prepared' and not state.get('reviewIntent') and not state.get('nativePUTIntent'):
            prepared = state  # Setup failed before admission; retain the exact original identity.
        else:
            print(json.dumps({'mode': 'observation_only', 'newNativeWrites': 0, 'newCodingTasks': 0,
                              'taskId': task['id'], 'memoryStatus': task['memoryStatus'], 'phase': state['phase']}))
            return
    if not execute:
        raise RuntimeError('First real write requires --execute')
    assert json.loads((DATA / 'plan.json').read_text())['phase'] in ('backed_up_stopped', 'bootstrap_verified')
    task_id = json.loads((DATA / 'bootstrap-state.json').read_text())['taskId']
    task = store.get(task_id)
    assert task['executionStatus'] == 'succeeded' and task['verificationStatus'] == 'passed' and task['resourcesReleased']
    assert task['memoryStatus'] == 'not_requested'
    content = ('验收探针记录，仅用于记忆回执恢复验证，不作为业务项目的长期约束。\n'
               f'来源任务：{task_id}。本轮代码已通过三项独立测试。\n'
               '记忆写入确认丢失时，只读取原文件核对，不重新委派 Codex，也不重复写入正文。')
    state = {'phase': 'prepared', 'taskId': task_id, 'bindingId': 'native-memory',
             'decisionId': 'memory-receipt-' + task_id, 'contentSha256': hashlib.sha256(content.strip().encode()).hexdigest(),
             'executionSha256': execution_hash(task), 'nativePUTs': 0, 'createdAt': time.time(),
             'reviewExecution': 'explicit acceptance probe using configured reviewer credential; not a human UI approval'}
    host = json.loads((ROOT / 'var/qwenpaw/agent-line-process.json').read_text())
    state.update(qwenPid=host['pid'], qwenLog=host['log'], qwenLogOffset=Path(host['log']).stat().st_size)
    if prepared:
        assert all(prepared[key] == state[key] for key in ('taskId', 'bindingId', 'decisionId', 'contentSha256', 'executionSha256'))
        state = prepared
    save(path, state)
    persist = lambda: save(path, state)
    api = load_memory_api(DATA / 'memory.json', projects=config['projects'], bindings=config['bindings'],
                          service_tokens=config['tokens'], tasks=store)
    coordinator = api.coordinators['native-memory']
    for lookup in (lambda: coordinator.store.get(coordinator.role, task_id), lambda: api.reviews.get(coordinator.role, task_id)):
        try:
            lookup()
        except KeyError:
            pass
        else:
            raise RuntimeError('Original task already has a memory submission; observe only')
    ordinary = coordinator.client.client
    fault = LostReceipt(coordinator.client.base_url + coordinator.client._path(task_id), state, persist)
    coordinator.client.client = httpx.Client(transport=fault, timeout=30, trust_env=False)
    credential = next(v['token'] for v in memory['credentials'] if 'native-memory-roundtrip' in v['projectScopes'])
    try:
        # No execution routes/gateway are configured in this in-process review facade.
        app = create_app(store, ServiceConfig(tokens={k: set(v) for k, v in config['tokens'].items()},
            projects=config['projects'], bindings={}), memories=api)
        with TestClient(app) as client:
            state['reviewIntent'] = True
            persist()
            response = client.post('/extension/v1/memories/native-memory/' + task_id + '/review',
                headers={'Authorization': 'Bearer ' + credential}, json={'decisionId': state['decisionId'], 'content': content})
            assert response.status_code == 202 and response.json()['status'] == 'pending'
            assert state['nativePUTs'] == 1 and state['initialReadbackDropped'] == 1
            assert store.get(task_id)['memoryStatus'] == 'pending'
            assert execution_hash(store.get(task_id)) == state['executionSha256']
            actual = ordinary.get(coordinator.client.base_url + coordinator.client._path(task_id), params={'section': 'digest'},
                                  headers=coordinator.client.headers, follow_redirects=False)
            assert actual.status_code == 200
            assert hashlib.sha256(actual.json()['content'].strip().encode()).hexdigest() == state['contentSha256']
            state.update(phase='native_written_confirmation_pending', actualNativeContentMatches=True,
                         codingExecutionUnchanged=True, finishedAt=time.time())
            persist()
            print(json.dumps({'phase': state['phase'], 'nativePUTs': 1, 'memoryStatus': 'pending',
                              'actualNativeContentMatches': True, 'newCodingTasks': 0, 'taskId': task_id}))
    finally:
        ordinary.close()
        api.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute', action='store_true')
    main(parser.parse_args().execute)
