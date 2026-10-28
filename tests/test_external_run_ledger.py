import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from agent_line.approval_discovery import WorkflowRunLedger, ApprovalDiscovery
from agent_line.approvals import ApprovalLedger
from agent_line.store import Conflict

class ExternalRunLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.ledger=WorkflowRunLedger.initialize(self.root/'runs.sqlite')
        self.policy={'roleBindingId':'binding','roleAgentId':'role','instruction':'完成当前任务','memoryPolicyId':'native','outputSchemaId':'result','requireMemoryCommit':False,'allowFailureBranch':False}
        self.args=dict(project_id='project',workflow_id='workflow',run_id='run',revision='1',deployment_version_id='deployment',agent_policies={'wait':self.policy})

    def reserve(self,**changes):
        return self.ledger.reserve_tasks(**(self.args|changes))

    def human(self,run='run'):
        return self.ledger.reserve(project_id='project',workflow_id='workflow',run_id=run,revision='1',deployment_version_id='deployment',human_block_ids=['human'],ttl_seconds=600)

    def test_restart_immutable_snapshot_and_caller_mutation(self):
        self.assertTrue(self.reserve());self.assertFalse(self.reserve())
        original=WorkflowRunLedger(self.ledger.path).get('workflow','run')
        self.assertEqual(original['purpose'],'external_task_result')
        self.assertEqual(original['humanBlockIds'],['wait'])
        self.assertEqual(original['ttlSeconds'],604800)
        self.policy['instruction']='changed'
        self.assertEqual(self.ledger.get('workflow','run'),original)
        with self.assertRaises(Conflict):self.reserve()
        original['agentPolicies']['wait']['instruction']='local edit'
        self.assertEqual(self.ledger.get('workflow','run')['agentPolicies']['wait']['instruction'],'完成当前任务')

    def test_each_snapshot_identity_change_conflicts(self):
        self.reserve()
        for changes in ({'project_id':'different'},{'revision':'2'},{'deployment_version_id':'other'},{'ttl_seconds':500},{'agent_policies':{'other':self.policy}}):
            with self.subTest(changes=changes),self.assertRaises(Conflict):self.reserve(**changes)

    def test_human_and_external_cannot_take_over_same_run(self):
        self.human()
        with self.assertRaises(Conflict):self.reserve()
        self.reserve(run_id='external')
        with self.assertRaises(Conflict):self.human('external')

    def test_invalid_policies_and_fields(self):
        for policies in ({},[],None,{'wait':None},{'bad/path':self.policy},{str(i):self.policy for i in range(201)}):
            with self.subTest(policies=type(policies)),self.assertRaises(ValueError):self.reserve(agent_policies=policies)
        for field,value in [('instruction',' '),('instruction','x'*4001),('roleAgentId','../role'),('roleBindingId',''),('memoryPolicyId',False),('outputSchemaId','x'*161),('requireMemoryCommit',1),('allowFailureBranch','false')]:
            policy=copy.deepcopy(self.policy);policy[field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):self.reserve(agent_policies={'wait':policy})
        for policy in (self.policy|{'extra':1},{k:v for k,v in self.policy.items() if k!='instruction'}):
            with self.assertRaises(ValueError):self.reserve(agent_policies={'wait':policy})

    def test_invalid_publication_identity_and_ttl(self):
        for revision in (True,0,-1,1.5,'0','-1','latest','v1','1.0','01',None):
            with self.subTest(revision=revision),self.assertRaises(ValueError):self.reserve(revision=revision)
        for ttl in (True,0,-1,1.5,float('inf'),float('nan'),31536001,'10'):
            with self.subTest(ttl=ttl),self.assertRaises(ValueError):self.reserve(ttl_seconds=ttl)
        for field in ('project_id','workflow_id','run_id','deployment_version_id'):
            with self.assertRaises(ValueError):self.reserve(**{field:''})

    def test_integer_revision_normalizes_to_existing_string_contract(self):
        self.assertTrue(self.reserve(revision=1));self.assertFalse(self.reserve(revision='1'))
        self.assertEqual(self.ledger.get('workflow','run')['revision'],'1')

    def test_external_diagnostics_are_bounded_and_preserved_by_human_discovery(self):
        self.reserve();self.human('human-run')
        run=self.ledger.get('workflow','run')
        for suffix in ('bound','input_unavailable','role_unavailable','admission_closed','conflict','invalid'):
            self.ledger.record_registration(run,'external_task_'+suffix)
        with self.assertRaises(ValueError):self.ledger.record_registration(run,'external_task_secret')
        original=self.ledger.get('workflow','run')
        approvals=ApprovalLedger.initialize(self.root/'approvals.sqlite')
        client=Mock()
        client.observe.return_value={'response':{'data':{'workflowId':'workflow','runId':'human-run','status':'paused','paused':{'contextId':'ctx','blockedOnBlockId':'human','pauseKind':'human','pausePointCount':1}}}}
        verifier=Mock();verifier.verify_run.return_value=True
        discovery=ApprovalDiscovery(self.ledger,approvals,client,publication_verifier=verifier,batch_size=1)
        self.assertEqual(discovery.tick(),[])
        client.observe.assert_not_called();verifier.verify_run.assert_not_called()
        self.assertEqual(discovery.tick()[0]['status'],'bound')
        client.observe.assert_called_once_with('workflow','human-run')
        self.assertEqual(discovery.tick(),[])
        self.assertEqual(self.ledger.get('workflow','run'),original)
        self.assertEqual(len(approvals.list_approvals({'project'})['approvals']),1)

    def test_missing_database_not_recreated(self):
        self.ledger.path.unlink()
        with self.assertRaises(Exception):self.reserve()
        self.assertFalse(self.ledger.path.exists())

    def test_concurrent_reservation_has_one_winner_without_new_schema(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:self.reserve(),range(8)))
        self.assertEqual(results.count(True),1)
        with self.ledger.connect() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],1)
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(tables,{'runs','pauses'})

    def test_policy_order_does_not_change_snapshot_but_any_value_does(self):
        policies={'wait-b':dict(self.policy),'wait-a':dict(self.policy)}
        self.assertTrue(self.reserve(agent_policies=policies))
        self.assertFalse(self.reserve(agent_policies=dict(reversed(list(policies.items())))))
        self.assertEqual(self.ledger.get('workflow','run')['humanBlockIds'],['wait-a','wait-b'])
        for field,value in [('instruction','another'),('roleBindingId','other'),('roleAgentId','other'),('memoryPolicyId','other'),('outputSchemaId','other'),('requireMemoryCommit',True),('allowFailureBranch',True)]:
            changed=copy.deepcopy(policies);changed['wait-a'][field]=value
            with self.subTest(field=field),self.assertRaises(Conflict):self.reserve(agent_policies=changed)
