"""Offline transport proofs for the real-Sim zero-model trigger probe."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import httpx

from agent_line.adapters.sim import SimClient
from agent_line.sim_authoring import decode_document
from probes.sim.authoring_probe import write
from probes.sim.triggers_live import (
    KIND, TriggerProbe, run_probe,
)


class NativeFixture:
    """Only Sim transport is fake; HTTP APIs, all journals and compilation are real."""
    def __init__(self):
        self.calls = []
        self.workflow = 'fixture-workflow'
        self.state = {'blocks': {}, 'edges': [], 'variables': {}, 'loops': {}, 'parallels': {}}
        self.detail = {'id': self.workflow, 'workspaceId': 'fixture-workspace',
                       'name': 'fixture', 'description': '', 'isDeployed': False}
        self.runs = {}
        self.lose = None
        self.lose_once = False
        self.wrong_marker = False
        self.wrong_deployment = False

    def handler(self, request):
        self.calls.append((request.method, request.url.path))
        if request.headers.get('x-api-key') != 'fixture-secret':
            raise AssertionError('missing authenticated Sim transport')
        base = '/api/v2/workflows/' + self.workflow
        method, path = request.method, request.url.path
        status = 200
        if method == 'POST' and path == '/api/v2/workflows':
            result = self.detail
            status = 201
        elif method == 'GET' and path == base:
            result = self.detail
        elif method == 'GET' and path == base + '/state':
            result = self.state
        elif method == 'PUT' and path == base + '/state':
            self.state = json.loads(request.content) | {'loops': {}, 'parallels': {}}
            decode_document(self.state)
            result = {'id': self.workflow, 'dryRun': False, 'needsRedeployment': True,
                      'warnings': [], 'lint': {}}
        elif method == 'POST' and path == base + '/deploy':
            self.detail['isDeployed'] = True
            result = {'id': self.workflow, 'isDeployed': True, 'version': 1,
                      'activeDeployment': {'deploymentVersionId': 'fixture-deployment'}}
        elif method == 'GET' and path == base + '/versions/1':
            result = {'id': 'fixture-deployment', 'version': 1, 'isActive': True, 'state': self.state}
        elif method == 'POST' and path == base + '/execute':
            rid = request.headers['X-Run-Id']
            if rid in self.runs:
                raise AssertionError('native execute repeated')
            body = json.loads(request.content)
            if body['async'] is not True:
                raise AssertionError('expected async native execution')
            marker = next(node['config']['output']['marker']
                          for node in decode_document(self.state)['nodes'] if node['type'] == 'end')
            self.runs[rid] = {'runId': rid, 'workflowId': self.workflow, 'status': 'completed',
                             'output': {'data': {'marker': 'wrong' if self.wrong_marker else marker}}}
            result = {'runId': rid, 'workflowId': self.workflow}
            status = 202
        elif method == 'GET' and path.startswith(base + '/runs/'):
            result = self.runs[path.rsplit('/', 1)[1]]
        elif method == 'GET' and path.startswith('/api/v2/logs/'):
            rid = path.rsplit('/', 1)[1]
            result = {'runId': rid, 'workflowId': self.workflow,
                      'deploymentVersionId': 'wrong' if self.wrong_deployment else 'fixture-deployment'}
        else:
            raise AssertionError('unexpected native request: ' + method + ' ' + path)
        if not self.lose_once and self.lose == (method, path):
            self.lose_once = True
            raise httpx.ReadTimeout('fixture-secret must not be exposed', request=request)
        return httpx.Response(status, json={'data': copy.deepcopy(result)})

    def factory(self, base, key):
        transport = httpx.Client(transport=httpx.MockTransport(self.handler))
        sim = SimClient(base, key, client=transport)
        sim._owns_client = True
        return sim

    def writes(self):
        return [call for call in self.calls if call[0] != 'GET']


class LiveTriggerProbeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.directory = self.root / 'probe'
        self.auth = self.root / 'auth.json'
        write(self.auth, {'workspaceId': 'fixture-workspace', 'baseUrl': 'http://sim.fixture',
                          'apiKey': 'fixture-secret'})
        self.remote = NativeFixture()
        self.snapshot = {'files': {'main-config': 'fixture-sha'}, 'guard': {'active': True}}

    def run_probe(self, execute=False):
        return run_probe(execute=execute, directory=self.directory, auth_path=self.auth,
                         sim_factory=self.remote.factory, snapshot=lambda: copy.deepcopy(self.snapshot))

    def state(self):
        return json.loads((self.directory / 'state.json').read_text())

    def test_default_fresh_probe_needs_no_credentials_or_initialization(self):
        self.auth.unlink()
        result = self.run_probe()
        self.assertEqual(result['status'], 'not_executed')
        self.assertEqual(self.remote.calls, [])
        self.assertFalse(self.directory.exists())

    def test_complete_http_path_reopens_ledgers_and_executes_three_original_runs(self):
        result = self.run_probe(execute=True)
        self.assertEqual(result['status'], 'passed_live_zero_model')
        self.assertEqual(result['nativeWritesThisInvocation'], 6)
        self.assertEqual(result['nativeExecuteCount'], 3)
        self.assertEqual(result['checks']['controlledExecution'], 'unknown')
        self.assertTrue(result['checks']['reopenedApiReplay'])
        self.assertEqual(result['nodeTaskCount'], 0)
        self.assertEqual(result['modelCalls'], 0)
        state = self.state()
        self.assertEqual(len(state['runs']), 3)
        self.assertEqual(len(state['nativeIntents']), 6)
        self.assertTrue(all(intent['status'] == 'received' for intent in state['nativeIntents'].values()))
        self.assertEqual(len(self.remote.writes()), 6)
        self.assertEqual(sum(path.endswith('/execute') for _, path in self.remote.writes()), 3)
        text = (self.directory / 'state.json').read_text()
        self.assertNotIn('fixture-secret', text)
        for execute in (False, True):
            before = len(self.remote.calls)
            observed = self.run_probe(execute=execute)
            self.assertEqual(observed['status'], 'observation_only')
            self.assertEqual(observed['nativeWritesThisInvocation'], 0)
            self.assertTrue(all(method == 'GET' for method, _ in self.remote.calls[before:]))
            self.assertEqual(self.state(), state)
            self.assertEqual(len(self.remote.writes()), 6)

    def test_lost_create_receipt_is_never_reposted_even_with_execute(self):
        self.remote.lose = ('POST', '/api/v2/workflows')
        with self.assertRaisesRegex(RuntimeError, 'not_confirmed_no_retry'):
            self.run_probe(execute=True)
        original = self.state()
        self.assertEqual(len(self.remote.writes()), 1)
        self.assertTrue(any(i['status'] == 'intent' for i in original['nativeIntents'].values()))
        self.assertNotIn('fixture-secret', (self.directory / 'failure.json').read_text())
        for execute in (False, True):
            result = self.run_probe(execute=execute)
            self.assertEqual(result['nativeWritesThisInvocation'], 0)
            self.assertEqual(result['originalRunCount'], 0)
            self.assertEqual(len(self.remote.writes()), 1)
            self.assertEqual(self.state(), original)

    def test_lost_execute_receipt_retains_original_run_and_observes_without_replay(self):
        self.remote.lose = ('POST', '/api/v2/workflows/fixture-workflow/execute')
        with self.assertRaisesRegex(RuntimeError, 'trigger_not_accepted_no_retry'):
            self.run_probe(execute=True)
        original = self.state()
        self.assertEqual(len(original['runs']), 1)
        self.assertEqual(len(self.remote.writes()), 4)
        result = self.run_probe(execute=True)
        self.assertEqual(result['nativeWritesThisInvocation'], 0)
        self.assertEqual(result['originalRunCount'], 1)
        self.assertEqual(len(self.remote.writes()), 4)
        self.assertEqual(self.state(), original)
        self.assertTrue(next(iter(result['native']['runs'].values()))['verified'])

    def test_outer_once_refuses_existing_intent_before_any_local_http_request(self):
        self.directory.mkdir(mode=0o700)
        state = {'kind': KIND, 'schemaVersion': 1, 'workspaceId': 'fixture-workspace',
                 'baseUrl': 'http://sim.fixture', 'steps': {'original': {'status': 'intent'}},
                 'nativeIntents': {}, 'runs': {}}
        probe = TriggerProbe(json.loads(self.auth.read_text()), self.directory, state, read_only=False)
        http = Mock()
        with self.assertRaisesRegex(RuntimeError, 'outer_intent_exists_no_retry'):
            probe.once('original', http, '/original', {}, {})
        http.post.assert_not_called()

    def test_partial_initialization_is_never_rebuilt(self):
        self.directory.mkdir(mode=0o700)
        (self.directory / 'tasks.sqlite').write_text('original unknown data')
        with self.assertRaisesRegex(RuntimeError, 'partial_directory_requires_reconciliation'):
            self.run_probe(execute=True)
        self.assertEqual(self.remote.calls, [])
        self.assertEqual((self.directory / 'tasks.sqlite').read_text(), 'original unknown data')

    def test_wrong_end_marker_or_execution_deployment_never_passes(self):
        for attribute, code in (('wrong_marker', 'original_end_result_mismatch'),
                                ('wrong_deployment', 'original_run_deployment_mismatch')):
            with self.subTest(attribute=attribute):
                self.directory = self.root / attribute
                self.remote = NativeFixture()
                setattr(self.remote, attribute, True)
                with self.assertRaisesRegex(RuntimeError, code):
                    self.run_probe(execute=True)
                self.assertFalse((self.directory / 'report.json').exists())
                self.assertEqual(len(self.remote.writes()), 6)

    def test_nonprivate_auth_rejected_before_transport(self):
        self.auth.chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, 'private_regular_file_required'):
            self.run_probe(execute=True)
        self.assertEqual(self.remote.calls, [])
        self.assertFalse(self.directory.exists())


if __name__ == '__main__':
    unittest.main()
