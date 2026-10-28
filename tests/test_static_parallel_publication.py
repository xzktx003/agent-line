"""Only an explicit frozen capability adopts the compiler's complete static DAG."""
import copy
import tempfile
import unittest
from pathlib import Path

import httpx

from agent_line.adapters.sim import SimClient
from agent_line.approval_discovery import WorkflowRunLedger
from agent_line.publication import PublicationVerifier, PublicationUnverified
from agent_line.sim_authoring import compile_document
from agent_line.store import Conflict
from tests import test_static_parallel_compiler as fixtures


class StaticParallelPublicationTests(unittest.TestCase):
    def setUp(self):
        self.graph = compile_document(fixtures.document(), 'workflow')
        self.graph.update(loops={}, parallels={})
        self.waits = [k for k, b in self.graph['blocks'].items() if b['type'] == 'human_in_the_loop_v2']
        self.version = dict(id='deployment', version=1, isActive=True, state=copy.deepcopy(self.graph))
        self.run = dict(runId='run', workflowId='workflow', deploymentVersionId='deployment',
                        workflowState=copy.deepcopy(self.graph))
        def handler(request):
            self.assertEqual(request.method, 'GET')
            return httpx.Response(200, json={'data': self.run if request.url.path == '/api/v2/logs/run' else self.version})
        http = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(http.close)
        self.verifier = PublicationVerifier(SimClient('http://sim.test', 'test-key', client=http))
        self.snapshot = dict(workflowId='workflow', runId='run', revision='1',
                             deploymentVersionId='deployment', humanBlockIds=self.waits, staticParallel=True)

    def test_explicit_capability_checks_original_definition_and_actual_graph(self):
        self.assertTrue(self.verifier.check_definition('workflow', '1', 'deployment', self.waits,
                                                      static_parallel=True))
        self.assertTrue(self.verifier.verify_run(self.snapshot))

    def test_mutated_policy_code_or_graph_is_not_adopted(self):
        join = next(k for k, b in self.graph['blocks'].items() if 'AL_JOIN_V1' in str(b['subBlocks']))
        for field in ('state', 'workflowState'):
            for mutation in (
                    lambda g: g['blocks'][join]['subBlocks']['code'].update(value='return {ok:true};'),
                    lambda g: g['edges'].append(dict(id='bypass', source=self.waits[0], target=join)),
                    lambda g: g['blocks'][join].update(enabled=False)):
                self.version['state'] = copy.deepcopy(self.graph)
                self.run['workflowState'] = copy.deepcopy(self.graph)
                target = self.version if field == 'state' else self.run
                mutation(target[field])
                with self.subTest(field=field), self.assertRaises(PublicationUnverified):
                    self.verifier.verify_run(self.snapshot)

    def test_runtime_omissions_are_normalized_only_after_publication_proof(self):
        self.run['workflowState'].pop('variables')
        for edge in self.run['workflowState']['edges']:
            edge.update(sourceHandle=None, targetHandle=None)
        self.assertTrue(self.verifier.verify_run(self.snapshot))
        self.run['workflowState']['variables'] = {'changed': 'not-adopted'}
        with self.assertRaises(PublicationUnverified):
            self.verifier.verify_run(self.snapshot)

    def test_invalid_flag_and_incomplete_allowlist_refuse(self):
        for value in (None, 1, 'true', {}, []):
            with self.subTest(value=value), self.assertRaises(PublicationUnverified):
                self.verifier.check_definition('workflow', '1', 'deployment', self.waits,
                                               static_parallel=value)
        with self.assertRaises(PublicationUnverified):
            self.verifier.check_definition('workflow', '1', 'deployment', self.waits[:1], static_parallel=True)

    def test_ledger_persists_strict_explicit_flag_without_promoting_old_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'runs.sqlite'
            ledger = WorkflowRunLedger.initialize(path)
            policy = dict(roleBindingId='role', roleAgentId='agent', instruction='work',
                          memoryPolicyId='native-role', outputSchemaId='task-result',
                          requireMemoryCommit=False, allowFailureBranch=True)
            args = dict(project_id='project', workflow_id='workflow', run_id='run', revision='1',
                        deployment_version_id='deployment', agent_policies={w: policy for w in self.waits})
            ledger.reserve_tasks(**args, static_parallel=True)
            self.assertIs(WorkflowRunLedger(path).get('workflow', 'run')['staticParallel'], True)
            with self.assertRaises(Conflict):
                ledger.reserve_tasks(**args)
            for flag in (None, 1, 'true'):
                with self.assertRaises(ValueError):
                    ledger.reserve_tasks(**{**args, 'run_id': 'invalid'}, static_parallel=flag)
