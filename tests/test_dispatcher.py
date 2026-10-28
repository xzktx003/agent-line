import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from agent_line.store import TaskStore
from agent_line.dispatcher import Dispatcher, DispatchLoop


class DispatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = TaskStore.initialize(self.root / "tasks.sqlite")
        self.task, _ = self.store.register(
            "key",
            {
                "projectId": "project",
                "roleAgentId": "role",
                "workspace": str(self.root),
                "workflow": {
                    "runId": "run",
                    "nodeExecutionId": "node",
                    "attemptId": "one",
                },
                "input": {"instruction": "do work"},
            },
        )
        self.gateway = Mock()
        self.gateway.submit.return_value = {
            "status": "accepted",
            "identity": {"upstreamTaskId": "native-task"},
        }
        self.dispatcher = Dispatcher(self.store, {"project": self.gateway})

    def test_role_start_is_recorded_once_without_changing_acceptance(self):
        self.dispatcher.submit(self.task['id'])
        events = self.store.events(self.task['id'])
        self.assertEqual([event['kind'] for event in events].count('role_started'), 1)
        role = next(event for event in events if event['kind'] == 'role_started')
        self.assertEqual(role['category'], 'role_started')
        self.assertEqual(role['source_identity'], 'role-binding')
        self.assertEqual(self.store.get(self.task['id'])['executionStatus'], 'accepted')
        Dispatcher(TaskStore(self.store.path), {'project': self.gateway}).submit(self.task['id'])
        self.assertEqual(self.store.events(self.task['id']), events)
        self.assertEqual(self.gateway.submit.call_count, 1)

    def test_role_note_failure_does_not_retry_or_undo_acceptance(self):
        import sqlite3

        def fail(_task_id):
            raise sqlite3.OperationalError('note unavailable')

        self.store.note_role_started = fail
        result = self.dispatcher.submit(self.task['id'])
        self.assertEqual(result['executionStatus'], 'accepted')
        self.assertEqual(self.gateway.submit.call_count, 1)
        self.assertNotIn('role_started', [event['kind'] for event in self.store.events(self.task['id'])])
        Dispatcher(TaskStore(self.store.path), {'project': self.gateway}).submit(self.task['id'])
        self.assertEqual(self.gateway.submit.call_count, 1)

    def test_submission_claim_survives_restart_without_duplicate(self):
        self.dispatcher.submit(self.task["id"])
        Dispatcher(TaskStore(self.store.path), {"project": self.gateway}).submit(
            self.task["id"]
        )
        self.assertEqual(self.gateway.submit.call_count, 1)
        self.assertEqual(self.store.get(self.task["id"])["executionStatus"], "accepted")

    def test_lost_receipt_reconciles_without_reposting(self):
        self.gateway.submit.side_effect = TimeoutError()
        self.dispatcher.submit(self.task["id"])
        self.assertEqual(
            self.store.get(self.task["id"])["executionStatus"], "uncertain"
        )
        self.gateway.observe.return_value = {
            "status": "accepted",
            "identity": {"upstreamTaskId": "native-task"},
        }
        self.dispatcher.reconcile(self.task["id"])
        self.assertEqual(self.store.get(self.task["id"])["executionStatus"], "accepted")
        self.assertEqual(self.gateway.submit.call_count, 1)

    def test_unknown_observation_keeps_lease_and_never_reposts(self):
        self.gateway.submit.side_effect = TimeoutError()
        self.dispatcher.submit(self.task["id"])
        self.gateway.observe.return_value = {"status": "unknown"}
        self.dispatcher.reconcile(self.task["id"])
        self.assertEqual(
            self.store.get(self.task["id"])["executionStatus"], "uncertain"
        )
        self.assertFalse(self.store.get(self.task["id"])["resourcesReleased"])
        self.assertEqual(self.gateway.submit.call_count, 1)

    def test_model_success_without_terminal_confirmation_is_not_completion(self):
        self.dispatcher.submit(self.task["id"])
        self.gateway.observe.return_value = {
            "status": "succeeded",
            "identity": {"upstreamTaskId": "native-task"},
            "output": "done",
        }
        self.dispatcher.reconcile(self.task["id"])
        self.assertEqual(self.store.get(self.task["id"])["executionStatus"], "accepted")

    def test_confirmed_terminal_does_not_mark_independent_verification_passed(self):
        self.dispatcher.submit(self.task["id"])
        self.gateway.observe.return_value = {
            "status": "succeeded",
            "identity": {"upstreamTaskId": "native-task"},
            "output": {"result": "done"},
            "terminalConfirmed": True,
            "resourcesReleased": True,
        }
        self.dispatcher.reconcile(self.task["id"])
        result = self.store.get(self.task["id"])
        self.assertEqual(result["executionStatus"], "succeeded")
        self.assertEqual(result["verificationStatus"], "pending")
        self.assertTrue(result["resourcesReleased"])


    def test_explicit_unaccepted_rejection_keeps_reason_until_corrected_resubmit(self):
        refusal = {
            "status": "rejected",
            "identity": {},
            "accepted": False,
            "pollHint": "submission_rejected",
            "rejectionReason": "submission_rejected_unaccepted",
            "terminalConfirmed": False,
            "resourcesReleased": False,
        }
        self.gateway.submit.return_value = refusal
        result = self.dispatcher.submit(self.task["id"])
        self.assertEqual(result["executionStatus"], "registered")
        self.assertEqual(result["submissionBlock"], "submission_rejected")
        self.assertEqual(result["executionIdentity"], {})
        self.assertEqual(result["payload"]["workflow"]["attemptId"], "one")
        rejected = [event for event in self.store.events(self.task["id"]) if event["kind"] == "submission_rejected"]
        self.assertEqual(len(rejected), 1)
        self.assertEqual(json.loads(rejected[0]["payload"])["reason"], "submission_rejected_unaccepted")
        self.dispatcher.submit(self.task["id"])
        DispatchLoop(self.dispatcher).tick()
        self.assertEqual(self.gateway.submit.call_count, 1)
        corrected = self.dispatcher.resubmit_after_correction(self.task["id"])
        self.assertEqual(self.gateway.submit.call_count, 2)
        self.assertEqual(corrected["executionStatus"], "registered")
        self.assertEqual(corrected["submissionBlock"], "submission_rejected")
        self.assertEqual(corrected["id"], self.task["id"])
        intents = [event for event in self.store.events(self.task["id"]) if event["kind"] == "submission_intent"]
        self.assertEqual(len(intents), 1)

    def test_incomplete_or_unknown_submission_stays_uncertain(self):
        cases = (
            {"status": "unknown", "pollHint": "unknown"},
            {"status": "unknown", "pollHint": "unavailable"},
            {"status": "rejected", "pollHint": "submission_rejected", "identity": {}},
            {"status": "rejected", "accepted": False, "pollHint": "submission_rejected",
             "rejectionReason": "submission_rejected_unaccepted", "identity": {"upstreamTaskId": "native"},
             "terminalConfirmed": False, "resourcesReleased": False},
        )
        for index, result in enumerate(cases):
            with self.subTest(result=result):
                task, _ = self.store.register(
                    "uncertain-" + str(index),
                    {
                        "projectId": "project",
                        "roleAgentId": "role-" + str(index),
                        "workspace": str(self.root.parent / (self.root.name + "-case-" + str(index))),
                        "workflow": {"runId": "run", "nodeExecutionId": "node-" + str(index), "attemptId": "one"},
                        "input": {"instruction": "do work"},
                    },
                )
                self.gateway.submit.return_value = result
                self.dispatcher.submit(task["id"])
                current = self.store.get(task["id"])
                self.assertEqual(current["executionStatus"], "uncertain")
                self.assertNotIn("submissionBlock", current)
                self.dispatcher.resubmit_after_correction(task["id"])
        self.assertEqual(self.gateway.submit.call_count, len(cases))

    def test_binding_specific_gateway_precedes_project_default(self):
        gateway = Mock()
        gateway.submit.return_value = {"status": "unknown"}
        dispatcher = Dispatcher(
            self.store, {"project": self.gateway, ("project", None): gateway}
        )
        dispatcher.submit(self.task["id"])
        gateway.submit.assert_called_once()
        self.gateway.submit.assert_not_called()

    def test_unregistered_binding_does_not_fall_back_to_project_gateway(self):
        dispatcher = Dispatcher(
            self.store, {"project": self.gateway, ("project", "other-binding"): Mock()}
        )
        result = dispatcher.submit(self.task["id"])
        self.assertEqual(result["executionStatus"], "registered")
        self.gateway.submit.assert_not_called()

    def test_wrong_role_or_workspace_is_rejected_before_claim(self):
        from agent_line.store import Conflict

        self.gateway.role = "different-role"
        with self.assertRaises(Conflict):
            self.dispatcher.submit(self.task["id"])
        self.gateway.role = "role"
        self.gateway.workspace = str(self.root / "another")
        with self.assertRaises(Conflict):
            self.dispatcher.submit(self.task["id"])
        self.assertEqual(
            self.store.get(self.task["id"])["executionStatus"], "registered"
        )
        self.gateway.submit.assert_not_called()

    def test_unconfigured_project_does_not_claim(self):
        Dispatcher(self.store, {}).submit(self.task["id"])
        self.assertEqual(
            self.store.get(self.task["id"])["executionStatus"], "registered"
        )

    def test_cancel_before_late_receipt_preserves_cancellation_intent(self):
        self.gateway.submit.side_effect = TimeoutError()
        self.dispatcher.submit(self.task["id"])
        self.store.request_cancel(self.task["id"])
        self.gateway.observe.return_value = {
            "status": "accepted",
            "identity": {"upstreamTaskId": "native-task"},
        }
        self.dispatcher.reconcile(self.task["id"])
        task = self.store.get(self.task["id"])
        self.assertEqual(task["executionStatus"], "cancel_requested")
        self.assertEqual(task["executionIdentity"]["upstreamTaskId"], "native-task")

    def test_native_identity_can_gain_thread_and_turn_without_replacement(self):
        self.dispatcher.submit(self.task["id"])
        identity = {
            "upstreamTaskId": "native-task",
            "codexThreadId": "thread",
            "codexTurnId": "turn",
        }
        self.gateway.observe.return_value = {"status": "accepted", "identity": identity}
        self.dispatcher.reconcile(self.task["id"])
        self.assertEqual(self.store.get(self.task["id"])["executionIdentity"], identity)
        self.gateway.observe.return_value = {
            "status": "succeeded",
            "identity": identity,
            "terminalConfirmed": True,
            "resourcesReleased": True,
            "output": {"done": True},
        }
        self.dispatcher.reconcile(self.task["id"])
        self.assertEqual(
            self.store.get(self.task["id"])["executionStatus"], "succeeded"
        )

    def test_cancel_request_is_sent_but_ack_is_not_terminal(self):
        self.dispatcher.submit(self.task["id"])
        self.store.request_cancel(self.task["id"])
        self.gateway.observe.return_value = {
            "status": "accepted",
            "identity": {"upstreamTaskId": "native-task"},
        }
        self.gateway.cancel.return_value = {
            "status": "accepted",
            "identity": {"upstreamTaskId": "native-task"},
            "cancellationRequested": True,
        }
        self.dispatcher.reconcile(self.task["id"])
        self.gateway.cancel.assert_called_once_with(self.task["id"])
        current = self.store.get(self.task["id"])
        self.assertEqual(current["executionStatus"], "cancel_requested")
        self.assertFalse(current["resourcesReleased"])

    def test_native_cancel_ack_without_container_exit_stays_cancel_requested(self):
        self.dispatcher.submit(self.task["id"])
        self.store.request_cancel(self.task["id"])
        identity = {"upstreamTaskId": "native-task", "codexThreadId": "thread", "codexTurnId": "turn"}
        self.gateway.observe.return_value = {"status": "accepted", "identity": identity}
        self.gateway.cancel.return_value = {
            "status": "canceled",
            "identity": identity,
            "terminalConfirmed": True,
            "resourcesReleased": True,
            "parent": {"cancelRequested": True, "delegationRevoked": True,
                       "parentStopAcknowledged": True},
        }
        current = self.dispatcher.reconcile(self.task["id"])
        self.assertEqual(current["executionStatus"], "cancel_requested")
        self.assertFalse(current["resourcesReleased"])
        self.gateway.cancel.assert_called_once_with(self.task["id"])

    def test_canceled_task_releases_only_with_matching_container_exit(self):
        self.dispatcher.submit(self.task["id"])
        self.store.request_cancel(self.task["id"])
        identity = {"upstreamTaskId": "native-task", "codexThreadId": "thread", "codexTurnId": "turn"}
        receipt = {
            "source": "docker-inspect-before-remove", "owner": "owner", "containerId": "container",
            "workspace": str(self.root), "identities": [identity], "running": False, "pid": 0,
            "status": "exited", "finishedAt": "2026-10-28T00:00:00Z",
        }
        self.gateway.observe.return_value = {"status": "accepted", "identity": identity}
        self.gateway.cancel.return_value = {
            "status": "canceled", "identity": identity, "terminalConfirmed": True,
            "resourcesReleased": True, "resourceExitReceipt": {"source": "parent-stop"},
        }
        held = self.dispatcher.reconcile(self.task["id"])
        self.assertEqual(held["executionStatus"], "cancel_requested")
        self.assertFalse(held["resourcesReleased"])
        self.gateway.cancel.return_value["resourceExitReceipt"] = receipt
        released = self.dispatcher.reconcile(self.task["id"])
        self.assertEqual(released["executionStatus"], "canceled")
        self.assertTrue(released["resourcesReleased"])

    def test_cancel_reaches_gateway_before_native_identity_is_known(self):
        self.gateway.submit.side_effect = TimeoutError()
        self.dispatcher.submit(self.task["id"])
        self.store.request_cancel(self.task["id"])
        self.gateway.observe.return_value = {"status": "unknown"}
        self.gateway.cancel.return_value = {"status": "unknown"}
        self.dispatcher.reconcile(self.task["id"])
        self.gateway.cancel.assert_called_once_with(self.task["id"])
        current = self.store.get(self.task["id"])
        self.assertEqual(current["executionStatus"], "cancel_requested")
        self.assertFalse(current["resourcesReleased"])

    def test_observation_failure_does_not_drop_cancellation_delivery(self):
        self.gateway.submit.side_effect = TimeoutError()
        self.dispatcher.submit(self.task["id"])
        self.store.request_cancel(self.task["id"])
        self.gateway.observe.side_effect = TimeoutError()
        self.gateway.cancel.return_value = {"status": "unknown"}
        self.dispatcher.reconcile(self.task["id"])
        self.gateway.cancel.assert_called_once_with(self.task["id"])
        self.assertEqual(
            self.store.get(self.task["id"])["executionStatus"], "cancel_requested"
        )

    def test_cancel_after_native_completion_does_not_send_stop(self):
        self.dispatcher.submit(self.task["id"])
        self.store.request_cancel(self.task["id"])
        self.gateway.observe.return_value = {
            "status": "succeeded",
            "identity": {"upstreamTaskId": "native-task"},
            "terminalConfirmed": True,
            "resourcesReleased": True,
            "output": {},
        }
        self.dispatcher.reconcile(self.task["id"])
        self.gateway.cancel.assert_not_called()
        self.assertEqual(
            self.store.get(self.task["id"])["executionStatus"], "succeeded"
        )

    def test_internal_absence_proof_closes_cancel_and_replays_after_restart(self):
        from agent_line.role_dispatch import UnstartedCancellationProof
        task_id=self.task['id']
        self.gateway.submit.side_effect=TimeoutError()
        self.dispatcher.submit(task_id);self.store.request_cancel(task_id)
        proof=UnstartedCancellationProof(task_id,'role','parent-original')
        self.gateway.observe.return_value={'identity':{},'unstartedCancellationProof':proof}
        result=self.dispatcher.reconcile(task_id)
        self.assertEqual(result['executionStatus'],'canceled')
        self.assertTrue(result['resourcesReleased'])
        self.assertEqual(result['executionIdentity'],{})
        reopened=Dispatcher(TaskStore(self.store.path),{'project':self.gateway})
        self.assertEqual(reopened.reconcile(task_id)['executionStatus'],'canceled')
        self.gateway.submit.assert_called_once()

    def test_json_absence_proof_cannot_close_cancellation(self):
        from agent_line.role_dispatch import UnstartedCancellationProof
        task_id=self.task['id'];self.gateway.submit.side_effect=TimeoutError()
        self.dispatcher.submit(task_id);self.store.request_cancel(task_id)
        forged={'identity':{},'unstartedCancellationProof':UnstartedCancellationProof(task_id,'role','parent').as_dict()}
        self.gateway.observe.return_value=forged;self.gateway.cancel.return_value=forged
        result=self.dispatcher.reconcile(task_id)
        self.assertEqual(result['executionStatus'],'cancel_requested')
        self.assertFalse(result['resourcesReleased'])

    def test_absence_proof_cannot_replace_existing_native_identity(self):
        from agent_line.role_dispatch import UnstartedCancellationProof
        from agent_line.store import Conflict
        task_id=self.task['id'];self.dispatcher.submit(task_id);self.store.request_cancel(task_id)
        self.gateway.observe.return_value={'identity':{},'unstartedCancellationProof':UnstartedCancellationProof(task_id,'role','parent')}
        with self.assertRaises(Conflict):self.dispatcher.reconcile(task_id)
        self.assertFalse(self.store.get(task_id)['resourcesReleased'])
