import tempfile
import unittest
import httpx
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

from agent_line.adapters.sim import SimClient, SubmissionUncertain
from agent_line.sim_journal import SimJournal
from agent_line.store import Conflict


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "operations.sqlite"
        self.journal = SimJournal.initialize(self.path)
        self.client = Mock()
        self.client.execute.return_value = {
            "transportStatus": "accepted",
            "verifiedComplete": False,
        }

    def test_parallel_same_run_submits_once(self):
        def submit(_):
            return self.journal.execute(self.client, "workflow", "run", {"x": 1})

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(submit, range(16)))
        self.assertEqual(self.client.execute.call_count, 1)
        self.assertTrue(all(r["runId"] == "run" for r in results))

    def test_lost_response_restart_never_reexecutes(self):
        self.client.execute.side_effect = SubmissionUncertain("run")
        result = self.journal.execute(self.client, "workflow", "run", {})
        self.assertEqual(result["status"], "uncertain")
        again = SimJournal(self.path).execute(self.client, "workflow", "run", {})
        self.assertEqual(again["status"], "uncertain")
        self.assertEqual(self.client.execute.call_count, 1)

    def test_crash_after_intent_preserves_identity(self):
        self.client.execute.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.journal.execute(self.client, "workflow", "run", {})
        result = SimJournal(self.path).execute(self.client, "workflow", "run", {})
        self.assertEqual(result["status"], "intent")
        self.assertEqual(self.client.execute.call_count, 1)

    def test_changed_input_cannot_reuse_run(self):
        self.journal.execute(self.client, "workflow", "run", {"x": 1})
        with self.assertRaises(Conflict):
            self.journal.execute(self.client, "workflow", "run", {"x": 2})
        self.assertEqual(self.client.execute.call_count, 1)

    def test_resume_identity_survives_restart_and_new_context_is_distinct(self):
        self.client.resume.return_value = {"transportStatus": "accepted"}
        self.journal.resume(self.client, "workflow", "run", "context1", {})
        SimJournal(self.path).resume(self.client, "workflow", "run", "context1", {})
        self.journal.resume(self.client, "workflow", "run", "context2", {})
        self.assertEqual(self.client.resume.call_count, 2)

    def test_missing_database_is_not_recreated(self):
        self.path.unlink()
        with self.assertRaises(Exception):
            self.journal.execute(self.client, "workflow", "run", {})
        self.assertFalse(self.path.exists())
        self.client.execute.assert_not_called()

    def test_real_client_lost_http_receipt_is_persisted_without_replay(self):
        requests = []

        def handler(request):
            requests.append(request)
            raise httpx.ReadTimeout("lost reply", request=request)

        with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
            client = SimClient("http://sim.test", "test-key", client=transport)
            self.journal.execute(client, "workflow", "run", {"question": "hello"})
            result = SimJournal(self.path).execute(
                client, "workflow", "run", {"question": "hello"}
            )
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].headers["X-Run-Id"], "run")
