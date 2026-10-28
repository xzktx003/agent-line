"""Machine workflow starts cannot outrun the assembled execution admission."""
from contextlib import nullcontext
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from fastapi.testclient import TestClient

from agent_line.adapters.sim import SubmissionUncertain
from agent_line.approval_discovery import WorkflowRunLedger
from agent_line.service import ServiceConfig, create_app
from agent_line.sim_journal import SimJournal
from agent_line.store import TaskStore
from agent_line.workflows import WorkflowGateway


class Readiness:
    confirmed = True
    reason = None

    def __init__(self):
        self.calls = 0
        self.stale = False

    def assert_current(self):
        self.calls += 1
        if self.stale:
            raise ValueError('PRIVATE proof path must not escape')


class WorkflowExecutionReadinessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.tasks = TaskStore.initialize(self.root / 'tasks.sqlite')
        self.runs = WorkflowRunLedger.initialize(self.root / 'runs.sqlite')
        self.journal = SimJournal.initialize(self.root / 'sim.sqlite')
        self.sim = Mock(); self.sim.execute.return_value = {'accepted': True}
        self.sim.observe.return_value = {'status': 'paused'}
        self.readiness = Readiness()
        self.flow = SimpleNamespace(runs=self.runs, workers=(),
                                    discovery=SimpleNamespace(allow_submissions=False))
        self.policy = {'roleBindingId': 'role', 'roleAgentId': 'parent', 'instruction': 'Implement',
            'memoryPolicyId': 'native-role', 'outputSchemaId': 'task-result',
            'requireMemoryCommit': False, 'allowFailureBranch': False}
        self.machine = {'revision': '1', 'deploymentVersionId': 'deployment',
                        'agentPolicies': {'agent': self.policy}, 'ttlSeconds': 600}
        self.human = {'revision': '1', 'deploymentVersionId': 'deployment',
                      'blockIds': ['approval'], 'ttlSeconds': 600}
        self.body = {'idempotencyKey': 'original', 'input': {'request': 'test'}}
        self.headers = {'Authorization': 'Bearer key'}

    def open(self, *, capability='confirmed', mixed=False, machine=True, human=False, published=False,
             proof=True):
        binding = {'projectId': 'project', 'workflowId': 'workflow'}
        if machine: binding['externalTasks'] = self.machine
        if mixed or human: binding['humanApproval'] = self.human
        self.gateway = WorkflowGateway(self.sim, self.journal, {'flow': binding}, installation_id='fixture')
        self.gateway.task_flow = self.flow
        self.gateway.publication_verifier = Mock()
        self.gateway.approval_discovery = SimpleNamespace(runs=self.runs) if mixed or human else None
        if published:
            binding['publication'] = {'revision': 1}
            self.gateway.bindings = {}
            self.gateway.catalog = Mock()
            self.gateway.catalog.bindings.return_value = {'flow': binding}
            self.gateway.catalog.execution_scope.side_effect = nullcontext
        app = create_app(self.tasks, ServiceConfig(tokens={'key': {'project'}, 'other': {'other'}},
            projects={'project': {'workspace': str(self.root)}}, bindings={}, controlled_execution=capability),
            workflows=self.gateway, task_flow=self.flow, execution_readiness=self.readiness if proof else None)
        http = TestClient(app); self.addCleanup(http.close)
        return http

    def start(self, http):
        return http.post('/extension/v1/workflows/flow/runs', headers=self.headers, json=self.body)

    def assert_no_intents(self):
        self.sim.execute.assert_not_called()
        with self.runs.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM runs').fetchone()[0], 0)
        with self.journal.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM operations').fetchone()[0], 0)
        with self.tasks.connect() as db:
            for table in ('tasks', 'events', 'workspace_leases'):
                self.assertEqual(db.execute('SELECT count(*) FROM ' + table).fetchone()[0], 0)

    def denied(self, http):
        response = self.start(http)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertNotIn('PRIVATE', response.text)
        self.assertNotIn(str(self.root), response.text)
        self.assert_no_intents()

    def test_unknown_machine_start_refuses_before_reservation_or_native_execute(self):
        self.denied(self.open(capability='unknown'))
        self.assertEqual(self.readiness.calls, 0)

    def test_stale_machine_start_checks_actual_proof_without_reserving_any_intent(self):
        http = self.open(); self.readiness.stale = True
        self.denied(http)
        self.assertEqual(self.readiness.calls, 1)

    def test_missing_discovery_is_fixed_rejection_without_creating_a_run(self):
        http = self.open(); self.flow.discovery = None
        self.denied(http)

    def test_machine_admission_requires_explicit_true_and_available_discovery_attribute(self):
        http = self.open()
        for flag in (False, None, 1):
            self.flow.discovery.allow_submissions = flag
            self.denied(http)
        del self.flow.discovery
        self.denied(http)
        self.assertEqual(self.readiness.calls, 0)

    def test_mixed_machine_human_start_also_requires_current_execution_proof(self):
        http = self.open(mixed=True); self.readiness.stale = True
        self.denied(http)
        self.assertEqual(self.readiness.calls, 1)

    def test_published_machine_refusal_leaves_no_frozen_publication_or_execution_intent(self):
        self.denied(self.open(capability='unknown', published=True))

    def test_current_readiness_is_checked_before_reserving_and_executing(self):
        http = self.open()
        def execute(workflow, run, inputs):
            self.assertEqual(self.readiness.calls, 1)
            self.assertEqual(self.runs.get(workflow, run)['submissionStatus'], 'intent')
            return {'accepted': True}
        self.sim.execute.side_effect = execute
        self.assertEqual(self.start(http).status_code, 202)
        self.sim.execute.assert_called_once()

    def test_explicit_standalone_discovery_without_readiness_retains_trusted_fixture_contract(self):
        self.assertEqual(self.start(self.open(proof=False)).status_code, 202)

    def test_accepted_replay_and_history_skip_new_admission_even_after_discovery_removed(self):
        http = self.open()
        first = self.start(http); self.assertEqual(first.status_code, 202)
        self.readiness.stale = True; self.flow.discovery = None
        second = self.start(http)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(second.json(), first.json())
        self.assertEqual(self.readiness.calls, 1)
        self.sim.execute.assert_called_once()
        observed = http.get('/extension/v1/workflows/flow/runs/' + first.json()['runId'], headers=self.headers)
        self.assertEqual(observed.status_code, 200)

    def test_uncertain_replay_never_rechecks_stale_proof_or_resubmits(self):
        http = self.open(); self.sim.execute.side_effect = SubmissionUncertain('unknown')
        first = self.start(http); self.assertEqual(first.status_code, 202)
        self.assertEqual(first.json()['status'], 'uncertain')
        self.readiness.stale = True; self.flow.discovery.allow_submissions = False
        self.assertEqual(self.start(http).json(), first.json())
        self.assertEqual(self.readiness.calls, 1)
        self.sim.execute.assert_called_once()

    def test_pure_control_and_human_only_are_independent_of_machine_readiness(self):
        for human in (False, True):
            with self.subTest(human=human):
                # Use a distinct idempotency key because policies differ.
                self.body['idempotencyKey'] = 'human' if human else 'control'
                self.readiness.stale = True
                self.assertEqual(self.start(self.open(capability='unknown', machine=False, human=human)).status_code, 202)
        self.assertEqual(self.readiness.calls, 0)

    def test_authentication_and_scope_still_precede_readiness(self):
        http = self.open(); self.readiness.stale = True
        url = '/extension/v1/workflows/flow/runs'
        self.assertEqual(http.post(url, json=self.body).status_code, 401)
        self.assertEqual(http.post(url, headers={'Authorization': 'Bearer other'}, json=self.body).status_code, 404)
        self.assertEqual(self.readiness.calls, 0)
        self.assert_no_intents()


if __name__ == '__main__':
    unittest.main()
