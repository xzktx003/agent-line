import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
from agent_line.store import TaskStore, Conflict


class TaskStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "tasks.sqlite"
        self.store = TaskStore.initialize(self.path)
        self.payload = {
            "projectId": "project",
            "roleAgentId": "role",
            "workspace": "/workspace",
            "workflow": {
                "runId": "run",
                "nodeExecutionId": "activation",
                "attemptId": "attempt",
            },
            "input": {"task": "fix"},
        }

    def test_concurrent_retries_register_once(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(lambda _: self.store.register("same", self.payload), range(16))
            )
        self.assertEqual(len({row["id"] for row, reused in results}), 1)
        self.assertEqual(sum(not reused for _, reused in results), 1)

    def test_input_conflict_does_not_overwrite(self):
        original, _ = self.store.register("same", self.payload)
        with self.assertRaises(Conflict):
            self.store.register("same", {**self.payload, "roleAgentId": "different"})
        self.assertEqual(self.store.get(original["id"])["payload"], self.payload)

    def test_attempt_cannot_be_resubmitted_under_another_key(self):
        self.store.register("same", self.payload)
        with self.assertRaises(Conflict):
            self.store.register("different", self.payload)

    def test_claim_is_exclusive_and_restart_preserves_uncertainty(self):
        task, _ = self.store.register("same", self.payload)
        with ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(
                pool.map(lambda _: self.store.claim(task["id"], "/workspace"), range(8))
            )
        self.assertEqual(sum(claims), 1)
        recovered = TaskStore(self.path)
        self.assertFalse(recovered.claim(task["id"], "/workspace"))
        self.assertEqual(recovered.get(task["id"])["executionStatus"], "submitting")
        # Explicit recovery after owning worker is known stopped, never automatic resend.
        recovered.mark_submission_uncertain(task["id"])
        self.assertFalse(recovered.claim(task["id"], "/workspace"))
        self.assertEqual(
            recovered.get(task["id"])["upstreamSubmissionStatus"], "uncertain"
        )

    def test_workspace_lock_survives_restart_and_requires_owner(self):
        a, _ = self.store.register("a", self.payload)
        b, _ = self.store.register(
            "b",
            {
                **self.payload,
                "workflow": {**self.payload["workflow"], "attemptId": "second"},
            },
        )
        self.assertTrue(self.store.acquire_workspace("/workspace", a["id"]))
        reboot = TaskStore(self.path)
        self.assertFalse(reboot.acquire_workspace("/workspace", b["id"]))
        with self.assertRaises(Conflict):
            reboot.release_workspace("/workspace", b["id"])
        with self.assertRaises(Conflict):
            reboot.release_workspace("/workspace", a["id"])

    def test_unregistered_task_cannot_lock(self):
        with self.assertRaises(Conflict):
            self.store.acquire_workspace("/workspace", "missing")

    def test_missing_database_is_not_silently_recreated(self):
        self.path.unlink()
        with self.assertRaises(ValueError):
            TaskStore(self.path)
        self.assertFalse(self.path.exists())

    def test_parent_and_child_workspace_writes_conflict(self):
        a, _ = self.store.register("a", self.payload)
        b, _ = self.store.register(
            "b",
            {
                **self.payload,
                "workflow": {**self.payload["workflow"], "attemptId": "second"},
            },
        )
        self.assertTrue(self.store.acquire_workspace("/workspace/project", a["id"]))
        self.assertFalse(self.store.acquire_workspace("/workspace", b["id"]))
        self.assertFalse(
            self.store.acquire_workspace("/workspace/project/subdir", b["id"])
        )

    def test_success_needs_execution_identity_and_verified_evidence(self):
        task, _ = self.store.register("a", self.payload)
        self.store.claim(task["id"], "/workspace")
        self.store.accept(
            task["id"], {"codexThreadId": "thread-1", "codexTurnId": "turn-1"}
        )
        with self.assertRaises(Conflict):
            self.store.finish(task["id"], "succeeded", {"testsPassed": True}, [])
        self.assertEqual(self.store.get(task["id"])["executionStatus"], "accepted")

    def test_memory_failure_preserves_result_and_does_not_reopen_execution(self):
        task, _ = self.store.register("a", self.payload)
        self.store.claim(task["id"], "/workspace")
        self.store.accept(
            task["id"], {"codexThreadId": "thread-1", "codexTurnId": "turn-1"}
        )
        self.store.finish(
            task["id"],
            "succeeded",
            {"summary": "done"},
            [
                {
                    "kind": "execution_terminal",
                    "executionId": "turn-1",
                    "confirmed": True,
                }
            ],
        )
        self.store.set_memory_status(task["id"], "failed")
        row = TaskStore(self.path).get(task["id"])
        self.assertEqual(row["executionStatus"], "succeeded")
        self.assertEqual(row["memoryStatus"], "failed")
        self.assertEqual(row["output"], {"summary": "done"})
        self.assertFalse(self.store.claim(task["id"], "/workspace"))

    def test_late_accept_cannot_replace_native_identity(self):
        task, _ = self.store.register("a", self.payload)
        self.store.claim(task["id"], "/workspace")
        identity = {"upstreamTaskId": "remote-1"}
        self.store.accept(task["id"], identity)
        self.store.accept(task["id"], identity)
        with self.assertRaises(Conflict):
            self.store.accept(task["id"], {"upstreamTaskId": "remote-2"})
        self.assertEqual(self.store.get(task["id"])["executionIdentity"], identity)

    def test_cancel_request_does_not_release_active_writer(self):
        task, _ = self.store.register("a", self.payload)
        self.store.acquire_workspace("/workspace", task["id"])
        self.store.claim(task["id"], "/workspace")
        self.store.accept(task["id"], {"upstreamTaskId": "remote-1"})
        self.store.request_cancel(task["id"])
        self.assertEqual(
            self.store.get(task["id"])["executionStatus"], "cancel_requested"
        )
        with self.assertRaises(Conflict):
            self.store.release_workspace("/workspace", task["id"])

    def test_event_cursor_replays_only_later_events(self):
        task, _ = self.store.register("a", self.payload)
        first = self.store.events(task["id"])
        self.store.claim(task["id"], "/workspace")
        rest = TaskStore(self.path).events(task["id"], after=first[-1]["seq"])
        self.assertEqual([x["kind"] for x in rest], ["submission_intent"])

    def test_workspace_contention_does_not_record_submission(self):
        a, _ = self.store.register("a", self.payload)
        b, _ = self.store.register(
            "b",
            {
                **self.payload,
                "workflow": {**self.payload["workflow"], "attemptId": "second"},
            },
        )
        self.assertTrue(self.store.claim(a["id"], "/workspace"))
        self.assertFalse(self.store.claim(b["id"], "/workspace/subdir"))
        self.assertEqual(self.store.get(b["id"])["executionStatus"], "registered")
        self.assertEqual(
            [e["kind"] for e in self.store.events(b["id"])], ["registered"]
        )

    def test_live_store_does_not_recreate_removed_database(self):
        self.path.unlink()
        import sqlite3

        with self.assertRaises(sqlite3.OperationalError):
            self.store.register("a", self.payload)
        self.assertFalse(self.path.exists())

    def test_completed_task_waits_for_resource_release_evidence(self):
        task, _ = self.store.register("a", self.payload)
        self.store.claim(task["id"], "/workspace")
        self.store.accept(task["id"], {"upstreamTaskId": "remote-1"})
        terminal = [
            {"kind": "execution_terminal", "executionId": "remote-1", "confirmed": True}
        ]
        self.store.finish(task["id"], "succeeded", {}, terminal)
        with self.assertRaises(Conflict):
            self.store.release_workspace("/workspace", task["id"])
        with self.assertRaises(Conflict):
            self.store.confirm_resources_released(task["id"], "another-run")
        self.store.confirm_resources_released(task["id"], "remote-1")
        self.assertFalse(self.store.release_workspace("/workspace", task["id"]))
        self.assertEqual(self.store.get(task["id"])["verificationStatus"], "pending")

    def test_verification_is_separate_and_does_not_break_terminal_replay(self):
        self.payload["workspace"] = "/tmp/verification-fixture"
        task, _ = self.store.register("verified", self.payload)
        self.store.claim(task["id"], "/tmp/verification-fixture")
        identity = {"codexThreadId": "thread", "codexTurnId": "turn"}
        self.store.accept(task["id"], identity)
        terminal = [
            {"kind": "execution_terminal", "executionId": "turn", "confirmed": True}
        ]
        self.store.finish(task["id"], "succeeded", {"result": "done"}, terminal)
        receipt = {
            "kind": "verification",
            "taskId": task["id"],
            "executionIdentity": identity,
            "status": "passed",
            "checks": [{"status": "passed", "path": "result.txt"}],
        }
        with self.assertRaises(Conflict):
            self.store.record_verification(task["id"], receipt)
        self.store.confirm_resources_released(task["id"], "turn")
        self.assertTrue(self.store.record_verification(task["id"], receipt))
        self.assertFalse(self.store.record_verification(task["id"], receipt))
        self.assertFalse(
            self.store.finish(task["id"], "succeeded", {"result": "done"}, terminal)
        )
        result = self.store.get(task["id"])
        self.assertEqual(result["verificationStatus"], "passed")
        self.assertEqual(result["evidence"][-1], receipt)

    def test_verification_rejects_unbound_or_empty_checks(self):
        self.payload["workspace"] = "/tmp/verification-fixture"
        task, _ = self.store.register("bad-verify", self.payload)
        self.store.claim(task["id"], "/tmp/verification-fixture")
        identity = {"upstreamTaskId": "native"}
        self.store.accept(task["id"], identity)
        self.store.finish(
            task["id"],
            "succeeded",
            {},
            [
                {
                    "kind": "execution_terminal",
                    "executionId": "native",
                    "confirmed": True,
                }
            ],
        )
        self.store.confirm_resources_released(task["id"], "native")
        receipt = {
            "kind": "verification",
            "taskId": task["id"],
            "executionIdentity": identity,
            "status": "passed",
            "checks": [],
        }
        with self.assertRaises(ValueError):
            self.store.record_verification(task["id"], receipt)
        receipt["checks"] = [{"status": "failed"}]
        with self.assertRaises(ValueError):
            self.store.record_verification(task["id"], receipt)
        receipt.update(status="failed", executionIdentity={"upstreamTaskId": "other"})
        with self.assertRaises(Conflict):
            self.store.record_verification(task["id"], receipt)

    def test_resource_quarantine_revokes_release_and_blocks_new_work(self):
        self.payload["workspace"] = "/tmp/quarantine-fixture"
        task, _ = self.store.register("quarantine", self.payload)
        self.store.claim(task["id"], "/tmp/quarantine-fixture")
        self.store.accept(task["id"], {"upstreamTaskId": "native"})
        self.store.finish(
            task["id"],
            "canceled",
            {},
            [
                {
                    "kind": "execution_terminal",
                    "executionId": "native",
                    "confirmed": True,
                }
            ],
        )
        self.store.confirm_resources_released(task["id"], "native")
        self.store.quarantine_resources(task["id"])
        current = self.store.get(task["id"])
        self.assertEqual(current["executionStatus"], "canceled")
        self.assertFalse(current["resourcesReleased"])
        self.assertTrue(self.store.maintenance_status()["guarded"])
        with self.assertRaises(Conflict):
            self.store.confirm_resources_released(task["id"], "native")
