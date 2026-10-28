"""Bounded in-memory cadence for observing local task snapshots."""

from collections import OrderedDict
import math
import time


_MAX_IDLE_SECONDS = 300.0
_FAILURE_DELAYS = (2.0, 4.0, 8.0, 16.0, 30.0)
_OFFLINE_DELAYS = (3.0, 6.0, 12.0, 24.0, 30.0)
_UNCHANGED_DELAYS = (2.0, 4.0, 5.0)
_OUTCOMES = {"observed", "unknown", "unavailable", "rate_limited", "authentication", "model_missing"}
_CONFIGURATION_BLOCKS = {"authentication", "model_missing"}


def _clock_value(clock):
    try:
        value = clock()
    except Exception as exc:
        raise ValueError("poll clock unavailable") from exc
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("poll clock invalid")
    return float(value)


def _task_key(task_id):
    if (type(task_id) is not str or not task_id or len(task_id) > 160
            or any(ord(char) < 32 or ord(char) == 127 for char in task_id)):
        raise ValueError("task identity invalid")
    return task_id


def _fingerprint(value):
    if type(value) is not tuple or len(value) != 5:
        raise ValueError("task fingerprint invalid")
    for item in value:
        if item is not None and type(item) not in (str, int, float, bool):
            raise ValueError("task fingerprint invalid")
        if type(item) is float and not math.isfinite(item):
            raise ValueError("task fingerprint invalid")
        if type(item) is str and (len(item) > 256 or any(ord(char) < 32 or ord(char) == 127 for char in item)):
            raise ValueError("task fingerprint invalid")
    return value


