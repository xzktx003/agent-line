"""Server-owned artifact policy propagation; no upstream execution."""
import copy
import unittest
from tests import test_authoring_api as authoring_base
from tests import test_task_discovery as discovery_base
from agent_line.approval_discovery import WorkflowRunLedger
from agent_line.sim_authoring import compile_document, AuthoringError
from agent_line.store import Conflict


def artifact():
    return {'schemaVersion':1,'policyId':'proof-v1','kind':'artifact_sha256','requireTests':False,
            'artifacts':[{'path':'proof.txt','sha256':'a'*64}]}


def document():
    doc=authoring_base.document()
    doc['nodes'].insert(1,{'id':'agent','type':'agent','name':'agent','position':{'x':1,'y':1},'config':{'roleBindingId':'binding','instruction':'work','inputFrom':{'nodeId':'start','field':'input'},'memoryPolicyId':'native-role','outputSchemaId':'task-result','requireMemoryCommit':False,'allowFailureBranch':False}})
    doc['edges']=[{'id':'a','source':'start','target':'agent'},{'id':'b','source':'agent','target':'end'}]
    return doc


class VerificationAuthoringTests(unittest.TestCase):
    setUp=authoring_base.AuthoringAPITests.setUp
    open_app=authoring_base.AuthoringAPITests.open_app
    request=authoring_base.AuthoringAPITests.request
    create=authoring_base.AuthoringAPITests.create
    owned=authoring_base.AuthoringAPITests.owned
    publish=authoring_base.AuthoringAPITests.publish

    def role(self):
        self.api.role_bindings={'binding':{'projectId':'one','roleAgentId':'agent','verificationPolicy':artifact()}}

    def test_verifier_flag_is_strict_boolean(self):
        from agent_line.authoring_api import AuthoringAPI
        for value in (1, 'true', None):
            with self.subTest(value=value),self.assertRaises(ValueError):
                AuthoringAPI(self.access,self.journal,{'one':self.remote},projects=self.projects,
                             verification_enabled=value)

    def test_default_verifier_unavailable_blocks_before_deploy(self):
        self.role();workflow=self.owned(document())
        self.assertIs(self.api.verification_enabled,False)
        self.assertEqual(self.publish(workflow).json()['receipt']['outcome'],'verification_required')
        self.assertEqual(self.remote.count('deploy'),0)

    def test_server_policy_frozen_in_publish_and_exact_replay(self):
        self.role();self.api.verification_enabled=True;workflow=self.owned(document())
        # Isolate immutable policy propagation; assembled-runtime checks have separate tests.
        self.api.publication_check=lambda project, policies: None
        first=self.publish(workflow).json()
        policy=next(iter(first['receipt']['externalTasks']['agentPolicies'].values()))['verificationPolicy']
        self.assertEqual(policy['policyId'],'proof-v1');self.assertIn('maxBytes',policy['artifacts'][0]);self.assertIn('exactSize',policy['artifacts'][0])
        self.api.role_bindings['binding']['verificationPolicy']['artifacts'][0]['sha256']='b'*64
        self.assertEqual(self.publish(workflow).json(),first)
        self.assertEqual(self.remote.count('deploy'),1)

    def test_browser_cannot_inject_policy_and_required_tests_block(self):
        doc=document();doc['nodes'][1]['config']['verificationPolicy']=artifact()
        with self.assertRaises(AuthoringError):compile_document(doc,'workflow')
        self.role();self.api.verification_enabled=True
        self.api.role_bindings['binding']['verificationPolicy']['requireTests']=True
        workflow=self.owned(document())
        self.assertEqual(self.publish(workflow).json()['receipt']['outcome'],'verification_required')
        self.assertEqual(self.remote.count('deploy'),0)


class VerificationRunTaskFreezeTests(unittest.TestCase):
    setUp=discovery_base.TaskDiscoveryTests.setUp
    reopen=discovery_base.TaskDiscoveryTests.reopen
    def reserve(self,run='run'):
        self.policy['verificationPolicy']=artifact()
        return self.runs.reserve_tasks(project_id='project',workflow_id='workflow',run_id=run,revision='1',deployment_version_id='deployment',agent_policies={'wait-one':copy.deepcopy(self.policy)})

    def test_run_and_task_copy_frozen_policy_not_updated_role_or_model_input(self):
        original=self.runs.get('workflow','run')['agentPolicies']['wait-one']['verificationPolicy']
        self.roles['binding']['verificationPolicy']=artifact();self.roles['binding']['verificationPolicy']['artifacts'][0]['sha256']='b'*64
        read = self.wait.read_input
        def injected(*args, **kwargs):
            result = read(*args, **kwargs)
            result['input']['verificationPolicy'] = artifact()
            result['input']['verificationPolicy']['artifacts'][0]['sha256'] = 'f'*64
            return result
        self.wait.read_input = injected
        first=self.discovery.tick()[0];self.assertEqual(first['status'],'external_task_bound')
        task=self.tasks.get(first['taskId'])
        self.assertEqual(task['payload']['verificationPolicy'],original)
        self.roles['binding']['verificationPolicy']['artifacts'][0]['sha256']='c'*64
        second=self.reopen().tick()[0];self.assertEqual(second['taskId'],first['taskId'])
        self.assertEqual(self.tasks.get(first['taskId'])['payload']['verificationPolicy'],original)
        self.assertEqual(WorkflowRunLedger(self.runs.path).get('workflow','run')['agentPolicies']['wait-one']['verificationPolicy'],original)
        changed=copy.deepcopy(self.policy);changed['verificationPolicy']['artifacts'][0]['sha256']='d'*64
        with self.assertRaises(Conflict):self.runs.reserve_tasks(project_id='project',workflow_id='workflow',run_id='run',revision='1',deployment_version_id='deployment',agent_policies={'wait-one':changed})

    def test_changed_frozen_policy_fields_conflict_without_mutating_original(self):
        original=self.runs.get('workflow','run')['agentPolicies']['wait-one']['verificationPolicy']
        for field,value in [('policyId','proof-v2'),('path','other.txt'),('sha256','e'*64),('exactSize',1),('maxBytes',100)]:
            changed=copy.deepcopy(self.policy)
            target=changed['verificationPolicy'] if field=='policyId' else changed['verificationPolicy']['artifacts'][0]
            target[field]=value
            with self.subTest(field=field),self.assertRaises(Conflict):
                self.runs.reserve_tasks(project_id='project',workflow_id='workflow',run_id='run',revision='1',deployment_version_id='deployment',agent_policies={'wait-one':changed})
        self.assertEqual(self.runs.get('workflow','run')['agentPolicies']['wait-one']['verificationPolicy'],original)

    def test_invalid_required_tests_policy_rejected_by_run_ledger(self):
        changed=copy.deepcopy(self.policy);changed['verificationPolicy']['requireTests']=True
        with self.assertRaises(ValueError):self.runs.reserve_tasks(project_id='project',workflow_id='workflow',run_id='new',revision='1',deployment_version_id='deployment',agent_policies={'wait-one':changed})
