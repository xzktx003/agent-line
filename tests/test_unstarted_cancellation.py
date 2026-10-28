import tempfile
import unittest
from pathlib import Path
from agent_line.store import TaskStore, Conflict


class UnstartedCancellationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "tasks.sqlite"
        self.store = TaskStore.initialize(self.path)
        task, _ = self.store.register(
            "key",
            {
                "projectId": "project",
                "roleAgentId": "role",
                "workspace": "/work",
                "workflow": {
                    "runId": "run",
                    "nodeExecutionId": "node",
                    "attemptId": "1",
                },
                "input": {},
            },
        )
        self.task = task["id"]
        self.store.claim(self.task, "/work")
        self.store.mark_submission_uncertain(self.task)
        self.store.request_cancel(self.task)
        self.proof = {
            "source": "revoked_delegation_and_finished_parent",
            "taskId": self.task,
            "roleAgentId": "role",
            "roleSessionId": "agent-line-" + self.task,
            "parentTaskId": "parent-native",
            "parentFinished": True,
            "delegationRevoked": True,
            "executionClaimed": False,
        }

    def test_positive_absence_releases_without_inventing_native_identity(self):
        self.assertTrue(
            self.store.resolve_unstarted_cancellation(self.task, self.proof)
        )
        reopened = TaskStore(self.path)
        self.assertFalse(reopened.resolve_unstarted_cancellation(self.task, self.proof))
        task = reopened.get(self.task)
        self.assertEqual(task["executionStatus"], "canceled")
        self.assertTrue(task["resourcesReleased"])
        self.assertEqual(task["executionIdentity"], {})
        reopened.release_workspace("/work", self.task)

    def test_missing_or_mismatched_absence_is_not_resource_exit(self):
        for key, value in [
            ("parentFinished", False),
            ("delegationRevoked", False),
            ("executionClaimed", True),
            ("roleAgentId", "other"),
            ("roleSessionId", "other"),
            ("taskId", "other"),
            ("parentTaskId", ""),
        ]:
            with self.subTest(key=key), self.assertRaises(Conflict):
                self.store.resolve_unstarted_cancellation(
                    self.task, {**self.proof, key: value}
                )
        self.assertFalse(self.store.get(self.task)["resourcesReleased"])

    def test_existing_native_identity_cannot_be_erased_by_absence_claim(self):
        self.store.accept(self.task, {"upstreamTaskId": "native"})
        with self.assertRaises(Conflict):
            self.store.resolve_unstarted_cancellation(self.task, self.proof)
        self.assertEqual(
            self.store.get(self.task)["executionIdentity"], {"upstreamTaskId": "native"}
        )
