"""Publish-to-run loop policy wiring, using existing real local stores."""
import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from fastapi.testclient import TestClient

from agent_line.approval_discovery import WorkflowRunLedger
from agent_line.approvals import TrustedPrincipal
from agent_line.authoring_api import AuthoringAPI, AuthoringAccess, state_hash
from agent_line.pause_bindings import PauseBindings
from agent_line.service import ServiceConfig, create_app
from agent_line.sim_authoring import _derived_loops, loop_policies
from agent_line.sim_journal import SimJournal
from agent_line.store import TaskStore
from agent_line.workflows import WorkflowGateway
from tests.test_loop_publication import document
from tests.test_published_workflows import PublishedRemote
from tests.test_publication_capabilities import verification_policy


class NativeRemote(PublishedRemote):
    def save_state(self, workflow, state):
        state = copy.deepcopy(state)
        state.update(loops=_derived_loops(state['blocks']), parallels={})
        return super().save_state(workflow, state)


class LoopWorkflowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.tasks = TaskStore.initialize(root/'tasks.sqlite')
        self.journal = SimJournal.initialize(root/'sim.sqlite')
        self.runs = WorkflowRunLedger.initialize(root/'runs.sqlite')
        self.sim = Mock(_key='sim-key'); self.sim.execute.return_value = {'accepted': True}
        self.remote = NativeRemote(self.sim)
        projects = {'one': {'workspace': str(root)}}
        roles = {'role': {'projectId': 'one', 'roleAgentId': 'native-parent'}}
        principal = TrustedPrincipal('owner', frozenset({'one'}),
            frozenset({'edit_workflows', 'publish_workflows'}), 200)
        self.api = AuthoringAPI(AuthoringAccess({'editor': principal}, clock=lambda: 100),
            self.journal, {'one': self.remote}, projects=projects, role_bindings=roles)
        self.gateway = WorkflowGateway(self.sim, self.journal, {}, installation_id='installation')
        self.flow = SimpleNamespace(runs=self.runs, workers=(),
            pauses=PauseBindings.initialize(root/'pauses.sqlite'),
            discovery=SimpleNamespace(allow_submissions=False))
        self.gateway.task_flow = self.flow
        self.verifier = Mock(); self.gateway.publication_verifier = self.verifier
        self.http = TestClient(create_app(self.tasks, ServiceConfig(tokens={'run': {'one'}},
            projects=projects, bindings=roles, controlled_execution='confirmed'), workflows=self.gateway, authoring=self.api, task_flow=self.flow))
        self.addCleanup(self.http.close)
        # Only loop policy propagation is under test; native execution is synthetic.
        self.api.role_bindings['role']['verificationPolicy'] = verification_policy()
        self.api.verification_enabled = True
        self.api.publication_check = lambda project, policies: None
        from agent_line.automatic_verification import AutomaticVerifier
        evidence = tempfile.TemporaryDirectory(); self.addCleanup(evidence.cleanup)
        self.gateway.verifier = AutomaticVerifier(self.tasks, Path(evidence.name))

    def edit(self, path, body):
        response = self.http.post('/extension/v1/authoring/projects/one/workflows'+path,
            headers={'Authorization': 'Bearer editor'}, json=body)
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()['receipt']

    def publish(self):
        workflow = self.edit('', {'operationId': 'create', 'name': '有限修复'})['workflowId']
        self.edit('/'+workflow+'/save', {'operationId': 'save', 'document': document(),
            'expectedStateSha256': state_hash(self.remote.states[workflow])})
        receipt = self.edit('/'+workflow+'/publish', {'operationId': 'publish',
            'expectedStateSha256': state_hash(self.remote.states[workflow])})
        self.assertEqual(receipt['outcome'], 'published')
        return workflow, receipt

    def start(self, binding):
        return self.http.post('/extension/v1/workflows/'+binding+'/runs',
            headers={'Authorization': 'Bearer run'}, json={'idempotencyKey': 'trigger', 'input': {}})

    def test_published_loop_policy_is_verified_and_frozen_before_execute(self):
        workflow, publication = self.publish()
        expected = loop_policies(document(), workflow)
        self.assertEqual(publication['externalTasks']['loopPolicies'], expected)
        def execute(wid, rid, inputs):
            self.assertEqual(self.runs.get(wid, rid)['loopPolicies'], expected)
            self.assertEqual(self.journal.get_publication(wid, rid)['externalTasks']['loopPolicies'], expected)
            return {'accepted': True}
        self.sim.execute.side_effect = execute
        first = self.start(publication['bindingId'])
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(self.start(publication['bindingId']).json(), first.json())
        self.sim.execute.assert_called_once()
        self.verifier.check_definition.assert_called_once_with(workflow, '1',
            publication['deploymentVersionId'], list(publication['externalTasks']['agentPolicies']),
            require_active=True, loop_policies=expected)

    def test_existing_run_rejects_changed_frozen_loop_policy(self):
        workflow, publication = self.publish()
        first = self.start(publication['bindingId']); self.assertEqual(first.status_code, 202, first.text)
        with self.runs.connect() as db:
            import json
            row = db.execute('SELECT snapshot FROM runs WHERE workflow=?', (workflow,)).fetchone()
            snapshot = json.loads(row[0]); body = next(iter(snapshot['loopPolicies']))
            snapshot['loopPolicies'][body]['maxIterations'] = 2
            db.execute('UPDATE runs SET snapshot=? WHERE workflow=?', (json.dumps(snapshot), workflow))
        self.assertEqual(self.start(publication['bindingId']).status_code, 409)
        self.sim.execute.assert_called_once()
