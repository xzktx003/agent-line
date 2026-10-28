import unittest
import httpx
from agent_line.adapters.qwenpaw import QwenPawExecutionGateway, configuration_marker, query_failure_hint, submission_marker


class QwenGatewayTests(unittest.TestCase):
    def gateway(self, handler):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        return QwenPawExecutionGateway(
            "http://qwen.test", "host-secret", "service-secret", client=client
        )

    def test_dual_auth_and_bound_receipt(self):
        def handler(request):
            self.assertEqual(request.headers["Authorization"], "Bearer host-secret")
            self.assertEqual(
                request.headers["X-Agent-Line-Service-Token"], "service-secret"
            )
            return httpx.Response(
                202,
                json={
                    "taskId": "task-one",
                    "status": "accepted",
                    "identity": {"upstreamTaskId": "native"},
                },
            )

        result = self.gateway(handler).submit(
            task_id="task-one", role_session_id="session", instruction="do work"
        )
        self.assertEqual(result["identity"]["upstreamTaskId"], "native")

    def test_wrong_task_receipt_does_not_change_task(self):
        gateway = self.gateway(
            lambda request: httpx.Response(
                200,
                json={
                    "taskId": "other",
                    "status": "succeeded",
                    "terminalConfirmed": True,
                },
            )
        )
        self.assertEqual(gateway.observe("task-one"), {"status": "unknown"})

    def test_redirect_never_sends_credentials_to_another_host(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(307, headers={"location": "http://another.test"})

        result = self.gateway(handler).submit(
            task_id="task-one", role_session_id="session", instruction="do work"
        )
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(len(requests), 1)

    def test_only_exact_model_marker_means_model_missing(self):
        self.assertIsNone(configuration_marker(None))
        self.assertIsNone(configuration_marker({"X-Agent-Line-Configuration": "model_unavailable "}))
        self.assertEqual(configuration_marker({"X-Agent-Line-Configuration": "model_unavailable"}), "model_unavailable")
        self.assertEqual(query_failure_hint(401)["pollHint"], "authentication")
        self.assertEqual(query_failure_hint(403)["pollHint"], "authentication")
        self.assertEqual(query_failure_hint(409)["pollHint"], "unknown")
        self.assertEqual(query_failure_hint(409, configuration="model_unavailable")["pollHint"], "model_missing")
        self.assertIsNone(submission_marker(None))
        self.assertIsNone(submission_marker({"X-Agent-Line-Submission": "rejected_unaccepted "}))
        self.assertEqual(submission_marker({"X-Agent-Line-Submission": "rejected_unaccepted"}), "rejected_unaccepted")
        self.assertEqual(query_failure_hint(409, submission="rejected_unaccepted")["pollHint"], "submission_rejected")
        self.assertEqual(query_failure_hint(422, submission="rejected_unaccepted")["pollHint"], "submission_rejected")
        self.assertEqual(query_failure_hint(500, submission="rejected_unaccepted")["pollHint"], "unavailable")
        self.assertEqual(query_failure_hint(None, submission="rejected_unaccepted")["pollHint"], "unavailable")
        self.assertEqual(query_failure_hint(401, submission="rejected_unaccepted")["pollHint"], "authentication")
        self.assertEqual(
            query_failure_hint(409, configuration="model_unavailable", submission="rejected_unaccepted")["pollHint"],
            "model_missing")
        self.assertNotIn("pollAfterSeconds", query_failure_hint(409, configuration="model_unavailable"))

        missing = self.gateway(lambda request: httpx.Response(
            409, headers={"X-Agent-Line-Configuration": "model_unavailable"}))
        self.assertEqual(missing.observe("task-one")["pollHint"], "model_missing")
        authenticated = self.gateway(lambda request: httpx.Response(
            401, headers={"X-Agent-Line-Configuration": "unavailable"}))
        self.assertEqual(authenticated.observe("task-one")["pollHint"], "authentication")
        accepted = self.gateway(lambda request: httpx.Response(
            202,
            headers={"X-Agent-Line-Configuration": "model_unavailable"},
            json={"taskId": "task-one", "status": "accepted", "identity": {"upstreamTaskId": "native"}},
        ))
        self.assertNotIn("pollHint", accepted.observe("task-one"))
        rejected = self.gateway(lambda request: httpx.Response(
            409, headers={"X-Agent-Line-Submission": "rejected_unaccepted"}))
        refusal = rejected.observe("task-one")
        self.assertEqual(refusal["status"], "rejected")
        self.assertEqual(refusal["rejectionReason"], "submission_rejected_unaccepted")
        self.assertEqual(refusal["identity"], {})
        self.assertIs(refusal["accepted"], False)
        self.assertIs(refusal["terminalConfirmed"], False)
        unavailable = self.gateway(lambda request: httpx.Response(
            503, headers={"X-Agent-Line-Submission": "rejected_unaccepted"}))
        self.assertEqual(unavailable.observe("task-one")["status"], "unknown")
        self.assertEqual(unavailable.observe("task-one")["pollHint"], "unavailable")
