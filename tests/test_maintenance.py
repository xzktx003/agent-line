import tempfile
import unittest
from pathlib import Path
from agent_line.store import TaskStore, Conflict


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = TaskStore.initialize(Path(self.tmp.name) / "tasks.sqlite")
        self.payload = {
            "projectId": "project",
            "roleAgentId": "role",
            "workspace": self.tmp.name,
            "workflow": {"runId": "run", "nodeExecutionId": "node", "attemptId": "1"},
        }

    def test_persistent_guard_blocks_new_registration_but_retains_idempotent_read(self):
        task, _ = self.store.register("one", self.payload)
        token = self.store.enter_maintenance()
        reopened = TaskStore(self.store.path)
        self.assertEqual(reopened.register("one", self.payload)[0]["id"], task["id"])
        with self.assertRaises(Conflict):
            reopened.register(
                "two",
                {
                    **self.payload,
                    "workflow": {**self.payload["workflow"], "attemptId": "2"},
                },
            )
        with self.assertRaises(Conflict):
            reopened.claim(task["id"], self.tmp.name)
        self.assertTrue(reopened.maintenance_status()["guarded"])
        reopened.leave_maintenance(token)
        self.assertFalse(reopened.maintenance_status()["guarded"])

    def test_only_exact_owner_can_remove_maintenance_guard(self):
        token = self.store.enter_maintenance()
        with self.assertRaises(Conflict):
            self.store.enter_maintenance()
        with self.assertRaises(Conflict):
            self.store.leave_maintenance("another-owner")
        self.assertTrue(self.store.maintenance_status()["guarded"])
        self.store.leave_maintenance(token)

    def test_restoration_guard_cannot_be_cleared_as_ordinary_maintenance(self):
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO admission_guard VALUES (1,?)",
                ("restore_requires_reconciliation",),
            )
        with self.assertRaises(Conflict):
            self.store.leave_maintenance("anything")
        with self.assertRaises(Conflict):
            self.store.enter_maintenance()

    def test_active_resources_prevent_quiescence_even_after_terminal_status(self):
        task, _ = self.store.register("one", self.payload)
        self.store.claim(task["id"], self.tmp.name)
        self.store.accept(task["id"], {"upstreamTaskId": "native"})
        self.store.enter_maintenance()
        self.assertFalse(self.store.maintenance_status()["executionQuiescent"])
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
        self.assertFalse(self.store.maintenance_status()["executionQuiescent"])
        self.store.confirm_resources_released(task["id"], "native")
        self.assertTrue(self.store.maintenance_status()["executionQuiescent"])
