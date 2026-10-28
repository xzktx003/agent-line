#!/usr/bin/env python3
"""Live Sim v2 probe. Uses upstream graph execution, never a local scheduler."""
import argparse
import json
import os
from pathlib import Path
import sys
import time
import uuid
from urllib.parse import quote

import httpx

ROOT = Path(__file__).resolve().parents[2]


def block(id, kind, fields=None, x=0, y=0):
    return {'id': id, 'type': kind, 'name': id, 'position': {'x': x, 'y': y},
            'subBlocks': {k: {'id': k, 'type': t, 'value': v} for k, (t, v) in (fields or {}).items()},
            'outputs': {}, 'enabled': True}


def edge(source, target, handle=None):
    result = {'id': f'{source}-{target}', 'source': source, 'target': target}
    if handle is not None:
        result['sourceHandle'] = handle
    return result


def response(id, value, y=0):
    return block(id, 'response', {'dataMode': ('dropdown', 'json'),
        'data': ('code', json.dumps(value)), 'status': ('short-input', '200')}, 650, y)


def graph(human=False):
    start = block('start', 'start_trigger')
    if human:
        gate = block('approval', 'human_in_the_loop_v2', {
            'inputFormat': ('input-format', [{'id': 'approved', 'name': 'approved', 'type': 'boolean', 'value': True}]),
            'notification': ('tool-input', []), 'builderData': ('response-format', [])}, 300)
        blocks = [start, gate, response('done', {'probe': 'human-resumed'})]
        edges = [edge('start', 'approval'), edge('approval', 'done')]
    else:
        # `input` is a built-in Start output; both executions use the same published graph.
        gate = block('gate', 'condition', {'conditions': ('condition-input', json.dumps([
            {'id': 'yes', 'title': 'if', 'value': '<start.input> === "yes"'},
            {'id': 'no', 'title': 'else', 'value': ''}]))}, 300)
        blocks = [start, gate, response('yes_end', {'branch': 'yes'}), response('no_end', {'branch': 'no'}, 250)]
        edges = [edge('start', 'gate'), edge('gate', 'yes_end', 'condition-yes'), edge('gate', 'no_end', 'condition-no')]
    if human:
        # Sim persists block IDs globally, not only within one workflow.
        for b in blocks:
            b['id'] = 'human-' + b['id']
        for e in edges:
            e['id'] = 'human-' + e['id']
            e['source'] = 'human-' + e['source']
            e['target'] = 'human-' + e['target']
    return {'blocks': {b['id']: b for b in blocks}, 'edges': edges, 'loops': {}, 'parallels': {}, 'variables': {}}


def write_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
    os.replace(temporary, path)


