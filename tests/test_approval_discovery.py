from pathlib import Path
import tempfile
import unittest
from agent_line.approvals import ApprovalLedger
from agent_line.approval_discovery import WorkflowRunLedger, ApprovalDiscovery
from agent_line.store import Conflict
from unittest.mock import Mock


class Sim:
    def __init__(self):self.calls=[];self.status='paused';self.pause={'pauseKind':'human','pausePointCount':1,'blockedOnBlockId':'human','contextId':'context'};self.override={}
    def observe(self,w,r):
        self.calls.append((w,r))
        return {'response':{'data':{'workflowId':w,'runId':r,'status':self.status,'paused':self.pause,**self.override}}}


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        self.runs=WorkflowRunLedger.initialize(self.root/'runs.sqlite')
        self.approvals=ApprovalLedger.initialize(self.root/'approvals.sqlite')
        self.sim=Sim();self.now=1000
        self.verifier=Mock();self.verifier.verify_run.return_value=True
        self.discovery=ApprovalDiscovery(self.runs,self.approvals,self.sim,publication_verifier=self.verifier,batch_size=1,clock=lambda:self.now)
        self.reserve('run')
    def reserve(self,run,**kw):
        args=dict(project_id='project',workflow_id='workflow',run_id=run,revision='v1',deployment_version_id='published-id',human_block_ids=['human'],ttl_seconds=600);args.update(kw)
        return self.runs.reserve(**args)
    def test_missing_database_rejected_and_private_initialization(self):
        with self.assertRaises(FileNotFoundError):WorkflowRunLedger(self.root/'missing')
        self.assertEqual(self.runs.path.stat().st_mode&0o777,0o600)
    def test_frozen_snapshot_conflicts_and_replays(self):
        self.assertFalse(self.reserve('run'))
        for changes in ({'revision':'v2'},{'project_id':'other'},{'human_block_ids':['external']},{'ttl_seconds':700}):
            with self.assertRaises(Conflict):self.reserve('run',**changes)
    def test_real_pause_binding_deterministic_and_expiry_not_extended(self):
        first=self.discovery.tick()[0];self.assertEqual(first['status'],'bound')
        binding=self.approvals.get(first['approvalId'])['binding'];self.assertEqual(binding.expires_at,1600)
        self.now=1200
        restarted=ApprovalDiscovery(WorkflowRunLedger(self.runs.path),ApprovalLedger(self.approvals.path),self.sim,publication_verifier=self.verifier,clock=lambda:self.now)
        second=restarted.tick()[0]
        self.assertEqual(second['approvalId'],first['approvalId']);self.assertEqual(self.approvals.get(first['approvalId'])['binding'],binding)
    def test_unknown_external_multi_and_wrong_run_never_bind(self):
        for change in ({'pauseKind':'external_task_result'},{'pausePointCount':2},{'blockedOnBlockId':'external'},{'contextId':None}):
            old=self.sim.pause.copy();self.sim.pause.update(change)
            self.discovery.tick();self.sim.pause=old
        self.sim.override={'runId':'other'};self.discovery.tick()
        self.assertEqual(self.approvals.list_approvals({'project'})['approvals'],[])
    def test_reappearing_context_refused_without_activation_identity(self):
        first=self.discovery.tick()[0];self.sim.status='running';self.discovery.tick()
        self.sim.status='paused'
        self.assertEqual(self.discovery.tick()[0]['status'],'ambiguous_activation')
        self.assertEqual(len(self.approvals.list_approvals({'project'})['approvals']),1)
    def test_fair_pages_and_terminal_unknown_not_retired(self):
        self.reserve('run-two');self.discovery.tick();self.discovery.tick()
        self.assertEqual([r for w,r in self.sim.calls],['run','run-two'])
        self.sim.status='completed';self.discovery.tick();self.discovery.tick()
        self.assertEqual(len(self.runs.page(limit=10)),2)
        self.runs.record_submission('workflow','run','accepted');self.runs.record_submission('workflow','run-two','accepted')
        self.discovery.tick();self.discovery.tick()
        self.assertEqual(self.runs.page(limit=10),[])
    def test_failure_is_bounded_and_does_not_stop_other_runs(self):
        self.reserve('run-two')
        original=self.sim.observe
        def observe(w,r):
            if r=='run':raise RuntimeError('private remote token')
            return original(w,r)
        self.sim.observe=observe
        self.assertEqual(self.discovery.tick()[0]['status'],'unknown')
        self.assertEqual(self.discovery.tick()[0]['status'],'bound')
    def test_decided_context_never_mints_new_approval(self):
        first=self.discovery.tick()[0]
        self.approvals._record(first['approvalId'],{'decisionId':'d','decision':'approve'},1000)
        self.assertEqual(self.discovery.tick()[0]['status'],'ambiguous_activation')

    def test_crash_between_ledgers_replays_original_binding_and_ttl(self):
        original=self.approvals.bind
        self.approvals.bind=lambda binding:(_ for _ in ()).throw(RuntimeError('write unavailable'))
        self.assertEqual(self.discovery.tick()[0]['status'],'unknown')
        self.approvals.bind=original;self.now=1300
        result=self.discovery.tick()[0]
        self.assertEqual(self.approvals.get(result['approvalId'])['binding'].expires_at,1600)

    def test_get_snapshot_exposes_frozen_server_policy(self):
        record=self.runs.get('workflow','run')
        self.assertEqual(record['humanBlockIds'],['human']);self.assertEqual(record['submissionStatus'],'intent')
        self.assertFalse(record['complete'])
        with self.assertRaises(KeyError):self.runs.get('workflow','absent')

    def test_rejected_submission_state_never_overwrites_acceptance(self):
        self.runs.record_submission('workflow','run','accepted')
        with self.assertRaises(Conflict):self.runs.record_submission('workflow','run','uncertain')

    def test_external_pause_between_observations_invalidates_old_activation(self):
        self.discovery.tick();self.sim.pause['pauseKind']='external_task_result';self.discovery.tick()
        self.sim.pause['pauseKind']='human'
        self.assertEqual(self.discovery.tick()[0]['status'],'ambiguous_activation')

    def test_actual_publication_missing_or_mismatched_never_registers(self):
        self.verifier.verify_run.return_value=False
        result=self.discovery.tick()[0]
        self.assertEqual(result['status'],'version_unverified')
        record=self.runs.get('workflow','run')
        self.assertEqual(record['registrationStatus'],'version_unverified')
        self.assertEqual(record['registrationReason'],'run_publication_not_verified')
        self.assertEqual(self.approvals.list_approvals({'project'})['approvals'],[])
        snapshot=self.verifier.verify_run.call_args.args[0]
        self.assertEqual(snapshot['deploymentVersionId'],'published-id')
        self.verifier.verify_run.side_effect=RuntimeError('private upstream message')
        self.discovery.tick()
        self.assertNotIn('private',str(self.runs.get('workflow','run')))
    def test_deployment_version_is_frozen_and_verifier_mandatory(self):
        with self.assertRaises(Conflict):self.reserve('run',deployment_version_id='changed')
        with self.assertRaises(ValueError):ApprovalDiscovery(self.runs,self.approvals,self.sim,publication_verifier=None)

    def test_sequential_human_pauses_use_total_minus_resumed_counts(self):
        self.reserve('sequential',human_block_ids=['human','second'])
        self.discovery.batch_size=50
        self.discovery.tick()
        self.sim.pause.update(blockedOnBlockId='second',contextId='context-two',pausePointCount=2,resumedCount=1)
        result=self.discovery.tick()
        self.assertEqual(next(r for r in result if r['runId']=='sequential')['status'],'bound')
        self.assertEqual(len(self.approvals.list_approvals({'project'})['approvals']),3)
        self.verifier.verify_run.assert_called()

    def test_multiple_or_no_active_pauses_and_boolean_counts_rejected(self):
        for points,resumed in [(2,0),(2,2),(True,0),(1,False),(2,None),(0,-1),(3,1)]:
            with self.subTest(points=points,resumed=resumed):
                self.sim.pause.update(pausePointCount=points,resumedCount=resumed)
                self.assertEqual(self.discovery.tick()[0]['status'],'not_allowed_human_pause')
        self.assertEqual(self.approvals.list_approvals({'project'})['approvals'],[])
