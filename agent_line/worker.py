"""Owned background polling lifecycle; native executions outlive polling stops."""

import threading
from time import monotonic, time

from .dispatcher import DispatchLoop


class ExecutionWorker:
    def __init__(self, dispatcher, *, interval=1.0, allow_submissions=True):
        if interval <= 0:
            raise ValueError("Worker interval must be positive")
        self.loop = DispatchLoop(dispatcher, allow_submissions=allow_submissions)
        self.interval = interval
        self.stop_event = threading.Event()
        self.thread = None
        self.last_report = []
        self._health_lock = threading.Lock()
        self._started_mono = None
        self._completed_mono = None
        self._completed_at = None
        self._scan_failed = False

    def start(self):
        if self.thread is not None:
            raise RuntimeError("Worker instance may only be started once")
        with self._health_lock:
            self._started_mono = monotonic()
        self.thread = threading.Thread(
            target=self.loop.run,
            args=(self.stop_event,),
            kwargs={"interval": self.interval, "report": self._report},
            name="agent-line-dispatch",
            daemon=True,
        )
        self.thread.start()

    def _report(self, report):
        with self._health_lock:
            self.last_report = report
            self._completed_mono = monotonic()
            self._completed_at = time()
            self._scan_failed = any('errorType' in item for item in report)

    def health_snapshot(self, *, project_scopes=None):
        """Inspect this observer only; never reconcile or infer native progress."""
        with self._health_lock:
            reference = self._completed_mono if self._completed_mono is not None else self._started_mono
            elapsed = max(0, monotonic() - reference) if reference is not None else None
            window = max(60, self.interval * 3)
            if self.thread is None:
                status = 'not_started'
            elif not self.thread.is_alive():
                status = 'stopped'
            elif elapsed is not None and elapsed > window:
                status = 'delayed'
            elif self._completed_mono is None:
                status = 'starting'
            else:
                status = 'degraded' if self._scan_failed else 'healthy'
            scoped_report = [item for item in self.last_report
                             if project_scopes is not None and item.get('projectId') in project_scopes]
            queried = sum(item.get('polling', {}).get('action') == 'queried' for item in scoped_report)
            deferred = [item['polling']['nextQueryInSeconds'] for item in scoped_report
                        if item.get('polling', {}).get('action') == 'deferred']
            cadence = {'scope': 'authorized_projects_last_scan_page', 'observedTasks': queried,
                       'deferredTasks': len(deferred), 'maxDeferredSeconds': max(deferred, default=0)}
            return {'status': status, 'lastScanCompletedAt': self._completed_at,
                    **({'queryCadence': cadence} if self._completed_mono is not None and project_scopes is not None else {}),
                    'secondsSinceCompletedScanOrStart': round(elapsed, 3) if elapsed is not None else None,
                    'observationWindowSeconds': window}

    def stop(self, timeout=35):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout)
            if self.thread.is_alive():
                raise RuntimeError("Dispatcher has not drained its current request")
