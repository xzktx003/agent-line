"""Mixed authoring is enabled only by shared, server-owned runtime wiring."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from agent_line.approvals import ApprovalLedger
from agent_line.approval_discovery import WorkflowRunLedger
from agent_line.approval_runtime import load_approval_api
from agent_line.authoring_runtime import load_authoring_api
from agent_line.pause_bindings import PauseBindings
from agent_line.runtime import load_app
from agent_line.service import ServiceConfig, create_app
from agent_line.sim_journal import SimJournal
from agent_line.store import TaskStore
from agent_line.task_flow_runtime import load_task_flow
from agent_line.workflows import WorkflowGateway


class MixedRuntimeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.tasks = TaskStore.initialize(self.root/'tasks.sqlite')
        self.runs = WorkflowRunLedger.initialize(self.root/'runs.sqlite')
        self.other_runs = WorkflowRunLedger.initialize(self.root/'other-runs.sqlite')
        self.pauses = PauseBindings.initialize(self.root/'pauses.sqlite')
        self.journal = SimJournal.initialize(self.root/'sim.sqlite')
        self.approval_ledger = ApprovalLedger.initialize(self.root/'approvals.sqlite')
        self.projects = {'project': {'workspace': str(self.root)}}
        self.sim = Mock(_key='sim-key')
        self.write('auth.json', {'baseUrl': 'http://sim.invalid', 'apiKey': 'sim-key'})
        self.flow_config = {'schemaVersion': 1, 'runLedgerPath': str(self.runs.path),
                            'pauseBindingsPath': str(self.pauses.path)}
        self.write('flow.json', self.flow_config)
        self.approval_config = {'schemaVersion': 1, 'ledgerPath': str(self.approval_ledger.path),
            'runLedgerPath': str(self.runs.path), 'credentials': [self.credential('a'*40, ['approve'])]}
        self.write('approval.json', self.approval_config)
        self.write('authoring.json', {'schemaVersion': 1, 'workspaces': {'project': 'native-workspace'},
            'credentials': [self.credential('e'*40, ['edit_workflows', 'publish_workflows'])]})
        self.service = {'schemaVersion': 1, 'database': 'tasks.sqlite', 'tokens': {'service': ['project']},
            'projects': self.projects, 'bindings': {}, 'taskFlowFile': 'flow.json',
            'approvalFile': 'approval.json', 'authoringFile': 'authoring.json',
            'sim': {'authFile': 'auth.json', 'journal': 'sim.sqlite', 'installationId': 'fixture', 'bindings': {}}}
        self.path = self.write('service.json', self.service)

    def credential(self, token, capabilities):
        return {'token': token, 'subject': token[0]+'-operator', 'projectScopes': ['project'],
                'capabilities': capabilities, 'expiresAt': 4102444800}

    def write(self, name, value):
        path = self.root/name; path.write_text(json.dumps(value)); path.chmod(0o600); return path

    def load(self):
        with patch('agent_line.adapters.sim.SimClient', return_value=self.sim):
            return load_app(self.path)

    def direct(self):
        self.approvals = load_approval_api(self.root/'approval.json', projects=self.projects,
            service_tokens=['service'], client=self.sim, journal=self.journal)
        self.author = load_authoring_api(self.root/'authoring.json', projects=self.projects,
            service_tokens=['service'], client=self.sim, journal=self.journal)
        self.flow = load_task_flow(self.root/'flow.json', tasks=self.tasks, projects=self.projects,
            role_bindings={}, client=self.sim, journal=self.journal)
        self.workflow = WorkflowGateway(self.sim, self.journal, {}, installation_id='fixture')
        self.workflow.task_flow = self.flow
        self.workflow.approval_discovery = self.approvals.discovery
        self.config = ServiceConfig(tokens={'service': {'project'}}, projects=self.projects, bindings={})

    def assemble(self, **kwargs):
        return create_app(self.tasks, self.config, workflows=self.workflow, authoring=self.author,
                          approvals=kwargs.get('approvals', self.approvals), task_flow=self.flow)

    def test_shared_real_load_app_enables_and_drains_every_worker_before_one_close(self):
        app = self.load()
        self.assertTrue(app.state.authoring_api.approval_enabled)
        self.assertEqual(self.sim.mock_calls, [])
        flow = app.state.task_flow; approval = app.state.approval_discovery_worker
        self.assertEqual(flow.runs.path, approval.discovery.runs.path)
        self.assertIs(flow.discovery.client, self.sim)
        self.assertIs(approval.discovery.client, self.sim)
        def close():
            self.assertFalse(approval.thread.is_alive())
            self.assertTrue(all(not worker.thread.is_alive() for worker in flow.workers))
        self.sim.close.side_effect = close
        with TestClient(app):
            self.assertTrue(approval.thread.is_alive())
            self.assertTrue(all(worker.thread.is_alive() for worker in flow.workers))
            self.sim.close.assert_not_called()
            self.assertFalse(flow.discovery.allow_submissions)
        self.sim.close.assert_called_once()

    def test_load_app_different_run_ledger_cannot_enable_mixed_publication(self):
        self.write('approval.json', self.approval_config | {'runLedgerPath': str(self.other_runs.path)})
        app = self.load()
        self.assertFalse(app.state.authoring_api.approval_enabled)
        self.assertEqual(self.sim.mock_calls, [])
        with TestClient(app): pass
        self.sim.close.assert_called_once()

    def test_missing_approval_cannot_preserve_manually_asserted_flag(self):
        self.direct(); self.author.approval_enabled = True
        self.assemble(approvals=None)
        self.assertFalse(self.author.approval_enabled)

    def test_discovery_must_be_same_real_object(self):
        self.direct(); self.author.approval_enabled = True
        self.workflow.approval_discovery = load_approval_api(self.root/'approval.json', projects=self.projects,
            service_tokens=['service'], client=self.sim, journal=self.journal).discovery
        self.assemble()
        self.assertFalse(self.author.approval_enabled)
        self.workflow.approval_discovery = self.approvals.discovery = Mock()
        self.author.approval_enabled = True
        self.assemble()
        self.assertFalse(self.author.approval_enabled)

    def test_wrong_approval_transport_or_journal_cannot_enable(self):
        for component in ('coordinator-client', 'discovery-client', 'journal'):
            self.direct(); self.author.approval_enabled = True
            if component == 'coordinator-client': self.approvals.coordinator.client = Mock()
            elif component == 'discovery-client': self.approvals.discovery.client = Mock()
            else:
                extra = self.root/'other-journal.sqlite'
                self.approvals.coordinator.journal = SimJournal.initialize(extra)
            with self.subTest(component=component):
                self.assemble(); self.assertFalse(self.author.approval_enabled)

    def test_absent_discovery_does_not_enable_flag(self):
        self.direct(); self.author.approval_enabled = True
        self.workflow.approval_discovery = self.approvals.discovery = None
        self.assemble(); self.assertFalse(self.author.approval_enabled)

    def test_no_workflow_gateway_cannot_keep_claimed_capability(self):
        self.direct(); self.author.approval_enabled = True
        create_app(self.tasks, self.config, authoring=self.author, approvals=self.approvals)
        self.assertFalse(self.author.approval_enabled)

    def test_pure_human_runtime_without_machine_flow_can_use_shared_approval(self):
        self.direct(); self.workflow.task_flow = None
        create_app(self.tasks, self.config, workflows=self.workflow,
                   authoring=self.author, approvals=self.approvals)
        self.assertTrue(self.author.approval_enabled)
        self.assertEqual(self.sim.mock_calls, [])

    def test_different_approval_audit_store_cannot_enable(self):
        self.direct(); self.author.approval_enabled = True
        self.approvals.discovery.approvals = ApprovalLedger.initialize(self.root/'unrelated-approval.sqlite')
        self.assemble()
        self.assertFalse(self.author.approval_enabled)
