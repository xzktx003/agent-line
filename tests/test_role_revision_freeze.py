"""Frozen role pins: temporary SQLite and FakeSim contracts, no host execution."""
import copy
import unittest
from tests.test_verification_policy_freeze import document
from tests import test_authoring_api as authoring_base
from tests import test_task_discovery as discovery_base
from agent_line.approval_discovery import WorkflowRunLedger
from agent_line.sim_authoring import compile_document, AuthoringError
from agent_line.store import Conflict
from tests.test_publication_capabilities import verification_policy

PIN = 'qwenpaw-role-v1:' + 'a'*64
OTHER = 'qwenpaw-role-v1:' + 'b'*64


class RolePublishFreezeTests(unittest.TestCase):
    setUp = authoring_base.AuthoringAPITests.setUp
    open_app = authoring_base.AuthoringAPITests.open_app
    request = authoring_base.AuthoringAPITests.request
    create = authoring_base.AuthoringAPITests.create
    owned = authoring_base.AuthoringAPITests.owned
    publish = authoring_base.AuthoringAPITests.publish

    def test_trusted_pin_published_and_old_operation_replay_unchanged(self):
        self.api.role_bindings = {'binding': {'projectId':'one','roleAgentId':'agent','roleRevision':PIN,
                                             'verificationPolicy':verification_policy()}}
        # Synthetic admission isolates the frozen pin contract, not native readiness.
        self.api.verification_enabled = True
        self.api.publication_check = lambda project, policies: None
        workflow = self.owned(document()); original = self.publish(workflow).json()
        policy = next(iter(original['receipt']['externalTasks']['agentPolicies'].values()))
        self.assertEqual(policy['roleRevision'], PIN)
        self.api.role_bindings['binding']['roleRevision'] = OTHER
        self.assertEqual(self.publish(workflow).json(), original)
        self.assertEqual(self.remote.count('deploy'), 1)

    def test_browser_cannot_set_revision(self):
        doc = document(); doc['nodes'][1]['config']['roleRevision'] = PIN
        with self.assertRaises(AuthoringError): compile_document(doc, 'workflow')

    def test_invalid_server_pin_rejected_before_publish(self):
        self.api.verification_enabled = True
        for bad in (None, '', 'latest', 'qwenpaw-role-v1:'+'A'*64):
            self.api.role_bindings = {'binding': {'projectId':'one','roleAgentId':'agent','roleRevision':bad,
                                                 'verificationPolicy':verification_policy()}}
            with self.subTest(bad=bad), self.assertRaises(AuthoringError):
                self.api._agent_policies('one', 'workflow', document())


class RoleRunFreezeTests(unittest.TestCase):
    setUp = discovery_base.TaskDiscoveryTests.setUp
    reopen = discovery_base.TaskDiscoveryTests.reopen
    count = discovery_base.TaskDiscoveryTests.count

    def reserve(self, run='run'):
        self.policy['roleRevision'] = PIN
        self.roles['binding']['roleRevision'] = PIN
        return self.runs.reserve_tasks(project_id='project',workflow_id='workflow',run_id=run,
            revision='1',deployment_version_id='deployment',agent_policies={'wait-one':copy.deepcopy(self.policy)})

    def test_snapshot_task_and_model_injection(self):
        read = self.wait.read_input
        def injected(*args, **kwargs):
            result = read(*args, **kwargs); result['input']['roleRevision'] = OTHER; return result
        self.wait.read_input = injected
        result = self.discovery.tick()[0]
        self.assertEqual(result['status'], 'external_task_bound')
        task = self.tasks.get(result['taskId'])
        self.assertEqual(task['payload']['roleRevision'], PIN)
        self.assertEqual(task['payload']['input']['taskInput']['roleRevision'], OTHER)
        self.assertEqual(WorkflowRunLedger(self.runs.path).get('workflow','run')['agentPolicies']['wait-one']['roleRevision'], PIN)
        self.assertEqual(self.reopen().tick()[0]['taskId'], result['taskId'])

    def test_changed_or_missing_current_pin_blocks_before_input(self):
        for value in (OTHER, None):
            if value is None: self.roles['binding'].pop('roleRevision', None)
            else: self.roles['binding']['roleRevision'] = value
            self.assertEqual(self.discovery.tick()[0]['status'], 'external_task_role_unavailable')
        self.assertEqual(self.count(), 0); self.assertEqual(self.wait.calls, [])

    def test_snapshot_revision_mutation_or_removal_conflicts(self):
        for value in (OTHER, None):
            changed = copy.deepcopy(self.policy)
            if value is None: changed.pop('roleRevision')
            else: changed['roleRevision'] = value
            with self.assertRaises(Conflict):
                self.runs.reserve_tasks(project_id='project',workflow_id='workflow',run_id='run',
                    revision='1',deployment_version_id='deployment',agent_policies={'wait-one':changed})
        self.assertEqual(self.runs.get('workflow','run')['agentPolicies']['wait-one']['roleRevision'], PIN)

    def test_invalid_pin_rejected(self):
        changed = dict(self.policy, roleRevision='latest')
        with self.assertRaises(ValueError):
            self.runs.reserve_tasks(project_id='project',workflow_id='workflow',run_id='bad',
                revision='1',deployment_version_id='deployment',agent_policies={'wait-one':changed})


class LegacyRoleFreezeTests(unittest.TestCase):
    setUp = discovery_base.TaskDiscoveryTests.setUp
    reopen = discovery_base.TaskDiscoveryTests.reopen

    def reserve(self, run='run'):
        return self.runs.reserve_tasks(project_id='project',workflow_id='workflow',run_id=run,
            revision='1',deployment_version_id='deployment',agent_policies={'wait-one':copy.deepcopy(self.policy)})

    def test_legacy_missing_pin_not_filled_from_current_binding(self):
        self.roles['binding']['roleRevision'] = PIN
        self.assertEqual(self.discovery.tick()[0]['status'], 'external_task_role_unavailable')
        self.assertNotIn('roleRevision', self.runs.get('workflow','run')['agentPolicies']['wait-one'])
        self.roles['binding'].pop('roleRevision')
        result = self.discovery.tick()[0]
        self.assertEqual(result['status'], 'external_task_bound')
        self.assertNotIn('roleRevision', self.tasks.get(result['taskId'])['payload'])
