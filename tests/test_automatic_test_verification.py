"""Trusted test receipts integrate with original leases and durable verification."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_line.automatic_verification import AutomaticVerifier
from agent_line.runtime import load_app
from agent_line.store import Conflict, TaskStore
from agent_line.test_executor import DockerTestExecutor
from agent_line.verification_policy import policy_hash, validate_policy
from agent_line.verification_runtime import load_verifier


class AutomaticTestVerificationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.workspace = self.root/'workspace'; self.workspace.mkdir()
        self.directory = self.root/'evidence'; self.directory.mkdir(mode=0o700)
        self.data = b'tested artifact\n'; (self.workspace/'result.txt').write_bytes(self.data)
        self.executor = DockerTestExecutor(self.directory, 'sha256:'+'a'*64)
        self.policy = validate_policy(dict(schemaVersion=2, policyId='tests-v1',
            kind='command_and_artifacts', requireTests=True, testEnvironmentId=self.executor.environment_id,
            artifacts=[dict(path='result.txt', sha256=hashlib.sha256(self.data).hexdigest(), exactSize=len(self.data))],
            tests=[dict(id='unit', argv=['python3', '-m', 'unittest'])]))
        self.store = TaskStore.initialize(self.root/'tasks.sqlite')
        task, _ = self.store.register('test', dict(projectId='project', roleAgentId='role',
            roleBindingId='role', workspace=str(self.workspace), verificationPolicy=self.policy,
            workflow=dict(id='workflow', runId='run', nodeExecutionId='node', attemptId='one')))
        self.task = task['id']; self.identity = dict(codexThreadId='thread', codexTurnId='turn')
        self.store.claim(self.task, self.workspace); self.store.accept(self.task, self.identity)
        self.store.finish(self.task, 'succeeded', {'testsPassed': True},
                          [dict(kind='execution_terminal', executionId='turn', confirmed=True)])
        self.store.confirm_resources_released(self.task, 'turn', dict(source='docker-inspect-before-remove',
            owner='b'*32, containerId='c'*64, workspace=str(self.workspace), running=False, pid=0,
            status='exited', finishedAt='time', identities=[self.identity]))
        self.verifier = AutomaticVerifier(self.store, self.directory, test_executor=self.executor)

    def completed(self, *, code=0):
        test = self.policy['tests'][0]
        proof = dict(source='docker-inspect-before-remove', containerId='d'*64, taskId=self.task,
            executionIdentity=self.identity, policySha256=policy_hash(self.policy), testId=test['id'],
            environmentId=self.executor.environment_id, owner='e'*32, workspace=str(self.workspace),
            startedAt='2026-01-01T00:00:00Z', finishedAt='2026-01-01T00:00:01Z',
            oomKilled=False, running=False, pid=0, status='exited', exitCode=code)
        return dict(status='completed', check=dict(kind='test_command', testId=test['id'], argv=test['argv'],
            timeoutSeconds=test['timeoutSeconds'], maxOutputBytes=test['maxOutputBytes'],
            environmentId=self.executor.environment_id, exitCode=code, containerId='d'*64,
            resourceExitReceipt=proof, stdout='ran test\n', stderr='', outputTruncated=False,
            status='passed' if code==0 else 'failed', reason='command_exit_zero' if code==0 else 'command_exit_nonzero'))

    def test_completed_commands_and_artifacts_commit_then_release_original_lease(self):
        with patch.object(self.executor, 'check', return_value=self.completed()) as runner:
            self.assertEqual(self.verifier.verify_pending(self.task)['verificationStatus'], 'passed')
            self.assertEqual(self.verifier.verify_pending(self.task)['status'], 'already_recorded')
            runner.assert_called_once()
        task = self.store.get(self.task)
        self.assertEqual([x['kind'] for x in task['evidence'][-1]['checks']], ['artifact_sha256', 'test_command'])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM workspace_leases').fetchone()[0], 0)

    def test_nonzero_exit_overrides_model_claim_of_tests_passed(self):
        with patch.object(self.executor, 'check', return_value=self.completed(code=3)):
            self.assertEqual(self.verifier.verify_pending(self.task)['verificationStatus'], 'failed')
        self.assertEqual(self.store.get(self.task)['executionStatus'], 'succeeded')

    def test_waiting_or_unknown_retains_lease_without_final_receipt(self):
        for status in ('waiting', 'blocked'):
            with self.subTest(status=status), patch.object(self.executor, 'check', return_value={'status':status}):
                self.assertEqual(self.verifier.verify_pending(self.task)['status'], status)
                self.assertEqual(self.store.verification_lease(self.task), str(self.workspace))
                self.assertFalse(list(self.directory.glob('verification-*.json')))

    def test_bad_exit_receipt_or_forged_success_never_commits(self):
        invalid = self.completed(code=2); invalid['check']['status'] = 'passed'
        wrong = self.completed(); wrong['check']['resourceExitReceipt']['workspace'] = '/other'
        for value in (invalid, wrong):
            with self.subTest(value=value), patch.object(self.executor, 'check', return_value=value):
                self.assertEqual(self.verifier.verify_pending(self.task)['status'], 'blocked')
        self.assertEqual(self.store.get(self.task)['verificationStatus'], 'pending')
        self.assertEqual(self.store.verification_lease(self.task), str(self.workspace))

    def interrupt_after_receipt(self):
        with patch.object(self.executor, 'check', return_value=self.completed()), patch.object(
                self.store, 'record_verification', side_effect=Conflict('database unavailable')):
            self.assertEqual(self.verifier.verify_pending(self.task)['status'], 'blocked')
        return next(self.directory.glob('verification-*.json'))

    def test_saved_final_receipt_recovery_never_runs_tests_or_rereads_artifacts(self):
        path = self.interrupt_after_receipt(); original = path.read_bytes()
        (self.workspace/'result.txt').write_bytes(b'later independent change')
        recovered = AutomaticVerifier(TaskStore(self.store.path), self.directory)
        with patch.object(DockerTestExecutor, 'check', side_effect=AssertionError('rerun')):
            self.assertEqual(recovered.verify_pending(self.task)['verificationStatus'], 'passed')
        self.assertEqual(path.read_bytes(), original)

    def test_bad_saved_command_proof_stays_pending_instead_of_rerunning(self):
        path = self.interrupt_after_receipt(); value = json.loads(path.read_text())
        value['checks'][-1]['resourceExitReceipt']['executionIdentity'] = {'codexTurnId':'other'}
        path.write_text(json.dumps(value))
        with patch.object(self.executor, 'check', side_effect=AssertionError('rerun')):
            self.assertEqual(self.verifier.verify_pending(self.task)['status'], 'blocked')
        self.assertEqual(self.store.verification_lease(self.task), str(self.workspace))

    def test_missing_executor_or_changed_environment_cannot_start_commands(self):
        missing = AutomaticVerifier(self.store, self.directory)
        self.assertEqual(missing.verify_pending(self.task)['status'], 'blocked')
        other = DockerTestExecutor(self.directory, 'sha256:'+'f'*64)
        changed = AutomaticVerifier(self.store, self.directory, test_executor=other)
        with patch.object(other, 'check', side_effect=AssertionError('new environment')):
            self.assertEqual(changed.verify_pending(self.task)['status'], 'blocked')

    def test_loader_and_service_validate_executor_without_enabling_execution(self):
        settings = dict(evidenceDirectory=str(self.directory), testExecutor={'imageId':'sha256:'+'a'*64})
        verifier = load_verifier(settings, tasks=self.store, projects={'project':{'workspace':str(self.workspace)}})
        self.assertEqual(verifier.test_executor.environment_id, self.executor.environment_id)
        config = dict(schemaVersion=1, database=str(self.store.path), tokens={'secret':['project']},
            projects={'project':{'workspace':str(self.workspace)}},
            bindings={'role':dict(projectId='project', roleAgentId='role', verificationPolicy=self.policy)},
            verification=settings)
        path = self.root/'service.json'; path.write_text(json.dumps(config)); path.chmod(0o600)
        with patch('agent_line.test_executor._command', side_effect=AssertionError('startup may not run Docker')):
            app = load_app(path)
        self.assertFalse(app.state.execution_worker.loop.allow_submissions)
        config['verification']['testExecutor']['imageId'] = 'sha256:'+'f'*64
        path.write_text(json.dumps(config))
        with self.assertRaises(ValueError): load_app(path)

    def test_loader_refuses_arbitrary_commands_or_mutable_image_in_executor_config(self):
        for executor in ({'imageId':'latest'}, {'imageId':'sha256:'+'a'*64,'command':['sh']}, None):
            with self.subTest(executor=executor), self.assertRaises(ValueError):
                load_verifier(dict(evidenceDirectory=str(self.directory), testExecutor=executor),
                              tasks=self.store, projects={'project':{'workspace':str(self.workspace)}})

    def test_only_concrete_executor_using_same_directory_is_accepted(self):
        with self.assertRaises(ValueError): AutomaticVerifier(self.store, self.directory, test_executor=object())
        other = self.root/'other'; other.mkdir(mode=0o700)
        with self.assertRaises(ValueError):
            AutomaticVerifier(self.store, self.directory, test_executor=DockerTestExecutor(other, 'sha256:'+'a'*64))
