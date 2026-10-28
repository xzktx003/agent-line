"""Offline real HTTP/SQLite wiring; only authenticated Sim transport is mocked."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from probes.sim.authoring_probe import write
from probes.sim.publication_capability_native import PROJECT, document, run_probe
from tests.test_live_trigger_probe import NativeFixture


class PublicationCapabilityNativeProbeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / 'probe'
        self.auth = self.root / 'auth.json'
        write(self.auth, {'workspaceId': 'fixture-workspace', 'baseUrl': 'http://sim.fixture',
                          'apiKey': 'fixture-secret'})
        self.native = NativeFixture()

    def run_probe(self, execute=False):
        return run_probe(execute=execute, directory=self.directory, auth_path=self.auth,
                         sim_factory=self.native.factory)

    def test_default_has_zero_io_and_unknown_readiness(self):
        self.auth.unlink()
        result = self.run_probe()
        self.assertEqual(result['status'], 'not_executed')
        self.assertEqual(result['readiness'], 'unknown')
        self.assertEqual(result['nativeWritesThisInvocation'], 0)
        self.assertFalse(self.directory.exists())
        self.assertEqual(self.native.calls, [])

    def test_create_save_agent_draft_blocks_publication_and_preserves_native_draft(self):
        report = self.run_probe(True)
        self.assertEqual(report.get('code'), None, report)
        self.assertEqual(report['status'], 'passed_live')
        self.assertEqual(report['readiness'], 'unknown')
        self.assertEqual(report['nativeWritesThisInvocation'], 2)
        self.assertEqual(report['deployExecuteResumeRequests'], 0)
        self.assertTrue(report['allTransportsClosed'])
        self.assertEqual(self.native.writes(), [('POST', '/api/v2/workflows'),
                                               ('PUT', '/api/v2/workflows/fixture-workflow/state')])
        self.assertFalse(self.native.detail['isDeployed'])
        self.assertEqual(report['detail']['document'], document())
        self.assertEqual(report['observations']['publish']['receipt']['outcome'], 'verification_required')
        configuration = json.loads((self.directory / 'service.json').read_text())
        self.assertEqual(set(configuration), {'schemaVersion', 'database', 'tokens', 'projects', 'bindings',
                                             'authoringFile', 'sim'})
        self.assertNotIn('verificationPolicy', configuration['bindings']['existing-role'])
        self.assertEqual(configuration['bindings']['existing-role']['roleAgentId'], 'agent-line-native-loop-parent')
        report_text = (self.directory / 'report.json').read_text()
        self.assertNotIn('fixture-secret', report_text)
        for item in report['nativeCalls']:
            self.assertEqual(set(item), {'method', 'path'})

    def test_existing_success_is_get_only_even_with_execute_flag(self):
        first = self.run_probe(True)
        self.assertEqual(first['status'], 'passed_live')
        original_state = (self.directory / 'state.json').read_bytes()
        for execute in (False, True):
            self.native.calls.clear()
            report = self.run_probe(execute)
            self.assertEqual(report['status'], 'passed_live')
            self.assertTrue(report['observationOnly'])
            self.assertEqual(report['nativeWritesThisInvocation'], 0)
            self.assertEqual(self.native.writes(), [])
            self.assertEqual((self.directory / 'state.json').read_bytes(), original_state)

    def test_lost_create_reply_never_recreates_or_continues(self):
        self.native.lose = ('POST', '/api/v2/workflows')
        first = self.run_probe(True)
        self.assertEqual(first['status'], 'incomplete')
        self.assertEqual(first['code'], 'operation_requires_reconciliation')
        original_state = (self.directory / 'state.json').read_bytes()
        for execute in (False, True):
            self.native.calls.clear()
            report = self.run_probe(execute)
            self.assertEqual(report['status'], 'incomplete')
            self.assertEqual(self.native.writes(), [])
            self.assertEqual(set(report['observations']), {'create'})
            self.assertEqual(report['observations']['create']['status'], 'uncertain')
            self.assertEqual((self.directory / 'state.json').read_bytes(), original_state)

    def test_lost_save_reply_never_repeats_save_or_attempts_publish(self):
        self.native.lose = ('PUT', '/api/v2/workflows/fixture-workflow/state')
        first = self.run_probe(True)
        self.assertEqual(first['status'], 'incomplete')
        self.native.calls.clear()
        report = self.run_probe(True)
        self.assertEqual(report['status'], 'incomplete')
        self.assertEqual(set(report['observations']), {'create', 'save'})
        self.assertEqual(report['observations']['save']['status'], 'uncertain')
        self.assertEqual(self.native.writes(), [])
        self.assertFalse(self.native.detail['isDeployed'])

    def test_initialization_intent_is_never_repaired_implicitly(self):
        with patch('probes.sim.publication_capability_native.TaskStore.initialize',
                   side_effect=RuntimeError('fixture-secret')):
            first = self.run_probe(True)
        self.assertEqual(first['status'], 'incomplete')
        self.assertNotIn('fixture-secret', json.dumps(first))
        for execute in (False, True):
            report = self.run_probe(execute)
            self.assertEqual(report['code'], 'initialization_requires_reconciliation')
            self.assertEqual(self.native.calls, [])
            self.assertFalse((self.directory / 'tasks.sqlite').exists())

    def test_before_post_intent_survives_interrupt_and_is_only_observed(self):
        from fastapi.testclient import TestClient
        original = TestClient.post
        def interrupt(http, url, *args, **kwargs):
            if url == '/extension/v1/authoring/projects/' + PROJECT + '/workflows':
                raise KeyboardInterrupt()
            return original(http, url, *args, **kwargs)
        with patch.object(TestClient, 'post', interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_probe(True)
        state = json.loads((self.directory / 'state.json').read_text())
        self.assertTrue(state['steps']['create']['intent'])
        self.assertNotIn('response', state['steps']['create'])
        for execute in (False, True):
            report = self.run_probe(execute)
            self.assertEqual(report['code'], 'read_failed')
            self.assertEqual(self.native.calls, [])

    def test_existing_directory_without_state_and_symlink_never_initialize(self):
        self.directory.mkdir(mode=0o700)
        self.assertEqual(self.run_probe(True)['status'], 'incomplete')
        self.assertEqual(list(self.directory.iterdir()), [])
        self.directory.rmdir()
        target = self.root / 'elsewhere'; target.mkdir(mode=0o700)
        self.directory.symlink_to(target)
        self.assertEqual(self.run_probe(True)['code'], 'private_directory_required')
        self.assertEqual(list(target.iterdir()), [])
        self.assertEqual(self.native.calls, [])

    def test_changed_workspace_or_config_stops_before_native_reads(self):
        self.assertEqual(self.run_probe(True)['status'], 'passed_live')
        self.native.calls.clear()
        write(self.auth, {'workspaceId': 'different-workspace', 'baseUrl': 'http://sim.fixture',
                          'apiKey': 'fixture-secret'})
        self.assertEqual(self.run_probe()['code'], 'native_workspace_or_origin_changed')
        self.assertEqual(self.native.calls, [])

    def test_regression_attempting_native_deploy_is_blocked_before_transport(self):
        # Simulate the previous permissive API; hook must protect the real server.
        with patch('agent_line.authoring_api.AuthoringAPI._agent_policies', return_value={}):
            report = self.run_probe(True)
        self.assertEqual(report['status'], 'incomplete')
        self.assertEqual(report['code'], 'operation_requires_reconciliation')
        self.assertEqual(report['deployExecuteResumeRequests'], 1)
        self.assertFalse(self.native.detail['isDeployed'])
        self.assertNotIn(('POST', '/api/v2/workflows/fixture-workflow/deploy'), self.native.calls)
        self.assertEqual(self.native.runs, {})


if __name__ == '__main__':
    unittest.main()
