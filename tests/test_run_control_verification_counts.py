"""Run summaries distinguish coding exit from pending test verification."""
import time
import unittest

from agent_line.verification_policy import policy_hash, validate_policy
from tests import test_run_control_api as control_fixtures
from tests import test_test_verification_policy as verification_fixtures


class RunControlVerificationCountsTests(unittest.TestCase):
    def setUp(self):
        self.f = control_fixtures.RunControlAPITests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.store = self.f.f.tasks
        self.policy = validate_policy(verification_fixtures.policy())
        self.identity = {'codexThreadId': 'thread', 'codexTurnId': 'turn'}

    def task(self, policy=None, **scope):
        payload = {'projectId': 'project', 'roleAgentId': 'parent',
                   'workspace': str(self.f.f.root),
                   'workflow': {'id': 'workflow', 'runId': self.f.run,
                                'nodeExecutionId': 'unit', 'attemptId': '1'}}
        payload['workflow'].update(scope)
        if policy is not None:
            payload['verificationPolicy'] = policy
        task, _ = self.store.register('task-' + str(scope), payload)
        return task['id']

    def finish(self, task_id, *, release=True):
        self.assertTrue(self.store.claim(task_id, self.f.f.root))
        self.store.accept(task_id, self.identity)
        self.store.finish(task_id, 'succeeded', {'testsPassed': True}, [
            {'kind': 'execution_terminal', 'executionId': 'turn', 'confirmed': True}])
        if release:
            self.store.confirm_resources_released(task_id, 'turn', {
                'source': 'docker-inspect-before-remove', 'running': False,
                'pid': 0, 'status': 'exited', 'owner': 'a' * 32,
                'containerId': 'b' * 64, 'finishedAt': 'time',
                'workspace': str(self.f.f.root), 'identities': [self.identity]})

    def counts(self):
        response = self.f.f.client.get(self.f.url, headers=self.f.f.headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()['control']['taskCounts']

    def test_coding_exit_does_not_hide_pending_tests_or_cancel_intent(self):
        task_id = self.task(self.policy)
        self.finish(task_id)
        counts = self.counts()
        self.assertEqual(counts['active'], 0)
        self.assertEqual(counts['resourcesUnreleased'], 0)
        self.assertEqual(counts['verificationPending'], 1)
        self.assertEqual(counts['verificationCancelRequested'], 0)
        stopped = self.f.command('stop')
        self.assertEqual(stopped.status_code, 200, stopped.text)
        counts = stopped.json()['control']['taskCounts']
        self.assertEqual(counts['verificationPending'], 1)
        self.assertEqual(counts['verificationCancelRequested'], 1)
        self.assertEqual(counts['verificationCanceled'], 0)
        self.store.record_verification_cancellation(task_id, {
            'kind': 'verification_cancellation', 'schemaVersion': 1,
            'taskId': task_id, 'executionIdentity': self.identity,
            'policySha256': policy_hash(self.policy), 'canceledAt': time.time(),
            'evidenceFile': 'cancellation-' + 'e' * 64 + '.json',
            'tests': [{'testId': 'unit', 'disposition': 'not_started',
                       'taskId': task_id, 'executionIdentity': self.identity,
                       'policySha256': policy_hash(self.policy),
                       'environmentId': self.policy['testEnvironmentId']}]})
        counts = self.counts()
        self.assertEqual(counts['verificationPending'], 0)
        self.assertEqual(counts['verificationCancelRequested'], 0)
        self.assertEqual(counts['verificationCanceled'], 1)

    def test_pending_tests_count_before_native_exit_is_confirmed(self):
        task_id = self.task(self.policy)
        self.finish(task_id, release=False)
        self.store.request_cancel(task_id)
        counts = self.counts()
        self.assertEqual(counts['resourcesUnreleased'], 1)
        self.assertEqual(counts['verificationPending'], 1)
        self.assertEqual(counts['verificationCancelRequested'], 1)

    def test_old_tasks_and_byte_policies_do_not_invent_test_counts(self):
        for index, policy in enumerate((None, {'schemaVersion': 1, 'policyId': 'bytes',
                'kind': 'artifact_sha256', 'requireTests': False,
                'artifacts': [{'path': 'result.txt', 'sha256': 'a' * 64}]})):
            with self.subTest(policy=policy):
                task_id = self.task(policy, nodeExecutionId=str(index))
                self.finish(task_id)
                counts = self.counts()
                for key in ('verificationPending', 'verificationCancelRequested', 'verificationCanceled'):
                    self.assertEqual(counts[key], 0)

    def test_other_workflow_tasks_are_excluded(self):
        task_id = self.task(self.policy, id='another-workflow')
        self.finish(task_id)
        counts = self.counts()
        self.assertEqual(counts['total'], 0)
        self.assertEqual(counts['verificationPending'], 0)
