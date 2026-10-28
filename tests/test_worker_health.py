import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from agent_line.dispatcher import Dispatcher
from agent_line.service import ServiceConfig, create_app
from agent_line.store import TaskStore
from agent_line.worker import ExecutionWorker


class WorkerHealthTests(unittest.TestCase):
    def test_blocked_scan_is_delayed_then_recovers_without_new_submission(self):
        entered, release, completed = threading.Event(), threading.Event(), threading.Event()
        now = [100.0]
        worker = ExecutionWorker(Mock(), allow_submissions=False)

        def tick():
            entered.set()
            release.wait(3)
            return []

        worker.loop.tick = tick
        original_report = worker._report
        def report(result):
            original_report(result)
            completed.set()
        worker._report = report
        with patch('agent_line.worker.monotonic', side_effect=lambda: now[0]):
            self.assertEqual(worker.health_snapshot()['status'], 'not_started')
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(worker.health_snapshot()['status'], 'starting')
                now[0] += 61
                self.assertEqual(worker.health_snapshot()['status'], 'delayed')
                release.set()
                self.assertTrue(completed.wait(2))
                self.assertEqual(worker.health_snapshot()['status'], 'healthy')
                worker.stop()
                self.assertEqual(worker.health_snapshot()['status'], 'stopped')
                self.assertIsNotNone(worker.health_snapshot()['lastScanCompletedAt'])
            finally:
                release.set()
                worker.stop()

    def test_failed_scan_recovers_without_disclosing_error_text_or_task_ids(self):
        worker = ExecutionWorker(Mock(), interval=.01)
        failed, release, recovered = threading.Event(), threading.Event(), threading.Event()
        calls = []
        def tick():
            calls.append(True)
            if len(calls) == 1:
                raise RuntimeError('private-provider-token')
            release.wait(3)
            return []
        original_report = worker._report
        def report(result):
            original_report(result)
            (failed if result else recovered).set()
        worker.loop.tick = tick
        worker._report = report
        worker.start()
        try:
            self.assertTrue(failed.wait(2))
            snapshot = worker.health_snapshot()
            self.assertEqual(snapshot['status'], 'degraded')
            self.assertNotIn('private-provider-token', str(snapshot))
            self.assertNotIn('RuntimeError', str(snapshot))
            release.set()
            self.assertTrue(recovered.wait(2))
            self.assertEqual(worker.health_snapshot()['status'], 'healthy')
        finally:
            release.set()
            worker.stop()

    def test_authenticated_idle_inspection_does_not_call_model_or_write_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = TaskStore.initialize(root / 'tasks.sqlite')
            gateway = Mock()
            app = create_app(store, ServiceConfig(tokens={'token': {'project'}},
                projects={'project': {'workspace': str(root)}}, bindings={}),
                dispatcher=Dispatcher(store, {'project': gateway}))
            completed = threading.Event()
            original_report = ExecutionWorker._report
            # Observe the actual idle scan, including the real dispatcher/store.
            def report(worker, result):
                original_report(worker, result)
                completed.set()
            with patch.object(ExecutionWorker, '_report', report), TestClient(app) as client:
                self.assertTrue(completed.wait(3))
                with store.connect() as db:
                    before = list(db.iterdump())
                self.assertEqual(client.get('/extension/v1/health').status_code, 401)
                for _ in range(3):
                    response = client.get('/extension/v1/health',
                        headers={'Authorization': 'Bearer token'})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()['executionObserver']['status'], 'healthy')
                with store.connect() as db:
                    self.assertEqual(list(db.iterdump()), before)
                gateway.submit.assert_not_called()
                gateway.observe.assert_not_called()

    def test_health_without_dispatcher_explicitly_reports_not_configured(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = create_app(TaskStore.initialize(root / 'tasks.sqlite'),
                ServiceConfig(tokens={'token': {'project'}},
                    projects={'project': {'workspace': str(root)}}, bindings={}))
            with TestClient(app) as client:
                response = client.get('/extension/v1/health',
                    headers={'Authorization': 'Bearer token'})
                self.assertEqual(response.json()['executionObserver']['status'], 'not_configured')
