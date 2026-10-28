"""Existing-role authoring binds trusted registry IDs, never browser authority."""
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_line.approvals import TrustedPrincipal
from agent_line.authoring_api import AuthoringAPI, AuthoringAccess, state_hash
from agent_line.sim_authoring import compile_document, native_id
from agent_line.sim_journal import SimJournal
from agent_line.static_batch import build_static_batch
from agent_line.workflow_template import export_template
from tests.test_authoring_api import FakeSimAuthoringClient
from tests.test_publication_capabilities import verification_policy


class AgentAuthoringAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.remote = FakeSimAuthoringClient()
        self.roles = {'existing': {'projectId': 'project', 'roleAgentId': 'official-existing-role',
                                   'verificationPolicy': verification_policy()},
                      'disabled': {'projectId': 'project', 'roleAgentId': 'disabled-role', 'enabled': False},
                      'other': {'projectId': 'other', 'roleAgentId': 'private-other-role'}}
        access = AuthoringAccess({'token': TrustedPrincipal('owner', frozenset({'project'}),
                                  frozenset({'edit_workflows', 'publish_workflows'}), 200)}, clock=lambda: 100)
        self.api = AuthoringAPI(access, SimJournal.initialize(self.root / 'journal.sqlite'),
                                {'project': self.remote}, projects={'project': {'workspace': str(self.root)}},
                                role_bindings=self.roles, verification_enabled=True,
                                publication_check=lambda project, policies: None)
        app = FastAPI(); app.include_router(self.api.router())
        self.http = TestClient(app); self.addCleanup(self.http.close)
        self.headers = {'Authorization': 'Bearer token'}
        self.base = '/extension/v1/authoring/projects/project'
        result = self.http.post(self.base + '/workflows', headers=self.headers,
                                json={'operationId': 'create', 'name': '角色节点'}).json()
        self.workflow = result['receipt']['workflowId']
        config = {'roleBindingId': 'existing', 'instruction': '完成当前任务，沿用原生角色记忆',
                  'inputFrom': {'nodeId': 'start', 'field': 'input'}, 'memoryPolicyId': 'native-role',
                  'outputSchemaId': 'task-result', 'requireMemoryCommit': False, 'allowFailureBranch': False}
        def node(nid, kind, config):
            return {'id': nid, 'type': kind, 'name': nid, 'position': {'x': 0, 'y': 0}, 'config': config}
        self.document = {'schemaVersion': 1, 'variables': {},
                         'nodes': [node('start', 'start', {}), node('agent', 'agent', config),
                                   node('end', 'end', {'output': {'状态': '完成'}})],
                         'edges': [{'id': 'a', 'source': 'start', 'target': 'agent'},
                                   {'id': 'b', 'source': 'agent', 'target': 'end'}]}
        self.remote.states[self.workflow] = compile_document(self.document, self.workflow)

    def publish(self):
        return self.http.post(self.base + '/workflows/' + self.workflow + '/publish', headers=self.headers,
                              json={'operationId': 'publish', 'expectedStateSha256': state_hash(self.remote.states[self.workflow])})

    def test_role_picker_uses_enabled_registered_ids_without_internal_paths(self):
        response = self.http.get(self.base + '/roles', headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['roles'], [{'roleBindingId': 'existing',
                         'roleAgentId': 'official-existing-role', 'memoryPolicyId': 'native-role',
                         'outputSchemaId': 'task-result', 'details': {
                             'source': 'configured_role_summary', 'displayName': 'existing',
                             'taskRole': None, 'access': None, 'revision': None,
                             'memory': 'qwenpaw_native_role',
                             'execution': {'source': 'unavailable', 'backend': None,
                                           'executorAgentId': None, 'sandbox': None,
                                           'approvalPolicy': None, 'modelId': None,
                                           'workspace': 'project_registered'},
                             'verification': {'state': 'configured', 'policyId': 'configured-artifact',
                                              'requireTests': False, 'artifactCount': 1, 'testCount': 0}}}])
        self.assertFalse(response.json()['nativeAvailabilityVerified'])
        self.assertNotIn(str(self.root), response.text)
        self.assertEqual(self.http.get(self.base + '/roles').status_code, 401)

    def test_publish_freezes_registry_role_and_native_wait_identity(self):
        response = self.publish(); self.assertEqual(response.status_code, 202)
        receipt = response.json()['receipt']; self.assertEqual(receipt['outcome'], 'published')
        machine = receipt['externalTasks']; policy = machine['agentPolicies'][native_id(self.workflow, 'agent')]
        self.assertEqual(policy['roleAgentId'], 'official-existing-role')
        self.assertEqual(policy['roleBindingId'], 'existing')
        self.assertNotIn('inputFrom', policy)
        self.assertNotIn('workspace', policy)
        self.assertEqual(machine['revision'], '1')
        self.assertEqual(self.publish().json(), response.json())
        self.assertEqual(self.remote.count('deploy'), 1)

    def test_bad_cross_project_disabled_or_changed_policy_never_deploys(self):
        for role in ('missing', 'other', 'disabled'):
            self.document['nodes'][1]['config']['roleBindingId'] = role
            self.remote.states[self.workflow] = compile_document(self.document, self.workflow)
            # Each separate request is an explicit new operation, not a retry of an unknown write.
            response = self.http.post(self.base + '/workflows/' + self.workflow + '/publish', headers=self.headers,
                                     json={'operationId': 'publish-' + role,
                                           'expectedStateSha256': state_hash(self.remote.states[self.workflow])})
            self.assertEqual(response.json()['receipt']['outcome'], 'role_binding_required')
        self.assertEqual(self.remote.count('deploy'), 0)

    def test_shared_host_role_is_refused_by_publish_template_and_batch(self):
        self.roles['other']['roleAgentId'] = 'official-existing-role'
        response = self.http.post(self.base + '/workflows/' + self.workflow + '/publish', headers=self.headers,
                                  json={'operationId': 'publish-shared',
                                        'expectedStateSha256': state_hash(self.remote.states[self.workflow])})
        self.assertEqual(response.json()['receipt']['outcome'], 'role_binding_required')
        self.assertEqual(self.remote.count('deploy'), 0)
        template = export_template(self.document)
        imported = self.http.post(self.base + '/templates/import', headers=self.headers,
                                  json={'template': template, 'roleBindings': {'slot_1': 'existing'}})
        self.assertEqual(imported.status_code, 422)
        batch = self.http.post(self.base + '/batches/preview', headers=self.headers, json={
            'recipeId': 'shared', 'roleBindingId': 'existing', 'instruction': '沿用当前任务',
            'items': [{'n': 1}, {'n': 2}], 'policy': 'all_success'})
        self.assertEqual(batch.status_code, 422)
        self.roles['other']['roleAgentId'] = 'private-other-role'
        self.roles['alias'] = {'projectId': 'project', 'roleAgentId': 'official-existing-role',
                               'verificationPolicy': verification_policy()}
        self.assertEqual(self.publish().json()['receipt']['outcome'], 'published')
        self.assertEqual(build_static_batch('project', {
            'recipeId': 'local', 'roleBindingId': 'existing', 'instruction': '沿用当前任务',
            'items': [{'n': 1}, {'n': 2}], 'policy': 'all_success'}, self.roles)['projectId'], 'project')

    def test_custom_memory_engine_is_not_implicitly_enabled_by_a_policy_id(self):
        self.document['nodes'][1]['config']['memoryPolicyId'] = 'replacement-engine'
        self.remote.states[self.workflow] = compile_document(self.document, self.workflow)
        self.assertEqual(self.publish().json()['receipt']['outcome'], 'role_binding_required')
        self.assertEqual(self.remote.count('deploy'), 0)
