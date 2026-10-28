"""Test-stage cancellation preserves execution facts and original ownership."""
import json
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent_line.automatic_verification import AutomaticVerifier
from agent_line.store import Conflict, TaskStore
from agent_line.service import ServiceConfig, create_app
from tests import test_automatic_test_verification as fixtures


class AutomaticVerificationCancellationTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.AutomaticTestVerificationTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)

    def request(self):
        self.assertTrue(self.f.store.request_cancel(self.f.task))

    def exited(self):
        check = self.f.completed()['check']
        return {'status':'completed', 'cancellation':dict(testId=check['testId'], disposition='exited',
            taskId=self.f.task, executionIdentity=self.f.identity, policySha256=check['resourceExitReceipt']['policySha256'],
            environmentId=check['environmentId'], containerId=check['containerId'],
            resourceExitReceipt=check['resourceExitReceipt'], check=check)}

    def test_cancel_before_test_creates_no_container_and_preserves_native_success(self):
        self.request()
        with patch('agent_line.test_executor._command', side_effect=AssertionError('must not create')):
            result = self.f.verifier.verify_pending(self.f.task)
        self.assertEqual(result['verificationStatus'], 'canceled')
        task = self.f.store.get(self.f.task)
        self.assertEqual(task['executionStatus'], 'succeeded')
        self.assertTrue(task['resourcesReleased']); self.assertTrue(task['cancellationRequested'])
        receipt = task['evidence'][-1]
        self.assertEqual(receipt['kind'], 'verification_cancellation')
        self.assertEqual(receipt['tests'][0]['disposition'], 'not_started')
        self.assertNotIn('resourceExitReceipt', receipt['tests'][0])
        with self.f.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM workspace_leases').fetchone()[0], 0)

    def test_waiting_or_unknown_stop_keeps_original_lease(self):
        self.request()
        for status in ('waiting','blocked'):
            with self.subTest(status=status), patch.object(self.f.executor, 'cancel', return_value={'status':status}):
                self.assertEqual(self.f.verifier.verify_pending(self.f.task)['status'], status)
            self.assertEqual(self.f.store.get(self.f.task)['verificationStatus'], 'pending')
            self.assertEqual(self.f.store.verification_lease(self.f.task), str(self.f.workspace))
        self.assertFalse(list(self.f.directory.glob('cancellation-*.json')))

    def test_completed_test_keeps_passed_fact_while_remaining_phase_is_canceled(self):
        self.request()
        with patch.object(self.f.executor, 'cancel', return_value=self.exited()):
            self.assertEqual(self.f.verifier.verify_pending(self.f.task)['verificationStatus'], 'canceled')
        test = self.f.store.get(self.f.task)['evidence'][-1]['tests'][0]
        self.assertEqual(test['check']['status'], 'passed')
        self.assertEqual(test['disposition'], 'exited')

    def test_invalid_exit_never_releases_lease(self):
        self.request(); wrong = self.exited()
        wrong['cancellation']['resourceExitReceipt']['running'] = True
        with patch.object(self.f.executor, 'cancel', return_value=wrong):
            self.assertEqual(self.f.verifier.verify_pending(self.f.task)['status'], 'blocked')
        self.assertEqual(self.f.store.verification_lease(self.f.task), str(self.f.workspace))

    def interrupt_cancellation_commit(self):
        self.request()
        with patch.object(self.f.store, 'record_verification_cancellation', side_effect=Conflict('database unavailable')):
            self.assertEqual(self.f.verifier.verify_pending(self.f.task)['status'], 'blocked')
        return next(self.f.directory.glob('cancellation-*.json'))

    def test_saved_cancellation_recovery_without_runtime_never_stops_or_starts_again(self):
        path = self.interrupt_cancellation_commit(); before = path.read_bytes()
        recovered = AutomaticVerifier(TaskStore(self.f.store.path), self.f.directory)
        with patch.object(self.f.executor, 'cancel', side_effect=AssertionError('repeat stop')):
            self.assertEqual(recovered.verify_pending(self.f.task)['verificationStatus'], 'canceled')
        self.assertEqual(path.read_bytes(), before)

    def test_corrupt_saved_cancellation_is_not_replaced(self):
        path = self.interrupt_cancellation_commit(); value = json.loads(path.read_text())
        value['policySha256'] = '0'*64; path.write_text(json.dumps(value))
        with patch.object(self.f.executor, 'cancel', side_effect=AssertionError('repeat stop')):
            self.assertEqual(self.f.verifier.verify_pending(self.f.task)['status'], 'blocked')
        self.assertEqual(self.f.store.verification_lease(self.f.task), str(self.f.workspace))

    def test_saved_normal_receipt_cannot_win_after_cancellation_commits(self):
        with patch.object(self.f.executor, 'check', return_value=self.f.completed()), patch.object(
                self.f.store, 'record_verification', side_effect=Conflict('pending database')):
            self.assertEqual(self.f.verifier.verify_pending(self.f.task)['status'], 'blocked')
        self.request()
        with patch.object(self.f.executor, 'check', side_effect=AssertionError('rerun')), patch.object(
                self.f.executor, 'cancel', return_value=self.exited()):
            self.assertEqual(self.f.verifier.verify_pending(self.f.task)['verificationStatus'], 'canceled')

    def test_cancel_waits_for_current_check_then_next_poll_cannot_start_another(self):
        entered = threading.Event(); release = threading.Event(); canceled = threading.Event(); results = []
        def checking(*args):
            entered.set(); self.assertTrue(release.wait(3)); return {'status':'waiting'}
        def canceling():
            results.append(self.f.store.request_cancel(self.f.task)); canceled.set()
        with patch.object(self.f.executor, 'check', side_effect=checking):
            thread = threading.Thread(target=lambda: self.f.verifier.verify_pending(self.f.task)); thread.start()
            self.assertTrue(entered.wait(2))
            requester = threading.Thread(target=canceling); requester.start()
            self.assertFalse(canceled.wait(.1))
            release.set(); thread.join(3); requester.join(3)
            self.assertFalse(thread.is_alive()); self.assertFalse(requester.is_alive())
        self.assertEqual(results,[True])
        with patch.object(self.f.executor, 'check', side_effect=AssertionError('post-cancel start')):
            self.assertEqual(self.f.verifier.verify_pending(self.f.task)['verificationStatus'], 'canceled')

    def test_missing_executor_or_changed_environment_cannot_invent_canceled_resources(self):
        self.request()
        verifier = AutomaticVerifier(self.f.store, self.f.directory)
        self.assertEqual(verifier.verify_pending(self.f.task)['status'], 'blocked')
        self.assertEqual(self.f.store.verification_lease(self.f.task), str(self.f.workspace))

    def test_repeated_final_observation_has_no_runtime_side_effect(self):
        self.request(); self.f.verifier.verify_pending(self.f.task)
        with patch.object(self.f.executor, 'cancel', side_effect=AssertionError('repeat')):
            self.assertEqual(self.f.verifier.verify_pending(self.f.task),
                             {'status':'already_recorded','verificationStatus':'canceled'})

    def client(self):
        app = create_app(self.f.store, ServiceConfig(tokens={'key':{'project'}},
            projects={'project':{'workspace':str(self.f.workspace)}},
            bindings={'role':{'projectId':'project','roleAgentId':'role','verificationPolicy':self.f.policy}}),
            verifier=self.f.verifier)
        client = TestClient(app); self.addCleanup(client.close)
        return client

    def test_http_exposes_phase_action_without_mistaking_native_exit_for_test_exit(self):
        client = self.client(); url='/extension/v1/node-tasks/'+self.f.task
        headers={'Authorization':'Bearer key'}
        before=client.get(url, headers=headers).json()
        self.assertTrue(before['canCancelVerification'])
        self.assertFalse(before['verificationCancellationRequested'])
        response=client.post(url+'/cancel', headers=headers, json={})
        self.assertEqual(response.status_code, 200)
        pending=response.json(); self.assertTrue(pending['changed'])
        self.assertTrue(pending['verificationCancellationRequested'])
        self.assertTrue(pending['resourcesReleased'])
        self.assertEqual(pending['verificationStatus'], 'pending')
        self.assertFalse(pending['canCancelVerification'])
        self.f.verifier.verify_pending(self.f.task)
        final=client.get(url, headers=headers).json()
        self.assertEqual(final['verificationStatus'], 'canceled')
        self.assertEqual(final['executionStatus'], 'succeeded')

    def test_public_nested_test_exit_proofs_omit_private_workspace(self):
        self.request()
        with patch.object(self.f.executor, 'cancel', return_value=self.exited()):
            self.f.verifier.verify_pending(self.f.task)
        client=self.client()
        task=client.get('/extension/v1/node-tasks/'+self.f.task, headers={'Authorization':'Bearer key'}).json()
        proof=task['evidence'][-1]['tests'][0]
        self.assertNotIn('workspace', proof['resourceExitReceipt'])
        self.assertNotIn('workspace', proof['check']['resourceExitReceipt'])
        self.assertIn('workspace', self.f.store.get(self.f.task)['evidence'][-1]['tests'][0]['resourceExitReceipt'])
