"""Uploads startup must not create a missing directory or ignore an unwritable one."""
from pathlib import Path
import os
import stat
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class UploadsStartGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.uploads = self.root / 'uploads'
        self.uploads.mkdir()
        tools = self.root / 'tools'
        tools.mkdir()
        for name in ('chown', 'chmod'):
            tool = tools / name
            tool.write_text('#!/bin/sh\nprintf "%s\\n" "$@" >> "$TOOL_LOG"\n')
            tool.chmod(0o700)
        self.env = {**os.environ, 'PATH': str(tools) + os.pathsep + os.environ['PATH'],
                    'TOOL_LOG': str(self.root / 'tools.log'),
                    'UPLOADS_DIR': str(self.uploads)}
        self.guard = ROOT / 'deploy/sim/uploads-start.sh'

    def start(self):
        return subprocess.run(['/bin/sh', str(self.guard)], env=self.env,
                              capture_output=True, timeout=5)

    def test_missing_directory_is_not_created(self):
        self.uploads.rmdir()
        result = self.start()
        self.assertEqual(result.returncode, 78)
        self.assertFalse(self.uploads.exists())
        self.assertFalse((self.root / 'tools.log').exists())

    def test_symlink_is_not_followed(self):
        target = self.root / 'outside'
        target.mkdir()
        self.uploads.rmdir()
        self.uploads.symlink_to(target, target_is_directory=True)
        result = self.start()
        self.assertEqual(result.returncode, 78)
        self.assertEqual(list(target.iterdir()), [])
        self.assertTrue(self.uploads.is_symlink())

    def test_writable_directory_removes_probe_after_permission_commands(self):
        marker = self.uploads / 'execution'
        marker.mkdir()
        result = self.start()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.uploads / '.agent-line-write-probe').exists())
        self.assertTrue(marker.is_dir())
        log = (self.root / 'tools.log').read_text()
        self.assertEqual(log, f'1001:1001\n{self.uploads}\n700\n{self.uploads}\n')

    def test_unwritable_directory_fails_without_leaving_a_probe(self):
        self.uploads.chmod(0o555)
        try:
            result = self.start()
            self.assertEqual(result.returncode, 78)
            self.assertFalse((self.uploads / '.agent-line-write-probe').exists())
            self.assertEqual(stat.S_IMODE(self.uploads.stat().st_mode), 0o555)
        finally:
            self.uploads.chmod(0o700)

    def test_compose_uses_the_guard_and_does_not_create_the_host_path(self):
        compose = (ROOT / 'deploy/sim/compose.yaml').read_text().split('\n  simstudio:\n', 1)[0]
        self.assertIn("entrypoint: ['/bin/sh', '/usr/local/bin/agent-line-uploads-start.sh']", compose)
        self.assertIn('source: ./uploads-start.sh', compose)
        self.assertNotIn('chown 1001:1001 /uploads && chmod 700 /uploads', compose)
