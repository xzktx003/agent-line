"""Real byte verification runs in the existing worker, without native execution."""
import copy
import json
import time
import unittest
from unittest.mock import Mock

from fastapi.testclient import TestClient

from agent_line.automatic_verification import AutomaticVerifier
from agent_line.dispatcher import Dispatcher, DispatchLoop
from agent_line.runtime import load_app
from agent_line.service import ServiceConfig, create_app
from agent_line.store import TaskStore
from agent_line.verification_runtime import load_verifier
from tests import test_automatic_verification as fixtures


class VerificationRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.AutomaticVerificationTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.roles = {'role': {'projectId': 'project', 'roleAgentId': 'role',
                               'verificationPolicy': copy.deepcopy(self.f.policy)}}
        self.projects = {'project': {'workspace': str(self.f.workspace)}}
        self.settings = {'schemaVersion': 1, 'database': str(self.f.store.path),
                         'tokens': {'service-key': ['project']}, 'projects': self.projects,
                         'bindings': self.roles, 'verification': {'evidenceDirectory': str(self.f.evidence)}}
        self.path = self.f.root/'service.json'

    def save(self):
        self.path.write_text(json.dumps(self.settings)); self.path.chmod(0o600)

    def test_runtime_uses_existing_worker_to_verify_without_enabling_execution(self):
        self.save(); app = load_app(self.path)
        self.assertIsInstance(app.state.verifier, AutomaticVerifier)
        self.assertFalse(app.state.execution_worker.loop.allow_submissions)
        with TestClient(app):
            deadline = time.monotonic()+3
            while self.f.store.get(self.f.task)['verificationStatus'] == 'pending' and time.monotonic()<deadline:
                time.sleep(.01)
            self.assertEqual(self.f.store.get(self.f.task)['verificationStatus'], 'passed')
        self.assertFalse(app.state.execution_worker.thread.is_alive())
        self.assertEqual(self.f.store.dispatch_candidates(), [])

    def test_missing_verifier_and_unsupported_tests_fail_at_startup(self):
        self.settings.pop('verification'); self.save()
        with self.assertRaisesRegex(ValueError, 'requires an automatic verifier'): load_app(self.path)
        self.settings['verification'] = {'evidenceDirectory': str(self.f.evidence)}
        self.roles['role']['verificationPolicy']['requireTests'] = True; self.save()
        with self.assertRaises(ValueError): load_app(self.path)

    def test_config_cannot_assert_capability_or_use_workspace_as_evidence_directory(self):
        for verification in ({'evidenceDirectory': str(self.f.evidence), 'autoPass': True},
                             {'evidenceDirectory': str(self.f.workspace)}, True):
            self.settings['verification'] = verification; self.save()
            with self.subTest(verification=verification), self.assertRaises(ValueError): load_app(self.path)
        self.settings['verification'] = {'evidenceDirectory': str(self.f.evidence)}
        self.settings['controlled_execution'] = 'confirmed'; self.save()
        with self.assertRaises(ValueError): load_app(self.path)

    def test_wrong_store_and_non_private_evidence_are_rejected(self):
        other = TaskStore.initialize(self.f.root/'other.sqlite')
        verifier = AutomaticVerifier(other, self.f.evidence)
        with self.assertRaises(ValueError):
            create_app(self.f.store, ServiceConfig(tokens={'key': {'project'}}, projects=self.projects,
                bindings=self.roles), verifier=verifier)
        self.f.evidence.chmod(0o755)
        with self.assertRaises(ValueError):
            load_verifier({'evidenceDirectory': str(self.f.evidence)}, tasks=self.f.store, projects=self.projects)

    def test_direct_app_cannot_put_verification_evidence_inside_registered_project(self):
        store = TaskStore.initialize(self.f.root/'empty.sqlite')
        evidence = self.f.workspace/'private-evidence'; evidence.mkdir(mode=0o700)
        verifier = AutomaticVerifier(store, evidence)  # No old tasks exist to reveal the project yet.
        with self.assertRaises(ValueError):
            create_app(store, ServiceConfig(tokens={'key': {'project'}}, projects=self.projects,
                bindings=self.roles), verifier=verifier)

    def test_api_freezes_server_policy_and_ignores_model_input_policy(self):
        gateway = Mock(role='role', workspace=str(self.f.workspace))
        dispatcher = Dispatcher(self.f.store, {('project', 'role'): gateway})
        app = create_app(self.f.store, ServiceConfig(tokens={'key': {'project'}}, projects=self.projects,
            bindings=self.roles, controlled_execution='confirmed'), verifier=self.f.verifier,
            dispatcher=dispatcher)
        client = TestClient(app); self.addCleanup(client.close)
        payload = {'schemaVersion': '1.0', 'idempotencyKey': 'second', 'projectId': 'project',
            'roleBindingId': 'role', 'workflow': {'id': 'workflow', 'revision': '1', 'runId': 'second',
                'nodeId': 'node', 'nodeExecutionId': 'node', 'attemptId': 'one'},
            'input': {'verificationPolicy': {'requireTests': True, 'sha256': 'fake'}},
            'memoryPolicyId': 'native-role', 'outputSchemaId': 'task-result'}
        response = client.post('/extension/v1/node-tasks', headers={'Authorization': 'Bearer key'}, json=payload)
        self.assertEqual(response.status_code, 202, response.text)
        task = self.f.store.get(response.json()['taskId'])
        self.assertEqual(task['payload']['verificationPolicy'], self.f.policy)
        self.roles['role']['verificationPolicy']['artifacts'][0]['sha256'] = 'f'*64
        self.assertEqual(self.f.store.get(task['id'])['payload']['verificationPolicy'], self.f.policy)
        payload['verificationPolicy'] = {'policyId': 'client'}
        self.assertEqual(client.post('/extension/v1/node-tasks', headers={'Authorization': 'Bearer key'},
                                    json=payload).status_code, 422)
        gateway.submit.assert_not_called()

    def test_queued_writer_cannot_start_until_prior_verification_is_persisted(self):
        payload = copy.deepcopy(self.f.store.get(self.f.task)['payload'])
        payload.pop('verificationPolicy'); payload['workflow']['runId'] = 'next'; payload['input'] = {}
        second, _ = self.f.store.register('second', payload)
        gateway = Mock()
        def submit(**kwargs):
            self.assertEqual(self.f.store.get(self.f.task)['verificationStatus'], 'passed')
            return {'status': 'accepted', 'identity': {'upstreamTaskId': 'next-native'}}
        gateway.submit.side_effect = submit; gateway.observe.return_value = {'status': 'unknown'}
        dispatcher = Dispatcher(self.f.store, {'project': gateway}, verifier=self.f.verifier)
        loop = DispatchLoop(dispatcher)
        loop.tick(); loop.tick()
        self.assertEqual(self.f.store.get(second['id'])['executionStatus'], 'accepted')
        gateway.submit.assert_called_once()
        self.assertEqual(self.f.store.get(self.f.task)['verificationStatus'], 'passed')
