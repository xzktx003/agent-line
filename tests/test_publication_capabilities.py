"""Publish checks trusted assembled capabilities before any native deployment."""
import copy
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_line.approvals import TrustedPrincipal
from agent_line.authoring_api import AuthoringAPI, AuthoringAccess, namespace, state_hash
from agent_line.sim_authoring import compile_document, native_id
from agent_line.sim_journal import SimJournal
from tests.test_authoring_api import FakeSimAuthoringClient, document


def verification_policy():
    return {'schemaVersion': 1, 'policyId': 'configured-artifact', 'kind': 'artifact_sha256',
            'requireTests': False, 'artifacts': [{'path': 'result.txt', 'sha256': 'a' * 64}]}


class PublicationCapabilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.now = 100
        self.remote = FakeSimAuthoringClient()
        self.access = AuthoringAccess({
            'owner': TrustedPrincipal('owner', frozenset({'project'}),
                                      frozenset({'edit_workflows', 'publish_workflows'}), 200),
            'editor': TrustedPrincipal('editor', frozenset({'project'}), frozenset({'edit_workflows'}), 200),
            'other': TrustedPrincipal('other', frozenset({'other'}),
                                      frozenset({'edit_workflows', 'publish_workflows'}), 200),
        }, clock=lambda: self.now)
        self.api = AuthoringAPI(self.access, SimJournal.initialize(root / 'journal.sqlite'),
                                {'project': self.remote}, projects={'project': {'workspace': str(root)}},
                                role_bindings={'role': {'projectId': 'project', 'roleAgentId': 'native-role',
                                                       'verificationPolicy': verification_policy()}},
                                verification_enabled=True)
        self.checks = []
        def ready(project, policies):
            self.checks.append((project, copy.deepcopy(policies)))
        self.ready = ready
        self.api.publication_check = ready
        app = FastAPI(); app.include_router(self.api.router())
        self.http = TestClient(app); self.addCleanup(self.http.close)
        self.base = '/extension/v1/authoring/projects/project'
        created = self.request('POST', '/workflows', {'operationId': 'create', 'name': '能力检查'})
        self.workflow = created.json()['receipt']['workflowId']
        self.doc = document()
        self.doc['nodes'].insert(1, {'id': 'agent', 'type': 'agent', 'name': '角色',
            'position': {'x': 0, 'y': 0}, 'config': {
                'roleBindingId': 'role', 'instruction': '完成任务',
                'inputFrom': {'nodeId': 'start', 'field': 'input'},
                'memoryPolicyId': 'native-role', 'outputSchemaId': 'task-result',
                'requireMemoryCommit': False, 'allowFailureBranch': False}})
        self.doc['edges'] = [{'id': 'a', 'source': 'start', 'target': 'agent'},
                             {'id': 'b', 'source': 'agent', 'target': 'end'}]
        self.set_document(self.doc)

    def request(self, method, path, body=None, token='owner'):
        headers = {} if token is None else {'Authorization': 'Bearer ' + token}
        return self.http.request(method, self.base + path, headers=headers, json=body)

    def set_document(self, doc):
        self.remote.states[self.workflow] = compile_document(doc, self.workflow)

    def publish(self, operation='publish', token='owner'):
        return self.request('POST', '/workflows/' + self.workflow + '/publish',
            {'operationId': operation, 'expectedStateSha256': state_hash(self.remote.states[self.workflow])}, token)

    def blocked(self, outcome, operation='publish'):
        result = self.publish(operation)
        self.assertEqual(result.status_code, 202)
        self.assertEqual(result.json()['receipt']['outcome'], outcome)
        self.assertFalse(result.json()['safeToResubmit'])
        self.assertEqual(self.remote.count('deploy'), 0)
        return result

    def test_missing_trusted_policy_blocks_even_with_live_capabilities(self):
        del self.api.role_bindings['role']['verificationPolicy']
        self.blocked('verification_required')
        self.assertEqual(self.checks, [])

    def test_malformed_policy_and_disabled_verifier_block_before_callback(self):
        self.api.role_bindings['role']['verificationPolicy'] = {'fromBrowser': True}
        self.blocked('verification_required', 'malformed')
        self.api.role_bindings['role']['verificationPolicy'] = verification_policy()
        self.api.verification_enabled = False
        self.blocked('verification_required', 'disabled')
        self.assertEqual(self.checks, [])

    def test_missing_assembled_authority_blocks_and_preserves_drafts(self):
        self.api.publication_check = None
        self.blocked('execution_required')
        saved = self.request('POST', '/workflows/' + self.workflow + '/save', {
            'operationId': 'save', 'expectedStateSha256': state_hash(self.remote.states[self.workflow]),
            'document': self.doc})
        self.assertEqual(saved.status_code, 202)
        self.assertEqual(saved.json()['receipt']['outcome'], 'saved')
        self.assertEqual(self.request('GET', '/workflows').status_code, 200)
        self.assertEqual(self.request('GET', '/workflows/' + self.workflow).status_code, 200)
        self.assertEqual(self.remote.count('deploy'), 0)

    def test_every_supported_runtime_rejection_is_durable_and_never_deploys(self):
        for outcome in ('execution_required', 'verification_required', 'memory_commit_required', 'role_binding_required'):
            self.api.publication_check = lambda project, policies, reason=outcome: reason
            first = self.blocked(outcome, outcome)
            self.api.publication_check = self.ready
            self.assertEqual(self.publish(outcome).json(), first.json())
        self.assertEqual(self.checks, [])

    def test_stale_or_wrong_scope_capability_check_receives_project_and_frozen_policies(self):
        def check(project, policies):
            self.assertEqual(project, 'project')
            policy = policies[native_id(self.workflow, 'agent')]
            self.assertEqual(policy['roleAgentId'], 'native-role')
            self.assertEqual(policy['verificationPolicy']['policyId'], 'configured-artifact')
            return 'execution_required'  # e.g. receipt expired or deployment scope changed.
        self.api.publication_check = check
        self.blocked('execution_required')

    def test_false_unknown_nonempty_objects_and_noncallable_do_not_authorize(self):
        for index, value in enumerate((False, True, '', 'ready', {}, [], {'outcome': 'ready'})):
            self.api.publication_check = lambda project, policies, result=value: result
            self.blocked('execution_required', 'invalid-' + str(index))
        self.api.publication_check = False
        self.blocked('execution_required', 'not-callable')

    def test_callback_exception_is_sanitized_and_durable(self):
        def broken(project, policies):
            raise RuntimeError('SECRET token internal-path')
        self.api.publication_check = broken
        first = self.blocked('execution_required')
        self.assertNotIn('SECRET', first.text)
        self.api.publication_check = self.ready
        self.assertEqual(self.publish().json(), first.json())
        self.assertEqual(self.checks, [])

    def test_success_checks_once_and_replay_never_rechecks_or_redeploys(self):
        first = self.publish()
        self.assertEqual(first.json()['receipt']['outcome'], 'published')
        self.api.publication_check = lambda *args: self.fail('Replay must use original journal receipt')
        self.assertEqual(self.publish().json(), first.json())
        self.assertEqual(len(self.checks), 1)
        self.assertEqual(self.remote.count('deploy'), 1)

    def test_pure_control_flow_does_not_require_execution_or_verification(self):
        self.set_document(document())
        self.api.publication_check = lambda *args: self.fail('No Agent capability needed')
        self.api.verification_enabled = False
        self.assertEqual(self.publish().json()['receipt']['outcome'], 'published')
        self.assertEqual(self.remote.count('deploy'), 1)

    def test_authentication_publish_permission_project_scope_and_expiry_precede_check(self):
        self.api.publication_check = lambda *args: self.fail('Unauthorized check')
        for token, status in ((None, 401), ('editor', 403), ('other', 404)):
            self.assertEqual(self.publish(token=token).status_code, status)
        self.now = 201
        self.assertEqual(self.publish().status_code, 403)
        self.assertEqual(self.remote.count('deploy'), 0)

    def test_uncertain_deploy_keeps_original_intent_and_never_rechecks(self):
        def uncertain(workflow):
            self.remote.calls.append(('deploy', workflow))
            raise TimeoutError('SECRET lost reply')
        self.remote.deploy = uncertain
        first = self.publish()
        self.assertEqual(first.json()['status'], 'uncertain')
        self.assertIsNone(first.json()['receipt'])
        self.api.publication_check = lambda *args: self.fail('Uncertain operation cannot retry')
        self.assertEqual(self.publish().json(), first.json())
        self.assertEqual(len(self.checks), 1)
        self.assertEqual(self.remote.count('deploy'), 1)

    def test_interrupted_original_intent_is_observed_without_capability_check_or_deploy(self):
        body = {'operationId': 'publish', 'expectedStateSha256': state_hash(self.remote.states[self.workflow]),
                'workflowId': self.workflow}
        def interrupted():
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.api.journal.mutate(namespace('project'), 'publish', 'publish',
                {'projectId': 'project', 'subject': 'owner', 'request': body}, interrupted)
        result = self.publish()
        self.assertEqual(result.status_code, 202)
        self.assertEqual(result.json()['status'], 'intent')
        self.assertIsNone(result.json()['receipt'])
        self.assertEqual(self.checks, [])
        self.assertEqual(self.remote.count('deploy'), 0)

    def test_unowned_workflow_is_rejected_before_capability_check(self):
        unowned = self.remote.create('原生未授权流程', '')['id']
        response = self.request('POST', '/workflows/' + unowned + '/publish', {
            'operationId': 'unowned', 'expectedStateSha256': state_hash(self.remote.states[unowned])})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.checks, [])
        self.assertEqual(self.remote.count('deploy'), 0)

    def test_agent_capability_check_preserves_existing_human_approval_requirement(self):
        human = document(human=True)['nodes'][1]
        self.doc['nodes'].insert(2, human)
        self.doc['edges'] = [{'id': 'a', 'source': 'start', 'target': 'agent'},
                             {'id': 'b', 'source': 'agent', 'target': 'approval'},
                             {'id': 'c', 'source': 'approval', 'target': 'end'}]
        self.set_document(self.doc)
        self.blocked('approval_binding_required')
        self.assertEqual(self.checks, [])

    def test_browser_capability_fields_are_not_authority(self):
        response = self.request('POST', '/workflows/' + self.workflow + '/publish', {
            'operationId': 'browser', 'expectedStateSha256': state_hash(self.remote.states[self.workflow]),
            'executionReady': True, 'verificationEnabled': True})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.checks, [])
        self.assertEqual(self.remote.count('deploy'), 0)


if __name__ == '__main__':
    unittest.main()
