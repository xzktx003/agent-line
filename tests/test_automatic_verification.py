import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from agent_line.automatic_verification import AutomaticVerifier
from agent_line.store import TaskStore, Conflict
from agent_line.verification import verify_artifacts


class AutomaticVerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root/'workspace'; self.workspace.mkdir()
        self.evidence = self.root/'evidence'; self.evidence.mkdir(mode=0o700)
        self.data = b'checked bytes'
        (self.workspace/'proof.txt').write_bytes(self.data)
        self.policy = dict(schemaVersion=1, policyId='proof', kind='artifact_sha256', requireTests=False,
                           artifacts=[dict(path='proof.txt', sha256=hashlib.sha256(self.data).hexdigest(),
                                           exactSize=len(self.data), maxBytes=100)])
        self.store = TaskStore.initialize(self.root/'tasks.sqlite')
        task, _ = self.store.register('key', dict(projectId='project', roleAgentId='role',
            workspace=str(self.workspace), verificationPolicy=self.policy,
            workflow=dict(id='workflow', runId='run', nodeExecutionId='node', attemptId='attempt')))
        self.task = task['id']
        self.store.claim(self.task, self.workspace)
        self.store.accept(self.task, {'codexThreadId': 'thread', 'codexTurnId': 'turn'})
        self.store.finish(self.task, 'succeeded', {'testsPassed': True, 'expectedSha256': 'model-lie'},
                          [{'kind': 'execution_terminal', 'executionId': 'turn', 'confirmed': True}])
        self.store.confirm_resources_released(self.task, 'turn', {
            'source':'docker-inspect-before-remove','running':False,'pid':0,'status':'exited',
            'owner':'b'*32,'containerId':'c'*64,'finishedAt':'time',
            'workspace':str(self.workspace),'identities':[{'codexThreadId':'thread','codexTurnId':'turn'}]})
        self.verifier = AutomaticVerifier(self.store, self.evidence)

    def test_actual_pass_and_idempotent_record(self):
        with patch('agent_line.automatic_verification.verify_artifacts', wraps=verify_artifacts) as checker:
            self.assertEqual(self.verifier.verify_pending(self.task)['verificationStatus'], 'passed')
            self.assertEqual(self.verifier.verify_pending(self.task)['status'], 'already_recorded')
            self.assertEqual(checker.call_count, 1)
        receipt = json.loads(next(self.evidence.glob('*.json')).read_text())
        self.assertEqual(receipt['verificationPolicy'], self.policy)
        self.assertEqual(self.store.get(self.task)['evidence'][-1], receipt)
        self.assertEqual(next(self.evidence.glob('*.json')).stat().st_mode & 0o777, 0o600)

    def test_model_claim_cannot_pass_wrong_bytes(self):
        (self.workspace/'proof.txt').write_bytes(b'wrong')
        self.assertEqual(self.verifier.verify_pending(self.task)['verificationStatus'], 'failed')

    def test_resources_and_lease_preconditions(self):
        with self.store.connect() as db:
            db.execute('UPDATE tasks SET resources_released=0 WHERE id=?', (self.task,))
        self.assertEqual(self.verifier.verify_pending(self.task)['status'], 'waiting')
        with self.store.connect() as db:
            db.execute('UPDATE tasks SET resources_released=1 WHERE id=?', (self.task,))
        with patch.object(self.store, 'verification_lease', side_effect=Conflict('missing')):
            self.assertEqual(self.verifier.verify_pending(self.task)['status'], 'blocked')
        self.assertFalse(list(self.evidence.glob('*.json')))

    def interrupt_after_file(self):
        with patch.object(self.store, 'record_verification', side_effect=Conflict('database unavailable')):
            self.assertEqual(self.verifier.verify_pending(self.task)['status'], 'blocked')
        return next(self.evidence.glob('*.json'))

    def test_saved_receipt_recovery_does_not_reread_changed_file(self):
        path = self.interrupt_after_file(); original = path.read_bytes()
        (self.workspace/'proof.txt').write_bytes(b'changed after verified')
        with patch('agent_line.automatic_verification.verify_artifacts', side_effect=AssertionError('reread')):
            result = AutomaticVerifier(TaskStore(self.store.path), self.evidence).verify_pending(self.task)
        self.assertEqual(result['verificationStatus'], 'passed')
        self.assertEqual(path.read_bytes(), original)

    def test_concurrent_once(self):
        with patch('agent_line.automatic_verification.verify_artifacts', wraps=verify_artifacts) as checker:
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(self.verifier.verify_pending, [self.task]*4))
            self.assertEqual(checker.call_count, 1)
        self.assertEqual(sum(r['status'] == 'recorded' for r in results), 1)

    def test_bad_receipt_identity_policy_time_and_permissions_fail_closed(self):
        path = self.interrupt_after_file(); receipt = json.loads(path.read_text())
        for key, value in [('taskId', 'other'), ('executionIdentity', {'codexTurnId': 'other'}),
                           ('policySha256', '0'*64), ('checkedAt', 1), ('scope', 'tests passed')]:
            path.write_text(json.dumps(dict(receipt, **{key: value})))
            self.assertEqual(self.verifier.verify_pending(self.task)['status'], 'blocked')
        path.write_text(json.dumps(receipt)); path.chmod(0o644)
        self.assertEqual(self.verifier.verify_pending(self.task)['status'], 'blocked')
        self.assertEqual(self.store.get(self.task)['verificationStatus'], 'pending')

    def test_directory_alias_and_workspace_location_rejected(self):
        alias = self.root/'alias'; alias.symlink_to(self.evidence, target_is_directory=True)
        with self.assertRaises((ValueError, OSError)):
            AutomaticVerifier(self.store, alias)
        inside = self.workspace/'evidence'; inside.mkdir(mode=0o700)
        with self.assertRaises(ValueError):
            AutomaticVerifier(self.store, inside)
        self.evidence.chmod(0o755)
        with self.assertRaises(ValueError):
            AutomaticVerifier(self.store, self.evidence)

    def test_missing_owned_exit_proof_does_not_read_artifact(self):
        with self.store.connect() as db:
            evidence = self.store.get(self.task)['evidence']
            db.execute('UPDATE tasks SET evidence=? WHERE id=?',
                       (json.dumps([e for e in evidence if e.get('kind') != 'resource_exit']), self.task))
        with patch('agent_line.automatic_verification.verify_artifacts', side_effect=AssertionError('read')):
            self.assertEqual(self.verifier.verify_pending(self.task)['status'], 'blocked')

    def test_bad_json_and_symlink_receipt_never_reverified(self):
        path = self.interrupt_after_file()
        original = path.read_bytes()
        path.write_bytes(b'{broken')
        self.assertEqual(self.verifier.verify_pending(self.task)['status'], 'blocked')
        outside = self.root/'original.json'; outside.write_bytes(original)
        path.unlink(); path.symlink_to(outside)
        with patch('agent_line.automatic_verification.verify_artifacts', side_effect=AssertionError('reread')):
            self.assertEqual(self.verifier.verify_pending(self.task)['status'], 'blocked')
