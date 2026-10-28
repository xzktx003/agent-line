#!/usr/bin/env python3
"""Bounded native Sim fan-out/join proof, using synthetic Human v2 results only."""
import argparse
import json
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agent_line.adapters.sim import SimClient, segment
from agent_line.adapters.sim_wait import SimWaitClient
from probes.sim.authoring_probe import write
from probes.sim.workflow_probe import block, edge, response

DATA = ROOT / 'var/sim-parallel-native'
TERMINAL = {'completed', 'failed', 'cancelled'}
JOIN_CODE = '''const a = <甲.resumeInput>;
const b = <乙.resumeInput>;
const spec = <开始.input>;
if (!spec || typeof spec !== "object" || Array.isArray(spec) ||
    typeof spec.nonce !== "string" || !/^[a-f0-9]{32}$/.test(spec.nonce))
  throw new Error("parallel_spec_invalid");
for (const [name, result] of [["a", a], ["b", b]]) {
  if (!result || typeof result !== "object" || Array.isArray(result) ||
      result.branch !== name || typeof result.ok !== "boolean" ||
      result.nonce !== spec.nonce)
    throw new Error("parallel_result_invalid");
}
let success;
if (spec.policy === "all_success") success = a.ok && b.ok;
else if (spec.policy === "allow_partial") success = a.ok || b.ok;
else if (spec.policy === "required_success" && spec.required === "a") success = a.ok;
else throw new Error("parallel_policy_invalid");
if (!success) throw new Error("parallel_policy_failed");
return {policy:spec.policy, nonce:spec.nonce, a:a, b:b, success:true};'''
CASES = (
    ('all_success', 'all_success', True, True, 'completed', None),
    ('all_failure', 'all_success', True, False, 'failed', 'parallel_policy_failed'),
    ('partial_success', 'allow_partial', True, False, 'completed', None),
    ('required_failure', 'required_success', False, True, 'failed', 'parallel_policy_failed'),
    ('missing_result', 'all_success', True, None, 'failed', 'parallel_result_invalid'),
)


def graph(prefix):
    start = block(prefix + '_start', 'start_trigger')
    start['name'] = '开始'
    waits = []
    for branch, name, y in [('a', '甲', 0), ('b', '乙', 230)]:
        wait = block(prefix + '_' + branch, 'human_in_the_loop_v2', {
            'inputFormat': ('input-format', [{'id': 'taskInput', 'name': 'taskInput',
                'type': 'object', 'value': json.dumps({'branch': branch})}]),
            'notification': ('tool-input', []), 'builderData': ('response-format', [])}, 250, y)
        wait['name'] = name
        waits.append(wait)
    join = block(prefix + '_join', 'function', {'language': ('dropdown', 'javascript'),
                  'code': ('code', JOIN_CODE)}, 500, 120)
    join['name'] = '汇合'
    end = response(prefix + '_end', {}, 120)
    end['name'] = '结束'
    end['subBlocks']['data']['value'] = '{"joined": <汇合.result>}'
    blocks = [start, *waits, join, end]
    edges = [edge(start['id'], wait['id']) for wait in waits]
    edges += [edge(wait['id'], join['id']) for wait in waits]
    edges.append(edge(join['id'], end['id']))
    return {'blocks': {b['id']: b for b in blocks}, 'edges': edges,
            'loops': {}, 'parallels': {}, 'variables': {}}


def verify_graph(actual, expected):
    if not isinstance(actual, dict) or set(actual.get('blocks', {})) != set(expected['blocks']):
        raise RuntimeError('published_blocks_mismatch')
    for bid, wanted in expected['blocks'].items():
        got = actual['blocks'][bid]
        if (got.get('type') != wanted['type'] or got.get('name') != wanted['name']
                or got.get('enabled') is not True or (got.get('data') or {}).get('parentId')):
            raise RuntimeError('published_block_identity_mismatch')
        for name, value in wanted['subBlocks'].items():
            if got.get('subBlocks', {}).get(name, {}).get('value') != value['value']:
                raise RuntimeError('published_block_config_mismatch')
    if any(e.get('sourceHandle') or e.get('targetHandle') for e in actual.get('edges', [])):
        raise RuntimeError('published_edge_routing_mismatch')
    actual_edges = {(e['source'], e['target']) for e in actual.get('edges', [])}
    expected_edges = {(e['source'], e['target']) for e in expected['edges']}
    if actual_edges != expected_edges or len(actual.get('edges', [])) != len(expected['edges']):
        raise RuntimeError('published_edges_mismatch')
    if actual.get('loops') or actual.get('parallels'):
        raise RuntimeError('unexpected_container_scope')


