"""Human/machine publication separation using real ownership journal and HTTP."""
import copy
import json
import unittest
from tests import test_authoring_api as base
from tests.test_authoring_api import document
from agent_line.authoring_api import namespace
from agent_line.published_workflows import PublishedWorkflowCatalog
from agent_line.sim_authoring import native_id
from tests.test_publication_capabilities import verification_policy


class HumanAuthoringTests(unittest.TestCase):
    setUp = base.AuthoringAPITests.setUp
    open_app = base.AuthoringAPITests.open_app
    request = base.AuthoringAPITests.request
    create = base.AuthoringAPITests.create
    owned = base.AuthoringAPITests.owned
    publish = base.AuthoringAPITests.publish

    def mixed(self):
        doc=document(True)
        doc['nodes'].insert(1,{'id':'worker','name':'worker','type':'agent','position':{'x':30,'y':30},'config':{'roleBindingId':'role','instruction':'task','inputFrom':{'nodeId':'start','field':'input'},'memoryPolicyId':'native-role','outputSchemaId':'task-result','requireMemoryCommit':False,'allowFailureBranch':False}})
        doc['edges']=[{'id':'a','source':'start','target':'worker'},{'id':'b','source':'worker','target':'approval'},{'id':'c','source':'approval','target':'end'}]
        # This test isolates human/machine allowlists; runtime capability is synthetic.
        self.api.verification_enabled=True
        self.api.publication_check=lambda project, policies: None
        self.api.role_bindings={'role':{'projectId':'one','roleAgentId':'agent-one','enabled':True,
                                       'verificationPolicy':verification_policy()}}
        return doc

    def test_disabled_default_preserves_human_refusal(self):
        workflow=self.owned(document(True))
        self.assertIs(self.api.approval_enabled,False)
        self.assertEqual(self.publish(workflow).json()['receipt']['outcome'],'approval_binding_required')
        self.assertEqual(self.remote.count('deploy'),0)

    def test_enabled_human_only_derives_native_allowlist_and_replay(self):
        self.api.approval_enabled=True
        workflow=self.owned(document(True))
        response=self.publish(workflow);receipt=response.json()['receipt']
        self.assertEqual(receipt['outcome'],'published')
        self.assertEqual(receipt['humanApproval'],{'revision':'1','deploymentVersionId':'deployment-1','blockIds':[native_id(workflow,'approval')],'ttlSeconds':604800})
        self.assertNotIn('externalTasks',receipt)
        self.assertEqual(self.publish(workflow).json(),response.json())
        self.assertEqual(self.remote.count('deploy'),1)
        catalog=PublishedWorkflowCatalog(self.api)
        self.assertEqual(catalog.bindings({'one'})[receipt['bindingId']]['humanApproval'],receipt['humanApproval'])
        self.assertEqual(catalog.bindings({'two'}),{})

    def test_enabled_capability_does_not_grant_publish_or_project_permission(self):
        self.api.approval_enabled=True
        workflow=self.owned(document(True))
        self.assertEqual(self.publish(workflow,token='edit').status_code,403)
        self.assertEqual(self.publish(workflow,token='other').status_code,404)
        self.assertEqual(self.remote.count('deploy'),0)

    def test_capability_requires_strict_server_boolean(self):
        from agent_line.authoring_api import AuthoringAPI
        for value in (1, 'true', None):
            with self.subTest(value=value),self.assertRaises(ValueError):
                AuthoringAPI(self.access,self.journal,{'one':self.remote},
                             projects=self.projects,approval_enabled=value)

    def test_mixed_allowlists_are_disjoint_and_fixed_roles_checked(self):
        self.api.approval_enabled=True
        workflow=self.owned(self.mixed())
        receipt=self.publish(workflow).json()['receipt']
        self.assertEqual(receipt['outcome'],'published')
        self.assertEqual(receipt['humanApproval']['blockIds'],[native_id(workflow,'approval')])
        self.assertEqual(set(receipt['externalTasks']['agentPolicies']),{native_id(workflow,'worker')})
        self.assertFalse(set(receipt['humanApproval']['blockIds']) & set(receipt['externalTasks']['agentPolicies']))
        self.api.role_bindings['role']['projectId']='two'
        self.assertEqual(self.publish(workflow,op='changed-role').json()['receipt']['outcome'],'role_binding_required')
        self.assertEqual(self.remote.count('deploy'),1)

    def test_changed_publication_cannot_produce_human_binding(self):
        self.api.approval_enabled=True
        workflow=self.owned(document(True))
        self.remote.version_override=copy.deepcopy(self.remote.states[workflow])
        key=next(k for k,v in self.remote.version_override['blocks'].items() if v['type']=='response')
        self.remote.version_override['blocks'][key]['enabled']=False
        result=self.publish(workflow).json()
        self.assertNotEqual((result.get('receipt') or {}).get('outcome'),'published')
        self.assertEqual(PublishedWorkflowCatalog(self.api).bindings({'one'}),{})

    def test_catalog_rejects_invalid_human_policy_and_machine_overlap(self):
        self.api.approval_enabled=True
        workflow=self.owned(self.mixed())
        receipt=self.publish(workflow).json()['receipt']
        original=copy.deepcopy(receipt)
        cases=[{'revision':'2'},{'deploymentVersionId':'other'},{'blockIds':[]},
               {'blockIds':[native_id(workflow,'approval')]*2},{'blockIds':[native_id(workflow,'worker')]},
               {'ttlSeconds':True},{'ttlSeconds':0},{'ttlSeconds':604801},{'extra':1}]
        for update in cases:
            changed=copy.deepcopy(original);changed['humanApproval'].update(update)
            with self.journal.connect() as db:
                db.execute("UPDATE operations SET receipt=? WHERE workflow=? AND context='author-publish'",(json.dumps(changed),namespace('one')))
            with self.subTest(update=update),self.assertRaises(ValueError):
                PublishedWorkflowCatalog(self.api).bindings({'one'})
