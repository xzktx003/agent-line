import sys
import unittest
from pathlib import Path
import httpx

sys.path.insert(0, str(Path(__file__).parents[1]))
from agent_line.adapters.sim import SimClient, SubmissionUncertain, UpstreamFailure


class SimClientTests(unittest.TestCase):
    def client(self, handler):
        client = httpx.Client(
            transport=httpx.MockTransport(handler), base_url="http://sim.test"
        )
        self.addCleanup(client.close)
        return SimClient("http://sim.test", "fixture-key", client=client)

    def test_start_timeout_never_retries(self):
        seen = []

        def handler(request):
            seen.append(request)
            raise httpx.ReadTimeout("timeout", request=request)

        api = self.client(handler)
        with self.assertRaises(SubmissionUncertain) as ctx:
            api.execute("workflow", "run-fixed", {"question": "task"})
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].headers["X-Run-Id"], "run-fixed")
        self.assertEqual(ctx.exception.run_id, "run-fixed")

    def test_conflict_preserves_run_identity_without_fallback(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(409, json={"error": {"code": "RUN_ID_CONFLICT"}})

        api = self.client(handler)
        with self.assertRaises(SubmissionUncertain):
            api.execute("workflow", "reserved", {})
        self.assertEqual(len(seen), 1)

    def test_missing_query_is_unknown_not_permission_to_resubmit(self):
        api = self.client(
            lambda request: httpx.Response(404, json={"error": "missing"})
        )
        result = api.observe("workflow", "run-fixed")
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(result["safeToResubmit"])

    def test_accepted_response_does_not_mean_completed(self):
        api = self.client(
            lambda request: httpx.Response(
                202, json={"success": True, "data": {"runId": "run-fixed"}}
            )
        )
        result = api.execute("workflow", "run-fixed", {})
        self.assertEqual(result["transportStatus"], "accepted")
        self.assertFalse(result["verifiedComplete"])

    def test_auth_error_is_actionable_without_key_or_response_leak(self):
        api = self.client(
            lambda request: httpx.Response(401, text="sensitive remote details")
        )
        with self.assertRaises(UpstreamFailure) as ctx:
            api.execute("workflow", "run-fixed", {})
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertNotIn("sensitive", str(ctx.exception))
        self.assertNotIn("fixture-key", str(ctx.exception))

    def test_resume_timeout_preserves_context_identity(self):
        seen = []

        def handler(request):
            seen.append(request)
            raise httpx.ReadTimeout("timeout", request=request)

        api = self.client(handler)
        with self.assertRaises(SubmissionUncertain) as ctx:
            api.resume("workflow", "run-fixed", "pause-fixed", {"result": True})
        self.assertEqual(ctx.exception.context_id, "pause-fixed")
        self.assertEqual(len(seen), 1)

    def test_redirect_is_not_followed_with_secret(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(
                307, headers={"location": "http://other.test/collect"}
            )

        api = self.client(handler)
        with self.assertRaises(SubmissionUncertain):
            api.execute("workflow", "run-fixed", {})
        self.assertEqual(len(seen), 1)

    def test_error_envelope_cannot_be_accepted_even_with_http_200(self):
        api = self.client(
            lambda request: httpx.Response(
                200, json={"success": False, "error": "failed"}
            )
        )
        with self.assertRaises(SubmissionUncertain):
            api.execute("workflow", "run-fixed", {})

    def test_mismatched_run_id_is_unknown(self):
        api = self.client(
            lambda request: httpx.Response(
                202, json={"data": {"runId": "different-run"}}
            )
        )
        with self.assertRaises(SubmissionUncertain):
            api.execute("workflow", "run-fixed", {})

    def test_observation_requests_final_output(self):
        def handler(request):
            self.assertEqual(request.url.params.get("includeOutput"), "true")
            return httpx.Response(
                200,
                json={
                    "data": {
                        "runId": "run",
                        "status": "completed",
                        "output": {"ok": True},
                    }
                },
            )

        result = self.client(handler).observe("workflow", "run")
        self.assertEqual(result["response"]["data"]["output"], {"ok": True})

    def test_resume_child_id_preserves_original_identity(self):
        api=self.client(lambda request:httpx.Response(200,headers={'X-Run-Id':'child-run'},json={'data':{'workflowId':'workflow','runId':'child-run','status':'completed'}}))
        result=api.resume('workflow','parent-run','pause',{'approval':True})
        self.assertEqual(result['runId'],'parent-run')
        self.assertEqual(result['contextId'],'pause')
        self.assertEqual(result['workflowId'],'workflow')
        self.assertEqual(result['resumedRunId'],'child-run')
        self.assertEqual(result['transportStatus'],'accepted')
        self.assertFalse(result['verifiedComplete'])

    def test_resume_async_child_status_url_validated_without_following_it(self):
        seen=[]
        def handler(request):
            seen.append(request)
            return httpx.Response(202,headers={'X-Run-Id':'child-run'},json={'data':{'runId':'child-run','statusUrl':'http://public-sim/api/v2/workflows/workflow/runs/child-run'}})
        result=self.client(handler).resume('workflow','parent-run','pause',{})
        self.assertEqual(result['resumedRunId'],'child-run');self.assertEqual(len(seen),1)

    def test_resume_contradictory_or_malformed_receipt_remains_unknown(self):
        cases=[({},'child'),({'runId':'child'},'other'),({'runId':'../escape'},'../escape'),
               ({'runId':'child','workflowId':'wrong'},'child'),
               ({'runId':'child','contextId':'wrong'},'child'),
               ({'runId':'child','statusUrl':'http://sim/api/v2/workflows/wrong/runs/child'},'child'),
               ({'runId':'child'},None)]
        for data,header in cases:
            with self.subTest(data=data,header=header):
                api=self.client(lambda request:httpx.Response(200,headers={'X-Run-Id':header} if header else {},json={'data':data}))
                with self.assertRaises(SubmissionUncertain) as raised:api.resume('workflow','parent','pause',{})
                self.assertEqual(raised.exception.run_id,'parent');self.assertEqual(raised.exception.context_id,'pause')

    def test_execute_still_rejects_child_or_contradictory_header(self):
        for data,header in [({'runId':'child'},'child'),({'runId':'parent'},'child'),({},'parent')]:
            api=self.client(lambda request:httpx.Response(202,headers={'X-Run-Id':header},json={'data':data}))
            with self.assertRaises(SubmissionUncertain):api.execute('workflow','parent',{})