class Probe:
    def __init__(self, config, state_file):
        if config.stat().st_mode & 0o077:
            raise RuntimeError('Credential file must have mode 0600')
        settings = json.loads(config.read_text())
        self.workspace = settings['workspaceId']
        self.client = httpx.Client(base_url=settings['baseUrl'].rstrip('/'),
                                   headers={'X-API-Key': settings['apiKey']}, timeout=120, follow_redirects=False)
        self.state_file = state_file
        self.state = json.loads(state_file.read_text()) if state_file.exists() else {
            'steps': {}, 'runs': {}, 'workspaceId': self.workspace, 'attemptId': uuid.uuid4().hex}
        if self.state['workspaceId'] != self.workspace:
            raise RuntimeError('Probe state belongs to a different workspace')

    def request(self, method, path, body=None, headers=None):
        response = self.client.request(method, path, json=body, headers=headers)
        # Responses belong only to the isolated probe; no credentials or headers are persisted.
        if response.status_code >= 400:
            raise RuntimeError(f'Sim request failed: {method} {path}, HTTP {response.status_code}; no automatic retry')
        value = response.json()
        if not isinstance(value, dict) or 'data' not in value or value.get('error'):
            raise RuntimeError('Invalid/error Sim v2 envelope; outcome requires reconciliation')
        return value['data']

    def save(self):
        write_private(self.state_file, self.state)

    def once(self, name, method, path, body=None, headers=None):
        old = self.state['steps'].get(name)
        if old:
            if old['status'] == 'received':
                return old['response']
            raise RuntimeError(f'{name}: previous request outcome unknown; inspect existing identities, never resubmit blindly')
        self.state['steps'][name] = {'status': 'intent', 'method': method, 'path': path, 'time': time.time()}
        self.save()
        data = self.request(method, path, body, headers)
        self.state['steps'][name].update(status='received', response=data)
        self.save()
        return data

    def prepare(self, kind):
        # Keep legacy attempts unchanged; new attempts own disjoint native IDs.
        attempt = self.state.get('attemptId')
        native = graph(kind == 'human')
        if attempt:
            prefix = attempt + '-'
            for block_value in native['blocks'].values():
                block_value['id'] = prefix + block_value['id']
            native['blocks'] = {b['id']: b for b in native['blocks'].values()}
            for connection in native['edges']:
                for field in ('id', 'source', 'target'):
                    connection[field] = prefix + connection[field]
        created = self.once(kind + '-create', 'POST', '/api/v2/workflows', {
            'workspaceId': self.workspace, 'name': 'Agent Line G0 ' + kind + (' ' + attempt if attempt else ''),
            'description': 'Isolated non-model integration probe; no notifications or project writes'})
        wid = created['id']
        base = '/api/v2/workflows/' + quote(wid, safe='')
        written = self.once(kind + '-state', 'PUT', base + '/state', native)
        if written.get('warnings'):
            raise RuntimeError('Graph normalization warnings require review before deployment')
        self.once(kind + '-deploy', 'POST', base + '/deploy', {})
        saved = self.request('GET', base + '/state')
        if set(saved['blocks']) != set(native['blocks']):
            raise RuntimeError('Persisted graph differs from requested block identities')
        self.state[kind + 'WorkflowId'] = wid
        self.save()
        return wid

    def run(self, kind, name, input):
        wid = self.prepare(kind)
        record = self.state['runs'].setdefault(name, {'workflowId': wid, 'runId': str(uuid.uuid4())})
        self.save()  # durable identity BEFORE one-shot X-Run-Id submission
        result = self.once(name + '-execute', 'POST', f'/api/v2/workflows/{wid}/execute',
                           {'input': input}, {'X-Run-Id': record['runId']})
        if result.get('runId') != record['runId']:
            raise RuntimeError('Returned run identity does not match submitted identity')
        return self.observe(name)

    def observe(self, name):
        run = self.state['runs'][name]
        path = f'/api/v2/workflows/{run["workflowId"]}/runs/{run["runId"]}?includeOutput=true'
        result = self.request('GET', path)
        run.setdefault('observations', []).append({'time': time.time(), 'result': result})
        run['observed'] = result
        run['observedAt'] = time.time()
        self.save()
        return result

    def branches(self):
        for branch in ['yes', 'no']:
            result = self.run('condition', branch, {'input': branch})
            if result.get('status') != 'completed' or (result.get('output') or {}).get('data') != {'branch': branch}:
                raise RuntimeError(f'{branch}: real result is not expected completed branch; inspect private state')
        self.state['conditionBranches'] = 'passed_live'
        self.save()

    def pause(self):
        result = self.run('human', 'human', {})
        pause = result.get('paused') or {}
        if result.get('status') != 'paused' or pause.get('pauseKind') != 'human' or not pause.get('contextId'):
            raise RuntimeError('Real Human v2 run is not durably paused with contextId')
        self.state['humanPause'] = 'observed_live'
        self.save()

    def resume(self):
        # Separate process invocation proves observation is not an in-memory handle.
        result = self.observe('human')
        if result.get('status') != 'paused' or not (result.get('paused') or {}).get('contextId'):
            raise RuntimeError('Original run is not resumable; no replacement run will be created')
        run = self.state['runs']['human']
        self.once('human-resume', 'POST', f'/api/v2/workflows/{run["workflowId"]}/runs/{run["runId"]}/resume',
                  {'contextId': result['paused']['contextId'], 'input': {'approved': True}})
        for _ in range(15):
            result = self.observe('human')
            if result.get('status') == 'completed':
                if (result.get('output') or {}).get('data') != {'probe': 'human-resumed'}:
                    raise RuntimeError('Resumed output mismatch')
                self.state['humanResume'] = 'passed_live'
                self.save()
                return
            if result.get('status') in ['failed', 'cancelled']:
                break
            time.sleep(2)
        raise RuntimeError('Resume not confirmed complete; inspect same run, do not resubmit')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['graphs', 'branches', 'pause', 'resume', 'inspect'])
    parser.add_argument('--config', type=Path, default=ROOT / 'var/sim/probe-auth.json')
    parser.add_argument('--state', type=Path, default=ROOT / 'var/sim/workflow-probe-state.json')
    args = parser.parse_args()
    if args.action == 'graphs':
        print(json.dumps({'condition': graph(), 'human': graph(True)}, indent=2))
        return
    probe = Probe(args.config, args.state)
    if args.action == 'inspect':
        for name in probe.state['runs']:
            probe.observe(name)
    else:
        getattr(probe, args.action)()
    print(json.dumps({'action': args.action, 'status': 'completed', 'evidenceFile': str(args.state)}))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        # No request headers, response body, or configuration printed.
        print(f'Probe stopped: {type(exc).__name__}: {exc}', file=sys.stderr)
        sys.exit(1)
