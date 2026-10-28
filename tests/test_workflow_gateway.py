import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
import httpx
from agent_line.adapters.sim import SimClient
from agent_line.sim_journal import SimJournal
from agent_line.service import create_app, ServiceConfig
from agent_line.store import TaskStore
from agent_line.workflows import WorkflowGateway


class WorkflowGatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.requests = []

        def handler(request):
            self.requests.append(request)
            return httpx.Response(202, json={"success": True})

        transport = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(transport.close)
        self.gateway = WorkflowGateway(
            SimClient("http://sim.test", "secret", client=transport),
            SimJournal.initialize(root / "sim.sqlite"),
            {"flow": {"projectId": "project", "workflowId": "native-flow"}},
            installation_id="fixture-installation",
        )
        config = ServiceConfig(
            tokens={"key": {"project"}, "other": {"another"}},
            projects={"project": {"workspace": str(root)}},
            bindings={},
        )
        self.app = create_app(
            TaskStore.initialize(root / "tasks.sqlite"), config, workflows=self.gateway
        )
        self.client = TestClient(self.app)
        self.headers = {"Authorization": "Bearer key"}

    def test_start_repeated_request_sends_one_native_mutation(self):
        body = {"idempotencyKey": "trigger-1", "input": {"value": 1}}
        first = self.client.post(
            "/extension/v1/workflows/flow/runs", json=body, headers=self.headers
        )
        self.assertEqual(first.status_code, 202, first.text)
        again = self.client.post(
            "/extension/v1/workflows/flow/runs", json=body, headers=self.headers
        )
        self.assertEqual(first.json()["runId"], again.json()["runId"])
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(first.json()["verifiedComplete"])

    def test_scope_and_auth_prevent_mutation(self):
        url = "/extension/v1/workflows/flow/runs"
        body = {"idempotencyKey": "trigger-1", "input": {}}
        self.assertEqual(self.client.post(url, json=body).status_code, 401)
        self.assertEqual(
            self.client.post(
                url, json=body, headers={"Authorization": "Bearer other"}
            ).status_code,
            404,
        )
        self.assertEqual(len(self.requests), 0)

    def test_changed_input_same_trigger_is_conflict(self):
        url = "/extension/v1/workflows/flow/runs"
        self.client.post(
            url, json={"idempotencyKey": "trigger", "input": {}}, headers=self.headers
        )
        result = self.client.post(
            url,
            json={"idempotencyKey": "trigger", "input": {"changed": True}},
            headers=self.headers,
        )
        self.assertEqual(result.status_code, 409)
        self.assertEqual(len(self.requests), 1)

    def test_unknown_run_cannot_be_resumed(self):
        response = self.client.post(
            "/extension/v1/workflows/flow/runs/arbitrary/resume",
            json={"contextId": "context", "input": {}},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(len(self.requests), 0)

    def test_native_workflow_cannot_be_shared_across_project_scopes(self):
        with self.assertRaises(ValueError):
            WorkflowGateway(
                self.gateway.client,
                self.gateway.journal,
                {
                    "one": {"projectId": "first", "workflowId": "shared"},
                    "two": {"projectId": "second", "workflowId": "shared"},
                },
                installation_id="fixture",
            )

    def test_catalog_is_scoped_and_hides_native_configuration(self):
        response = self.client.get("/extension/v1/workflows", headers=self.headers)
        self.assertEqual(
            response.json(),
            {"workflows": [{"bindingId": "flow", "projectId": "project", "workflowId": "native-flow"}]},
        )
        hidden = self.client.get(
            "/extension/v1/workflows", headers={"Authorization": "Bearer other"}
        )
        self.assertEqual(hidden.json(), {"workflows": []})
        self.assertEqual(self.client.get("/extension/v1/workflows").status_code, 401)
