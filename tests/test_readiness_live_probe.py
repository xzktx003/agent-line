"""Offline safety checks; these do not stand in for the explicit native probe."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import httpx

from agent_line.role_dispatch import RoleDispatchLedger
from agent_line.service import NodeTaskRequest, ServiceConfig, create_app
from agent_line.store import TaskStore
from agent_line.verification_policy import validate_policy
from probes.execution_readiness import live
from probes.qwenpaw_memory.native_roundtrip import save


class ReadinessLiveProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / 'candidate'

    def prepared(self):
        self.directory.mkdir(mode=0o700)
        store = TaskStore.initialize(self.directory / 'tasks.sqlite')
        RoleDispatchLedger.initialize(self.directory / 'roles.sqlite')
        state = {'status': 'incomplete', 'submissionIntent': True, 'baseURL': 'http://host.fixture',
                 'hostToken': 'original-token', 'taskId': 'original-task'}
        save(self.directory / 'state.json', state)
        return store, state

    def test_default_needs_no_credentials_and_creates_nothing(self):
        factory = Mock()
        with patch.object(live, '_execute') as execute:
            self.assertEqual(live.run_probe(root=self.root, directory=self.directory, client_factory=factory),
                             {'status': 'not_executed', 'newSubmissions': 0})
        execute.assert_not_called(); factory.assert_not_called()
        self.assertFalse(self.directory.exists())

    def test_existing_state_even_execute_is_observation_only_and_byte_preserving(self):
        self.prepared()
        before = {p.name: p.read_bytes() for p in self.directory.iterdir()}
        factory = Mock()
        with patch.object(live, '_execute', side_effect=AssertionError('must not execute')), \
                patch.object(live, 'load_app', side_effect=AssertionError('must not start worker')), \
                patch.object(live, 'load_execution_gateways', side_effect=AssertionError('must not log in')):
            result = live.run_probe(execute=True, directory=self.directory, client_factory=factory)
        self.assertEqual(result['mode'], 'observation_only'); self.assertEqual(result['newSubmissions'], 0)
        self.assertEqual(result['activeLeaseCount'], 0); factory.assert_not_called()
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.directory.iterdir()})

    def parent(self):
        _, state = self.prepared()
        with sqlite3.connect(self.directory / 'roles.sqlite') as db:
            db.execute('INSERT INTO deliveries VALUES (?,?,?,?,?,?)',
                       ('original-task', '{}', 'original-parent', 'submitted', None, 1.0))
        return state

    def test_replay_only_gets_original_parent_with_saved_token(self):
        self.parent(); requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={'status': 'finished'})
        factory = lambda **kwargs: httpx.Client(transport=httpx.MockTransport(handler), **kwargs)
        result = live.run_probe(execute=True, directory=self.directory, client_factory=factory)
        self.assertEqual(result['parentStatus'], 'finished')
        self.assertEqual([(r.method, r.url.path) for r in requests],
                         [('GET', '/api/console/chat/task/original-parent')])
        self.assertEqual(requests[0].headers['Authorization'], 'Bearer original-token')

    def test_expired_original_token_never_logs_in_or_submits(self):
        self.parent(); requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(401, json={'detail': 'expired-private-detail'})
        factory = lambda **kwargs: httpx.Client(transport=httpx.MockTransport(handler), **kwargs)
        with self.assertRaisesRegex(RuntimeError, 'original_parent_observation_unavailable'):
            live.run_probe(execute=True, directory=self.directory, client_factory=factory)
        self.assertEqual([r.method for r in requests], ['GET'])

    def test_uncheckpointed_wal_blocks_observation_instead_of_reading_stale_database(self):
        self.prepared()
        with sqlite3.connect(self.directory / 'tasks.sqlite') as writer:
            writer.execute("INSERT INTO admission_guard VALUES (1, 'fixture-owner')")
            writer.commit()
            self.assertGreater((self.directory / 'tasks.sqlite-wal').stat().st_size, 0)
            factory = Mock()
            with self.assertRaisesRegex(RuntimeError, 'observation_database_has_live_wal'):
                live.run_probe(execute=True, directory=self.directory, client_factory=factory)
            factory.assert_not_called()

    def test_early_intent_with_no_database_is_not_automatically_repaired(self):
        self.directory.mkdir(mode=0o700)
        save(self.directory / 'state.json', {'status': 'prepared', 'registrationIntent': True})
        factory = Mock()
        with patch.object(live, '_execute', side_effect=AssertionError('must not prepare again')):
            result = live.run_probe(execute=True, directory=self.directory, client_factory=factory)
        self.assertEqual(result['newSubmissions'], 0)
        self.assertEqual(result['tasks'], []); factory.assert_not_called()
        self.assertEqual(sorted(p.name for p in self.directory.iterdir()), ['state.json'])

    def test_submission_intent_survives_unknown_response_and_cannot_repeat(self):
        state = {'taskId': 'one'}; durable = []
        dispatcher = Mock(); dispatcher.submit.side_effect = TimeoutError('unknown-response')
        persist = lambda: durable.append(dict(state))
        with self.assertRaises(TimeoutError): live.submit_once(dispatcher, state, persist)
        self.assertEqual(durable, [{'taskId': 'one', 'submissionIntent': True}])
        with self.assertRaisesRegex(RuntimeError, 'submission_intent_already_exists'):
            live.submit_once(dispatcher, state, persist)
        dispatcher.submit.assert_called_once_with('one')

    def test_failure_summary_keeps_fixed_code_and_stack_locations_without_exception_body(self):
        try:
            raise RuntimeError('binding_scope_mismatch')
        except RuntimeError as error:
            summary = live.failure_summary(error)
        self.assertEqual(summary['code'], 'binding_scope_mismatch')
        self.assertEqual(summary['type'], 'RuntimeError')
        self.assertTrue(1 <= len(summary['frames']) <= 4)
        self.assertEqual(set(summary['frames'][-1]), {'file', 'function', 'line'})
        for error in (RuntimeError('GET https://host/private?token=SECRET'),
                      RuntimeError('MODEL_BODY'), RuntimeError('敏感内容'),
                      ValueError('secret_token_from_native')):
            summary = live.failure_summary(error)
            self.assertEqual(summary['code'], 'native_readiness_probe_incomplete')
            self.assertNotIn('SECRET', json.dumps(summary))

    def test_trusted_independent_policy_rejects_wrong_bytes_and_accepts_exact_bytes(self):
        file = self.root / 'artifact.txt'; marker = 'TEST_NONCE'
        policy = live.policy(file.name, marker, 'docker-test-v1:' + 'a' * 64, 'b' * 16)
        self.assertEqual(validate_policy(policy), policy)
        command = policy['tests'][0]
        self.assertEqual(command['timeoutSeconds'], 60); self.assertEqual(command['maxOutputBytes'], 65536)
        for content, passed in ((marker, False), (marker + '\nextra', False), (marker + '\n', True)):
            file.write_text(content)
            result = subprocess.run([sys.executable, *command['argv'][1:]], cwd=self.root, capture_output=True)
            self.assertEqual(result.returncode == 0, passed)

    def test_candidate_configuration_cannot_self_assert_capability(self):
        service, execution, _ = live.candidate_settings(self.root, self.directory, self.root,
            'http://127.0.0.1:12345', 'a' * 64, {}, 'sha256:' + 'a' * 64)
        self.assertNotIn('controlledExecution', service); self.assertNotIn('readinessFile', service)
        self.assertEqual(service['database'], str(self.directory / 'tasks.sqlite'))
        entry = execution['gateways'][0]
        self.assertEqual(entry['roleAgentId'], live.ROLE)
        self.assertEqual(entry['installationId'], 'agent-line-g0-installation')
        self.assertEqual(entry['serviceTokenFile'], str(self.root / 'var/qwenpaw-secrets/execution-service-token'))

    def app(self, store, confirmed):
        return create_app(store, ServiceConfig(tokens={'fixture-token': {live.PROJECT}},
            projects={live.PROJECT: {'workspace': str(self.root)}},
            bindings={live.BINDING: {'projectId': live.PROJECT, 'roleAgentId': live.ROLE}},
            controlled_execution='confirmed' if confirmed else 'unknown'))

    def test_http_unknown_rejection_does_not_register_the_probe_task(self):
        store, _ = self.prepared(); body = live.request_body('nonce', 'instruction')
        NodeTaskRequest.model_validate(body)
        with patch.object(live, 'load_app', return_value=self.app(store, False)) as factory:
            result = live.check_candidate_http(self.directory / 'service.json', 'fixture-token', body, store=store)
        factory.assert_called_once()
        self.assertTrue(result['newTaskRejected']); self.assertEqual(result['taskCount'], 0)

    def test_guard_rejects_new_task_but_preserves_authenticated_history(self):
        store, _ = self.prepared(); body = live.request_body('nonce', 'instruction')
        payload = {k: v for k, v in body.items() if k != 'idempotencyKey'}
        payload.update(roleAgentId=live.ROLE, workspace=str(self.root))
        task, _ = store.register('existing-task', payload)
        store.enter_maintenance()
        guard = store.maintenance_status()
        with patch.object(live, 'load_app', return_value=self.app(store, True)):
            result = live.check_candidate_http(self.directory / 'service.json', 'fixture-token', body,
                                              store=store, task_id=task['id'], guarded=True)
        self.assertFalse(result['readyForExecution']); self.assertTrue(result['historyReadable'])
        self.assertEqual(len(store.list_tasks({live.PROJECT})['tasks']), 1)
        self.assertEqual(store.maintenance_status(), guard)


if __name__ == '__main__':
    unittest.main()
