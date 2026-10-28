"""Real compiler, local ledgers, discovery and relay; HTTP/Sim execution are fixtures."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import httpx

from agent_line.adapters.sim import SimClient
from agent_line.adapters.sim_wait import SimWaitClient
from agent_line.completion_relay import CompletionRelay
from agent_line.publication import PublicationVerifier
from agent_line.sim_authoring import compile_document, decode_document, native_id
from agent_line.sim_journal import SimJournal
from probes.sim.compiled_parallel_probe import (
    AGENT, PROJECT, ROLE, CompiledProbe, discovery, document, inject_synthetic, open_context, policies,
    same_compiled_document,
)


class CompiledParallelIntegrationTests(unittest.TestCase):
    def fixture(self, policy='all_success', *, static_parallel=True):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        workspace = directory / 'synthetic-workspace'
        workspace.mkdir()
        context = open_context(directory, initialize=True)
        doc = document(policy)
        graph = compile_document(doc, 'workflow') | {'loops': {}, 'parallels': {}}
        inputs = {'synthetic': True, 'nonce': 'f' * 32, 'nested': {'preserve': [True, False, None]}}
        state = {'resumed': [], 'posts': [], 'getSelectors': [], 'uncertain': False,
                 'deployment': 'deployment', 'calls': []}
        ids = {branch: native_id('workflow', branch) for branch in ('left', 'right')}
        def handler(request):
            state['calls'].append((request.method, request.url.path))
            self.assertEqual(request.headers['x-api-key'], 'fixture-only-key')
            if request.method == 'POST':
                self.assertEqual(request.url.path, '/api/v2/workflows/workflow/runs/root/resume')
                body = json.loads(request.content)
                state['posts'].append(body)
                if state['uncertain']:
                    raise httpx.ReadTimeout('fixture-only-key', request=request)
                self.assertIn(body['contextId'], ids.values())
                self.assertNotIn(body['contextId'], state['resumed'])
                state['resumed'].append(body['contextId'])
                child = 'child-' + ('left' if body['contextId'] == ids['left'] else 'right')
                return httpx.Response(202, json={'data': {'runId': child, 'workflowId': 'workflow',
                    'contextId': body['contextId']}}, headers={'X-Run-Id': child})
            if request.url.path == '/api/v2/workflows/workflow/versions/1':
                return httpx.Response(200, json={'data': {'id': 'deployment', 'version': 1,
                    'isActive': True, 'state': graph}})
            if request.url.path == '/api/v2/logs/root':
                return httpx.Response(200, json={'data': {'runId': 'root', 'workflowId': 'workflow',
                    'deploymentVersionId': state['deployment'], 'workflowState': graph}})
            if request.url.path == '/api/resume/workflow/root':
                return httpx.Response(200, json={'workflowId': 'workflow', 'executionId': 'root',
                    'totalPauseCount': 2, 'resumedCount': len(state['resumed']), 'pausePoints': [
                        {'blockId': bid, 'contextId': bid, 'pauseKind': 'human', 'snapshotReady': True,
                         'resumeStatus': 'resumed' if bid in state['resumed'] else 'paused'}
                        for bid in ids.values()]})
            self.assertEqual(request.url.path, '/api/v2/workflows/workflow/runs/root')
            active = next((bid for bid in ids.values() if bid not in state['resumed']), ids['left'])
            value = {'runId': 'root', 'workflowId': 'workflow', 'status': 'paused',
                     'paused': {'pauseKind': 'human', 'pausePointCount': 2,
                                'resumedCount': len(state['resumed']),
                                'blockedOnBlockId': active, 'contextId': active}}
            selector = request.url.params.get('selectedOutputs')
            if selector:
                state['getSelectors'].append(selector)
                self.assertIn(selector, [bid + '.taskInput' for bid in ids.values()])
                value['blockOutputs'] = {selector: inputs}
            return httpx.Response(200, json={'data': value})
        transport = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(transport.close)
        sim = SimClient('http://sim.fixture', 'fixture-only-key', client=transport)
        waits, verifier = SimWaitClient(sim), PublicationVerifier(sim)
        context['runs'].reserve_tasks(project_id=PROJECT, workflow_id='workflow', run_id='root',
            revision='1', deployment_version_id='deployment', agent_policies=policies(doc, 'workflow'),
            static_parallel=static_parallel)
        context['runs'].record_submission('workflow', 'root', 'accepted')
        observer = discovery(context, sim, waits, verifier, workspace)
        return context, sim, waits, verifier, observer, workspace, doc, graph, inputs, ids, state

    def exercise(self, policy, left_passed, right_passed, expected_join_success):
        context, sim, waits, verifier, observer, workspace, doc, graph, inputs, ids, state = self.fixture(policy)
        self.assertEqual(decode_document(graph), doc)
        first = observer.tick()[0]
        self.assertEqual(first['status'], 'external_task_bound')
        self.assertEqual(len(first['tasks']), 2)
        tasks = {}
        for branch in ('left', 'right'):
            binding = context['pauses'].for_context('workflow', 'root', ids[branch])
            self.assertTrue(binding.allow_multiple)
            task = context['tasks'].get(binding.task_id)
            self.assertEqual(task['payload']['input']['taskInput'], inputs)
            self.assertEqual(task['payload']['roleAgentId'], AGENT)
            tasks[branch] = task['id']
        rebuilt = discovery(open_context(workspace.parent), sim, waits, verifier, workspace).tick()[0]
        self.assertEqual({item['taskId'] for item in rebuilt['tasks']}, set(tasks.values()))
        self.assertTrue(all(item['reusedExistingTask'] for item in rebuilt['tasks']))
        self.assertEqual(state['posts'], [])
        frozen = {}
        for branch in sorted(tasks, key=lambda branch: context['tasks'].events(tasks[branch])[0]['seq']):
            passed = left_passed if branch == 'left' else right_passed
            task = inject_synthetic(context['tasks'], tasks[branch], branch=branch,
                nonce=inputs['nonce'], verification_passed=passed)
            self.assertEqual(task['memoryStatus'], 'not_requested')
            self.assertEqual(set(task['executionIdentity']), {'upstreamTaskId'})
            self.assertTrue(task['executionIdentity']['upstreamTaskId'].startswith('synthetic-'))
            self.assertTrue(all(item['synthetic'] is True for item in task['evidence']))
            self.assertFalse(any(item.get('kind') == 'resource_exit' for item in task['evidence']))
            binding = context['pauses'].get(task['id'])
            relay = CompletionRelay(sim, context['journal'], wait_client=waits)
            delivered = relay.deliver(task, binding)
            self.assertEqual(delivered['status'], 'accepted')
            frozen[branch] = context['journal'].resume_input('workflow', 'root', binding.context_id)
            self.assertEqual(frozen[branch]['evidence'], task['evidence'])
            self.assertEqual(frozen[branch]['output'], task['output'])
            count = len(state['calls'])
            reopened = CompletionRelay(sim, SimJournal(context['journal'].path), wait_client=waits)
            self.assertEqual(reopened.deliver(task, binding), delivered)
            self.assertEqual(len(state['calls']), count)
        self.assertEqual([post['contextId'] for post in state['posts']], [ids[b] for b in sorted(tasks, key=lambda b: context['tasks'].events(tasks[b])[0]['seq'])])
        self.assertEqual(len(set(tasks.values())), 2)
        with context['tasks'].connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM tasks').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT count(*) FROM workspace_leases').fetchone()[0], 0)
        code = graph['blocks'][native_id('workflow', 'merge')]['subBlocks']['code']['value']
        for branch, name in [('left', '甲'), ('right', '乙')]:
            code = code.replace('<' + name + '.resumeInput>', json.dumps(frozen[branch]))
        # Actual compiler Function in Node, not a fake native workflow execution claim.
        source = 'try { console.log(JSON.stringify({ok:true,value:(()=>{' + code + '})()})); } catch(e) { console.log(JSON.stringify({ok:false,error:e.message})); }'
        execution = subprocess.run(['node', '-e', source], capture_output=True, text=True, check=True)
        result = json.loads(execution.stdout)
        self.assertEqual(result['ok'], expected_join_success)
        if expected_join_success:
            self.assertEqual(result['value']['output']['branches'], frozen)
            expected = [b for b, passed in [('left', left_passed), ('right', right_passed)] if passed]
            self.assertEqual(result['value']['output']['successfulBranches'], expected)
        else:
            self.assertEqual(result['error'], 'join_policy_unsatisfied')
        return context, sim, waits, state

    def test_compiled_all_success_preserves_both_original_store_results(self):
        self.exercise('all_success', True, True, True)

    def test_compiled_all_success_refuses_synthetic_verification_failure(self):
        self.exercise('all_success', True, False, False)

    def test_compiled_partial_success_preserves_failed_branch(self):
        self.exercise('allow_partial', True, False, True)

    def test_compiled_required_success_refuses_required_branch_failure(self):
        self.exercise('required_success', False, True, False)

    def test_uncertain_relay_reopens_without_a_second_native_submission(self):
        context, sim, waits, verifier, observer, workspace, doc, graph, inputs, ids, state = self.fixture()
        observer.tick()
        binding = context['pauses'].for_context('workflow', 'root', ids['left'])
        task = inject_synthetic(context['tasks'], binding.task_id, branch='left', nonce=inputs['nonce'],
                                verification_passed=True)
        state['uncertain'] = True
        result = CompletionRelay(sim, context['journal'], wait_client=waits).deliver(task, binding)
        self.assertEqual(result['status'], 'uncertain')
        self.assertEqual(len(state['posts']), 1)
        count = len(state['calls'])
        result2 = CompletionRelay(sim, SimJournal(context['journal'].path), wait_client=waits).deliver(task, binding)
        self.assertEqual(result2, result)
        self.assertEqual(len(state['posts']), 1)
        self.assertEqual(len(state['calls']), count)

    def test_changed_native_publication_prevents_any_discovery_task(self):
        context, sim, waits, verifier, observer, workspace, doc, graph, inputs, ids, state = self.fixture()
        state['deployment'] = 'other-deployment'
        self.assertEqual(observer.tick()[0]['status'], 'version_unverified')
        with context['tasks'].connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM tasks').fetchone()[0], 0)
        self.assertEqual(state['posts'], [])

    def test_no_static_parallel_flag_keeps_multiple_pause_gate_closed(self):
        context, sim, waits, verifier, observer, workspace, doc, graph, inputs, ids, state = self.fixture(static_parallel=False)
        result = observer.tick()[0]
        self.assertNotEqual(result['status'], 'external_task_bound')
        with context['tasks'].connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM tasks').fetchone()[0], 0)
        self.assertEqual(state['posts'], [])

    def test_document_roundtrip_ignores_only_compiled_mapping_order(self):
        expected = document()
        reordered = copy.deepcopy(expected)
        reordered['nodes'] = reordered['nodes'][2:3] + reordered['nodes'][:2] + reordered['nodes'][3:]
        self.assertNotEqual(reordered, expected)
        self.assertTrue(same_compiled_document(reordered, expected, 'workflow'))
        reordered['nodes'][0]['position']['x'] += 1
        self.assertFalse(same_compiled_document(reordered, expected, 'workflow'))
        reordered = copy.deepcopy(expected)
        reordered['nodes'][2]['config']['instruction'] = 'changed instruction'
        self.assertFalse(same_compiled_document(reordered, expected, 'workflow'))

    def recovery_fixture(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        directory.chmod(0o700)
        context = open_context(directory, initialize=True)
        workspace = directory / 'synthetic-workspace'
        workspace.mkdir(mode=0o700)
        wid = 'fcad35eb-e5ed-421d-a2e7-3071c16bcaf1'
        graph = compile_document(document(), wid)
        p = CompiledProbe.__new__(CompiledProbe)
        p.existed, p.directory = True, directory
        p.workspace = 'workspace'
        p.state = {'workflowId': wid, 'runs': {}, 'steps': {
            'create': {'status': 'received', 'method': 'POST', 'path': '/api/v2/workflows',
                       'result': {'id': wid}},
            'all_success-save': {'status': 'received', 'method': 'PUT',
                'path': f'/api/v2/workflows/{wid}/state', 'payload': graph,
                'result': {'id': wid, 'warnings': []}}},
            'syntheticConfiguration': {'projectId': PROJECT, 'roleBindingId': ROLE,
                'roleAgentId': AGENT, 'synthetic': True, 'dispatcherConfigured': False,
                'workspace': str(workspace), 'requireMemoryCommit': False}}
        calls = []
        def request(method, path, **kwargs):
            calls.append((method, path))
            self.assertEqual(method, 'GET')
            if path.endswith('/versions'):
                return []
            if path.endswith('/state'):
                return graph | {'loops': {}, 'parallels': {}}
            return {'id': wid, 'workspaceId': 'workspace', 'isDeployed': False}
        p.request = request
        p.save = lambda: None
        p.run_case = lambda case: calls.append(('synthetic-run-case', case[0]))
        p.report = lambda status: {'status': status}
        return p, context, calls

    def test_initial_continuation_reuses_original_create_and_save_receipts(self):
        probe, context, calls = self.recovery_fixture()
        self.assertEqual(probe.continue_initial()['status'], 'passed_live_synthetic_execution')
        self.assertEqual([method for method, path in calls[:3]], ['GET', 'GET', 'GET'])
        self.assertEqual([path for method, path in calls if method == 'synthetic-run-case'],
                         ['all_pass', 'all_verification_failure', 'partial_verification_failure',
                          'required_verification_failure'])
        self.assertTrue(probe._reuse_initial_save)
        self.assertEqual(probe.state['recovery']['status'], 'completed')
        self.assertTrue(all(value == 0 for value in probe.state['recovery']['emptyDatabaseCounts'].values()))
        with self.assertRaisesRegex(RuntimeError, 'recovery_state_invalid'):
            probe.continue_initial()

    def test_initial_continuation_rejects_unknown_or_later_phase(self):
        for mutate in (
            lambda s: s['steps']['all_success-save'].update(status='intent'),
            lambda s: s['steps'].update(publish={'status': 'intent'}),
            lambda s: s['runs'].update(root={'runId': 'root'}),
            lambda s: s.update(recovery={'status': 'validated'}),
            lambda s: s.update(workflowId='different-workflow'),
        ):
            probe, context, calls = self.recovery_fixture()
            mutate(probe.state)
            with self.subTest(mutate=mutate), self.assertRaises(RuntimeError):
                probe.continue_initial()
            self.assertEqual(calls, [])

    def test_initial_continuation_refuses_any_existing_native_journal_operation(self):
        probe, context, calls = self.recovery_fixture()
        # Use the real journal's supported reservation to prove nonempty records reject.
        context['journal'].reserve_publication('workflow', 'run', {'fixture': True})
        with self.assertRaisesRegex(RuntimeError, 'database_not_empty'):
            probe.continue_initial()
        self.assertEqual(calls, [])

    def test_synthetic_terminal_cannot_be_injected_twice(self):
        context, sim, waits, verifier, observer, workspace, doc, graph, inputs, ids, state = self.fixture()
        observer.tick()
        binding = context['pauses'].for_context('workflow', 'root', ids['left'])
        inject_synthetic(context['tasks'], binding.task_id, branch='left', nonce=inputs['nonce'],
                         verification_passed=True)
        with self.assertRaisesRegex(RuntimeError, 'already_started'):
            inject_synthetic(context['tasks'], binding.task_id, branch='left', nonce=inputs['nonce'],
                             verification_passed=True)
        self.assertEqual(state['posts'], [])


if __name__ == '__main__':
    unittest.main()