class PollCadence:
    """Track when each task may be observed again without delaying due work."""

    def __init__(self, *, clock=time.monotonic, max_entries=2048):
        if not callable(clock):
            raise ValueError("poll clock invalid")
        if type(max_entries) is not int or max_entries < 1 or max_entries > 2048:
            raise ValueError("poll capacity invalid")
        self._clock = clock
        self._max_entries = max_entries
        self._entries = OrderedDict()

    def _prune_at(self, now):
        removed = 0
        for task_id, entry in list(self._entries.items()):
            if now - entry["touched"] > _MAX_IDLE_SECONDS:
                del self._entries[task_id]
                removed += 1
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
            removed += 1
        return removed

    @staticmethod
    def _summary(entry, now):
        if entry.get("blocked") is True:
            return {"reason": entry["reason"], "blocked": True}
        remaining = max(0.0, min(30.0, entry["due"] - now))
        return {"reason": entry["reason"], "nextQueryInSeconds": remaining}

    def remaining(self, task_id, fingerprint):
        task_id = _task_key(task_id)
        fingerprint = _fingerprint(fingerprint)
        now = _clock_value(self._clock)
        self._prune_at(now)
        entry = self._entries.get(task_id)
        if entry is None:
            entry = {
                "fingerprint": fingerprint,
                "touched": now,
                "due": now,
                "reason": "observed",
                "failures": 0,
                "offline_count": 0,
                "unchanged_since": None,
                "unchanged_count": 0,
                "outcome": None,
                "blocked": False,
            }
            self._entries[task_id] = entry
            self._prune_at(now)
            return None
        entry["touched"] = now
        self._entries.move_to_end(task_id)
        if entry["fingerprint"] != fingerprint:
            entry.update({
                "fingerprint": fingerprint,
                "due": now,
                "reason": "observed",
                "failures": 0,
                "offline_count": 0,
                "unchanged_since": None,
                "unchanged_count": 0,
                "outcome": None,
                "blocked": False,
            })
            return None
        if entry.get("blocked") is True:
            return self._summary(entry, now)
        if now >= entry["due"]:
            return None
        return self._summary(entry, now)

    def record(self, task_id, fingerprint, outcome, retry_after=None):
        task_id = _task_key(task_id)
        fingerprint = _fingerprint(fingerprint)
        if type(outcome) is not str or outcome not in _OUTCOMES:
            raise ValueError("poll outcome invalid")
        if retry_after is not None and (type(retry_after) is not int or not 1 <= retry_after <= 30):
            raise ValueError("retry delay invalid")
        if outcome != "rate_limited" and retry_after is not None:
            raise ValueError("retry delay invalid")
        now = _clock_value(self._clock)
        self._prune_at(now)
        entry = self._entries.get(task_id)
        if entry is None:
            entry = {
                "fingerprint": fingerprint,
                "touched": now,
                "due": now,
                "reason": "observed",
                "failures": 0,
                "offline_count": 0,
                "unchanged_since": None,
                "unchanged_count": 0,
                "outcome": None,
                "blocked": False,
            }
            self._entries[task_id] = entry
        else:
            if entry["fingerprint"] != fingerprint:
                entry.update({
                    "fingerprint": fingerprint,
                    "failures": 0,
                    "offline_count": 0,
                    "unchanged_since": None,
                    "unchanged_count": 0,
                    "outcome": None,
                    "blocked": False,
                })
            entry["touched"] = now
            self._entries.move_to_end(task_id)

        if outcome == "observed":
            entry["blocked"] = False
            entry["failures"] = 0
            entry["offline_count"] = 0
            entry["outcome"] = "observed"
            if entry["unchanged_since"] is None:
                entry["unchanged_since"] = now
                entry["unchanged_count"] = 0
            if now - entry["unchanged_since"] >= 30.0:
                delay = _UNCHANGED_DELAYS[min(entry["unchanged_count"], len(_UNCHANGED_DELAYS) - 1)]
                entry["unchanged_count"] = min(2, entry["unchanged_count"] + 1)
                entry["reason"] = "unchanged_local_snapshot"
            else:
                delay = 1.0
                entry["reason"] = "observed"
        elif outcome == "unknown":
            entry["blocked"] = False
            entry["unchanged_since"] = None
            entry["unchanged_count"] = 0
            entry["offline_count"] = 0
            entry["failures"] = min(5, entry["failures"] + 1) if entry["outcome"] == "unknown" else 1
            delay = _FAILURE_DELAYS[min(entry["failures"] - 1, len(_FAILURE_DELAYS) - 1)]
            entry["reason"] = outcome
            entry["outcome"] = outcome
        elif outcome == "unavailable":
            entry["blocked"] = False
            entry["unchanged_since"] = None
            entry["unchanged_count"] = 0
            entry["failures"] = 0
            entry["offline_count"] = min(5, entry.get("offline_count", 0) + 1) if entry["outcome"] == "unavailable" else 1
            delay = _OFFLINE_DELAYS[min(entry["offline_count"] - 1, len(_OFFLINE_DELAYS) - 1)]
            entry["reason"] = outcome
            entry["outcome"] = outcome
        elif outcome == "rate_limited":
            entry["blocked"] = False
            entry["unchanged_since"] = None
            entry["unchanged_count"] = 0
            entry["failures"] = 0
            entry["offline_count"] = 0
            delay = max(5.0, float(retry_after or 5))
            entry["reason"] = "rate_limited"
            entry["outcome"] = outcome
        elif outcome in _CONFIGURATION_BLOCKS:
            entry["unchanged_since"] = None
            entry["unchanged_count"] = 0
            entry["failures"] = 0
            entry["offline_count"] = 0
            entry["reason"] = outcome
            entry["outcome"] = outcome
            entry["blocked"] = True
            entry["due"] = now
            entry["touched"] = now
            self._entries.move_to_end(task_id)
            self._prune_at(now)
            return self._summary(entry, now)

        entry["due"] = now + min(30.0, delay)
        entry["touched"] = now
        self._entries.move_to_end(task_id)
        self._prune_at(now)
        return self._summary(entry, now)

    def forget(self, task_id):
        task_id = _task_key(task_id)
        self._entries.pop(task_id, None)

    def prune(self):
        now = _clock_value(self._clock)
        return self._prune_at(now)
