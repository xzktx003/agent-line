"""Maintenance admission exercised via HTTP with real SQLite journals and locks."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from fastapi.testclient import TestClient
from agent_line.approvals import ApprovalBinding, ApprovalCoordinator, ApprovalLedger, TrustedPrincipal
from agent_line.approval_api import ApprovalAccess, ApprovalAPI
from agent_line.approval_discovery import ApprovalDiscovery, WorkflowRunLedger
from agent_line.service import create_app, ServiceConfig
from agent_line.sim_journal import SimJournal
from agent_line.store import TaskStore, Conflict
from agent_line.workflows import WorkflowGateway


class MaintenanceRouteTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.store=TaskStore.initialize(self.root/'tasks.sqlite')
        self.journal=SimJournal.initialize(self.root/'sim.sqlite')
        self.ledger=ApprovalLedger.initialize(self.root/'approvals.sqlite')
        self.runs=WorkflowRunLedger.initialize(self.root/'runs.sqlite')
        self.sim=Mock()
        self.sim.execute.side_effect=lambda w,r,data:{'runId':r,'transportStatus':'accepted'}
        self.sim.resume.side_effect=lambda w,r,c,data:{'runId':r,'contextId':c,'transportStatus':'accepted'}
        self.sim.observe.side_effect=lambda w,r:{'response':{'data':{'workflowId':w,'runId':r,'status':'paused','paused':{'contextId':'context','blockedOnBlockId':'human','pauseKind':'human','pausePointCount':1}}}}
        self.binding=ApprovalBinding('approval','project','human-workflow','human-run','human','context','1','activation','one','human_approval',2000)
        self.ledger.bind(self.binding)
        self.access=ApprovalAccess({'human-token':TrustedPrincipal('reviewer',frozenset({'project'}),frozenset({'approve'}),2000)},clock=lambda:1000)
        self.approvals=ApprovalAPI(self.access,ApprovalCoordinator(self.ledger,self.sim,self.journal,authorize=self.access.authorize,clock=lambda:1000))
        self.gateway=WorkflowGateway(self.sim,self.journal,{'flow':{'projectId':'project','workflowId':'workflow'},'protected':{'projectId':'project','workflowId':'protected-workflow','humanApproval':{'revision':'1','deploymentVersionId':'publication','blockIds':['human'],'ttlSeconds':3600}}},installation_id='installation')
        self.verifier=Mock();self.verifier.check_definition.return_value=True;self.verifier.verify_run.return_value=True
        self.gateway.publication_verifier=self.verifier
        self.gateway.approval_discovery=ApprovalDiscovery(self.runs,self.ledger,self.sim,publication_verifier=self.verifier)
        config=ServiceConfig(tokens={'service-token':{'project'}},projects={'project':{'workspace':str(self.root)}},bindings={})
        self.client=TestClient(create_app(self.store,config,workflows=self.gateway,approvals=self.approvals),raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.service={'Authorization':'Bearer service-token'};self.human={'Authorization':'Bearer human-token'}
    def start(self,key='trigger',binding='flow'):
        return self.client.post('/extension/v1/workflows/'+binding+'/runs',headers=self.service,json={'idempotencyKey':key,'input':{'task':'fixture'}})
    def resume(self,run,context='context'):
        return self.client.post('/extension/v1/workflows/flow/runs/'+run+'/resume',headers=self.service,json={'contextId':context,'input':{'value':True}})
    def decide(self):
        return self.client.post('/extension/v1/approvals/approval/decision',headers=self.human,json={'decisionId':'decision','decision':'approve'})
    def intent_count(self):
        with self.journal.connect() as db:return db.execute('SELECT count(*) FROM operations').fetchone()[0]
    def test_new_workflow_start_is_blocked_without_intent(self):
        owner=self.store.enter_maintenance()
        response=self.start();self.assertEqual(response.status_code,503,response.text)
        self.assertEqual(self.intent_count(),0);self.sim.execute.assert_not_called()
        self.store.leave_maintenance(owner)
        self.assertEqual(self.start().status_code,202);self.sim.execute.assert_called_once()
    def test_protected_start_is_blocked_before_preflight_or_run_reservation(self):
        self.store.enter_maintenance()
        response=self.start(binding='protected');self.assertEqual(response.status_code,503,response.text)
        self.assertEqual(self.runs.page(),[]);self.assertEqual(self.intent_count(),0)
        self.verifier.check_definition.assert_not_called();self.sim.execute.assert_not_called()
    def test_new_resume_is_blocked_but_existing_start_replays(self):
        first=self.start();self.assertEqual(first.status_code,202,first.text);run=first.json()['runId']
        owner=self.store.enter_maintenance()
        replay=self.start();self.assertEqual(replay.status_code,202,replay.text)
        self.assertEqual(replay.json()['runId'],run);self.sim.execute.assert_called_once()
        response=self.resume(run);self.assertEqual(response.status_code,503,response.text)
        self.assertEqual(self.intent_count(),1);self.sim.resume.assert_not_called()
        self.store.leave_maintenance(owner)
        self.assertEqual(self.resume(run).status_code,202);self.sim.resume.assert_called_once()
    def test_existing_resume_replays_during_maintenance_without_second_write(self):
        run=self.start().json()['runId'];self.assertEqual(self.resume(run).status_code,202)
        self.store.enter_maintenance()
        replay=self.resume(run);self.assertEqual(replay.status_code,202,replay.text)
        self.sim.resume.assert_called_once();self.assertEqual(self.intent_count(),2)
        self.assertEqual(self.resume(run,'new-context').status_code,503)
        self.sim.resume.assert_called_once();self.assertEqual(self.intent_count(),2)
    def test_new_human_decision_maintenance_leaves_no_decision_or_intent(self):
        owner=self.store.enter_maintenance()
        response=self.decide();self.assertEqual(response.status_code,503,response.text)
        self.assertIsNone(self.ledger.get('approval')['decision'])
        self.assertEqual(self.intent_count(),0);self.sim.resume.assert_not_called()
        with self.assertRaises(Conflict):self.store.leave_maintenance('wrong-owner')
        self.assertEqual(self.decide().status_code,503)
        self.store.leave_maintenance(owner)
        result=self.decide();self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['deliveryStatus'],'accepted');self.sim.resume.assert_called_once()
    def test_existing_human_decision_replays_and_queries_during_maintenance(self):
        initial=self.decide();self.assertEqual(initial.status_code,200,initial.text)
        original=self.ledger.get('approval');self.store.enter_maintenance()
        replay=self.decide();self.assertEqual(replay.status_code,200,replay.text)
        self.assertEqual(replay.json()['decision'],initial.json()['decision'])
        detail=self.client.get('/extension/v1/approvals/approval',headers=self.human)
        listing=self.client.get('/extension/v1/approvals',headers=self.human)
        self.assertEqual(detail.status_code,200);self.assertEqual(listing.status_code,200)
        self.assertEqual(detail.json()['deliveryStatus'],'accepted')
        self.assertEqual(self.ledger.get('approval'),original)
        self.sim.resume.assert_called_once();self.assertEqual(self.intent_count(),1)
    def test_persisted_guard_blocks_after_store_and_app_reconstruction(self):
        self.store.enter_maintenance()
        reopened=TaskStore(self.store.path)
        config=ServiceConfig(tokens={'service-token':{'project'}},projects={'project':{'workspace':str(self.root)}},bindings={})
        with TestClient(create_app(reopened,config,workflows=self.gateway,approvals=self.approvals),raise_server_exceptions=False) as client:
            result=client.post('/extension/v1/approvals/approval/decision',headers=self.human,json={'decisionId':'decision','decision':'approve'})
            self.assertEqual(result.status_code,503,result.text)
        self.assertIsNone(self.ledger.get('approval')['decision']);self.assertEqual(self.intent_count(),0)
        self.sim.resume.assert_not_called()
