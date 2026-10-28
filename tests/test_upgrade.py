import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_line.upgrade import REQUIRED_PROBES, evaluate


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.current = {}
        self.candidate = {}
        for name in ('qwenpaw', 'sim'):
            path = self.root / 'upstream' / name
            path.mkdir(parents=True)
            def git(*args):
                return subprocess.check_output(['git', '-C', str(path), *args], stderr=subprocess.DEVNULL, text=True).strip()
            git('init')
            git('config', 'user.email', 'fixture@example.invalid')
            git('config', 'user.name', 'Fixture')
            git('remote', 'add', 'origin', f'https://example.invalid/{name}.git')
            git('commit', '--allow-empty', '-m', 'test initial')
            first = git('rev-parse', 'HEAD')
            self.current[name] = {'path': f'upstream/{name}', 'remote': f'https://example.invalid/{name}.git', 'commit': first}
            target = self.root / 'candidates' / name
            target.parent.mkdir(exist_ok=True)
            subprocess.check_call(['git', 'clone', '-q', str(path), str(target)])
            subprocess.check_call(['git', '-C', str(target), 'remote', 'set-url', 'origin', f'https://example.invalid/{name}.git'])
            subprocess.check_call(['git', '-C', str(target), '-c', 'user.email=fixture@example.invalid', '-c', 'user.name=Fixture', 'commit', '--allow-empty', '-qm', 'test candidate'])
            candidate_sha = subprocess.check_output(['git', '-C', str(target), 'rev-parse', 'HEAD'], text=True).strip()
            self.candidate[name] = {**self.current[name], 'path': f'candidates/{name}', 'commit': candidate_sha}
        (self.root / 'upstreams.lock.json').write_text(json.dumps({'schemaVersion': 1, 'repositories': self.current}))
        self.manifest = {'schemaVersion': 1, 'candidates': self.candidate, 'probes': {}}
        for probe in REQUIRED_PROBES:
            self.evidence(probe)

    def evidence(self, probe, *, status='passed', override=None):
        evidence = {'probeId': probe, 'status': status,
                    'currentCommits': {n:p['commit'] for n,p in self.current.items()},
                    'candidateCommits': {n:p['commit'] for n,p in self.candidate.items()},
                    'observedAt': '2026-10-21T00:00:00Z', 'mode': 'live',
                    'details': 'Synthetic evidence for validator unit test; not product acceptance.'}
        if override:
            evidence.update(override)
        path = self.root / 'evidence' / (probe + '.json')
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(evidence))
        self.manifest['probes'][probe] = {'evidencePath': str(path.relative_to(self.root)), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}

    def test_complete_bound_evidence_passes_only_compatibility_gate(self):
        report = evaluate(self.root, self.manifest)
        self.assertTrue(report['compatibilityApproved'], report)
        self.assertFalse(report['upgradeExecuted'])
        self.assertEqual(set(report['requiredProbes']), set(REQUIRED_PROBES))

    def test_empty_or_unknown_probe_set_never_approves(self):
        self.manifest['probes'] = {}
        self.assertFalse(evaluate(self.root, self.manifest)['compatibilityApproved'])
        self.evidence(REQUIRED_PROBES[0], status='unknown')
        self.assertFalse(evaluate(self.root, self.manifest)['compatibilityApproved'])

    def test_wrong_candidate_sha_rejected_even_when_hash_matches(self):
        wrong = {n:p['commit'] for n,p in self.candidate.items()}
        wrong['qwenpaw'] = 'f' * 40
        self.evidence(REQUIRED_PROBES[0], override={'candidateCommits': wrong})
        report = evaluate(self.root, self.manifest)
        self.assertFalse(report['compatibilityApproved'])
        self.assertIn('candidate_evidence_mismatch', report['probes'][REQUIRED_PROBES[0]]['errors'])

    def test_modified_evidence_or_mock_mode_rejected(self):
        probe = REQUIRED_PROBES[0]
        self.evidence(probe, override={'mode': 'mock'})
        self.assertFalse(evaluate(self.root, self.manifest)['compatibilityApproved'])
        self.evidence(probe)
        (self.root / self.manifest['probes'][probe]['evidencePath']).write_text('{}')
        self.assertFalse(evaluate(self.root, self.manifest)['compatibilityApproved'])

    def test_dirty_current_checkout_or_unpinned_candidate_rejected(self):
        (self.root / self.current['qwenpaw']['path'] / 'untracked').write_text('patch')
        self.assertFalse(evaluate(self.root, self.manifest)['compatibilityApproved'])
        (self.root / self.current['qwenpaw']['path'] / 'untracked').unlink()
        self.manifest['candidates']['qwenpaw']['commit'] = 'f' * 40
        self.assertFalse(evaluate(self.root, self.manifest)['compatibilityApproved'])

    def test_candidate_remote_and_evidence_paths_are_constrained(self):
        self.manifest['candidates']['qwenpaw']['remote'] = 'https://other.invalid/fork.git'
        self.assertFalse(evaluate(self.root, self.manifest)['compatibilityApproved'])
        self.manifest['candidates']['qwenpaw']['remote'] = self.current['qwenpaw']['remote']
        self.manifest['probes'][REQUIRED_PROBES[0]]['evidencePath'] = '../outside.json'
        self.assertFalse(evaluate(self.root, self.manifest)['compatibilityApproved'])
