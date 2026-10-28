import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock
from agent_line.store import TaskStore
from agent_line.dispatcher import Dispatcher
from agent_line.worker import ExecutionWorker


class WorkerTests(unittest.TestCase):
    def test_owned_worker_stops_polling_and_restart_observes_same_task(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = TaskStore.initialize(root / "tasks.sqlite")
            task, _ = store.register(
                "trigger",
                {
                    "projectId": "project",
                    "roleAgentId": "role",
                    "workspace": str(root),
                    "input": {},
                    "workflow": {
                        "runId": "run",
                        "nodeExecutionId": "node",
                        "attemptId": "1",
                    },
                },
            )
            gateway = Mock()
            accepted = threading.Event()

            def submit(**kwargs):
                accepted.set()
                return {"status": "accepted", "identity": {"upstreamTaskId": "native"}}

            gateway.submit.side_effect = submit
            observed = threading.Event()

            def observe(task_id):
                observed.set()
                return {"status": "accepted", "identity": {"upstreamTaskId": "native"}}

            gateway.observe.side_effect = observe
            worker = ExecutionWorker(
                Dispatcher(store, {"project": gateway}), interval=0.01
            )
            worker.start()
            self.assertTrue(accepted.wait(3))
            worker.stop()
            self.assertFalse(worker.thread.is_alive())
            again = ExecutionWorker(
                Dispatcher(TaskStore(store.path), {"project": gateway}), interval=0.01
            )
            again.start()
            self.assertTrue(observed.wait(3))
            again.stop()
            self.assertEqual(gateway.submit.call_count, 1)
            self.assertFalse(store.get(task["id"])["resourcesReleased"])

    def test_http_lifespan_owns_and_stops_worker(self):
        from fastapi.testclient import TestClient
        from agent_line.service import create_app, ServiceConfig

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = TaskStore.initialize(root / "tasks.sqlite")
            dispatcher = Dispatcher(store, {})
            app = create_app(
                store,
                ServiceConfig(
                    tokens={"test-key": {"project"}},
                    projects={"project": {"workspace": str(root)}},
                    bindings={},
                ),
                dispatcher=dispatcher,
            )
            with TestClient(app) as client:
                self.assertTrue(app.state.execution_worker.thread.is_alive())
                response = client.get(
                    "/extension/v1/health", headers={"Authorization": "Bearer test-key"}
                )
                self.assertEqual(response.status_code, 200)
                self.assertFalse(response.json()["readyForExecution"])
            self.assertFalse(app.state.execution_worker.thread.is_alive())
