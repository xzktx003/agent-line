import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from agent_line.store import TaskStore
from agent_line.dispatcher import Dispatcher, DispatchLoop
from agent_line.polling import PollCadence


class DispatchLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = TaskStore.initialize(self.root / "tasks.sqlite")
        self.gateway = Mock()
        self.gateway.submit.return_value = {
            "status": "accepted",
            "identity": {"upstreamTaskId": "native"},
        }
        self.gateway.observe.return_value = {
            "status": "accepted",
            "identity": {"upstreamTaskId": "native"},
        }
        self.loop = DispatchLoop(
            Dispatcher(self.store, {"project": self.gateway}), batch_size=1
        )

    def register(self, key, workspace=None, *, role="role"):
        return self.store.register(
            key,
            {
                "projectId": "project",
                "roleAgentId": role,
                "workspace": str(workspace or self.root),
                "input": {},
                "workflow": {"runId": key, "nodeExecutionId": "node", "attemptId": "1"},
            },
        )[0]

    def test_loop_submits_registered_and_next_tick_only_observes(self):
        task = self.register("one")
        for _ in range(3):
            self.loop.tick()
        self.assertEqual(self.gateway.submit.call_count, 1)
        self.assertGreaterEqual(self.gateway.observe.call_count, 1)
        self.assertEqual(self.store.get(task["id"])["executionStatus"], "accepted")

    def test_reconcile_only_mode_observes_existing_without_claiming_queue(self):
        active = self.register("active")
        self.loop.dispatcher.submit(active["id"])
        queued = self.register("queued", self.root / "queued")
        loop = DispatchLoop(self.loop.dispatcher, allow_submissions=False)
        loop.tick()
        self.assertEqual(self.gateway.submit.call_count, 1)
        self.gateway.observe.assert_called_once_with(active["id"])
        self.assertEqual(self.store.get(queued["id"])["executionStatus"], "registered")

    def test_restarted_loop_reconciles_original_submission(self):
        task = self.register("one")
        self.loop.tick()
        restarted = DispatchLoop(
            Dispatcher(TaskStore(self.store.path), {"project": self.gateway})
        )
        restarted.tick()
        self.assertEqual(self.gateway.submit.call_count, 1)
        self.assertEqual(self.gateway.observe.call_args.args, (task["id"],))

    def test_pages_do_not_starve_later_registered_tasks(self):
        self.register("one", self.root / "one")
        self.register("two", self.root / "two", role="independent-role")
        for _ in range(5):
            self.loop.tick()
        self.assertEqual(self.gateway.submit.call_count, 2)

    def test_terminal_released_tasks_are_not_polled(self):
        task = self.register("one")
        self.loop.tick()
        self.gateway.observe.return_value = {
            "status": "succeeded",
            "identity": {"upstreamTaskId": "native"},
            "terminalConfirmed": True,
            "resourcesReleased": True,
            "output": {},
        }
        for _ in range(5):
            self.loop.tick()
        self.assertEqual(self.gateway.observe.call_count, 1)
        self.assertTrue(self.store.get(task["id"])["resourcesReleased"])

    def test_unstarted_canceled_task_is_not_dispatched(self):
        task = self.register("one")
        self.store.request_cancel(task["id"])
        self.loop.tick()
        self.gateway.submit.assert_not_called()

    def _clock(self):
        now = {"t": 1000.0}
        self.loop.cadence._clock = lambda: now["t"]
        return now

    def _block_on_submit(self, hint):
        now = self._clock()
        self.gateway.submit.return_value = {"status": "unknown", "pollHint": hint}
        self.gateway.observe.return_value = {"status": "unknown", "pollHint": hint}
        self.gateway.cancel.return_value = {"status": "unknown", "pollHint": hint}
        task = self.register("blocked")
        blocked = self.loop.tick()
        self.assertEqual(blocked[0]["polling"], {"action": "blocked", "reason": hint})
        self.assertEqual(self.loop.tick(), [])
        self.gateway.submit.assert_called_once()
        self.gateway.observe.assert_not_called()
        return task, now

    def test_authentication_submit_is_not_queried_again(self):
        _, now = self._block_on_submit("authentication")
        now["t"] += 120
        again = self.loop.tick()
        self.assertEqual(again[0]["polling"]["action"], "blocked")
        self.gateway.observe.assert_not_called()
        self.gateway.submit.assert_called_once()

    def test_missing_model_submit_is_not_queried_again(self):
        _, now = self._block_on_submit("model_missing")
        now["t"] += 120
        again = self.loop.tick()
        self.assertEqual(again[0]["polling"]["reason"], "model_missing")
        self.gateway.observe.assert_not_called()
        self.gateway.submit.assert_called_once()

    def test_cancel_still_queries_a_configuration_block(self):
        task, now = self._block_on_submit("authentication")
        self.store.request_cancel(task["id"])
        now["t"] += 120
        queried = self.loop.tick()
        self.assertEqual(queried[0]["polling"]["action"], "queried")
        self.gateway.observe.assert_called_once_with(task["id"])
        self.gateway.cancel.assert_called_once_with(task["id"])
        self.gateway.submit.assert_called_once()

    def test_deadline_still_queries_a_configuration_block(self):
        task, now = self._block_on_submit("model_missing")
        self.store.execution_deadline_exceeded = lambda task_id: task_id == task["id"]
        now["t"] += 120
        queried = self.loop.tick()
        self.assertEqual(queried[0]["polling"]["action"], "queried")
        self.gateway.observe.assert_called_once_with(task["id"])
        self.gateway.submit.assert_called_once()

    def test_unknown_query_retries_only_after_its_delay(self):
        now = self._clock()
        self.gateway.submit.return_value = {"status": "unknown", "pollHint": "unknown"}
        self.gateway.observe.return_value = {"status": "unknown", "pollHint": "unavailable"}
        self.register("transient")
        self.loop.tick()
        self.assertEqual(self.loop.tick(), [])
        queried = self.loop.tick()
        self.assertEqual(queried[0]["polling"]["action"], "queried")
        self.assertEqual(queried[0]["polling"]["reason"], "unavailable")
        self.assertEqual(self.loop.tick(), [])
        deferred = self.loop.tick()
        self.assertEqual(deferred[0]["polling"]["action"], "deferred")
        self.assertEqual(self.gateway.observe.call_count, 1)
        now["t"] += 2
        self.assertEqual(self.loop.tick(), [])
        waiting = self.loop.tick()
        self.assertEqual(waiting[0]["polling"]["action"], "deferred")
        now["t"] += 1
        self.assertEqual(self.loop.tick(), [])
        retried = self.loop.tick()
        self.assertEqual(retried[0]["polling"]["action"], "queried")
        self.assertEqual(self.gateway.observe.call_count, 2)
        self.gateway.submit.assert_called_once()


