import copy
import tempfile
import unittest
from pathlib import Path
from agent_line.store import TaskStore, Conflict


class ResourceEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "tasks.sqlite"
        self.store = TaskStore.initialize(self.path)
        self.task, _ = self.store.register(
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
        self.id = self.task["id"]
        self.store.claim(self.id, "/work")
        self.identity = {"codexThreadId": "thread", "codexTurnId": "turn"}
        self.store.accept(self.id, self.identity)
        self.terminal = [
            {"kind": "execution_terminal", "executionId": "turn", "confirmed": True}
        ]
        self.store.finish(self.id, "succeeded", {}, self.terminal)
        self.receipt = {
            "source": "docker-inspect-before-remove",
            "owner": "owner",
            "containerId": "container",
            "running": False,
            "pid": 0,
            "status": "exited",
            "finishedAt": "2026-10-21T10:00:00Z",
            "workspace": "/work",
            "identities": [self.identity],
        }

    def test_exit_receipt_survives_restart_and_terminal_reconciliation(self):
        self.store.confirm_resources_released(self.id, "turn", self.receipt)
        recovered = TaskStore(self.path)
        self.assertFalse(recovered.finish(self.id, "succeeded", {}, self.terminal))
        self.assertFalse(
            recovered.confirm_resources_released(self.id, "turn", self.receipt)
        )
        evidence = recovered.get(self.id)["evidence"]
        self.assertEqual(len(evidence), 2)
        self.assertEqual(evidence[1]["receipt"], self.receipt)
        self.assertEqual(evidence[1]["executionIdentity"], self.identity)

    def test_unrelated_or_missing_exit_cannot_release(self):
        for key, value in [
            ("workspace", "/other"),
            ("identities", []),
            ("running", True),
            ("pid", 12),
            ("source", "native-terminal"),
            ("finishedAt", ""),
        ]:
            with self.subTest(key=key):
                receipt = {**self.receipt, key: value}
                with self.assertRaises(Conflict):
                    self.store.confirm_resources_released(self.id, "turn", receipt)
                self.assertFalse(self.store.get(self.id)["resourcesReleased"])

    def test_existing_proof_cannot_be_replaced(self):
        self.store.confirm_resources_released(self.id, "turn", self.receipt)
        changed = copy.deepcopy(self.receipt)
        changed["containerId"] = "different"
        with self.assertRaises(Conflict):
            self.store.confirm_resources_released(self.id, "turn", changed)
        self.assertEqual(
            self.store.get(self.id)["evidence"][1]["receipt"], self.receipt
        )
