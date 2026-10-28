import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from agent_line.store import TaskStore
from agent_line.verification import ArtifactPolicy, verify_artifacts


class ArtifactVerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.workspace=self.root/'workspace';self.workspace.mkdir()
        self.artifacts=self.root/'artifacts';self.artifacts.mkdir()
        self.store=TaskStore.initialize(self.root/'tasks.db')
        task,_=self.store.register('key',{'projectId':'project','roleAgentId':'role',
            'workspace':str(self.workspace),'workflow':{'runId':'run','nodeExecutionId':'node','attemptId':'attempt'}})
        self.task=task['id'];self.identity={'codexThreadId':'thread','codexTurnId':'turn'}
        self.store.claim(self.task,self.workspace);self.store.accept(self.task,self.identity)
        self.store.finish(self.task,'succeeded',{'testsPassed':True},[{'kind':'execution_terminal','executionId':'turn','confirmed':True}])
        self.store.confirm_resources_released(self.task,'turn')
        self.data=b'EXPECTED\n';(self.workspace/'proof.txt').write_bytes(self.data)
        self.policy=ArtifactPolicy('proof.txt',hashlib.sha256(self.data).hexdigest(),exact_size=len(self.data))

    def verify(self,policies=None):
        return verify_artifacts(self.store,self.task,workspace=self.workspace,policies=policies or [self.policy],evidence_dir=self.artifacts)

    def test_real_file_receipt_bound_to_task_identity_and_atomic_file(self):
        receipt=self.verify()
        self.assertEqual(receipt['status'],'passed')
        self.assertEqual(receipt['taskId'],self.task)
        self.assertEqual(receipt['executionIdentity'],self.identity)
        self.assertEqual(receipt['checks'][0]['actualSha256'],self.policy.expected_sha256)
        files=list(self.artifacts.glob('*.json'));self.assertEqual(len(files),1)
        self.assertEqual(json.loads(files[0].read_text()),receipt)
        self.assertEqual(files[0].stat().st_mode&0o777,0o600)
        self.assertEqual(self.store.get(self.task)['verificationStatus'],'pending')

    def test_model_claim_does_not_pass_wrong_artifact(self):
        (self.workspace/'proof.txt').write_text('WRONG')
        receipt=self.verify();self.assertEqual(receipt['status'],'failed')
        self.assertEqual(receipt['checks'][0]['status'],'failed')

    def test_symlink_file_and_parent_rejected(self):
        (self.workspace/'proof.txt').unlink()
        outside=self.root/'outside';outside.write_bytes(self.data)
        (self.workspace/'proof.txt').symlink_to(outside)
        self.assertEqual(self.verify()['status'],'failed')
        (self.workspace/'nested').symlink_to(self.root,target_is_directory=True)
        p=ArtifactPolicy('nested/outside',self.policy.expected_sha256)
        self.assertEqual(self.verify([p])['status'],'failed')

    def test_escape_absolute_and_empty_policy_rejected(self):
        for path in ['../outside','/etc/passwd','a/../proof.txt','',r'a\b']:
            with self.assertRaises(ValueError):ArtifactPolicy(path,self.policy.expected_sha256)
        with self.assertRaises(ValueError):verify_artifacts(self.store,self.task,workspace=self.workspace,policies=[],evidence_dir=self.artifacts)

    def test_workspace_mismatch_and_unreleased_resources_rejected(self):
        with self.assertRaises(ValueError):verify_artifacts(self.store,self.task,workspace=self.root,policies=[self.policy],evidence_dir=self.artifacts)
        with self.store.connect()as db:db.execute('UPDATE tasks SET resources_released=0 WHERE id=?',(self.task,))
        with self.assertRaises(ValueError):self.verify()
        self.assertFalse(list(self.artifacts.iterdir()))

    def test_missing_oversize_nonregular_artifacts_fail(self):
        (self.workspace/'proof.txt').unlink();self.assertEqual(self.verify()['status'],'failed')
        (self.workspace/'proof.txt').mkdir();self.assertEqual(self.verify()['status'],'failed')
        (self.workspace/'proof.txt').rmdir();(self.workspace/'proof.txt').write_bytes(b'X'*20)
        p=ArtifactPolicy('proof.txt',self.policy.expected_sha256,max_bytes=10)
        self.assertEqual(self.verify([p])['status'],'failed')

    def test_fifo_is_rejected_without_blocking(self):
        (self.workspace/'proof.txt').unlink()
        os.mkfifo(self.workspace/'proof.txt')
        self.assertEqual(self.verify()['checks'][0]['reason'],'artifact_not_regular_file')

    def test_symlink_evidence_directory_is_rejected(self):
        alias=self.root/'evidence-alias';alias.symlink_to(self.artifacts,target_is_directory=True)
        with self.assertRaises(OSError):verify_artifacts(self.store,self.task,workspace=self.workspace,
            policies=[self.policy],evidence_dir=alias)
        self.assertFalse(list(self.artifacts.iterdir()))
