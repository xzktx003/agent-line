import copy
import tempfile
import unittest
from pathlib import Path

from agent_line.store import TaskStore, Conflict


class VerificationLeaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.store = TaskStore.initialize(self.root / 'tasks.sqlite')
        self.policy = {'schemaVersion': 1, 'policyId': 'bytes', 'kind': 'artifact_sha256',
                       'requireTests': False, 'artifacts': [{'path': 'out.txt', 'sha256': 'a'*64}]}
        self.identity = {'codexThreadId': 'thread', 'codexTurnId': 'turn'}

    def register(self, key='a', workspace=None, policy=True):
        payload = {'projectId': 'p', 'roleAgentId': 'role', 'workspace': str(workspace or self.workspace),
                   'workflow': {'runId': key, 'nodeExecutionId': 'node', 'attemptId': '1'}}
        if policy:
            payload['verificationPolicy'] = copy.deepcopy(self.policy)
        return self.store.register(key, payload)[0]['id']

    def finish(self, task, status='succeeded'):
        self.assertTrue(self.store.claim(task, self.workspace))
        self.store.accept(task, self.identity)
        self.store.finish(task, status, {}, [{'kind': 'execution_terminal', 'executionId': 'turn', 'confirmed': True}])
        self.exit = {'source': 'docker-inspect-before-remove', 'running': False, 'pid': 0,
                     'status': 'exited', 'owner': 'b'*32, 'containerId': 'c'*64, 'finishedAt': 'time',
                     'workspace': str(self.workspace), 'identities': [self.identity]}
        self.store.confirm_resources_released(task, 'turn', self.exit)

    def receipt(self, task):
        from agent_line.verification_policy import policy_hash
        return {'kind': 'verification', 'taskId': task, 'executionIdentity': self.identity,
                'policySha256': policy_hash(self.store.get(task)['payload']['verificationPolicy']),
                'status': 'passed', 'checks': [{'kind': 'artifact_sha256', 'path': 'out.txt',
                'expectedSha256': 'a'*64, 'expectedSize': None, 'maxBytes': 10000000, 'status': 'passed'}]}

    def test_release_keeps_lease_and_blocks_same_parent_child_workspaces(self):
        task = self.register(); self.finish(task)
        self.assertTrue(self.store.get(task)['resourcesReleased'])
        self.assertEqual(self.store.verification_lease(task), str(self.workspace))
        for i, path in enumerate((self.workspace, self.root, self.workspace/'child')):
            other = self.register('b'+str(i), path)
            self.assertFalse(self.store.claim(other, path))
        self.assertFalse(self.store.release_workspace(self.workspace, task))
        self.store.confirm_resources_released(task, 'turn', self.exit)
        self.assertEqual(self.store.verification_lease(task), str(self.workspace))

    def test_record_releases_atomically_and_replay_does_not_delete_successor(self):
        task = self.register(); self.finish(task); receipt = self.receipt(task)
        self.assertTrue(self.store.record_verification(task, receipt))
        other = self.register('b')
        self.assertTrue(self.store.claim(other, self.workspace))
        self.assertFalse(self.store.record_verification(task, receipt))
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT task_id FROM workspace_leases').fetchone()[0], other)

    def test_missing_or_wrong_owner_refuses_verification(self):
        task = self.register(); self.finish(task)
        with self.store.connect() as db:
            db.execute('DELETE FROM workspace_leases')
        with self.assertRaises(Conflict): self.store.verification_lease(task)
        with self.assertRaises(Conflict): self.store.record_verification(task, self.receipt(task))
        other = self.register('b'); self.assertTrue(self.store.claim(other, self.workspace))
        with self.assertRaises(Conflict): self.store.record_verification(task, self.receipt(task))

    def test_policy_check_and_resource_evidence_must_match(self):
        task = self.register(); self.finish(task)
        for field, value in [('policySha256', 'd'*64), ('executionIdentity', {'upstreamTaskId': 'wrong'})]:
            receipt = self.receipt(task); receipt[field] = value
            with self.assertRaises(Conflict): self.store.record_verification(task, receipt)
        for field, value in [('path','wrong'), ('expectedSha256','d'*64), ('expectedSize',1), ('maxBytes',1)]:
            receipt = self.receipt(task); receipt['checks'][0][field] = value
            with self.assertRaises(Conflict): self.store.record_verification(task, receipt)
        receipt = self.receipt(task); receipt['checks'] *= 2
        with self.assertRaises(Conflict): self.store.record_verification(task, receipt)
        with self.store.connect() as db:
            db.execute("UPDATE tasks SET evidence='[]' WHERE id=?", (task,))
        with self.assertRaises(Conflict): self.store.record_verification(task, self.receipt(task))

    def test_transaction_failure_rolls_back_receipt_and_lease(self):
        task = self.register(); self.finish(task)
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER fail_release BEFORE DELETE ON workspace_leases BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(Exception): self.store.record_verification(task, self.receipt(task))
        self.assertEqual(self.store.get(task)['verificationStatus'], 'pending')
        self.assertEqual(self.store.verification_lease(task), str(self.workspace))

    def test_failed_canceled_and_legacy_keep_release_behavior(self):
        for index, (status, policy) in enumerate((('failed',True), ('canceled',True), ('succeeded',False))):
            task = self.register(str(index), policy=policy); self.finish(task, status)
            with self.store.connect() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM workspace_leases').fetchone()[0], 0)

    def test_policy_is_normalized_and_bad_policy_never_registered(self):
        task = self.register()
        artifact = self.store.get(task)['payload']['verificationPolicy']['artifacts'][0]
        self.assertEqual(artifact['maxBytes'], 10000000)
        self.assertIsNone(artifact['exactSize'])
        self.assertEqual(self.register(), task)
        self.policy['requireTests'] = True
        with self.assertRaises(ValueError): self.register('bad')

    def test_failed_verification_also_releases_original_lease(self):
        task = self.register(); self.finish(task)
        receipt = self.receipt(task)
        receipt['status'] = receipt['checks'][0]['status'] = 'failed'
        self.store.record_verification(task, receipt)
        other = self.register('b')
        self.assertTrue(self.store.claim(other, self.workspace))
        self.assertEqual(self.store.get(task)['verificationStatus'], 'failed')

    def test_missing_check_fields_and_non_strict_exit_refused(self):
        task = self.register(); self.finish(task)
        receipt = self.receipt(task); del receipt['checks'][0]['expectedSize']
        with self.assertRaises(Conflict): self.store.record_verification(task, receipt)
        with self.store.connect() as db:
            import json
            evidence = json.loads(db.execute('SELECT evidence FROM tasks WHERE id=?', (task,)).fetchone()[0])
            evidence[-1]['receipt']['pid'] = False
            db.execute('UPDATE tasks SET evidence=? WHERE id=?', (json.dumps(evidence), task))
        with self.assertRaises(Conflict): self.store.record_verification(task, self.receipt(task))

    def test_reopen_preserves_original_verification_lease(self):
        task = self.register(); self.finish(task)
        reopened = TaskStore(self.store.path)
        self.assertEqual(reopened.verification_lease(task), str(self.workspace))
        self.assertIn(task, [t['id'] for t in reopened.dispatch_candidates()])
