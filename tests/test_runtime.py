import json
import tempfile
import unittest
from unittest.mock import Mock, patch
from pathlib import Path
from fastapi.testclient import TestClient
from agent_line.runtime import load_app
from agent_line.store import TaskStore


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        TaskStore.initialize(self.root / "tasks.sqlite")
        self.config = self.root / "service.json"
        self.data = {
            "schemaVersion": 1,
            "database": "tasks.sqlite",
            "tokens": {"test-secret": ["project"]},
            "projects": {"project": {"workspace": str(self.root)}},
            "bindings": {"binding": {"projectId": "project", "roleAgentId": "role"}},
        }
        self.write()

    def write(self):
        self.config.write_text(json.dumps(self.data))
        self.config.chmod(0o600)

    def test_loads_existing_store_and_exposes_authenticated_readiness(self):
        with TestClient(load_app(self.config)) as client:
            self.assertEqual(client.get("/extension/v1/health").status_code, 401)
            response = client.get(
                "/extension/v1/health", headers={"Authorization": "Bearer test-secret"}
            )
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()["readyForExecution"])
            self.assertNotIn("test-secret", response.text)
            self.assertNotIn(str(self.root), response.text)

    def test_execution_factory_wires_reconciliation_without_enabling_admission(self):
        self.data["executionFile"] = "execution.json"
        self.write()
        gateway = Mock()
        gateway.role = "role"
        gateway.workspace = self.root
        with patch(
            "agent_line.execution_runtime.load_execution_gateways",
            return_value={("project", "binding"): gateway},
        ) as load:
            app = load_app(self.config)
            load.assert_called_once_with(
                self.root / "execution.json",
                projects=self.data["projects"],
                bindings=self.data["bindings"],
            )
            with TestClient(app) as client:
                self.assertTrue(app.state.execution_worker.thread.is_alive())
                self.assertFalse(app.state.execution_worker.loop.allow_submissions)
                response = client.get(
                    "/extension/v1/health",
                    headers={"Authorization": "Bearer test-secret"},
                )
                self.assertFalse(response.json()["readyForExecution"])
            self.assertFalse(app.state.execution_worker.thread.is_alive())
            gateway.close.assert_called_once()
            gateway.submit.assert_not_called()

    def test_never_initializes_missing_database(self):
        (self.root / "tasks.sqlite").unlink()
        with self.assertRaises(ValueError):
            load_app(self.config)
        self.assertFalse((self.root / "tasks.sqlite").exists())

    def test_rejects_world_readable_secret_configuration(self):
        self.config.chmod(0o644)
        with self.assertRaises(ValueError):
            load_app(self.config)

    def test_execution_cannot_be_enabled_by_unverified_boolean(self):
        self.data["controlled_execution"] = "confirmed"
        self.write()
        with self.assertRaises(ValueError):
            load_app(self.config)

    def test_readiness_configuration_errors_are_rejected_before_any_factory(self):
        for proof in (True, '', ' ', ' proof.json ', 'proof.json'):
            self.data['readinessFile'] = proof
            self.write()
            with self.subTest(proof=proof), self.assertRaisesRegex(ValueError, 'readiness requires'):
                load_app(self.config)

    def test_rejects_unknown_scope_or_binding(self):
        self.data["tokens"]["test-secret"] = ["other-project"]
        self.write()
        with self.assertRaises(ValueError):
            load_app(self.config)
