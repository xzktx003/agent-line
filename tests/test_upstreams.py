import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location('check', Path(__file__).parents[1] / 'scripts/check_upstreams.py')

class UpstreamGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.git('init', '-q')
        self.git('config', 'user.email', 'fixture@example.invalid')
        self.git('config', 'user.name', 'Fixture')
        self.git('remote', 'add', 'origin', 'https://example.invalid/upstream.git')
        (self.path / 'source.txt').write_text('original\n')
        self.git('add', 'source.txt')
        self.git('commit', '-qm', 'Fixture')
        self.pin = {'commit': self.git('rev-parse', 'HEAD'), 'remote': 'https://example.invalid/upstream.git'}
        self.module = importlib.util.module_from_spec(SPEC)
        SPEC.loader.exec_module(self.module)

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.path), *args], text=True).strip()

    def test_clean_pin_passes(self):
        self.assertEqual(self.module.inspect(self.path, self.pin), [])

    def test_source_patch_is_rejected_without_reset(self):
        (self.path / 'source.txt').write_text('custom patch\n')
        errors = self.module.inspect(self.path, self.pin)
        self.assertIn('tracked_changes', errors)
        self.assertEqual((self.path / 'source.txt').read_text(), 'custom patch\n')

    def test_wrong_commit_and_remote_rejected(self):
        self.pin.update(commit='0' * 40, remote='https://wrong.invalid/repo.git')
        self.assertEqual(set(self.module.inspect(self.path, self.pin)), {'commit_mismatch', 'remote_mismatch'})

    def test_untracked_files_reported_not_removed(self):
        (self.path / 'local-secret.env').write_text('fixture-only')
        self.assertIn('untracked_files', self.module.inspect(self.path, self.pin))
        self.assertTrue((self.path / 'local-secret.env').exists())

    def test_missing_repo_fails_closed(self):
        self.assertEqual(self.module.inspect(self.path / 'missing', self.pin), ['repository_unavailable'])