class PollCadenceTests(unittest.TestCase):
    def test_configuration_blocks_survive_the_clock_until_identity_changes(self):
        now = {"t": 50.0}
        cadence = PollCadence(clock=lambda: now["t"])
        fingerprint = ("uncertain", 2, False, "pending", "idle")
        changed = ("uncertain", 3, False, "pending", "idle")
        for outcome in ("authentication", "model_missing"):
            with self.subTest(outcome=outcome):
                summary = cadence.record("task-" + outcome, fingerprint, outcome)
                self.assertEqual(summary, {"reason": outcome, "blocked": True})
                self.assertNotIn("nextQueryInSeconds", summary)
                now["t"] += 300
                self.assertEqual(
                    cadence.remaining("task-" + outcome, fingerprint),
                    {"reason": outcome, "blocked": True},
                )
                self.assertIsNone(cadence.remaining("task-" + outcome, changed))

    def test_unknown_becomes_due_after_its_delay(self):
        now = {"t": 10.0}
        cadence = PollCadence(clock=lambda: now["t"])
        fingerprint = ("uncertain", 2, False, "pending", "idle")
        summary = cadence.record("task", fingerprint, "unknown")
        self.assertEqual(summary["reason"], "unknown")
        self.assertEqual(summary["nextQueryInSeconds"], 2.0)
        self.assertNotIn("blocked", summary)
        now["t"] += 1.9
        waiting = cadence.remaining("task", fingerprint)
        self.assertGreater(waiting["nextQueryInSeconds"], 0)
        now["t"] += 0.1
        self.assertIsNone(cadence.remaining("task", fingerprint))
