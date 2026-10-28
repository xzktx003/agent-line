#!/usr/bin/env python3
"""Dedicated native loop proof; no model, role, memory, or production mutations."""
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agent_line.adapters.sim import SimClient
from probes.sim.authoring_probe import write
from probes.sim.workflow_probe import block, edge, response

DATA = ROOT / 'var/sim-finite-loop'


def main():
    DATA.mkdir(mode=0o700, exist_ok=True)
    path = DATA / 'state.json'
    authpath = ROOT / 'var/sim/probe-auth.json'
    if authpath.stat().st_mode & 0o077:
        raise RuntimeError('credential_permissions')
    auth = json.loads(authpath.read_text())
    sim = SimClient(auth['baseUrl'], auth['apiKey'])
    calls = []
    sim._client.event_hooks['request'].append(lambda r: calls.append({'method': r.method, 'path': r.url.path}))
    existed = path.exists()
    state = json.loads(path.read_text()) if existed else {'steps': {}, 'runs': {}, 'prefix': 'fl_' + uuid.uuid4().hex, 'workspaceId': auth['workspaceId']}
    def persist():write(path, state)
    def request(method, suffix, body=None, params=None):
        r = sim._client.request(method, sim.base_url + suffix, headers={'x-api-key': sim._key}, json=body, params=params, follow_redirects=False)
        if r.status_code not in (200, 201, 202):
            raise RuntimeError('native_response_' + str(r.status_code))
        value = r.json()
        if type(value) is not dict or 'data' not in value:raise RuntimeError('native_envelope_invalid')
        return value['data']
    def once(key, payload, operation):
        old = state['steps'].get(key)
        if old:
            if old['status'] != 'received' or old['payload'] != payload:raise RuntimeError('unknown_intent_no_resubmit')
            return old['result']
        state['steps'][key] = {'status': 'intent', 'payload': payload};persist()
        result = operation()
        state['steps'][key].update(status='received', result=result);persist()
        return result
    try:
        if existed and '--continue-known' not in sys.argv:
            observations = {}
            for name, run in state['runs'].items():
                observations[name] = {rid: sim.observe(state['workflowId'], rid) for rid in dict.fromkeys([run['runId']] + run.get('children', []))}
            write(DATA / 'observation.json', {'observations': observations, 'nativeCalls': calls, 'nativeWrites': 0, 'priorResult': state.get('result')})
            print(json.dumps({'mode': 'observation_only', 'nativeWrites': 0, 'priorResult': state.get('result')}));return 0
        persist()
        check = subprocess.run(['docker', 'ps', '--filter', 'label=com.docker.compose.project=agent-line', '--format', '{{.Names}} {{.Status}}'], capture_output=True, text=True, check=True).stdout
        if 'agent-line-simstudio-1' not in check or 'healthy' not in check:raise RuntimeError('sim_health_unconfirmed')
        state['processCheck'] = check;persist()
        payload = {'workspaceId': auth['workspaceId'], 'name': 'agent-line-finite-loop-probe', 'description': 'Native single loop, maximum three iterations, no models'}
        created = once('create', payload, lambda: request('POST', '/api/v2/workflows', payload))
        wid = created['id'];state['workflowId'] = wid;base = '/api/v2/workflows/' + wid
        prefix = state['prefix']
        start = block(prefix + '_start', 'start_trigger', {'runMetadata': ('switch', True)});start['name'] = '开始'
        loop = block(prefix + '_loop', 'loop', {}, 250);loop['name'] = '修复循环'
        loop['data'] = {'loopType': 'doWhile', 'count': 3, 'doWhileCondition': '<loop.index> < 3 && <等待.resumeInput.repair> === true', 'width': 500, 'height': 300}
        wait = block(prefix + '_wait', 'human_in_the_loop_v2', {'inputFormat': ('input-format', [{'id': 'taskInput', 'name': 'taskInput', 'type': 'object', 'value': '{"iteration": <loop.index>, "marker": "原生循环"}'}]), 'notification': ('tool-input', []), 'builderData': ('response-format', [])}, 50, 80)
        wait['name'] = '等待';wait['data'] = {'parentId': loop['id'], 'extent': 'parent'}
        end = response(prefix + '_end', {});end['name'] = '结束'
        end['subBlocks']['data']['value'] = '{"exhausted": <等待.resumeInput.repair>, "result": <等待.resumeInput>}'
        graph = {'blocks': {b['id']: b for b in (start, loop, wait, end)}, 'edges': [edge(start['id'], loop['id']), edge(loop['id'], wait['id'], 'loop-start-source'), edge(loop['id'], end['id'], 'loop-end-source')], 'variables': {}}
        state['graph'] = graph;persist()
        once('save', graph, lambda: request('PUT', base + '/state', graph))
        deployment = once('publish', {}, lambda: request('POST', base + '/deploy', {}))
        version = request('GET', base + '/versions/' + str(deployment['version']))
        state['published'] = version;persist()
        loops = version['state']['loops']
        if loop['id'] not in loops or loops[loop['id']]['nodes'] != [wait['id']] or loops[loop['id']]['doWhileCondition'] != loop['data']['doWhileCondition']:raise RuntimeError('published_loop_mismatch')
        for name, rounds in [('exhausted', 3), ('early_success', 2)]:
            run = state['runs'].setdefault(name, {'runId': str(uuid.uuid4()), 'children': [], 'pauses': []});persist()
            rid = run['runId'];inputs = {'input': {'case': name}}
            once(name + '-execute', {'runId': rid, 'input': inputs}, lambda: sim.execute(wid, rid, inputs))
            for iteration in range(rounds):
                context = wait['id'] + '_loop' + str(iteration)
                key = name + '-resume-' + str(iteration)
                if key in state['steps']:
                    if state['steps'][key]['status'] != 'received':raise RuntimeError('unknown_intent_no_resubmit')
                    continue
                for _ in range(80):
                    snapshot = request('GET', base + '/runs/' + rid)
                    pause = snapshot.get('paused') or {}
                    if snapshot['status'] in ('failed', 'completed'):raise RuntimeError('premature_terminal')
                    if pause.get('contextId') == context:break
                    time.sleep(0.25)
                else:raise RuntimeError('next_pause_not_observed')
                snapshot = request('GET', base + '/runs/' + rid, params={'selectedOutputs': wait['id']})
                pause = snapshot.get('paused') or {}
                if pause.get('contextId') != context:raise RuntimeError('activation_changed')
                out = snapshot['blockOutputs'][wait['id']]
                actual = out['taskInput'];actual = json.loads(actual) if isinstance(actual, str) else actual
                output_context = out['response']['resume']['contextId']
                if actual != {'iteration': iteration, 'marker': '原生循环'} or output_context != context or pause['blockedOnBlockId'] != wait['id']:raise RuntimeError('activation_input_mismatch')
                run['pauses'].append({'iteration': iteration, 'contextId': context, 'input': actual, 'snapshot': snapshot});persist()
                # A deliberately stale request is a separate, explicit negative test,
                # never a retry of an uncertain request. Expect the native API to reject it.
                if name == 'exhausted' and iteration == 2:
                    old_context = wait['id'] + '_loop1'
                    body = {'contextId': old_context, 'input': {'repair': False, 'marker': 'stale-result'}}
                    def stale():
                        r = sim._client.post(sim.base_url + base + '/runs/' + rid + '/resume', headers={'x-api-key': sim._key}, json=body, follow_redirects=False)
                        return {'statusCode': r.status_code}
                    result = once('stale-context-negative', body, stale)
                    if result['statusCode'] not in (400, 404, 409):raise RuntimeError('stale_context_not_rejected')
                    unchanged = sim.observe(wid, rid);run['staleContextObservation'] = unchanged;persist()
                    if (unchanged['response']['data'].get('paused') or {}).get('contextId') != context:raise RuntimeError('stale_context_advanced')
                supplied = {'repair': name == 'exhausted' or iteration == 0, 'iteration': iteration, 'marker': name}
                receipt = once(key, {'runId': rid, 'contextId': context, 'input': supplied}, lambda: sim.resume(wid, rid, context, supplied))
                run['children'].append(receipt['resumedRunId']);persist()
                print(json.dumps({'case': name, 'iteration': iteration, 'contextVerified': True, 'resumeAccepted': True}), flush=True)
            final_id = state['steps'][name + '-resume-' + str(rounds - 1)]['result']['resumedRunId']
            for _ in range(80):
                final = sim.observe(wid, final_id)
                if final['status'] in ('completed', 'failed', 'paused'):break
                time.sleep(0.25)
            run['final'] = final;run['parentFinal'] = sim.observe(wid, rid);persist()
            expected = {'exhausted': name == 'exhausted', 'result': {'repair': name == 'exhausted', 'iteration': rounds - 1, 'marker': name}}
            if final['status'] != 'completed' or final['response']['data'].get('output', {}).get('data') != expected:raise RuntimeError('loop_terminal_mismatch')
        state['result'] = 'passed_live';persist()
        report = {'status': state['result'], 'workflowId': wid, 'nativeWritesThisInvocation': sum(x['method'] != 'GET' for x in calls), 'nativeCalls': calls, 'steps': {k: v['status'] for k, v in state['steps'].items()}, 'scope': 'native loop; manually supplied results; no model or product loop support claim'}
        write(DATA / 'report.json', report);print(json.dumps({'status': report['status'], 'nativeWrites': report['nativeWritesThisInvocation']}));return 0
    except Exception as exc:
        error = {'status': 'incomplete', 'code': str(exc) if isinstance(exc, RuntimeError) else 'probe_' + type(exc).__name__, 'nativeCalls': calls}
        write(DATA / ('failure-' + uuid.uuid4().hex + '.json'), error);print(json.dumps({'status': error['status'], 'code': error['code']}));return 1
    finally:sim.close()


if __name__ == '__main__':raise SystemExit(main())
