"""Actual assembled services gate publishing; native network is an explicit fixture."""
import copy
import json
from pathlib import Path
import unittest

import httpx
from fastapi.testclient import TestClient

from agent_line.approval_api import ApprovalAccess
from agent_line.approval_discovery import WorkflowRunLedger
from agent_line.automatic_verification import AutomaticVerifier
from agent_line.dispatcher import Dispatcher
from agent_line.memory import MemoryCommitStore, MemoryCoordinator
from agent_line.memory_api import MemoryAPI, MemoryReviewLedger
from agent_line.adapters.qwenpaw_memory import QwenPawMemoryClient
from agent_line.pause_bindings import PauseBindings
from agent_line.role_dispatch import RoleDispatchGateway, RoleDispatchLedger
from agent_line.service import ServiceConfig, create_app
from agent_line.store import TaskStore
from agent_line.task_flow_runtime import load_task_flow
from agent_line.workflows import WorkflowGateway
from tests import test_publication_capabilities as base
from tests.test_authoring_api import document
from tests.test_readiness_integration import Readiness


class PublicationRuntimeTests(unittest.TestCase):
    request = base.PublicationCapabilityTests.request
    set_document = base.PublicationCapabilityTests.set_document
    publish = base.PublicationCapabilityTests.publish
    blocked = base.PublicationCapabilityTests.blocked

    def setUp(self):
        base.PublicationCapabilityTests.setUp(self)
        self.root = Path(self.api.projects['project']['workspace'])
        workspace = self.root / 'workspace'; workspace.mkdir()
        self.projects = {'project': {'workspace': str(workspace)}}
        self.api.projects = copy.deepcopy(self.projects)
        self.revision = 'qwenpaw-role-v1:' + 'a' * 64
        self.api.role_bindings['role']['roleRevision'] = self.revision
        self.bindings = copy.deepcopy(self.api.role_bindings)
        self.tasks = TaskStore.initialize(self.root / 'tasks.sqlite')
        evidence = self.root / 'evidence'; evidence.mkdir(mode=0o700)
        self.verifier = AutomaticVerifier(self.tasks, evidence)
        self.native_requests = []
        self.current_revision = self.revision
        def transport(request):
            self.native_requests.append((request.method, request.url.path))
            if request.method != 'GET' or request.url.path != '/api/agent-line/execution-binding':
                raise AssertionError('Publication cannot execute or write native data')
            return httpx.Response(200, json={'roleRevisions': {'native-role': self.current_revision}})
        client = httpx.Client(transport=httpx.MockTransport(transport))
        self.addCleanup(client.close)
        self.gateway = RoleDispatchGateway('http://qwen.invalid', 'native-user', 'native-service',
            role_agent_id='native-role', deployment_id='test',
            ledger=RoleDispatchLedger.initialize(self.root / 'parents.sqlite'),
            memory_query='Use native role memory', client=client)
        self.dispatcher = Dispatcher(self.tasks, {('project', 'role'): self.gateway})
        runs = WorkflowRunLedger.initialize(self.root / 'runs.sqlite')
        pauses = PauseBindings.initialize(self.root / 'pauses.sqlite')
        flow_file = self.root / 'flow.json'
        flow_file.write_text(json.dumps({'schemaVersion': 1,
            'runLedgerPath': str(runs.path), 'pauseBindingsPath': str(pauses.path)}))
        flow_file.chmod(0o600)
        self.flow = load_task_flow(flow_file, tasks=self.tasks, projects=copy.deepcopy(self.projects),
            role_bindings=copy.deepcopy(self.bindings), client=self.remote.sim, journal=self.api.journal)
        self.workflows = WorkflowGateway(self.remote.sim, self.api.journal, {}, installation_id='fixture')
        self.workflows.task_flow = self.flow
        self.readiness = Readiness()
        self.memories = None

    def assemble(self, *, capability='confirmed', dispatcher=True, flow=True, verifier=True):
        self.http.close()
        # create_app must replace this synthetic AuthoringAPI-only authority.
        self.api.publication_check = self.ready
        app = create_app(self.tasks, ServiceConfig(tokens={'service': {'project'}},
            projects=self.projects, bindings=self.bindings if verifier else {}, controlled_execution=capability),
            dispatcher=self.dispatcher if dispatcher else None, verifier=self.verifier if verifier else None,
            workflows=self.workflows, task_flow=self.flow if flow else None,
            memories=self.memories, authoring=self.api, execution_readiness=self.readiness)
        self.http = TestClient(app); self.addCleanup(self.http.close)
        return app

    def assert_no_execution(self):
        with self.tasks.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM tasks').fetchone()[0], 0)
        with self.gateway.ledger.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM deliveries').fetchone()[0], 0)
        self.assertTrue(all(method == 'GET' for method, _ in self.native_requests))

    def test_actual_pipeline_publishes_after_read_only_role_check_without_execution(self):
        self.assemble()
        receipt = self.publish().json()['receipt']
        self.assertEqual(receipt['outcome'], 'published')
        self.assertEqual(self.native_requests, [('GET', '/api/agent-line/execution-binding')])
        self.assertEqual(self.remote.count('deploy'), 1)
        self.assertEqual(self.checks, [])
        self.assert_no_execution()

    def test_unknown_execution_and_missing_dispatcher_or_flow_reject_synthetic_authority(self):
        for index, options in enumerate(({'capability': 'unknown'}, {'dispatcher': False}, {'flow': False})):
            self.assemble(**options)
            self.blocked('execution_required', str(index))
        self.assertEqual(self.checks, [])
        self.assertEqual(self.native_requests, [])
        self.assert_no_execution()

    def test_stale_readiness_blocks_but_previous_receipt_does_not_recheck(self):
        self.assemble()
        first = self.publish().json()
        self.readiness.stale = True
        self.assertEqual(self.publish().json(), first)
        self.assertEqual(len(self.native_requests), 1)
        self.blocked_without_prior_deploy('execution_required', 'new')
        self.assert_no_execution()

    def blocked_without_prior_deploy(self, outcome, operation):
        before = self.remote.count('deploy')
        response = self.publish(operation)
        self.assertEqual(response.json()['receipt']['outcome'], outcome)
        self.assertEqual(self.remote.count('deploy'), before)

    def test_current_native_revision_is_required_not_just_cached_binding(self):
        self.assemble()
        self.current_revision = 'qwenpaw-role-v1:' + 'b' * 64
        self.blocked('role_binding_required')
        self.assertEqual(len(self.native_requests), 1)
        self.assert_no_execution()

    def test_missing_gateway_never_falls_back_to_another_explicit_role(self):
        self.dispatcher.gateways = {('project', 'another'): self.gateway, 'project': self.gateway}
        self.assemble()
        self.blocked('execution_required')
        self.assert_no_execution()

    def test_discovery_role_revision_workspace_and_journal_must_match(self):
        self.assemble()
        changes = (
            (self.flow.discovery.role_bindings['role'], 'roleRevision', 'qwenpaw-role-v1:' + 'b' * 64),
            (self.flow.discovery.role_bindings['role'], 'roleAgentId', 'other'),
            (self.flow.discovery.projects['project'], 'workspace', str(self.root)),
        )
        for index, (target, key, value) in enumerate(changes):
            original = target[key]; target[key] = value
            self.blocked('role_binding_required', str(index))
            target[key] = original
        self.flow.discovery.journal = None
        self.blocked('execution_required', 'journal')
        self.assert_no_execution()

    def test_result_relay_and_discovery_workers_are_both_required(self):
        self.assemble()
        original = self.flow.workers
        for index, workers in enumerate((original[:1], original[1:])):
            self.flow.workers = workers
            self.blocked('execution_required', str(index))
        self.flow.workers = original
        self.flow.workers[1].loop.relay.journal = None
        self.blocked('execution_required', 'relay')
        self.assert_no_execution()

    def test_memory_commit_requires_same_role_coordinator_not_native_auto_memory_alone(self):
        self.doc['nodes'][1]['config']['requireMemoryCommit'] = True
        self.set_document(self.doc)
        self.assemble()
        self.blocked('memory_commit_required', 'missing')
        native = QwenPawMemoryClient('http://qwen.invalid', 'native-user', role_agent_id='native-role')
        self.addCleanup(native.close)
        coordinator = MemoryCoordinator(MemoryCommitStore.initialize(self.root / 'memory.sqlite'), native)
        self.memories = MemoryAPI(ApprovalAccess({}), {'memory': coordinator},
            MemoryReviewLedger.initialize(self.root / 'reviews.sqlite'), self.tasks,
            bindings={'memory': {'projectId': 'project', 'roleBindingId': 'other', 'roleAgentId': 'native-role'}})
        self.assemble()
        self.blocked('memory_commit_required', 'wrong-binding')
        self.memories.bindings['memory']['roleBindingId'] = 'role'
        native.origin = 'http://another-qwen.invalid'
        self.blocked('memory_commit_required', 'wrong-native-origin')
        native.origin = 'http://qwen.invalid'
        self.assertEqual(self.publish('ready').json()['receipt']['outcome'], 'published')
        self.assert_no_execution()

    def test_runtime_policy_validator_rejects_unavailable_test_executor(self):
        self.assemble()
        policy = {'schemaVersion': 2, 'policyId': 'tests', 'kind': 'command_and_artifacts',
            'requireTests': True, 'artifacts': [], 'testEnvironmentId': 'docker-test-v1:' + 'a' * 64,
            'tests': [{'id': 'unit', 'argv': ['python', '-m', 'unittest']}]}
        # Syntax is valid; this assembled AutomaticVerifier has no test executor.
        self.api.role_bindings['role']['verificationPolicy'] = policy
        self.blocked('verification_required')
        self.assert_no_execution()

    def test_pure_control_drafts_remain_publishable_without_execution_backend(self):
        self.set_document(document())
        self.assemble(capability='unknown', dispatcher=False, flow=False, verifier=False)
        self.assertEqual(self.publish().json()['receipt']['outcome'], 'published')
        self.assertEqual(self.native_requests, [])
        self.assert_no_execution()
