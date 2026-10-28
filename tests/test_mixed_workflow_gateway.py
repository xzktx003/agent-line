"""One frozen run separates human decisions from machine task results."""
import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from fastapi.testclient import TestClient

from agent_line.approval_discovery import ApprovalDiscovery, WorkflowRunLedger
from agent_line.approvals import ApprovalLedger
from agent_line.pause_bindings import PauseBindings
from agent_line.service import ServiceConfig, create_app
from agent_line.sim_journal import SimJournal
from agent_line.store import Conflict, TaskStore
from agent_line.task_discovery import ExternalTaskDiscovery
from agent_line.workflows import WorkflowGateway


class MixedWorkflowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.tasks = TaskStore.initialize(self.root/'tasks.sqlite')
        self.runs = WorkflowRunLedger.initialize(self.root/'runs.sqlite')
        self.journal = SimJournal.initialize(self.root/'sim.sqlite')
        self.approvals = ApprovalLedger.initialize(self.root/'approvals.sqlite')
        self.pauses = PauseBindings.initialize(self.root/'pauses.sqlite')
        self.sim = Mock(); self.sim.execute.return_value = {'accepted': True}
        self.verifier = Mock(); self.verifier.verify_run.return_value = True
        self.projects = {'project': {'workspace': str(self.root)}}
        self.roles = {'role': {'projectId': 'project', 'roleAgentId': 'parent'}}
        self.agent = dict(roleBindingId='role', roleAgentId='parent', instruction='Implement',
            memoryPolicyId='native-role', outputSchemaId='task-result',
            requireMemoryCommit=False, allowFailureBranch=False)
        self.machine = dict(revision='1', deploymentVersionId='deployment',
            agentPolicies={'machine': self.agent}, ttlSeconds=600)
        self.human = dict(revision='1', deploymentVersionId='deployment',
            blockIds=['human'], ttlSeconds=300)
        self.binding = dict(projectId='project', workflowId='workflow',
            externalTasks=self.machine, humanApproval=self.human)
        self.discovery = ApprovalDiscovery(self.runs, self.approvals, self.sim,
            publication_verifier=self.verifier, clock=lambda: 1000)
        self.flow = SimpleNamespace(runs=self.runs, workers=(),
            discovery=SimpleNamespace(allow_submissions=False))

    def gateway(self, binding=None):
        gateway = WorkflowGateway(self.sim, self.journal, {'flow': binding or self.binding},
            installation_id='installation')
        gateway.task_flow = self.flow
        gateway.approval_discovery = self.discovery
        gateway.publication_verifier = self.verifier
        return gateway

    def client(self, gateway):
        client = TestClient(create_app(self.tasks, ServiceConfig(tokens={'run': {'project'}},
            projects=self.projects, bindings=self.roles, controlled_execution='confirmed'), workflows=gateway, task_flow=self.flow))
        self.addCleanup(client.close)
        return client

    def start(self, client):
        return client.post('/extension/v1/workflows/flow/runs', headers={'Authorization': 'Bearer run'},
            json={'idempotencyKey': 'trigger', 'input': {}})

    def reserve(self, **changes):
        args = dict(project_id='project', workflow_id='workflow', run_id='run', revision='1',
            deployment_version_id='deployment', agent_policies={'machine': self.agent},
            ttl_seconds=600, human_approval=self.human)
        args.update(changes)
        return self.runs.reserve_tasks(**args)

    def observed(self, block):
        self.sim.observe.side_effect = lambda wid, rid: {'response': {'data': {
            'workflowId': wid, 'runId': rid, 'status': 'paused',
            'paused': {'blockedOnBlockId': block, 'contextId': block, 'pauseKind': 'human',
                       'pausePointCount': 2, 'resumedCount': 1}}}}

    def test_one_snapshot_and_union_proof_precede_native_execution(self):
        gateway = self.gateway(); client = self.client(gateway)
        def execute(wid, rid, payload):
            frozen = WorkflowRunLedger(self.runs.path).get(wid, rid)
            self.assertEqual(frozen['humanBlockIds'], ['human', 'machine'])
            self.assertEqual(frozen['approvalBlockIds'], ['human'])
            self.assertEqual(frozen['approvalTtlSeconds'], 300)
            self.assertEqual(frozen['agentPolicies'], {'machine': self.agent})
            self.assertEqual(frozen['submissionStatus'], 'intent')
            return {'accepted': True}
        self.sim.execute.side_effect = execute
        first = self.start(client); self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(self.start(client).json(), first.json())
        self.sim.execute.assert_called_once()
        self.verifier.check_definition.assert_called_once_with('workflow', '1', 'deployment',
            ['human', 'machine'], require_active=True)
        frozen = self.runs.get('workflow', first.json()['runId'])
        self.assertEqual(frozen['submissionStatus'], 'accepted')

    def test_missing_approval_or_distinct_run_store_cannot_start(self):
        gateway = self.gateway(); client = self.client(gateway)
        gateway.approval_discovery = None
        self.assertEqual(self.start(client).status_code, 503)
        gateway.approval_discovery = self.discovery
        self.discovery.runs = WorkflowRunLedger.initialize(self.root/'other.sqlite')
        self.assertEqual(self.start(client).status_code, 503)
        self.sim.execute.assert_not_called()
        self.assertEqual(self.runs.page(), [])

    def test_changed_human_policy_conflicts_without_second_execution(self):
        gateway = self.gateway(); client = self.client(gateway)
        self.assertEqual(self.start(client).status_code, 202)
        gateway.bindings['flow']['humanApproval']['ttlSeconds'] += 1
        self.assertEqual(self.start(client).status_code, 409)
        self.sim.execute.assert_called_once()

    def test_policy_versions_and_wait_purposes_must_not_overlap(self):
        for changes in ({'revision': '2'}, {'deploymentVersionId': 'other'}, {'blockIds': ['machine']}):
            binding = copy.deepcopy(self.binding); binding['humanApproval'].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.gateway(binding)

    def test_snapshot_is_immutable_and_cannot_adopt_machine_only_run(self):
        self.assertTrue(self.reserve())
        self.assertFalse(self.reserve())
        changed = self.human | {'ttlSeconds': 301}
        with self.assertRaises(Conflict): self.reserve(human_approval=changed)
        with self.assertRaises(Conflict): self.reserve(human_approval=None)

    def test_human_discovery_skips_machine_wait_and_uses_approval_ttl_after_restart(self):
        self.reserve(); self.observed('machine')
        result = self.discovery.tick()
        self.assertEqual(result[0]['otherPurpose'], 'external_task_result')
        self.assertEqual(self.approvals.list_approvals({'project'})['approvals'], [])
        self.observed('human')
        bound = self.discovery.tick()[0]
        self.assertEqual(bound['status'], 'bound')
        record = self.approvals.get(bound['approvalId'])
        self.assertEqual(record['binding'].expires_at, 1300)
        restarted = ApprovalDiscovery(WorkflowRunLedger(self.runs.path), ApprovalLedger(self.approvals.path),
            self.sim, publication_verifier=self.verifier, clock=lambda: 1100)
        self.assertEqual(restarted.tick()[0]['approvalId'], bound['approvalId'])
        self.assertEqual(self.approvals.get(bound['approvalId'])['binding'].expires_at, 1300)

    def test_machine_only_snapshot_never_acquires_human_permission(self):
        self.reserve(human_approval=None); self.observed('machine')
        self.assertEqual(self.discovery.tick(), [])
        self.sim.observe.assert_not_called()

    def test_machine_discovery_cannot_turn_human_wait_into_task(self):
        self.reserve(); self.observed('human')
        wait = Mock()
        machine = ExternalTaskDiscovery(self.runs, self.tasks, self.pauses, self.sim, wait,
            publication_verifier=self.verifier, projects=self.projects, role_bindings=self.roles,
            allow_submissions=True)
        self.discovery.tick()
        self.assertEqual(machine.tick()[0]['otherPurpose'], 'human_approval')
        self.assertEqual(self.runs.get('workflow', 'run')['registrationStatus'], 'bound')
        wait.read_input.assert_not_called()
        self.assertEqual(self.tasks.list_tasks({'project'})['tasks'], [])

    def test_loop_policy_is_preserved_with_human_gate(self):
        policy = self.agent | {'allowFailureBranch': True}
        loops = {'machine': {'loopId': 'loop', 'maxIterations': 3}}
        self.reserve(agent_policies={'machine': policy}, loop_policies=loops)
        self.assertEqual(self.runs.get('workflow', 'run')['loopPolicies'], loops)
