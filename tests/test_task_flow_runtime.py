"""Offline lifecycle tests with real ledgers and owned worker threads."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from agent_line.approval_discovery import WorkflowRunLedger
from agent_line.pause_bindings import PauseBindings
from agent_line.runtime import load_app
from agent_line.sim_journal import SimJournal
from agent_line.store import TaskStore
from agent_line.task_flow_runtime import load_task_flow


class TaskFlowRuntimeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.tasks = TaskStore.initialize(self.root / 'tasks.sqlite')
        self.runs = WorkflowRunLedger.initialize(self.root / 'runs.sqlite')
        self.pauses = PauseBindings.initialize(self.root / 'pauses.sqlite')
        self.journal = SimJournal.initialize(self.root / 'journal.sqlite')
        self.projects = {'project': {'workspace': str(self.root)}}
        self.config = dict(schemaVersion=1, runLedgerPath=str(self.runs.path),
                           pauseBindingsPath=str(self.pauses.path))
        self.flow_path = self.write('flow.json', self.config)
        self.sim = Mock()
        self.write('auth.json', {'baseUrl': 'http://sim.invalid', 'apiKey': 'test-key'})
        self.service = dict(schemaVersion=1, database='tasks.sqlite', tokens={'key': ['project']},
            projects=self.projects, bindings={}, taskFlowFile='flow.json',
            sim=dict(authFile='auth.json', journal='journal.sqlite', installationId='test', bindings={}))
        self.path = self.write('service.json', self.service)

    def write(self, name, data):
        path = self.root / name; path.write_text(json.dumps(data)); path.chmod(0o600)
        return path

    def load_flow(self):
        return load_task_flow(self.flow_path, tasks=self.tasks, projects=self.projects,
                              role_bindings={}, client=self.sim, journal=self.journal)

    def test_factory_is_offline_and_reuses_transport_and_existing_stores(self):
        flow = self.load_flow()
        self.assertIs(flow.discovery.client, self.sim)
        self.assertIs(flow.discovery.wait_client.sim, self.sim)
        self.assertIs(flow.workers[1].loop.relay.client, self.sim)
        self.assertIs(flow.workers[1].loop.relay.journal, self.journal)
        self.assertFalse(flow.discovery.allow_submissions)
        self.assertEqual(self.sim.mock_calls, [])
        self.assertTrue(all(worker.thread is None for worker in flow.workers))

    def test_private_config_and_ledgers_required(self):
        for path in (self.flow_path, self.runs.path, self.pauses.path):
            path.chmod(0o644)
            with self.subTest(path=path.name), self.assertRaises(ValueError): self.load_flow()
            path.chmod(0o600)
        self.assertEqual(self.sim.mock_calls, [])

    def test_missing_database_is_not_initialized(self):
        for field in ('runLedgerPath', 'pauseBindingsPath'):
            missing = self.root / (field + '.sqlite')
            self.write('flow.json', {**self.config, field: str(missing)})
            with self.assertRaises((ValueError, FileNotFoundError)): self.load_flow()
            self.assertFalse(missing.exists())
        self.assertEqual(self.sim.mock_calls, [])

    def test_schema_number_alone_does_not_allow_wrong_store_or_alias(self):
        for changes in ({'pauseBindingsPath': str(self.runs.path)},
                        {'runLedgerPath': str(self.journal.path)},
                        {'pauseBindingsPath': str(self.journal.path)}):
            self.write('flow.json', self.config | changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError): self.load_flow()
        self.assertEqual(self.sim.mock_calls, [])

    def test_exact_config_cannot_assert_confirmed_execution(self):
        for extra in ({'controlled_execution': 'confirmed'}, {'controlledExecution': 'confirmed'}):
            self.write('flow.json', {**self.config, **extra})
            with self.assertRaises(ValueError): self.load_flow()
            self.write('service.json', {**self.service, **extra})
            with self.assertRaises(ValueError): load_app(self.path)
        self.assertEqual(self.sim.mock_calls, [])

    def test_real_workers_start_stop_and_close_transport_after_drain(self):
        with patch('agent_line.adapters.sim.SimClient', return_value=self.sim):
            app = load_app(self.path)
        flow = app.state.task_flow
        self.assertEqual(self.sim.mock_calls, [])
        reached = [threading.Event(), threading.Event()]
        for worker, event in zip(flow.workers, reached):
            original = worker.loop.tick
            def tick(original=original, event=event):
                result = original(); event.set(); return result
            worker.loop.tick = tick
        with TestClient(app):
            self.assertTrue(all(event.wait(2) for event in reached))
            self.assertTrue(all(worker.thread.is_alive() for worker in flow.workers))
            self.assertFalse(flow.discovery.allow_submissions)
            self.sim.close.assert_not_called()
        self.assertTrue(all(not worker.thread.is_alive() for worker in flow.workers))
        self.sim.close.assert_called_once()
        with self.tasks.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM tasks').fetchone()[0], 0)

    def test_undrained_owned_worker_keeps_transport_open(self):
        with patch('agent_line.adapters.sim.SimClient', return_value=self.sim):
            app = load_app(self.path)
        worker = app.state.task_flow.workers[0]
        entered, release = threading.Event(), threading.Event()
        def blocking_tick():
            entered.set(); release.wait(5); return []
        worker.loop.tick = blocking_tick
        stop = worker.stop
        worker.stop = lambda: stop(timeout=0.01)
        try:
            with self.assertRaisesRegex(RuntimeError, 'still own their transport'):
                with TestClient(app):
                    self.assertTrue(entered.wait(2))
            self.sim.close.assert_not_called()
        finally:
            release.set(); stop(timeout=2)
            self.sim.close()

    def test_unknown_capability_cannot_register_a_real_machine_pause(self):
        flow = self.load_flow()
        policy = dict(roleBindingId='role', roleAgentId='parent', instruction='Implement',
                      memoryPolicyId='native', outputSchemaId='artifact',
                      requireMemoryCommit=False, allowFailureBranch=False)
        flow.runs.reserve_tasks(project_id='project', workflow_id='workflow', run_id='run',
            revision='1', deployment_version_id='deployment', agent_policies={'wait': policy})
        flow.discovery.role_bindings = {'role': {'projectId': 'project', 'roleAgentId': 'parent'}}
        flow.discovery.publication_verifier = Mock()
        flow.discovery.publication_verifier.verify_run.return_value = True
        flow.discovery.wait_client = Mock()
        self.sim.observe.return_value = {'response': {'data': {
            'workflowId': 'workflow', 'runId': 'run', 'status': 'paused',
            'paused': {'blockedOnBlockId': 'wait', 'contextId': 'wait', 'pauseKind': 'human',
                       'pausePointCount': 1, 'resumedCount': 0}}}}
        self.assertEqual(flow.discovery.tick()[0]['status'], 'external_task_admission_closed')
        flow.discovery.wait_client.read_input.assert_not_called()
        self.sim.execute.assert_not_called()
        with self.tasks.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM tasks').fetchone()[0], 0)

    def test_restart_observes_intent_and_uncertain_runs_without_resending(self):
        policy = dict(roleBindingId='role', roleAgentId='parent', instruction='Implement',
                      memoryPolicyId='native', outputSchemaId='artifact',
                      requireMemoryCommit=False, allowFailureBranch=False)
        for run, status in [('pending', 'intent'), ('ambiguous', 'uncertain')]:
            self.runs.reserve_tasks(project_id='project', workflow_id='workflow', run_id=run,
                revision='1', deployment_version_id='deployment', agent_policies={'wait': policy})
            self.runs.record_submission('workflow', run, status)
        self.sim.observe.return_value = {'response': {'data': {'status': 'unknown'}}}
        with patch('agent_line.adapters.sim.SimClient', return_value=self.sim):
            app = load_app(self.path)
        reached = threading.Event()
        discovery = app.state.task_flow.discovery
        original = discovery.tick
        def tick():
            result = original(); reached.set(); return result
        discovery.tick = tick
        with TestClient(app):
            self.assertTrue(reached.wait(2))
        self.assertEqual({call.args[1] for call in self.sim.observe.call_args_list},
                         {'pending', 'ambiguous'})
        self.sim.execute.assert_not_called()
        self.sim.resume.assert_not_called()
        with self.tasks.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM tasks').fetchone()[0], 0)
