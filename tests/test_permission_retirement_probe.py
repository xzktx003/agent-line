"""Post-fix validation cannot adopt or rewrite a historical unknown task."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from probes.qwenpaw import native_permissions as probe


class PermissionRetirementProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.old = self.root / 'var/native-permissions-attempt2'
        self.old.mkdir(parents=True)
        self.write(self.old / 'state.json', {'status':'incomplete','hostStopped':True})
        self.write(self.old / 'root-final-check.json', {'status':'partial_approve_reject_unresolved',
            'temporaryHostPidAbsent':True,'taskResubmissions':0})
        for file in ('tasks.sqlite','roles.sqlite','receipts.sqlite'):
            (self.old / file).write_bytes(b'preserved-original-unknown')
        self.parallel = self.root / 'var/pinned-parallel-role-flow/root-check.json'
        self.write(self.parallel, {'status':'passed_real_static_parallel_read_only','taskCount':2,
            'activeLeaseCount':0,'ownedActiveContainerCount':0,'mainNineConfigHashesUnchanged':True})

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    def test_fixed_purposes_have_distinct_workspaces_and_cannot_choose_arbitrary_path(self):
        self.assertEqual(probe.validation_directory(self.root), self.old)
        self.assertEqual(probe.validation_directory(self.root, True), self.root/'var/native-permissions-retirement')

    def test_new_validation_hashes_original_evidence_without_changing_it(self):
        before = {x.name:x.read_bytes() for x in self.old.iterdir()}
        hashes = probe.prior_retirement_proof(self.root)
        self.assertEqual(len(hashes), 5)
        for file, expected in hashes.items():
            self.assertEqual(hashlib.sha256((self.root/file).read_bytes()).hexdigest(), expected)
        self.assertEqual(before, {x.name:x.read_bytes() for x in self.old.iterdir()})

    def test_synthetic_or_incomplete_parallel_result_does_not_authorize_new_window(self):
        original = json.loads(self.parallel.read_text())
        for key,value in [('status','synthetic'),('taskCount',1),('activeLeaseCount',1),
                          ('ownedActiveContainerCount',1),('mainNineConfigHashesUnchanged',False)]:
            self.write(self.parallel, {**original,key:value})
            with self.assertRaisesRegex(ValueError,'real_runtime_retirement_proof_required'):
                probe.prior_retirement_proof(self.root)

    def test_old_task_must_remain_unresolved_and_old_host_stopped(self):
        path=self.old/'state.json';original=json.loads(path.read_text())
        for key,value in [('status','passed'),('hostStopped',False)]:
            self.write(path,{**original,key:value})
            with self.assertRaisesRegex(ValueError,'legacy_unknown_task_must_remain_preserved'):
                probe.prior_retirement_proof(self.root)

    def test_changed_legacy_state_blocks_existing_new_probe_before_any_execution(self):
        saved=probe.prior_retirement_proof(self.root)
        current=probe.validation_directory(self.root,True)
        self.write(current/'state.json', {'status':'incomplete','preservedLegacyEvidence':saved})
        (self.old/'tasks.sqlite').write_bytes(b'changed-original-evidence')
        with patch.object(probe,'ROOT',self.root), patch('subprocess.run') as run:
            with self.assertRaisesRegex(ValueError,'legacy_evidence_changed'):
                probe.run_live(retirement_validation=True)
            run.assert_not_called()

    def test_existing_post_fix_unknown_state_observes_without_new_host_or_task(self):
        current=probe.validation_directory(self.root,True)
        self.write(current/'state.json', {'status':'incomplete',
            'preservedLegacyEvidence':probe.prior_retirement_proof(self.root)})
        before={str(x):x.read_bytes() for x in self.root.rglob('*') if x.is_file()}
        with patch.object(probe,'ROOT',self.root), patch('subprocess.Popen') as popen:
            result=probe.run_live(retirement_validation=True)
            self.assertEqual(result, {'status':'existing_identity_observe_only','writes':0,'state':'incomplete'})
            popen.assert_not_called()
        self.assertEqual(before,{str(x):x.read_bytes() for x in self.root.rglob('*') if x.is_file()})

    def test_new_model_window_requires_separate_completed_bootstrap(self):
        with patch.object(probe,'ROOT',self.root), patch('subprocess.Popen') as popen:
            with self.assertRaisesRegex(ValueError,'bootstrap_required_before_execution'):
                probe.run_live(retirement_validation=True)
            popen.assert_not_called()
        self.assertFalse(probe.validation_directory(self.root,True).exists())
