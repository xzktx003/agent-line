import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from agent_line.service import ServiceConfig, create_app
from agent_line.store import TaskStore


class PluginRemovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = TaskStore.initialize(self.root / "tasks.sqlite")
        self.payload = {
            "projectId": "project",
            "roleAgentId": "role",
            "workspace": str(self.root),
            "workflow": {
                "runId": "run",
                "nodeExecutionId": "activation",
                "attemptId": "attempt",
            },
            "input": {"task": "fix"},
        }
        config = ServiceConfig(
            tokens={"owner-token": {"project"}},
            projects={"project": {"workspace": str(self.root)}},
            bindings={"developer": {"projectId": "project", "roleAgentId": "role"}},
            controlled_execution="confirmed",
        )
        self.client = TestClient(create_app(self.store, config))
        self.addCleanup(self.client.close)
        self.headers = {"Authorization": "Bearer owner-token"}

    def leases(self):
        with self.store.connect() as db:
            return db.execute("SELECT task_id FROM workspace_leases ORDER BY task_id").fetchall()

    def test_hot_unload_is_refused_and_unknown_execution_keeps_its_lease(self):
        task, replay = self.store.register("inflight", self.payload)
        self.assertFalse(replay)
        self.assertTrue(self.store.claim(task["id"], self.root))
        self.assertTrue(self.store.mark_submission_uncertain(task["id"]))
        before = self.leases()
        self.assertEqual([row["task_id"] for row in before], [task["id"]])
        version = self.store.get(task["id"])["version"]

        refused = self.client.post(
            "/extension/v1/maintenance/plugin-removal", headers=self.headers
        )
        self.assertEqual(refused.status_code, 409, refused.text)
        body = refused.json()
        self.assertFalse(body["hotUpdateSupported"])
        self.assertFalse(body["hotUnloadAllowed"])
        self.assertFalse(body["uninstallPerformed"])
        self.assertFalse(body["leasesReleased"])
        self.assertFalse(body["coldRemovalAllowed"])
        self.assertEqual(body["reason"], "inflight_uninstall_forbidden")
        self.assertEqual(body["blockers"][0]["taskId"], task["id"])
        self.assertTrue(body["blockers"][0]["unknown"])
        self.assertTrue(body["blockers"][0]["ownsLease"])
        self.assertFalse(body["blockers"][0]["resourcesReleased"])
        self.assertIn("未知执行", " ".join(body["steps"]))
        self.assertEqual(self.leases(), before)
        self.assertEqual(self.store.get(task["id"])["version"], version)
        self.assertEqual(self.store.get(task["id"])["executionStatus"], "uncertain")

    def test_quiet_guarded_store_allows_only_cold_steps_and_removes_nothing(self):
        open_admission = self.client.get(
            "/extension/v1/maintenance/plugin-removal", headers=self.headers
        )
        self.assertEqual(open_admission.status_code, 200, open_admission.text)
        self.assertEqual(open_admission.json()["reason"], "admission_still_open")
        self.assertFalse(open_admission.json()["coldRemovalAllowed"])
        blocked = self.client.post(
            "/extension/v1/maintenance/plugin-removal", headers=self.headers
        )
        self.assertEqual(blocked.status_code, 409, blocked.text)

        owner = self.store.enter_maintenance()
        allowed = self.client.post(
            "/extension/v1/maintenance/plugin-removal", headers=self.headers
        )
        self.assertEqual(allowed.status_code, 200, allowed.text)
        body = allowed.json()
        self.assertEqual(body["reason"], "cold_removal_only")
        self.assertTrue(body["coldRemovalAllowed"])
        self.assertFalse(body["hotUnloadAllowed"])
        self.assertFalse(body["uninstallPerformed"])
        self.assertEqual(body["leaseCount"], 0)
        self.assertTrue(self.store.maintenance_status()["guarded"])
        self.store.leave_maintenance(owner)

    def test_plugin_removal_requires_service_credentials(self):
        self.assertEqual(
            self.client.get("/extension/v1/maintenance/plugin-removal").status_code, 401
        )
        self.assertEqual(
            self.client.post("/extension/v1/maintenance/plugin-removal").status_code, 401
        )


if __name__ == "__main__":
    unittest.main()
