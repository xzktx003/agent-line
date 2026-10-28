import unittest

from agent_line.polling import PollCadence


FINGERPRINT = ("running", 1, False, "pending", "idle")


class SeparateInspectionBackoffTests(unittest.TestCase):
    def cadence(self, now):
        return PollCadence(clock=lambda: now["t"])

    def test_offline_rate_limit_and_no_progress_start_apart(self):
        now = {"t": 100.0}
        cadence = self.cadence(now)
        offline = cadence.record("offline", FINGERPRINT, "unavailable")
        limited = cadence.record("limited", FINGERPRINT, "rate_limited")
        cadence.record("stalled", FINGERPRINT, "observed")
        now["t"] += 30
        stalled = cadence.record("stalled", FINGERPRINT, "observed")
        self.assertEqual(
            (
                offline["reason"],
                limited["reason"],
                stalled["reason"],
            ),
            ("unavailable", "rate_limited", "unchanged_local_snapshot"),
        )
        self.assertEqual(
            (
                offline["nextQueryInSeconds"],
                limited["nextQueryInSeconds"],
                stalled["nextQueryInSeconds"],
            ),
            (3.0, 5.0, 2.0),
        )

    def test_each_condition_keeps_its_own_ladder(self):
        self.assertEqual(self._steps("unknown", 5), [2.0, 4.0, 8.0, 16.0, 30.0])
        self.assertEqual(self._steps("unavailable", 5), [3.0, 6.0, 12.0, 24.0, 30.0])
        self.assertEqual(self._steps("rate_limited", 3), [5.0, 5.0, 5.0])
        now = {"t": 0.0}
        cadence = self.cadence(now)
        self.assertEqual(cadence.record("stalled", FINGERPRINT, "observed")["nextQueryInSeconds"], 1.0)
        progressed = []
        for _ in range(3):
            now["t"] += 30
            progressed.append(cadence.record("stalled", FINGERPRINT, "observed")["nextQueryInSeconds"])
        self.assertEqual(progressed, [2.0, 4.0, 5.0])

    def test_switching_condition_does_not_continue_another_ladder(self):
        now = {"t": 0.0}
        cadence = self.cadence(now)
        self.assertEqual(cadence.record("task", FINGERPRINT, "unknown")["nextQueryInSeconds"], 2.0)
        now["t"] += 31
        self.assertEqual(cadence.record("task", FINGERPRINT, "unknown")["nextQueryInSeconds"], 4.0)
        now["t"] += 31
        self.assertEqual(cadence.record("task", FINGERPRINT, "unavailable")["nextQueryInSeconds"], 3.0)
        now["t"] += 31
        self.assertEqual(cadence.record("task", FINGERPRINT, "unavailable")["nextQueryInSeconds"], 6.0)
        now["t"] += 31
        self.assertEqual(cadence.record("task", FINGERPRINT, "rate_limited")["nextQueryInSeconds"], 5.0)
        now["t"] += 31
        hinted = cadence.record("task", FINGERPRINT, "rate_limited", retry_after=8)
        self.assertEqual(hinted["nextQueryInSeconds"], 8.0)
        now["t"] += 31
        self.assertEqual(cadence.record("task", FINGERPRINT, "rate_limited", retry_after=3)["nextQueryInSeconds"], 5.0)
        now["t"] += 31
        cadence.record("task", FINGERPRINT, "observed")
        now["t"] += 30
        stalled = cadence.record("task", FINGERPRINT, "observed")
        self.assertEqual(stalled["reason"], "unchanged_local_snapshot")
        self.assertEqual(stalled["nextQueryInSeconds"], 2.0)
        now["t"] += 31
        self.assertEqual(cadence.record("task", FINGERPRINT, "unknown")["nextQueryInSeconds"], 2.0)
        now["t"] += 31
        self.assertEqual(cadence.record("task", FINGERPRINT, "unavailable")["nextQueryInSeconds"], 3.0)

    def test_fingerprint_change_resets_every_ladder(self):
        now = {"t": 10.0}
        cadence = self.cadence(now)
        cadence.record("task", FINGERPRINT, "unavailable")
        now["t"] += 31
        changed = ("running", 2, False, "pending", "idle")
        self.assertIsNone(cadence.remaining("task", changed))
        again = cadence.record("task", changed, "unavailable")
        self.assertEqual(again["nextQueryInSeconds"], 3.0)

    def _steps(self, outcome, times):
        now = {"t": 0.0}
        cadence = self.cadence(now)
        found = []
        for _ in range(times):
            found.append(cadence.record("task", FINGERPRINT, outcome)["nextQueryInSeconds"])
            now["t"] += 31
        return found
