"""Journal safety using real local stores and explicitly fake native gateways.

Deployment/rollout/readiness validators are mocked: these are not live execution
or product-acceptance claims.
"""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import httpx

from agent_line.store import TaskStore, canonical
from deploy.project import verify
from deploy.project.setup import adapter_configuration, initialize_stores, write_new


class ProjectVerifyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / 'installation'
        self.root.mkdir(mode=0o700)
        for relative in ('var', 'var/adapter', 'var/adapter/evidence', 'var/qwenpaw', 'var/qwenpaw-secrets'):
            (self.root / relative).mkdir(mode=0o700)
        self.directory = self.root / 'var/adapter'
        self.workspace = self.base / 'business'
        self.workspace.mkdir(mode=0o700)
        self.pin = 'qwenpaw-role-v1:' + 'a' * 64
        self.policy = {'schemaVersion': 2, 'policyId': 'business-tests',
            'kind': 'command_and_artifacts', 'requireTests': True, 'artifacts': [],
            'testEnvironmentId': 'docker-test-v1:' + 'b' * 64,
            'tests': [{'id': 'unit', 'argv': ['python3', '-m', 'unittest'],
                       'timeoutSeconds': 60, 'maxOutputBytes': 4096}]}
        self.spec = {'project': {'id': 'business', 'name': '项目', 'workspace': str(self.workspace)},
            'coordinator': {'id': 'business-parent', 'name': '开发协调'},
            'verification': {'imageId': 'sha256:' + 'c' * 64},
            'verificationPolicy': self.policy, 'memoryQuery': '项目约定',
            'sim': {'authFile': str(self.base / 'sim-auth.json'), 'workspaceId': 'fixture-workspace'},
            'qwenpaw': {'baseURL': 'http://127.0.0.1:65199'}}
        write_new(self.directory / 'project-spec.json', self.spec)
        write_new(self.directory / 'native-role.json', {'roleRevision': self.pin})
        write_new(self.root / 'var' / verify.MARKER,
                  {'schemaVersion': 1, 'phase': 'configured', 'projectId': 'business',
                   'controlledExecution': 'unknown', 'installationId': 'fixture-installation', 'roleRevision': self.pin})
        initialize_stores(self.root)
        adapter_configuration(self.root, self.spec, self.pin, 'fixture-installation')
        self.store = TaskStore(self.directory / 'tasks.sqlite')
        self.gateway = Mock()
        self.gateway._gateway.base = 'http://127.0.0.1:65199/api'
        self.gateway._gateway.headers = {'Authorization': 'Bearer private-fixture'}
        self.gateway._gateway.ledger.get.return_value = {'parentTaskId': 'original-parent'}
        self.gateway._client.get.return_value = httpx.Response(200, json={'status': 'finished'},
                request=httpx.Request('GET', 'http://127.0.0.1:65199/api/console/chat/task/original-parent'))
        self.verifier = Mock(directory=self.directory / 'evidence')
        self.dispatcher = Mock()
        self.dispatcher.reconcile.side_effect = lambda task_id: self.store.get(task_id)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.load_gateways = self.stack.enter_context(patch.object(verify, 'load_execution_gateways',
            return_value={('business', 'developer'): self.gateway}))
        self.stack.enter_context(patch.object(verify, 'load_verifier', return_value=self.verifier))
        self.fingerprint = self.stack.enter_context(patch.object(verify, '_fingerprint', return_value={'fixture': True}))
        self.dispatcher_factory = self.stack.enter_context(patch.object(verify, 'Dispatcher', return_value=self.dispatcher))
        self.prepare = self.stack.enter_context(patch.object(verify, 'prepare_readiness', side_effect=lambda **kw:
            {'schemaVersion': 1, 'fingerprint': {'fixture': True}, 'coverage': kw['coverage']}))
        self.evaluate = self.stack.enter_context(patch.object(verify, 'evaluate_readiness', return_value=Mock(confirmed=True)))

    def run_cli(self, **kwargs):
        kwargs.setdefault('wait_seconds', 0)
        return verify.run_verify(root=self.root, **kwargs)

    def state(self):
        return json.loads((self.directory / verify.STATE).read_text())

    def rows(self):
        with self.store.connect() as db:
            return db.execute('SELECT count(*) FROM tasks').fetchone()[0]

    def make_successful(self, task_id):
        with self.store.connect() as db:
            db.execute("UPDATE tasks SET execution_status='succeeded', resources_released=1, "
                       "verification_status='passed', submission_status='accepted', output=?, evidence=? WHERE id=?",
                       (canonical({'text': 'readonly inspection complete', 'toolEvidence': [
                           {'type': 'CommandExecution', 'status': 'completed', 'exit_code': 0}]}),
                        canonical([{'kind': 'execution_terminal', 'confirmed': True},
                           {'kind': 'execution_policy', 'source': 'qwenpaw-plugin-task-binding',
                            'controls': {'sandbox': 'read-only', 'approval_policy': 'never'}}]), task_id))

    def configure_team_fixture(self):
        self.spec['team'] = {
            'planner': {'id': 'business-planner', 'name': '规划', 'providerId': 'fixture', 'modelId': 'planner-model'},
            'reviewer': {'id': 'business-reviewer', 'name': '审查', 'providerId': 'fixture', 'modelId': 'reviewer-model'}}
        verify.update_marker(self.directory / 'project-spec.json', self.spec)
        revisions = {'planner': 'qwenpaw-role-v1:' + 'b' * 64, 'developer': self.pin,
                     'reviewer': 'qwenpaw-role-v1:' + 'c' * 64}
        verify.update_marker(self.directory / 'native-role.json',
                             {'roleRevision': self.pin, 'roleRevisions': revisions})
        marker = json.loads((self.root / 'var' / verify.MARKER).read_text())
        marker['roleRevisions'] = revisions
        verify.update_marker(self.root / 'var' / verify.MARKER, marker)
        for name in ('service', 'execution', 'authoring', 'approvals', 'memory', 'task-flow'):
            (self.directory / (name + '.json')).unlink()
        adapter_configuration(self.root, self.spec, revisions, 'fixture-installation')
        gateways = {}
        for binding_id in ('planner', 'developer', 'reviewer'):
            gateway = Mock()
            gateway._gateway.base = 'http://127.0.0.1:65199/api'
            gateway._gateway.headers = {'Authorization': 'Bearer private-fixture'}
            gateway._gateway.ledger.get.return_value = {'parentTaskId': 'parent-' + binding_id}
            gateway._client.get.return_value = httpx.Response(200, json={'status': 'finished'},
                request=httpx.Request('GET', 'http://127.0.0.1:65199/api/console/chat/task/parent-' + binding_id))
            gateways[('business', binding_id)] = gateway
        self.load_gateways.return_value = gateways
        return gateways

    def test_default_before_attempt_is_local_and_creates_nothing(self):
        before = sorted(p.name for p in self.directory.iterdir())
        result = self.run_cli()
        self.assertEqual(result['status'], 'not_executed')
        self.assertEqual(before, sorted(p.name for p in self.directory.iterdir()))
        self.load_gateways.assert_not_called()
        self.dispatcher_factory.assert_not_called()

    def test_team_runs_all_three_cases_before_confirmed_readiness(self):
        self.configure_team_fixture()
        self.dispatcher.submit.side_effect = self.make_successful
        result = self.run_cli(execute=True, wait_seconds=60)
        self.assertEqual(result['status'], 'confirmed')
        self.assertEqual(result['newSubmissions'], 3)
        state = self.state()
        self.assertEqual(state['schemaVersion'], 2)
        self.assertEqual(state['bindingIds'], ['planner', 'developer', 'reviewer'])
        self.assertEqual([case['status'] for case in state['cases']], ['confirmed'] * 3)
        self.assertTrue(all(case['registrationIntent'] and case['submissionIntent'] for case in state['cases']))
        proof = json.loads((self.directory / 'readiness.json').read_text())
        self.assertEqual([item['roleBindingId'] for item in proof['coverage']],
                         ['planner', 'developer', 'reviewer'])
        self.assertEqual(self.dispatcher.submit.call_count, 3)

    def test_team_pending_first_case_does_not_start_later_cases(self):
        self.configure_team_fixture()
        self.dispatcher.submit.side_effect = lambda task_id: self.store.claim(task_id, str(self.workspace))
        result = self.run_cli(execute=True)
        self.assertEqual(result['status'], 'pending')
        self.assertEqual(self.rows(), 1)
        self.assertEqual(self.dispatcher.submit.call_count, 1)
        self.assertEqual(self.state()['cases'][0]['roleBindingId'], 'planner')
        self.assertFalse((self.directory / 'readiness.json').exists())

    def test_team_readonly_case_without_successful_native_operation_cannot_advance_or_prove(self):
        self.configure_team_fixture()
        def failed_readonly_attempt(task_id):
            self.make_successful(task_id)
            with self.store.connect() as db:
                db.execute('UPDATE tasks SET output=?,evidence=? WHERE id=?',
                    (canonical({'text': 'sandbox denied all commands', 'toolEvidence': [
                        {'type': 'CommandExecution', 'status': 'failed', 'exit_code': 101}]}),
                     canonical([{'kind': 'execution_terminal', 'confirmed': True},
                        {'kind': 'execution_policy', 'source': 'qwenpaw-plugin-task-binding',
                         'controls': {'sandbox': 'read-only', 'approval_policy': 'never'}}]), task_id))
        self.dispatcher.submit.side_effect = failed_readonly_attempt
        result = self.run_cli(execute=True)
        self.assertEqual(result['status'], 'readonly_operation_unproven')
        self.assertEqual(self.dispatcher.submit.call_count, 1)
        self.assertEqual(self.rows(), 1)
        self.prepare.assert_not_called()
        self.assertFalse((self.directory / 'readiness.json').exists())

    def test_team_default_after_attempt_only_observes_local_journals(self):
        self.configure_team_fixture()
        self.dispatcher.submit.side_effect = lambda task_id: self.store.claim(task_id, str(self.workspace))
        self.run_cli(execute=True)
        self.load_gateways.reset_mock()
        self.verifier.reset_mock()
        result = self.run_cli()
        self.assertEqual(result['mode'], 'local_observation')
        self.assertEqual(result['cases'][0]['roleBindingId'], 'planner')
        self.assertIsNotNone(result['cases'][0]['taskId'])
        self.load_gateways.assert_not_called()
        self.verifier.verify_pending.assert_not_called()

    def test_team_submit_intent_replay_never_resubmits_registered_case(self):
        self.configure_team_fixture()
        self.dispatcher.submit.side_effect = RuntimeError('fixture lost native reply')
        with self.assertRaises(RuntimeError):
            self.run_cli(execute=True)
        case = self.state()['cases'][0]
        self.assertTrue(case['registrationIntent'] and case['submissionIntent'])
        with patch.object(TaskStore, 'register', side_effect=AssertionError('never register again')):
            result = self.run_cli(execute=True)
        self.assertEqual(result['status'], 'original_submission_not_confirmed')
        self.assertEqual(self.dispatcher.submit.call_count, 1)
        self.assertEqual(self.rows(), 1)

    def test_team_unknown_registration_replay_never_registers_again(self):
        self.configure_team_fixture()
        with patch.object(TaskStore, 'register', side_effect=RuntimeError('fixture registration crash')):
            with self.assertRaises(RuntimeError):
                self.run_cli(execute=True)
        case = self.state()['cases'][0]
        self.assertTrue(case['registrationIntent'])
        self.assertIsNone(case['taskId'])
        with patch.object(TaskStore, 'register', side_effect=AssertionError('no replay register')):
            result = self.run_cli(execute=True)
        self.assertEqual(result['status'], 'registration_not_recorded')
        self.assertEqual(self.rows(), 0)
        self.dispatcher.submit.assert_not_called()

    def test_team_registered_case_without_submission_intent_stays_unsubmitted(self):
        self.configure_team_fixture()
        self.dispatcher.submit.side_effect = lambda task_id: None
        result = self.run_cli(execute=True)
        self.assertEqual(result['status'], 'pending')
        state = self.state()
        state['cases'][0]['submissionIntent'] = False
        verify.update_marker(self.directory / verify.STATE, state)
        with patch.object(TaskStore, 'register', side_effect=AssertionError('no replay register')):
            replay = self.run_cli(execute=True)
        self.assertEqual(replay['status'], 'original_submission_not_confirmed')
        self.assertEqual(self.dispatcher.submit.call_count, 1)

    def test_first_submission_uses_actual_binding_policy_and_durable_intents(self):
        def submitted(task_id):
            state = self.state()
            self.assertTrue(state['registrationIntent'])
            self.assertTrue(state['submissionIntent'])
            self.assertEqual(state['taskId'], task_id)
            self.assertEqual(state['payload'], self.store.get(task_id)['payload'])
        self.dispatcher.submit.side_effect = submitted
        result = self.run_cli(execute=True)
        self.assertEqual(result['newSubmissions'], 1)
        self.assertEqual(self.rows(), 1)
        state = self.state()
        self.assertEqual(state['payload']['verificationPolicy'], self.policy)
        self.assertEqual(state['payload']['roleRevision'], self.pin)
        self.assertEqual(state['payload']['workspace'], str(self.workspace))
        self.assertFalse(result['businessTaskCompleted'])
        self.assertNotIn('instruction', canonical(result))
        self.assertFalse((self.directory / 'readiness.json').exists())
        self.assertEqual((self.directory / verify.STATE).stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.directory / verify.LOCK).stat().st_mode & 0o777, 0o600)

    def test_existing_execute_never_registers_or_submits_even_if_registered(self):
        self.run_cli(execute=True)
        with patch.object(TaskStore, 'register', side_effect=AssertionError('no duplicate registration')):
            result = self.run_cli(execute=True)
        self.assertEqual(result['newSubmissions'], 0)
        self.assertEqual(result['status'], 'original_submission_not_confirmed')
        self.assertEqual(self.dispatcher.submit.call_count, 1)
        self.assertEqual(self.rows(), 1)

    def test_crash_before_submission_intent_never_submits_on_replay(self):
        self.run_cli(execute=True)
        state = self.state(); state['submissionIntent'] = False
        verify.update_marker(self.directory / verify.STATE, state)
        self.assertEqual(self.run_cli(execute=True)['status'], 'original_submission_not_confirmed')
        self.assertEqual(self.dispatcher.submit.call_count, 1)

    def test_observation_deadline_keeps_original_lease_without_cancel_or_replacement(self):
        self.dispatcher.submit.side_effect = lambda task_id: self.store.claim(task_id, str(self.workspace))
        result = self.run_cli(execute=True)
        self.assertEqual(result['status'], 'pending')
        self.assertEqual(self.rows(), 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM workspace_leases').fetchone()[0], 1)
        self.dispatcher.cancel.assert_not_called()
        self.assertFalse((self.directory / 'readiness.json').exists())

    def test_default_existing_attempt_never_contacts_native_or_verifies(self):
        self.run_cli(execute=True)
        self.load_gateways.reset_mock()
        self.verifier.reset_mock()
        result = self.run_cli()
        self.assertEqual(result['taskId'], self.state()['taskId'])
        self.load_gateways.assert_not_called()
        self.verifier.verify_pending.assert_not_called()

    def test_registration_reply_loss_recovers_original_key_without_register_or_submit(self):
        self.run_cli(execute=True)
        state = self.state(); original = state['taskId']; state['taskId'] = None
        verify.update_marker(self.directory / verify.STATE, state)
        with patch.object(TaskStore, 'register', side_effect=AssertionError('no register')):
            result = self.run_cli(execute=True)
        self.assertEqual(result['taskId'], original)
        self.assertEqual(self.state()['taskId'], original)
        self.assertEqual(self.dispatcher.submit.call_count, 1)

    def test_persisted_registration_intent_without_row_never_registers_on_replay(self):
        with patch.object(TaskStore, 'register', side_effect=RuntimeError('fixture registration crash')):
            with self.assertRaises(RuntimeError):
                self.run_cli(execute=True)
        with patch.object(TaskStore, 'register', side_effect=AssertionError('no register')):
            result = self.run_cli(execute=True)
        self.assertEqual(result['status'], 'registration_not_recorded')
        self.assertEqual(self.rows(), 0)
        self.dispatcher.submit.assert_not_called()

    def test_submit_exception_retains_intent_and_never_resubmits(self):
        self.dispatcher.submit.side_effect = RuntimeError('fixture lost native reply')
        with self.assertRaises(RuntimeError):
            self.run_cli(execute=True)
        result = self.run_cli(execute=True)
        self.assertEqual(result['status'], 'original_submission_not_confirmed')
        self.assertTrue(self.state()['submissionIntent'])
        self.assertEqual(self.dispatcher.submit.call_count, 1)
        self.assertEqual(self.rows(), 1)

    def test_original_success_generates_proof_with_unchanged_user_policy_and_setup_marker(self):
        before = (self.root / 'var' / verify.MARKER).read_bytes()
        self.dispatcher.submit.side_effect = self.make_successful
        result = self.run_cli(execute=True)
        self.assertEqual(result['status'], 'confirmed')
        proof = json.loads((self.directory / 'readiness.json').read_text())
        self.assertEqual(proof['coverage'], [{'projectId': 'business', 'roleBindingId': 'developer',
                                             'taskId': result['taskId']}])
        self.assertEqual(self.prepare.call_args.kwargs['bindings']['developer']['verificationPolicy'], self.policy)
        self.assertEqual(before, (self.root / 'var' / verify.MARKER).read_bytes())
        self.assertEqual(self.verifier.verify_pending.call_count, 1)
        self.assertEqual(self.gateway._client.get.call_args.args[0],
                         'http://127.0.0.1:65199/api/console/chat/task/original-parent')

    def test_parent_not_finished_keeps_pending_without_proof(self):
        self.dispatcher.submit.side_effect = self.make_successful
        self.gateway._client.get.return_value = httpx.Response(200, json={'status': 'running'},
            request=httpx.Request('GET', 'http://127.0.0.1:65199/api/console/chat/task/original-parent'))
        self.assertEqual(self.run_cli(execute=True)['status'], 'pending')
        self.prepare.assert_not_called()

    def test_proof_replay_only_evaluates_existing_proof_and_never_replaces_it(self):
        self.dispatcher.submit.side_effect = self.make_successful
        self.run_cli(execute=True)
        before = (self.directory / 'readiness.json').read_bytes()
        self.prepare.reset_mock()
        result = self.run_cli(execute=True)
        self.assertEqual(result['status'], 'confirmed')
        self.prepare.assert_not_called()
        self.evaluate.assert_called_once()
        self.assertEqual(before, (self.directory / 'readiness.json').read_bytes())
        self.assertEqual(self.dispatcher.submit.call_count, 1)

    def test_invalid_existing_proof_is_not_replaced(self):
        self.dispatcher.submit.side_effect = self.make_successful
        self.run_cli(execute=True)
        before = (self.directory / 'readiness.json').read_bytes()
        self.evaluate.return_value = Mock(confirmed=False)
        with self.assertRaisesRegex(ValueError, 'existing_readiness_unproven'):
            self.run_cli(execute=True)
        self.assertEqual(before, (self.directory / 'readiness.json').read_bytes())

    def test_configuration_change_rejects_attempt_before_native_access(self):
        self.run_cli(execute=True)
        path = self.directory / 'authoring.json'
        value = json.loads(path.read_text()); value['credentials'][0]['subject'] = 'different'
        verify.update_marker(path, value)
        self.load_gateways.reset_mock()
        with self.assertRaisesRegex(ValueError, 'verification_attempt_changed'):
            self.run_cli(execute=True)
        self.load_gateways.assert_not_called()

    def test_nonempty_store_refuses_before_attempt_or_submission(self):
        _, service, project, _ = verify.settings(self.root)
        self.store.register('foreign-existing-key', verify._payload(service, project))
        with self.assertRaisesRegex(ValueError, 'fresh_empty_unguarded_store_required'):
            self.run_cli(execute=True)
        self.assertFalse((self.directory / verify.STATE).exists())
        self.dispatcher.submit.assert_not_called()

    def test_guarded_store_refuses_and_keeps_guard(self):
        self.store.enter_maintenance()
        with self.assertRaisesRegex(ValueError, 'fresh_empty_unguarded_store_required'):
            self.run_cli(execute=True)
        self.assertTrue(self.store.maintenance_status()['guarded'])
        self.dispatcher.submit.assert_not_called()

    def test_preflight_failure_never_creates_task_or_intent(self):
        self.fingerprint.side_effect = ValueError('fixture upstream mismatch')
        with self.assertRaises(ValueError):
            self.run_cli(execute=True)
        self.assertEqual(self.rows(), 0)
        self.assertFalse((self.directory / verify.STATE).exists())

    def test_symlink_lock_refused_without_touching_target(self):
        target = self.base / 'foreign'; target.write_text('unchanged')
        (self.directory / verify.LOCK).symlink_to(target)
        with self.assertRaises(OSError):
            self.run_cli(execute=True)
        self.assertEqual(target.read_text(), 'unchanged')
        self.dispatcher.submit.assert_not_called()

    def test_exclusive_lock_prevents_second_attempt(self):
        with verify.attempt_lock(self.directory):
            with self.assertRaises(BlockingIOError):
                self.run_cli(execute=True)
        self.assertEqual(self.rows(), 0)

    def test_unbounded_or_nonfinite_wait_rejected(self):
        for value in (-1, 61, float('nan'), float('inf'), True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                verify.run_verify(root=self.root, wait_seconds=value)


if __name__ == '__main__':
    unittest.main()
