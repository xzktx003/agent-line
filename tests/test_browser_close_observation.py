import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class BrowserCloseObservationTests(unittest.TestCase):
    def test_close_does_not_cancel_and_reopen_uses_run_id(self):
        done = subprocess.run(
            ['node', str(ROOT / 'tests' / 'browser_close_observation_check.mjs')],
            cwd=ROOT, text=True, capture_output=True, check=False,
        )
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


if __name__ == '__main__':
    unittest.main()
