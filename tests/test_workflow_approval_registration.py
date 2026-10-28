"""HTTP protection with real durable stores; only the upstream transport is fake."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from fastapi.testclient import TestClient
from agent_line.adapters.sim import SubmissionUncertain
from agent_line.approval_discovery import ApprovalDiscovery, WorkflowRunLedger
from agent_line.approvals import ApprovalLedger
from agent_line.publication import PublicationUnverified
from agent_line.service import create_app, ServiceConfig
from agent_line.sim_journal import SimJournal
from agent_line.store import TaskStore
from agent_line.workflows import WorkflowGateway


class WorkflowApprovalRegistrationTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.runs=WorkflowRunLedger.initialize(self.root/'runs.sqlite')
        self.journal=SimJournal.initialize(self.root/'sim.sqlite')
        self.approvals=ApprovalLedger.initialize(self.root/'approvals.sqlite')
        self.store=TaskStore.initialize(self.root/'tasks.sqlite')
        self.sim=Mock();self.verifier=Mock()
        self.verifier.check_definition.return_value=True;self.verifier.verify_run.return_value=True
        self.submitted_snapshots=[]
        def execute(workflow,run,inputs):
            # Proves the snapshot has committed before any upstream write.
            snapshot=WorkflowRunLedger(self.runs.path).get(workflow,run)
            self.submitted_snapshots.append(snapshot)
            return {'runId':run,'transportStatus':'accepted'}
        self.sim.execute.side_effect=execute
        self.sim.observe.side_effect=lambda w,r:{'runId':r,'status':'running'}
        self.policy={'revision':'1','deploymentVersionId':'version-id','blockIds':['human-node'],'ttlSeconds':3600}
        self.bindings={'flow':{'projectId':'project','workflowId':'workflow','humanApproval':self.policy}}
        self.config=ServiceConfig(tokens={'service-key':{'project'}},projects={'project':{'workspace':str(self.root)}},bindings={})
        self.headers={'Authorization':'Bearer service-key'}
        self.gateway,self.client=self.make_client()
    def make_client(self,bindings=None,discovery=True):
        gateway=WorkflowGateway(self.sim,self.journal,bindings if bindings is not None else self.bindings,installation_id='fixed-installation')
        if discovery:
            gateway.approval_discovery=ApprovalDiscovery(self.runs,self.approvals,self.sim,publication_verifier=self.verifier)
            gateway.publication_verifier=self.verifier
        client=TestClient(create_app(self.store,self.config,workflows=gateway),raise_server_exceptions=False)
        self.addCleanup(client.close)
        return gateway,client
    def start(self,client=None,inputs=None):
        return (client or self.client).post('/extension/v1/workflows/flow/runs',headers=self.headers,json={'idempotencyKey':'fixed-trigger','input':inputs or {'work':'item'}})
    def resume(self,run,client=None):
        return (client or self.client).post('/extension/v1/workflows/flow/runs/'+run+'/resume',headers=self.headers,json={'contextId':'human-node','input':{'approval':{'approved':True}}})
    def test_protected_start_reserves_before_execute_and_records_accepted(self):
        response=self.start();self.assertEqual(response.status_code,202,response.text)
        record=self.runs.get('workflow',response.json()['runId'])
        self.assertEqual(record['submissionStatus'],'accepted')
        self.assertEqual(record['humanBlockIds'],['human-node']);self.assertEqual(record['deploymentVersionId'],'version-id')
        self.assertEqual(self.submitted_snapshots[0]['submissionStatus'],'intent')
        self.verifier.check_definition.assert_called_once_with('workflow','1','version-id',['human-node'],require_active=True)
    def test_missing_discovery_fails_before_any_submission(self):
        _,client=self.make_client(discovery=False)
        response=self.start(client);self.assertEqual(response.status_code,503)
        self.sim.execute.assert_not_called();self.assertEqual(self.runs.page(),[])
    def test_unverified_publication_fails_closed(self):
        self.verifier.check_definition.side_effect=PublicationUnverified('unavailable')
        self.assertEqual(self.start().status_code,503)
        self.sim.execute.assert_not_called();self.assertEqual(self.runs.page(),[])
    def test_repeated_start_after_restart_never_resubmits_or_reinterprets_config(self):
        first=self.start();self.assertEqual(first.status_code,202,first.text)
        self.journal=SimJournal(self.journal.path);self.runs=WorkflowRunLedger(self.runs.path)
        changed=copy.deepcopy(self.bindings);changed['flow']['humanApproval']['revision']='2'
        _,client=self.make_client(changed)
        self.verifier.check_definition.side_effect=PublicationUnverified('new-version-not-authorized')
        second=self.start(client);self.assertEqual(second.status_code,202,second.text)
        self.assertEqual(second.json()['runId'],first.json()['runId'])
        self.assertEqual(self.runs.get('workflow',first.json()['runId'])['revision'],'1')
        self.sim.execute.assert_called_once()
    def test_ordinary_resume_denied_by_policy(self):
        started=self.start();self.assertEqual(started.status_code,202,started.text)
        self.assertEqual(self.resume(started.json()['runId']).status_code,403)
        self.sim.resume.assert_not_called()
    def test_removing_policy_does_not_remove_persisted_protection(self):
        started=self.start();self.assertEqual(started.status_code,202,started.text)
        _,client=self.make_client({'flow':{'projectId':'project','workflowId':'workflow'}})
        self.assertEqual(self.resume(started.json()['runId'],client).status_code,403)
        self.sim.resume.assert_not_called()
    def test_changed_input_returns_conflict_without_second_write(self):
        first=self.start();self.assertEqual(first.status_code,202,first.text)
        self.assertEqual(self.start(inputs={'work':'changed'}).status_code,409)
        self.sim.execute.assert_called_once()
    def test_uncertain_start_retains_snapshot_and_never_reposts(self):
        def lost(w,r,inputs):
            self.runs.get(w,r)
            raise SubmissionUncertain(r)
        self.sim.execute.side_effect=lost
        first=self.start();self.assertEqual(first.status_code,202,first.text)
        self.assertEqual(self.runs.get('workflow',first.json()['runId'])['submissionStatus'],'uncertain')
        self.assertEqual(self.start().status_code,202);self.sim.execute.assert_called_once()
