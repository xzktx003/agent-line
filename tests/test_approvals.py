from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest

from agent_line.approvals import ApprovalBinding, ApprovalLedger, ApprovalCoordinator, TrustedPrincipal
from agent_line.sim_journal import SimJournal
from agent_line.adapters.sim import SubmissionUncertain
from agent_line.store import Conflict


class Sim:
    def __init__(self):self.posts=[];self.gets=0;self.status='paused';self.pause={'contextId':'ctx','blockedOnBlockId':'block','pauseKind':'human','pausePointCount':1};self.uncertain=False
    def observe(self,w,r):
        self.gets+=1
        return {'status':self.status,'response':{'data':{'workflowId':w,'runId':r,'status':self.status,'paused':self.pause}}}
    def resume(self,w,r,c,data):
        self.posts.append(data)
        if self.uncertain:raise SubmissionUncertain(r,c)
        return {'runId':r,'contextId':c,'transportStatus':'accepted'}


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.ledger=ApprovalLedger.initialize(self.root/'approval.sqlite');self.journal=SimJournal.initialize(self.root/'sim.sqlite')
        self.binding=ApprovalBinding('approval','project','workflow','run','block','ctx','published-1','activation-1','attempt-1','human_approval',2000)
        self.ledger.bind(self.binding);self.principal=TrustedPrincipal('operator',frozenset({'project'}),frozenset({'approve'}),2000)
        self.sim=Sim();self.allowed=True
        self.coordinator=ApprovalCoordinator(self.ledger,self.sim,self.journal,authorize=lambda p:self.allowed,clock=lambda:1000)
    def decide(self,choice='approve',decision='decision'):
        return self.coordinator.decide('approval',decision_id=decision,decision=choice,principal=self.principal)
    def test_missing_database_not_silently_created(self):
        path=self.root/'missing'
        with self.assertRaises(FileNotFoundError):ApprovalLedger(path)
        self.assertFalse(path.exists())
    def test_approve_and_reject_have_distinct_explicit_contract(self):
        result=self.decide('reject')
        self.assertEqual(result['deliveryStatus'],'accepted')
        payload=self.sim.posts[0]['approval']
        self.assertEqual(payload['decision'],'reject');self.assertIs(payload['approved'],False)
        self.assertEqual(payload['decidedBy'],'operator');self.assertEqual(payload['revision'],'published-1')
    def test_concurrent_identical_decisions_resume_once(self):
        with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(lambda _:self.decide(),range(4)))
        self.assertEqual(len(self.sim.posts),1)
        self.assertEqual(self.ledger.get('approval')['decision']['decision'],'approve')
    def test_identical_decision_committed_between_audit_read_and_journal_read(self):
        audit_read = threading.Event(); winner_done = threading.Event()
        original = self.ledger.get
        def stale_read(approval_id):
            record = original(approval_id)
            if not audit_read.is_set():
                self.assertIsNone(record['decision'])
                audit_read.set()
                self.assertTrue(winner_done.wait(5), 'Competing decision did not finish')
            return record
        self.ledger.get = stale_read
        winner = ApprovalCoordinator(ApprovalLedger(self.root/'approval.sqlite'), self.sim,
                                     SimJournal(self.root/'sim.sqlite'), authorize=lambda p: True, clock=lambda: 1000)
        with ThreadPoolExecutor(max_workers=1) as pool:
            loser = pool.submit(self.decide)
            try:
                self.assertTrue(audit_read.wait(5), 'First audit read did not complete')
                result = winner.decide('approval', decision_id='decision', decision='approve', principal=self.principal)
                self.assertEqual(result['deliveryStatus'], 'accepted')
            finally:
                winner_done.set()
            self.assertEqual(loser.result(timeout=5)['deliveryStatus'], 'accepted')
        self.assertEqual(len(self.sim.posts), 1)

    def test_existing_outbound_without_human_decision_still_conflicts(self):
        self.journal.resume(self.sim, 'workflow', 'run', 'ctx', {'unrelated': 'operation'})
        with self.assertRaises(Conflict):
            self.decide()
        self.assertIsNone(self.ledger.get('approval')['decision'])
        self.assertEqual(len(self.sim.posts), 1)

    def test_conflicting_decision_committed_after_initial_audit_read_is_rejected(self):
        original = self.coordinator._journal
        winner = ApprovalCoordinator(ApprovalLedger(self.root/'approval.sqlite'), self.sim,
                                     SimJournal(self.root/'sim.sqlite'), authorize=lambda p: True, clock=lambda: 1000)
        def journal_after_winner(binding):
            winner.decide('approval', decision_id='opposite', decision='reject', principal=self.principal)
            return original(binding)
        self.coordinator._journal = journal_after_winner
        with self.assertRaises(Conflict):
            self.decide()
        self.assertEqual(self.ledger.get('approval')['decision']['decision'], 'reject')
        self.assertEqual(len(self.sim.posts), 1)
    def test_changed_decision_or_id_or_actor_conflicts(self):
        self.decide()
        with self.assertRaises(Conflict):self.decide('reject')
        with self.assertRaises(Conflict):self.decide(decision='other')
        with self.assertRaises(Conflict):self.coordinator.decide('approval',decision_id='decision',decision='approve',principal=replace(self.principal,subject='other'))
        self.assertEqual(len(self.sim.posts),1)
    def test_unauthorized_expired_and_revoked_have_no_side_effects(self):
        for principal in [replace(self.principal,project_scopes=frozenset()),replace(self.principal,capabilities=frozenset()),replace(self.principal,expires_at=900)]:
            with self.assertRaises(PermissionError):self.coordinator.decide('approval',decision_id='decision',decision='approve',principal=principal)
        self.allowed=False
        with self.assertRaises(PermissionError):self.decide()
        self.assertEqual(self.sim.gets,0);self.assertEqual(self.sim.posts,[]);self.assertIsNone(self.ledger.get('approval')['decision'])
    def test_expired_gate_or_external_task_purpose_rejected(self):
        with self.assertRaises(ValueError):self.ledger.bind(replace(self.binding,approval_id='bad',purpose='external_task_result'))
        coordinator=ApprovalCoordinator(self.ledger,self.sim,self.journal,authorize=lambda p:True,clock=lambda:2100)
        with self.assertRaises(PermissionError):coordinator.decide('approval',decision_id='d',decision='approve',principal=replace(self.principal,expires_at=3000))
        self.assertEqual(self.sim.posts,[])
    def test_wrong_pause_identity_count_kind_do_not_record_or_resume(self):
        for change in [{'contextId':'old'},{'blockedOnBlockId':'other'},{'pauseKind':'external_task_result'},{'pausePointCount':2}]:
            original=self.sim.pause.copy();self.sim.pause.update(change)
            self.assertEqual(self.decide()['status'],'waiting');self.sim.pause=original
        self.assertIsNone(self.ledger.get('approval')['decision']);self.assertEqual(self.sim.posts,[])
    def test_revision_and_attempt_rebinding_refused(self):
        with self.assertRaises(Conflict):self.ledger.bind(replace(self.binding,revision='new'))
        with self.assertRaises(Conflict):self.ledger.bind(replace(self.binding,approval_id='new',attempt_id='attempt-2'))
    def test_uncertain_delivery_after_restart_is_never_reposted(self):
        self.sim.uncertain=True;self.assertEqual(self.decide()['deliveryStatus'],'uncertain')
        self.sim.status='completed'
        coordinator=ApprovalCoordinator(ApprovalLedger(self.root/'approval.sqlite'),self.sim,SimJournal(self.root/'sim.sqlite'),authorize=lambda p:True,clock=lambda:1000)
        result=coordinator.decide('approval',decision_id='decision',decision='approve',principal=self.principal)
        self.assertEqual(result['deliveryStatus'],'uncertain');self.assertEqual(result['runStatus'],'completed');self.assertEqual(len(self.sim.posts),1)
    def test_crash_after_decision_before_journal_requires_same_live_pause(self):
        original=self.journal.resume
        self.journal.resume=lambda *args:(_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):self.decide()
        self.journal.resume=original;self.sim.pause['contextId']='new-context'
        self.assertEqual(self.decide()['status'],'waiting');self.assertEqual(self.sim.posts,[])
    def test_no_actor_request_field_or_invalid_choice(self):
        with self.assertRaises(TypeError):self.coordinator.decide('approval',decision_id='d',decision='approve',principal=self.principal,actor='model')
        with self.assertRaises(ValueError):self.decide('approved')
        self.assertEqual(self.sim.posts,[])
    def test_revocation_during_pause_observation_records_nothing(self):
        original=self.sim.observe
        def observe(w,r):
            result=original(w,r);self.allowed=False;return result
        self.sim.observe=observe
        with self.assertRaises(PermissionError):self.decide()
        self.assertIsNone(self.ledger.get('approval')['decision']);self.assertEqual(self.sim.posts,[])
    def test_decision_id_cannot_be_reused_for_another_gate(self):
        self.decide()
        self.ledger.bind(replace(self.binding,approval_id='second',run_id='run-two'))
        with self.assertRaises(Conflict):self.coordinator.decide('second',decision_id='decision',decision='approve',principal=self.principal)
        self.assertIsNone(self.ledger.get('second')['decision']);self.assertEqual(len(self.sim.posts),1)
    def test_concurrent_opposite_decisions_one_winner(self):
        def act(choice):
            try:self.decide(choice,choice);return 'won'
            except Conflict:return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(act,['approve','reject']))
        self.assertCountEqual(results,['won','conflict']);self.assertEqual(len(self.sim.posts),1)
    def test_wrong_run_identity_is_unknown_and_never_resumed(self):
        original=self.sim.observe
        def observe(w,r):
            value=original(w,r);value['response']['data']['runId']='other-run';return value
        self.sim.observe=observe
        self.assertEqual(self.decide()['runStatus'],'unknown');self.assertEqual(self.sim.posts,[])
    def test_reconcile_only_reads_and_does_not_decide(self):
        result=self.coordinator.reconcile('approval',principal=self.principal)
        self.assertEqual(result['deliveryStatus'],'not_submitted');self.assertEqual(self.sim.posts,[])
        self.assertIsNone(self.ledger.get('approval')['decision'])

    def test_mixed_machine_and_human_pause_global_counts_allow_one_outstanding(self):
        self.sim.pause.update(pausePointCount=3, resumedCount=2)
        result = self.decide()
        self.assertEqual(result['deliveryStatus'], 'accepted')
        self.assertEqual(len(self.sim.posts), 1)
        self.assertEqual(self.sim.posts[0]['approval']['decision'], 'approve')
        self.sim.pause.update(pausePointCount=4, resumedCount=3, contextId='next-pause')
        replay = self.decide()
        self.assertEqual(replay['deliveryStatus'], 'accepted')
        self.assertEqual(len(self.sim.posts), 1)

    def test_mixed_pause_bad_counters_and_identity_never_record_or_deliver(self):
        valid = dict(self.sim.pause, pausePointCount=3, resumedCount=2)
        changes = [{'pausePointCount': True}, {'resumedCount': True}, {'pausePointCount': 0},
                   {'resumedCount': -1}, {'pausePointCount': -1}, {'pausePointCount': '3'},
                   {'resumedCount': 2.0}, {'resumedCount': None}, {'resumedCount': 1},
                   {'resumedCount': 3}, {'resumedCount': 4}, {'contextId': 'old'},
                   {'blockedOnBlockId': 'other'}, {'pauseKind': 'timer'}]
        for change in changes:
            self.sim.pause = {**valid, **change}
            with self.subTest(change=change):
                self.assertEqual(self.decide()['status'], 'waiting')
                self.assertIsNone(self.ledger.get('approval')['decision'])
                self.assertEqual(self.sim.posts, [])
        self.sim.pause = dict(valid); del self.sim.pause['resumedCount']
        self.assertEqual(self.decide()['status'], 'waiting')
        self.assertEqual(self.sim.posts, [])

    def test_legacy_single_pause_missing_count_is_only_compatible_default(self):
        self.assertNotIn('resumedCount', self.sim.pause)
        self.assertEqual(self.decide()['deliveryStatus'], 'accepted')
        self.assertEqual(len(self.sim.posts), 1)

    def test_mixed_pause_expired_binding_refuses_without_journal_intent(self):
        self.sim.pause.update(pausePointCount=3, resumedCount=2)
        coordinator = ApprovalCoordinator(self.ledger, self.sim, self.journal,
                                          authorize=lambda p: True, clock=lambda: 2100)
        with self.assertRaises(PermissionError):
            coordinator.decide('approval', decision_id='d', decision='approve',
                               principal=replace(self.principal, expires_at=3000))
        self.assertEqual(self.sim.posts, [])
        with self.assertRaises(KeyError): self.journal.get('workflow', 'run', 'ctx')

    def test_mixed_pause_uncertain_receipt_reopen_never_reposts(self):
        self.sim.pause.update(pausePointCount=3, resumedCount=2); self.sim.uncertain = True
        self.assertEqual(self.decide()['deliveryStatus'], 'uncertain')
        coordinator = ApprovalCoordinator(ApprovalLedger(self.root/'approval.sqlite'), self.sim,
            SimJournal(self.root/'sim.sqlite'), authorize=lambda p: True, clock=lambda: 1000)
        self.sim.pause.update(contextId='later', pausePointCount=4, resumedCount=3)
        self.assertEqual(coordinator.decide('approval', decision_id='decision', decision='approve',
                                          principal=self.principal)['deliveryStatus'], 'uncertain')
        self.assertEqual(len(self.sim.posts), 1)


if __name__=='__main__':unittest.main()
