"""Offline proofs for the real three-round independent-test failure probe."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock

import httpx
from fastapi.testclient import TestClient

from agent_line.adapters.sim import SimClient
from agent_line.approval_discovery import WorkflowRunLedger
from agent_line.pause_bindings import PauseBindings
from agent_line.store import TaskStore
from agent_line.test_executor import DockerTestExecutor
from agent_line.sim_authoring import SimAuthoringClient
from agent_line.sim_authoring import compile_document, decode_document, native_id
from agent_line.sim_journal import SimJournal
from agent_line.verification_policy import validate_policy
from probes.full_workflow.parallel_roles import once
from probes.full_workflow.test_loop import (
    CONTENT, FAILURE_LINE, KIND, TEMPLATE, check_guard, checked_template, document,
    failed_test, feedback_proof, run_probe, verification_policy, build_application, PROJECT, BINDING,
)
from probes.sim.authoring_probe import write


class TestLoopProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / 'new'
        self.source = self.root / 'source'; self.source.mkdir(mode=0o700)
        self.auth = self.root / 'auth.json'
        write(self.auth, {'baseUrl': 'http://sim.fixture', 'apiKey': 'private-secret', 'workspaceId': 'fixture-workspace'})

    def test_default_is_inert_without_credentials_and_does_not_create_directory(self):
        self.auth.unlink(); factory = Mock()
        result = run_probe(directory=self.directory, auth_path=self.auth, sim_factory=factory)
        self.assertEqual(result['status'], 'not_executed')
        self.assertFalse(self.directory.exists()); factory.assert_not_called()

    def test_missing_template_proof_blocks_before_any_host_or_state_initialization(self):
        sim = SimClient('http://sim.fixture', 'private-secret', client=httpx.Client(transport=httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(AssertionError('No native IO needed')))))
        with self.assertRaisesRegex(RuntimeError, 'loop_root_check_missing'):
            run_probe(execute=True, directory=self.directory, source=self.source,
                      auth_path=self.auth, sim_factory=lambda *_: sim)
        self.assertFalse(self.directory.exists())

    def template(self):
        journal = SimJournal.initialize(self.source / 'sim.sqlite')
        doc = document(); graph = compile_document(doc, TEMPLATE)
        old = {'workflowId': TEMPLATE, 'result': 'passed_live', 'document': doc,
               'publication': {'revision': 1, 'deploymentVersionId': 'deployed-one'}, 'cases': {}}
        proof = {'status': 'passed_read_only', 'workflowId': TEMPLATE, 'cases': {}}
        observed, history = {}, []
        for name, rounds in (('early_success', 2), ('exhausted', 3)):
            root = 'root-' + name; chain = [root]; tasks = {}
            for iteration in range(rounds):
                context = native_id(TEMPLATE, 'agent') + '_loop' + str(iteration)
                child = 'child-' + name + '-' + str(iteration)
                remote = Mock()
                remote.resume.return_value = {'workflowId': TEMPLATE, 'runId': root,
                    'contextId': context, 'resumedRunId': child, 'transportStatus': 'accepted'}
                journal.resume(remote, TEMPLATE, root, context, {'fixture': True})
                task = {'id': name + str(iteration), 'executionStatus': 'succeeded', 'resourcesReleased': True,
                        'verificationStatus': 'failed', 'evidence': [{'kind': 'resource_exit'}],
                        'payload': {'workflow': {'attemptId': str(iteration + 1), 'nodeExecutionId': context}}}
                tasks[task['id']] = {'task': task, 'resumeReceipt': journal.get(TEMPLATE, root, context)}
                chain.append(child)
            case = {'result': 'passed_live', 'runId': root, 'finalRunId': chain[-1], 'tasks': tasks}
            old['cases'][name] = case
            proof['cases'][name] = {'runId': root, 'finalRunId': chain[-1], 'acceptedChainRunIds': chain}
            for index, rid in enumerate(chain):
                observed[rid] = {'status': 'completed' if index == len(chain) - 1 else 'paused',
                    'response': {'data': {'runId': rid, 'workflowId': TEMPLATE}}}
            history.append({'runId': root, 'workflowId': TEMPLATE, 'status': 'completed'})
        write(self.source / 'state.json', old)
        write(self.source / 'report.json', {'status': 'passed_live', 'workflowId': TEMPLATE, 'cases': old['cases']})
        write(self.source / 'root-check.json', proof)
        api = Mock()
        api.get_detail.return_value = {'id': TEMPLATE}; api.get_state.return_value = graph
        api.get_version.return_value = {'id': 'deployed-one', 'isActive': True, 'state': graph}
        sim = Mock(); sim.base_url = 'http://sim.fixture'; sim._key = 'private-secret'
        sim.observe.side_effect = lambda wid, rid: copy.deepcopy(observed[rid])
        response = Mock(); response.json.side_effect = lambda: {'data': copy.deepcopy(history)}
        sim._client.get.return_value = response
        return old, proof, sim, api, observed, history

    def test_template_accepts_root_only_listing_and_individually_reads_all_seven_chain_ids(self):
        old, _, sim, api, _, history = self.template()
        actual, proof = checked_template(self.source, sim, api)
        self.assertEqual(actual, old)
        self.assertEqual(len(proof['allowedRunIds']), 7)
        self.assertEqual(len(history), 2)
        self.assertEqual(sim.observe.call_count, 7)
        sim.execute.assert_not_called(); sim.resume.assert_not_called()

    def test_unknown_root_unconfirmed_child_or_changed_draft_blocks_reuse(self):
        _, _, sim, api, observed, history = self.template()
        history.append({'runId': 'unknown', 'workflowId': TEMPLATE, 'status': 'completed'})
        with self.assertRaisesRegex(RuntimeError, 'unknown_or_unresolved'): checked_template(self.source, sim, api)
        history.pop()
        observed['child-exhausted-0']['status'] = 'running'
        with self.assertRaisesRegex(RuntimeError, 'not_terminal'): checked_template(self.source, sim, api)
        observed['child-exhausted-0']['status'] = 'paused'
        changed = document(); changed['nodes'][0]['name'] = 'changed'; api.get_state.return_value = compile_document(changed, TEMPLATE)
        with self.assertRaisesRegex(RuntimeError, 'draft_changed'): checked_template(self.source, sim, api)

    def test_unknown_journal_resume_and_inconsistent_root_proof_cannot_be_adopted(self):
        _, proof, sim, api, _, _ = self.template()
        proof['cases']['exhausted']['acceptedChainRunIds'][1] = 'invented-child'
        write(self.source / 'root-check.json', proof)
        with self.assertRaisesRegex(RuntimeError, 'root_chain'): checked_template(self.source, sim, api)

    def test_existing_state_is_get_only_even_with_execute_and_does_not_read_template(self):
        self.directory.mkdir(mode=0o700)
        state = {'kind': KIND, 'workflowId': 'existing', 'runId': 'original'}
        write(self.directory / 'state.json', state)
        before = (self.directory / 'state.json').read_bytes(); calls = []
        def handler(request):
            calls.append(request.method)
            return httpx.Response(200, json={'data': {'workflowId': 'existing', 'runId': 'original', 'status': 'paused'}})
        def factory(base, key):
            return SimClient(base, key, client=httpx.Client(transport=httpx.MockTransport(handler)))
        for execute in (False, True):
            result = run_probe(execute=execute, directory=self.directory, source=self.source,
                               auth_path=self.auth, sim_factory=factory)
            self.assertEqual(result['status'], 'observation_only')
            self.assertEqual(result['newSubmissions'], 0)
            self.assertEqual((self.directory / 'state.json').read_bytes(), before)
        self.assertEqual(calls, ['GET', 'GET'])

    def test_live_assembly_connects_real_taskflow_verifier_executor_and_catalog_without_model_or_docker(self):
        self.directory.mkdir(mode=0o700)
        workspace = self.root / 'workspace'; workspace.mkdir()
        evidence = self.directory / 'verification-evidence'; evidence.mkdir(mode=0o700)
        store = TaskStore.initialize(self.directory / 'tasks.sqlite')
        journal = SimJournal.initialize(self.directory / 'sim.sqlite')
        WorkflowRunLedger.initialize(self.directory / 'runs.sqlite')
        PauseBindings.initialize(self.directory / 'pauses.sqlite')
        executor = DockerTestExecutor(evidence, 'sha256:' + 'a' * 64)
        state = {'suffix': 'fixture', 'roleRevision': 'qwenpaw-role-v1:' + 'b' * 64,
                 'verificationPolicy': verification_policy('dedicated.py', executor.environment_id, 'fixture')}
        calls = []
        def network(request):
            calls.append(request.method)
            raise AssertionError('No native request expected without a submitted workflow')
        sim = SimClient('http://sim.fixture', 'private-secret', client=httpx.Client(transport=httpx.MockTransport(network)))
        self.addCleanup(sim.close)
        api = SimAuthoringClient(sim, 'fixture-workspace'); gateway = Mock()
        app, runtime, editor, runner, _ = build_application(self.directory, state, workspace, store, journal,
                                                           sim, api, gateway, executor)
        self.assertIs(app.state.verifier.test_executor, executor)
        self.assertEqual(app.state.verifier.store.path, store.path)
        self.assertIs(app.state.task_flow, runtime)
        dispatcher = app.state.execution_worker.loop.dispatcher
        self.assertIs(dispatcher.gateways[(PROJECT, BINDING)], gateway)
        self.assertIs(dispatcher.verifier, app.state.verifier)
        self.assertTrue(app.state.authoring_api.verification_enabled)
        self.assertEqual(app.state.authoring_api.journal.path, journal.path)
        with TestClient(app) as http:
            headers = {'Authorization': 'Bearer ' + runner}
            self.assertEqual(http.get('/extension/v1/health', headers=headers).status_code, 200)
            result = http.get('/extension/v1/workflows', headers=headers)
            self.assertEqual(result.status_code, 200)
            editor_result = http.get('/extension/v1/authoring/projects', headers={'Authorization': 'Bearer ' + editor})
            self.assertIn('loop', editor_result.json()['nodeTypes'])
        gateway.submit.assert_not_called()
        self.assertEqual(calls, [])
        self.assertEqual(store.list_tasks({PROJECT})['tasks'], [])

    def test_real_unittest_command_fails_twice_and_passes_zero_case_in_each_of_three_rounds(self):
        artifact = 'dedicated.py'; (self.root / artifact).write_bytes(CONTENT)
        policy = verification_policy(artifact, 'docker-test-v1:' + 'a' * 64, 'fixture')
        self.assertEqual(validate_policy(policy), policy)
        for _ in range(3):
            result = subprocess.run([sys.executable, *policy['tests'][0]['argv'][1:]], cwd=self.root,
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 1)
            self.assertIn('Ran 3 tests', result.stderr)
            self.assertIn(FAILURE_LINE, result.stderr)
            self.assertIn('test_zero', result.stderr)
            self.assertEqual((self.root / artifact).read_bytes(), CONTENT)

    def failed(self, tid='previous'):
        check = {'kind': 'test_command', 'testId': 'addition-unittests', 'status': 'failed',
                 'exitCode': 1, 'stdout': '', 'stderr': 'Ran 3 tests\n' + FAILURE_LINE}
        task = {'id': tid, 'executionStatus': 'succeeded', 'resourcesReleased': True, 'verificationStatus': 'failed',
                'evidence': [{'kind': 'verification', 'status': 'failed', 'checks': [
                    {'kind': 'artifact_sha256', 'status': 'passed'}, check]}]}
        return task, check

    def test_failed_test_requires_successful_execution_passed_bytes_and_actual_independent_exit(self):
        task, _ = self.failed(); self.assertEqual(failed_test(task)['exitCode'], 1)
        for key, value in (('executionStatus', 'failed'), ('resourcesReleased', False), ('verificationStatus', 'passed')):
            bad = copy.deepcopy(task); bad[key] = value
            with self.assertRaises(RuntimeError): failed_test(bad)
        bad = copy.deepcopy(task); bad['evidence'][0]['checks'][1]['exitCode'] = 0
        with self.assertRaises(RuntimeError): failed_test(bad)

    def test_feedback_must_reach_parent_delegate_receipt_and_original_codex_user_input(self):
        previous, check = self.failed()
        frozen = {'taskId': previous['id'], 'verificationStatus': 'failed', 'evidence': previous['evidence']}
        task = {'id': 'next', 'payload': {'input': {'repair': {'iteration': 1, 'previousResult': frozen}}},
                'executionIdentity': {'codexThreadId': 'thread-unique', 'upstreamTaskId': 'native-next'}}
        instruction = 'previous addition-unittests exitCode=1 ' + FAILURE_LINE
        parent = {'result': {'output': [
            {'type': 'plugin_call', 'content': [{'data': {'name': 'agent_line_delegate', 'call_id': 'call',
                'arguments': {'instruction': instruction}}}]},
            {'type': 'plugin_call_output', 'content': [{'data': {'name': 'agent_line_delegate', 'call_id': 'call',
                'state': 'success', 'output': {'taskId': 'next', 'identity': {'upstreamTaskId': 'native-next'}}}}]}]}}
        sessions = self.root / 'sessions'; sessions.mkdir()
        rollout = sessions / 'rollout-thread-unique.jsonl'
        rollout.write_text(json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': instruction}}) + '\n')
        result = feedback_proof(parent, task, previous, self.root)
        self.assertEqual(result['previousTaskId'], 'previous'); self.assertEqual(len(result['logSha256']), 64)
        rollout.write_text(json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'content': instruction}}) + '\n')
        with self.assertRaisesRegex(RuntimeError, 'user_input_missing'): feedback_proof(parent, task, previous, self.root)

    def test_compiler_native_loop_is_bounded_at_three_and_final_failed_guard_is_business_failure(self):
        doc = document(); compiled = compile_document(doc, 'workflow')
        self.assertEqual(decode_document(compiled), doc)
        self.assertEqual(compiled['blocks'][native_id('workflow', 'repair')]['data']['count'], 3)
        state = {'workflowId': 'workflow', 'finalRunId': 'child3'}
        result = {'taskId': 'third-task', 'executionStatus': 'succeeded', 'verificationStatus': 'failed'}
        native = {'workflowId': 'workflow', 'runId': 'child3', 'status': 'completed', 'blockOutputs': {
            native_id('workflow', 'repair') + '--guard': {'result': {'result': result, 'needsRepair': True}},
            native_id('workflow', 'end') + '.data': {'agentLineResult': result}}}
        self.assertFalse(check_guard(native, state, result)['businessVerified'])
        native['blockOutputs'][native_id('workflow', 'repair') + '--guard']['result']['needsRepair'] = False
        with self.assertRaises(RuntimeError): check_guard(native, state, result)

    def test_once_unknown_publication_or_start_is_never_reposted(self):
        state = {'steps': {}}; persisted = []
        def lost(): raise TimeoutError('unknown original receipt')
        with self.assertRaises(TimeoutError): once(state, 'start', lost, lambda: persisted.append(copy.deepcopy(state)))
        self.assertEqual(persisted[0]['steps']['start']['status'], 'intent')
        operation = Mock()
        with self.assertRaises(RuntimeError): once(state, 'start', operation, lambda: None)
        operation.assert_not_called()


if __name__ == '__main__':
    unittest.main()
