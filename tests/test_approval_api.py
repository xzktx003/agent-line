from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from fastapi.testclient import TestClient
from agent_line.approvals import ApprovalBinding, ApprovalLedger, ApprovalCoordinator, TrustedPrincipal
from agent_line.approval_api import ApprovalAccess, ApprovalAPI
from agent_line.service import create_app, ServiceConfig
from agent_line.store import TaskStore
from agent_line.sim_journal import SimJournal
from agent_line.adapters.sim import SubmissionUncertain


class Sim:
    def __init__(self): self.posts=[]; self.uncertain=False; self.before_observe=lambda:None
    def observe(self,w,r):
        self.before_observe()
        return {'response':{'data':{'workflowId':w,'runId':r,'status':'paused','paused':{'contextId':'ctx','blockedOnBlockId':'block','pauseKind':'human','pausePointCount':1}}}}
    def resume(self,w,r,c,data):
        self.posts.append(data)
        if self.uncertain: raise SubmissionUncertain(r,c)
        return {'runId':r,'contextId':c,'transportStatus':'accepted'}


class ApprovalAPITests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        self.ledger=ApprovalLedger.initialize(self.root/'approvals.sqlite')
        self.binding=ApprovalBinding('approval','project','workflow','run','block','ctx','revision','activation','attempt','human_approval',2000)
        self.ledger.bind(self.binding)
        self.principal=TrustedPrincipal('human',frozenset({'project'}),frozenset({'approve'}),2000)
        self.access=ApprovalAccess({'human-token':self.principal,'other-token':replace(self.principal,subject='other',project_scopes=frozenset({'other'})), 'read-token':replace(self.principal,subject='reader',capabilities=frozenset()), 'expired-token':replace(self.principal,subject='expired',expires_at=900)},clock=lambda:1000)
        self.sim=Sim();self.journal=SimJournal.initialize(self.root/'sim.sqlite')
        self.coordinator=ApprovalCoordinator(self.ledger,self.sim,self.journal,authorize=self.access.authorize,clock=lambda:1000)
        self.api=ApprovalAPI(self.access,self.coordinator)
        self.store=TaskStore.initialize(self.root/'tasks.sqlite')
        self.config=ServiceConfig(tokens={'service-token':{'project'}},projects={'project':{'workspace':str(self.root)}},bindings={})
        self.client=TestClient(create_app(self.store,self.config,approvals=self.api));self.addCleanup(self.client.close)
    def request(self,method='get',path='',token='human-token',**kw):
        return self.client.request(method,'/extension/v1/approvals'+path,headers={'Authorization':'Bearer '+token},**kw)
    def decision(self,**kw): return self.request('post','/approval/decision',json={'decisionId':'decision','decision':'reject'},**kw)
    def test_separate_credentials_and_permissions(self):
        for token,expected in [('bad',401),('service-token',401),('read-token',403),('expired-token',403)]:
            self.assertEqual(self.request(token=token).status_code,expected)
        self.assertEqual(self.sim.posts,[])
    def test_cross_project_hidden_and_scoped_page(self):
        self.ledger.bind(replace(self.binding,approval_id='a-other',project_id='other',run_id='other-run'))
        self.ledger.bind(replace(self.binding,approval_id='b-second',run_id='second-run'))
        page=self.request(params={'limit':1})
        self.assertEqual(page.status_code,200);data=page.json()
        self.assertEqual([x['approvalId'] for x in data['approvals']],['approval'])
        second=self.request(params={'limit':1,'after':data['nextCursor']}).json()
        self.assertEqual([x['approvalId'] for x in second['approvals']],['b-second']);self.assertIsNone(second['nextCursor'])
        self.assertEqual(self.request(path='/approval',token='other-token').status_code,404)
        self.assertEqual(self.decision(token='other-token').status_code,404)
    def test_reject_is_explicit_and_idempotent(self):
        result=self.decision();self.assertEqual(result.status_code,200)
        self.assertFalse(result.json()['decision']['approved']);self.assertEqual(result.json()['decision']['decidedBy'],'human')
        self.assertEqual(self.decision().status_code,200);self.assertEqual(len(self.sim.posts),1)
        conflict=self.request('post','/approval/decision',json={'decisionId':'different','decision':'approve'})
        self.assertEqual(conflict.status_code,409)
    def test_forbidden_actor_and_binding_fields(self):
        for field in ('actor','principal','projectId','contextId'):
            response=self.request('post','/approval/decision',json={'decisionId':'d','decision':'approve',field:'injected'})
            self.assertEqual(response.status_code,422)
        self.assertEqual(self.sim.posts,[])
    def test_unknown_delivery_is_observed_never_reposted(self):
        self.sim.uncertain=True
        self.assertEqual(self.decision().json()['deliveryStatus'],'uncertain')
        self.assertEqual(self.request(path='/approval').json()['deliveryStatus'],'uncertain')
        self.assertEqual(self.decision().json()['deliveryStatus'],'uncertain')
        self.assertEqual(len(self.sim.posts),1)
    def test_live_revocation_during_observation_prevents_decision(self):
        self.sim.before_observe=lambda:self.access.replace_credentials({})
        self.assertEqual(self.decision().status_code,403)
        self.assertIsNone(self.ledger.get('approval')['decision']);self.assertEqual(self.sim.posts,[])
    def test_token_overlap_rejects_startup(self):
        config=replace(self.config,tokens={'human-token':{'project'}})
        with self.assertRaises(ValueError):create_app(self.store,config,approvals=self.api)
    def test_upstream_error_is_redacted(self):
        def broken(*args): raise RuntimeError('secret-token/private/path')
        self.sim.observe=broken
        response=self.request(path='/approval')
        self.assertEqual(response.status_code,503);self.assertNotIn('secret-token',response.text)

    def test_live_revoke_and_replacement_cannot_grant_service_credential(self):
        self.access.revoke('human-token')
        self.assertEqual(self.request().status_code,401)
        with self.assertRaises(ValueError):self.access.replace_credentials({'service-token':self.principal})
        self.assertEqual(self.request(token='service-token').status_code,401)

    def test_expired_binding_is_readable_but_cannot_resume(self):
        self.ledger.bind(replace(self.binding,approval_id='expired',run_id='expired-run',expires_at=900))
        self.assertEqual(self.request(path='/expired').json()['status'],'expired')
        result=self.request('post','/expired/decision',json={'decisionId':'d','decision':'approve'})
        self.assertEqual(result.status_code,403);self.assertEqual(self.sim.posts,[])

    def test_page_limits_and_invalid_decision_are_rejected(self):
        for limit in (0,501):self.assertEqual(self.request(params={'limit':limit}).status_code,422)
        self.assertEqual(self.request('post','/approval/decision',json={'decisionId':'d','decision':'maybe'}).status_code,422)
        self.assertEqual(self.request('post','/approval/decision',json={'decisionId':'../escape','decision':'approve'}).status_code,422)
        self.assertEqual(self.sim.posts,[])

    def test_detail_never_exposes_raw_delivery_receipts(self):
        self.decision()
        detail=self.request(path='/approval').json()
        self.assertNotIn('delivery',detail);self.assertNotIn('receipt',detail)
        self.assertEqual(detail['projectId'],'project');self.assertEqual(detail['deliveryStatus'],'accepted')
