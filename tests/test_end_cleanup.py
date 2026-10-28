"""Ending a flow removes only the owned container."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'deploy' / 'executor'))
import lifecycle


class EndCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'launches'
        self.state.mkdir()
        self.workspace = self.root / 'project'
        self.workspace.mkdir()
        (self.workspace / 'agent.json').write_text('{"id":"developer"}')
        (self.workspace / 'MEMORY.md').write_text('keep this memory')
        (self.workspace / 'src').mkdir()
        (self.workspace / 'src' / 'app.py').write_text('print("keep")\n')
        self.review = self.workspace / 'review-worktree'
        self.review.mkdir()
        (self.review / 'diff.patch').write_text('review me\n')
        self.owner = 'a' * 32
        self.container = 'b' * 64
        self.receipt = self.state / (self.owner + '.json')
        self.receipt.write_text(json.dumps({
            'name': 'agent-line-executor-' + self.owner,
            'owner': self.owner,
            'pid': 1,
            'start': '1',
            'uid': os.getuid(),
            'workspace': str(self.workspace),
            'accepting': True,
            'identities': [],
        }))

    def labels(self):
        return {
            'agent-line.owner': self.owner,
            'agent-line.pid': '1',
            'agent-line.start': '1',
            'agent-line.uid': str(os.getuid()),
        }

    def inspect(self, container_id):
        return {
            'Id': container_id,
            'Image': 'sha256:' + ('c' * 64),
            'Config': {'Labels': self.labels()},
            'State': {
                'Running': False,
                'Pid': 0,
                'Status': 'exited',
                'ExitCode': 0,
                'StartedAt': '2026-10-28T00:00:00.000000000Z',
                'FinishedAt': '2026-10-28T00:00:01.000000000Z',
            },
        }

    def docker(self, replies):
        calls = []

        def run(command, **kwargs):
            calls.append(list(command))
            if command[:2] == ['docker', 'inspect']:
                payload = replies.pop(0)
                result = Mock()
                result.returncode = 0
                result.stdout = json.dumps([payload])
                return result
            if command[:2] in (['docker', 'kill'], ['docker', 'rm']):
                result = Mock()
                result.returncode = 0
                result.stdout = ''
                return result
            raise AssertionError(command)

        return calls, run

    def assert_kept(self):
        self.assertEqual((self.workspace / 'agent.json').read_text(), '{"id":"developer"}')
        self.assertEqual((self.workspace / 'MEMORY.md').read_text(), 'keep this memory')
        self.assertEqual((self.workspace / 'src' / 'app.py').read_text(), 'print("keep")\n')
        self.assertEqual((self.review / 'diff.patch').read_text(), 'review me\n')

    def test_container_removal_keeps_agent_memory_project_and_review_tree(self):
        calls, run = self.docker([self.inspect(self.container), self.inspect(self.container)])
        with patch.object(lifecycle, 'STATE', self.state), patch.object(lifecycle.subprocess, 'run', side_effect=run):
            proof = lifecycle.cleanup({'owner': self.owner})
        self.assertEqual(proof['containerId'], self.container)
        self.assertEqual(proof['workspace'], str(self.workspace))
        self.assertEqual(calls, [
            ['docker', 'inspect', 'agent-line-executor-' + self.owner],
            ['docker', 'inspect', self.container],
            ['docker', 'rm', self.container],
        ])
        self.assert_kept()

    def test_unproven_container_is_not_removed_and_files_stay(self):
        calls, run = self.docker([self.inspect(str(self.workspace))])
        with patch.object(lifecycle, 'STATE', self.state), patch.object(lifecycle.subprocess, 'run', side_effect=run):
            with self.assertRaisesRegex(RuntimeError, 'durable state'):
                lifecycle.cleanup({'owner': self.owner})
        self.assertEqual(calls, [['docker', 'inspect', 'agent-line-executor-' + self.owner]])
        self.assert_kept()
        saved = json.loads(self.receipt.read_text())
        self.assertNotIn('exitReceipt', saved)

    def test_container_removal_that_deletes_a_marker_is_rejected(self):
        calls, run = self.docker([self.inspect(self.container), self.inspect(self.container)])

        def run_and_delete(command, **kwargs):
            result = run(command, **kwargs)
            if command[:2] == ['docker', 'rm']:
                (self.workspace / 'MEMORY.md').unlink()
                (self.workspace / 'agent.json').unlink()
            return result

        with patch.object(lifecycle, 'STATE', self.state), patch.object(lifecycle.subprocess, 'run', side_effect=run_and_delete):
            with self.assertRaisesRegex(RuntimeError, 'removed durable state'):
                lifecycle.cleanup({'owner': self.owner})
        self.assertIn(['docker', 'rm', self.container], calls)
        self.assertTrue((self.workspace / 'src' / 'app.py').is_file())
        self.assertTrue((self.review / 'diff.patch').is_file())


if __name__ == '__main__':
    unittest.main()