class Probe:
    def __init__(self, authpath, directory):
        if authpath.stat().st_mode & 0o077:
            raise RuntimeError('credential_permissions')
        auth = json.loads(authpath.read_text())
        self.workspace = segment(auth['workspaceId'])
        self.sim = SimClient(auth['baseUrl'], auth['apiKey'])
        self.waits = SimWaitClient(self.sim)
        self.directory = directory
        self.path = directory / 'state.json'
        self.existed = self.path.exists()
        if self.existed and (self.path.stat().st_mode & 0o077):
            raise RuntimeError('state_permissions')
        self.state = json.loads(self.path.read_text()) if self.existed else {
            'schemaVersion': 1, 'workspaceId': self.workspace, 'baseUrl': self.sim.base_url,
            'prefix': 'pn_' + uuid.uuid4().hex, 'steps': {}, 'runs': {}}
        if (self.state.get('schemaVersion') != 1 or self.state['workspaceId'] != self.workspace
                or self.state['baseUrl'] != self.sim.base_url):
            raise RuntimeError('state_origin_or_workspace_changed')
        self.calls = []
        self.sim._client.event_hooks['request'].append(self.record_call)

    def record_call(self, request):
        self.calls.append({'method': request.method, 'path': request.url.path})

    def save(self):
        write(self.path, self.state)

    def request(self, method, path, body=None, *, bare=False, params=None, missing=False, allow_list=False):
        r = self.sim._client.request(method, self.sim.base_url + path,
            headers={'x-api-key': self.sim._key}, json=body, params=params, follow_redirects=False)
        if missing and r.status_code == 404:
            return None
        if r.status_code not in (200, 201, 202):
            raise RuntimeError('native_response_' + str(r.status_code))
        payload = r.json()
        if not isinstance(payload, dict) or payload.get('success') is False or 'error' in payload:
            raise RuntimeError('native_envelope_invalid')
        data = payload if bare else payload.get('data')
        if not isinstance(data, dict) and not (allow_list and isinstance(data, list)):
            raise RuntimeError('native_data_invalid')
        return data

    def once(self, key, method, path, payload, operation):
        # Any existing step requires reconciliation, even when a receipt is present.
        # This process alone may advance its original new deployment; re-entry is read-only.
        if key in self.state['steps']:
            raise RuntimeError('existing_mutation_no_resubmit')
        self.state['steps'][key] = {'status': 'intent', 'method': method,
            'path': path, 'payload': payload, 'recordedAt': time.time()}
        self.save()
        result = operation()
        self.state['steps'][key].update(status='received', result=result, receivedAt=time.time())
        self.save()
        return result

    def mutate(self, key, method, path, payload):
        return self.once(key, method, path, payload,
                         lambda: self.request(method, path, payload))

    def observe(self, run):
        wid = segment(self.state['workflowId'])
        snapshots = {}
        for rid in dict.fromkeys([run['runId']] + run['children']):
            value = self.request('GET', f'/api/v2/workflows/{wid}/runs/{segment(rid)}',
                                 params={'includeOutput': 'true'})
            if value.get('runId') != rid or value.get('workflowId') != wid:
                raise RuntimeError('run_observation_identity_mismatch')
            snapshots[rid] = value
        detail = self.request('GET', f'/api/resume/{wid}/{segment(run["runId"])}',
                              bare=True, missing=True)
        if detail is not None and (detail.get('executionId') != run['runId']
                                  or detail.get('workflowId') != wid):
            raise RuntimeError('pause_detail_identity_mismatch')
        # The helper independently reads the same fixed root. Its projection can be newer
        # than the raw snapshot; predicates use this complete read, never mixed fields.
        projection = self.waits.read_pauses(wid, run['runId']) if detail is not None else None
        observed = {'time': time.time(), 'runs': snapshots, 'pauseDetail': detail,
                    'pauseProjection': projection}
        run.setdefault('observations', []).append(observed)
        self.save()
        return observed

    def wait(self, run, predicate, failure):
        for _ in range(80):
            observed = self.observe(run)
            if predicate(observed):
                return observed
            if any(v.get('status') in ('failed', 'cancelled') for v in observed['runs'].values()):
                raise RuntimeError(failure)
            time.sleep(0.25)
        raise RuntimeError(failure + '_observation_timeout_no_resubmit')

    def points(self, observed):
        detail = observed.get('pauseProjection') or {}
        points = detail.get('pausePoints', [])
        if not isinstance(points, list):
            raise RuntimeError('pause_points_invalid')
        mapped = {}
        expected = {self.state['prefix'] + '_' + b for b in ('a', 'b')}
        for point in points:
            if not isinstance(point, dict) or point.get('blockId') not in expected:
                raise RuntimeError('unexpected_pause_scope')
            bid = point['blockId']
            if bid in mapped or point.get('contextId') != bid or point.get('pauseKind') != 'human':
                raise RuntimeError('pause_context_invalid')
            mapped[bid] = point
        return mapped

    def both_waiting(self, observed):
        points = self.points(observed)
        detail = observed.get('pauseProjection') or {}
        return (detail.get('totalPauseCount') == 2 and detail.get('resumedCount') == 0
            and len(points) == 2 and all(p.get('resumeStatus') == 'paused'
            and p.get('snapshotReady') is True for p in points.values()))

    def first_resumed(self, observed):
        points = self.points(observed)
        prefix = self.state['prefix']
        a, b = points.get(prefix + '_a', {}), points.get(prefix + '_b', {})
        detail = observed.get('pauseProjection') or {}
        if not (detail.get('totalPauseCount') == 2 and detail.get('resumedCount') == 1
                and a.get('resumeStatus') == 'paused' and a.get('snapshotReady') is True
                and b.get('resumeStatus') == 'resumed'):
            return False
        self.no_early_join(observed)
        for result in observed['runs'].values():
            outputs = result.get('blockOutputs') or {}
            if prefix + '_join' in outputs or prefix + '_end' in outputs or result.get('output'):
                raise RuntimeError('join_ran_before_second_result')
        return True

    def no_early_join(self, observed):
        # The raw snapshot is private evidence only. Its actual execution collections,
        # rather than workflow topology or unresolved selectedOutputs, prove no join ran.
        detail = observed.get('pauseDetail') or {}
        envelope = detail.get('executionSnapshot') or {}
        encoded = envelope.get('snapshot')
        if not isinstance(encoded, str) or len(encoded.encode()) > 1048576:
            raise RuntimeError('execution_snapshot_unavailable')
        decoded = json.loads(encoded)
        state = decoded.get('state') if isinstance(decoded, dict) else None
        if not isinstance(state, dict):
            raise RuntimeError('execution_snapshot_state_invalid')
        states, executed, logs = (state.get(k) for k in ('blockStates', 'executedBlocks', 'blockLogs'))
        if (not isinstance(states, dict) or not isinstance(executed, list)
                or not isinstance(logs, list) or not logs
                or any(not isinstance(i, str) for i in executed)
                or any(not isinstance(i, dict) or not isinstance(i.get('blockId'), str) for i in logs)):
            raise RuntimeError('execution_snapshot_collections_invalid')
        seen = set(states) | set(executed) | {i['blockId'] for i in logs}
        prefix = self.state['prefix']
        before_join = {prefix + '_' + b for b in ('start', 'a', 'b')}
        if not before_join.issubset(seen) or not seen.issubset(before_join):
            raise RuntimeError('join_ran_before_second_result')
        return {'blockStates': sorted(states), 'executedBlocks': executed,
                'blockLogs': [i['blockId'] for i in logs]}

    def continue_known(self):
        # One narrow recovery of the witnessed initial read-selector failure only.
        # It cannot replay execute, advance an unknown intent, or resume a later interruption.
        required = {'create', 'save', 'publish', 'all_success-execute'}
        if (not self.existed or set(self.state['steps']) != required
                or any(v.get('status') != 'received' for v in self.state['steps'].values())
                or set(self.state['runs']) != {'all_success'} or self.state.get('recovery')):
            raise RuntimeError('known_recovery_state_invalid')
        run = self.state['runs']['all_success']
        if (run.get('runId') != 'fb72d1fa-1f16-4a18-9b56-16cd620b006c'
                or run.get('children') != [] or run.get('contexts') != {}
                or run.get('expectedStatus') != 'completed' or run.get('expectedError') is not None
                or run.get('result')):
            raise RuntimeError('known_recovery_run_invalid')
        spec, supplied = run['inputs'], run['suppliedResults']
        nonce = spec.get('nonce')
        if (not isinstance(nonce, str) or len(nonce) != 32
                or any(c not in '0123456789abcdef' for c in nonce)
                or spec != {'policy': 'all_success', 'required': 'a', 'nonce': nonce}
                or supplied != {b: {'branch': b, 'ok': True, 'nonce': nonce} for b in ('a', 'b')}):
            raise RuntimeError('known_recovery_inputs_changed')
        wid = segment(self.state['workflowId'])
        steps = self.state['steps']
        execute = steps['all_success-execute']
        receipt = execute.get('result') or {}
        if (execute.get('method') != 'POST'
                or execute.get('path') != f'/api/v2/workflows/{wid}/execute'
                or execute.get('payload') != {'runId': run['runId'], 'input': {'input': spec}}
                or receipt.get('runId') != run['runId'] or receipt.get('workflowId') != wid
                or receipt.get('transportStatus') != 'accepted'
                or steps['create']['result'].get('id') != wid):
            raise RuntimeError('known_recovery_receipt_mismatch')
        expected = graph(self.state['prefix'])
        if self.state.get('graph') != expected:
            raise RuntimeError('known_recovery_graph_changed')
        deployment = steps['publish']['result']
        base = '/api/v2/workflows/' + wid
        published = self.request('GET', base + '/versions/' + str(deployment['version']))
        verify_graph(published.get('state'), expected)
        verify_graph(self.request('GET', base + '/state'), expected)
        if (published.get('id') != self.state['published'].get('id')
                or published.get('id') != deployment['activeDeployment'].get('deploymentVersionId')
                or published.get('isActive') is not True):
            raise RuntimeError('known_recovery_published_version_changed')
        observed = self.observe(run)
        if (not self.both_waiting(observed)
                or observed['runs'][run['runId']].get('status') != 'paused'):
            raise RuntimeError('known_recovery_original_run_not_dual_paused')
        proof = self.no_early_join(observed)
        self.state['recovery'] = {'status': 'validated', 'originalRunId': run['runId'],
            'recordedAt': time.time(), 'publishedVersionId': published['id'], 'noEarlyJoin': proof}
        self.save()
        self.run_case(CASES[0], existing=True)
        for case in CASES[1:]:
            self.run_case(case)
        self.state['result'] = 'passed_live'
        self.state['recovery']['status'] = 'completed'
        self.save()
        return self.report('passed_live')

    def execute(self):
        if self.existed:
            raise RuntimeError('existing_state_is_observation_only')
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.stat().st_mode & 0o077:
            raise RuntimeError('evidence_directory_permissions')
        self.save()
        # Verify authenticated access before the first mutation; no process/service changes.
        self.request('GET', '/api/v2/workflows', params={'workspaceId': self.workspace, 'limit': 1}, allow_list=True)
        payload = {'workspaceId': self.workspace, 'name': 'agent-line-native-parallel-probe',
                   'description': 'Two synthetic Human v2 branches; no model, role, memory, or notifications'}
        created = self.mutate('create', 'POST', '/api/v2/workflows', payload)
        wid = segment(created['id'])
        self.state['workflowId'] = wid
        expected = graph(self.state['prefix'])
        self.state['graph'] = expected
        self.save()
        base = '/api/v2/workflows/' + wid
        writable = {k: expected[k] for k in ('blocks', 'edges', 'variables')}
        saved = self.mutate('save', 'PUT', base + '/state', writable)
        if saved.get('warnings'):
            raise RuntimeError('graph_normalization_warning')
        deployment = self.mutate('publish', 'POST', base + '/deploy', {})
        published = self.request('GET', base + '/versions/' + str(deployment['version']))
        verify_graph(published.get('state'), expected)
        active = deployment.get('activeDeployment') or {}
        if published.get('id') != active.get('deploymentVersionId') or not published.get('id'):
            raise RuntimeError('published_version_identity_mismatch')
        self.state['published'] = published
        self.save()
        for case in CASES:
            self.run_case(case)
        self.state['result'] = 'passed_live'
        self.save()
        return self.report('passed_live')

    def run_case(self, case, *, existing=False):
        name, policy, ok_a, ok_b, expected_status, expected_error = case
        wid, prefix = self.state['workflowId'], self.state['prefix']
        if existing:
            run = self.state['runs'][name]
            spec, supplied = run['inputs'], run['suppliedResults']
            nonce = spec['nonce']
        else:
            nonce = uuid.uuid4().hex
            supplied = {'a': {'branch': 'a', 'ok': ok_a, 'nonce': nonce},
                        'b': {'branch': 'b', 'nonce': nonce}}
            if ok_b is not None:
                supplied['b']['ok'] = ok_b
            spec = {'policy': policy, 'required': 'a', 'nonce': nonce}
            run = {'runId': str(uuid.uuid4()), 'children': [], 'inputs': spec,
                   'suppliedResults': supplied, 'expectedStatus': expected_status,
                   'expectedError': expected_error, 'contexts': {}}
            self.state['runs'][name] = run
            self.save()  # Fixed run identity and both synthetic results precede submission.
            path = f'/api/v2/workflows/{wid}/execute'
            self.once(name + '-execute', 'POST', path, {'runId': run['runId'], 'input': {'input': spec}},
                      lambda: self.sim.execute(wid, run['runId'], {'input': spec}))
        paused = self.wait(run, self.both_waiting, 'both_pauses_not_observed')
        run['initialNoEarlyJoin'] = self.no_early_join(paused)
        run['initialParallelPause'] = paused
        points = self.points(paused)
        run['contexts'] = {b: points[prefix + '_' + b]['contextId'] for b in ('a', 'b')}
        self.save()
        # Reverse the graph's stable A/B order. Both contexts are frozen before either resume.
        for branch in ('b', 'a'):
            context = run['contexts'][branch]
            payload = {'runId': run['runId'], 'contextId': context, 'input': supplied[branch]}
            path = f'/api/v2/workflows/{wid}/runs/{run["runId"]}/resume'
            receipt = self.once(name + '-resume-' + branch, 'POST', path, payload,
                lambda: self.sim.resume(wid, run['runId'], context, supplied[branch]))
            child = segment(receipt['resumedRunId'])
            if child in [run['runId']] + run['children']:
                raise RuntimeError('resume_child_identity_reused')
            run['children'].append(child)
            self.save()
            if branch == 'b':
                run['afterFirstResume'] = self.wait(run, self.first_resumed,
                                                   'first_result_join_guard_not_observed')
                self.save()
        final_id = run['children'][-1]
        def terminal(observed):
            if observed['runs'][final_id].get('status') not in TERMINAL:
                return False
            points = self.points(observed)
            return len(points) == 2 and all(
                p.get('resumeStatus') in ('resumed', 'failed') for p in points.values())
        # Expected failures are terminal proof, not a reason to stop observation before checking.
        for _ in range(80):
            final = self.observe(run)
            if terminal(final):
                break
            time.sleep(0.25)
        else:
            raise RuntimeError('terminal_observation_timeout_no_resubmit')
        final_points = self.points(final)
        final_detail = final.get('pauseProjection') or {}
        expected_a_status = ('resumed',) if expected_status == 'completed' else ('resumed', 'failed')
        statuses = {p['blockId']: p['resumeStatus'] for p in final_points.values()}
        if (len(final_points) != 2 or final_detail.get('totalPauseCount') != 2
                or statuses.get(prefix + '_b') != 'resumed'
                or statuses.get(prefix + '_a') not in expected_a_status
                or final_detail.get('resumedCount') != sum(v == 'resumed' for v in statuses.values())):
            raise RuntimeError('original_context_terminal_states_not_confirmed')
        result = final['runs'][final_id]
        run['final'] = final
        self.save()
        if result.get('status') != expected_status:
            raise RuntimeError('join_terminal_status_mismatch')
        if expected_status == 'completed':
            expected = {'joined': {'policy': policy, 'nonce': nonce, 'a': supplied['a'],
                                  'b': supplied['b'], 'success': True}}
            if (result.get('output') or {}).get('data') != expected:
                raise RuntimeError('joined_both_results_or_nonce_mismatch')
            # The exact completed Response already proves Function execution; now it is
            # safe to select that emitted output. Unexecuted selectors return native 400.
            selected = self.request('GET', f'/api/v2/workflows/{wid}/runs/{final_id}',
                params={'includeOutput': 'true', 'selectedOutputs': prefix + '_join'})
            if selected.get('runId') != final_id or selected.get('workflowId') != wid:
                raise RuntimeError('join_selection_identity_mismatch')
            run['completedJoinSelection'] = selected
            self.save()
            joined = (selected.get('blockOutputs') or {}).get(prefix + '_join', {})
            if joined.get('result') != expected['joined']:
                raise RuntimeError('native_join_block_result_mismatch')
        elif expected_error not in json.dumps(result.get('error'), ensure_ascii=False):
            raise RuntimeError('expected_join_failure_reason_not_proven')
        run['result'] = 'passed_live'
        self.save()
        print(json.dumps({'case': name, 'status': 'passed_live', 'nativeStatus': expected_status}), flush=True)

    def inspect(self):
        if self.existed and self.state.get('workflowId'):
            for run in self.state['runs'].values():
                self.observe(run)
        return self.report('observation_only' if self.existed else 'not_executed')

    def report(self, status):
        report = {'status': status, 'priorResult': self.state.get('result'),
            'workflowId': self.state.get('workflowId'),
            'nativeWritesThisInvocation': sum(c['method'] != 'GET' for c in self.calls),
            'nativeCalls': self.calls,
            'cases': {name: {'runId': run['runId'], 'children': run['children'],
                'expectedStatus': run['expectedStatus'], 'result': run.get('result')}
                for name, run in self.state['runs'].items()},
            'scope': 'Native static DAG fan-out/join; synthetic Human v2 results only; '
                     'no model, QwenPaw, Codex, memory, notifications, or product parallel support claim'}
        if self.directory.exists():
            write(self.directory / ('observation.json' if status != 'passed_live' else 'report.json'), report)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--execute', action='store_true', help='first invocation only; creates one dedicated native graph')
    mode.add_argument('--continue-known', action='store_true',
        help='continue only the witnessed initial read interruption, using its original paused run')
    parser.add_argument('--config', type=Path, default=ROOT / 'var/sim/probe-auth.json')
    parser.add_argument('--directory', type=Path, default=DATA)
    parser.add_argument('--graph', action='store_true', help='print a credential-free graph; no network requests')
    args = parser.parse_args()
    if args.graph:
        print(json.dumps(graph('parallel_preview'), ensure_ascii=False, indent=2))
        return 0
    probe = Probe(args.config, args.directory)
    try:
        if args.continue_known:
            report = probe.continue_known()
        else:
            report = probe.execute() if args.execute and not probe.existed else probe.inspect()
        print(json.dumps({k: report[k] for k in ('status', 'nativeWritesThisInvocation')}, ensure_ascii=False))
        return 0
    except Exception as exc:
        failure = {'status': 'incomplete', 'errorType': type(exc).__name__,
                   'code': str(exc) if isinstance(exc, RuntimeError) else 'probe_check_failed',
                   'nativeCalls': probe.calls,
                   'nativeWritesThisInvocation': sum(c['method'] != 'GET' for c in probe.calls)}
        if args.directory.exists():
            write(args.directory / ('failure-' + uuid.uuid4().hex + '.json'), failure)
        print(json.dumps({k: failure[k] for k in ('status', 'errorType', 'code')}, ensure_ascii=False))
        return 1
    finally:
        probe.sim.close()


if __name__ == '__main__':
    raise SystemExit(main())
