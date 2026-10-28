"""Redis must not turn a missing dataset into an empty instance."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class RedisStartGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / 'data'
        self.data.mkdir()
        self.called = self.root / 'official-entrypoint-called'
        entrypoint = self.root / 'entrypoint'
        entrypoint.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$ENTRYPOINT_CAPTURE"\n')
        entrypoint.chmod(0o700)
        source = (ROOT / 'deploy/sim/redis-start.sh').read_text()
        self.assertEqual(source.count('/usr/local/bin/docker-entrypoint.sh'), 1)
        self.guard = self.root / 'guard.sh'
        self.guard.write_text(source.replace('/usr/local/bin/docker-entrypoint.sh', str(entrypoint)))

    def start(self):
        return subprocess.run(['/bin/sh', str(self.guard), 'redis-server', '--appendonly', 'yes'],
                              env={**os.environ, 'REDIS_DATA': str(self.data),
                                   'ENTRYPOINT_CAPTURE': str(self.called)},
                              capture_output=True, timeout=5)

    def dataset(self):
        (self.data / 'appendonlydir').mkdir()
        (self.data / 'dump.rdb').write_bytes(b'redis-fixture')

    def test_empty_data_fails_before_official_entrypoint(self):
        result = self.start()
        self.assertEqual(result.returncode, 78)
        self.assertFalse(self.called.exists())
        self.assertEqual(list(self.data.iterdir()), [])

    def test_incomplete_or_symlinked_dataset_cannot_start(self):
        self.dataset()
        for name in ('appendonlydir', 'dump.rdb'):
            with self.subTest(missing=name):
                path = self.data / name
                saved = self.root / 'saved'
                path.rename(saved)
                result = self.start()
                self.assertEqual(result.returncode, 78)
                self.assertFalse(self.called.exists())
                saved.rename(path)
        dump = self.data / 'dump.rdb'
        original = dump.read_bytes()
        dump.unlink()
        dump.symlink_to(self.root / 'outside-rdb')
        (self.root / 'outside-rdb').write_bytes(original)
        self.assertEqual(self.start().returncode, 78)
        self.assertFalse(self.called.exists())
        self.assertEqual((self.root / 'outside-rdb').read_bytes(), original)

    def test_existing_dataset_delegates_and_removes_probe(self):
        self.dataset()
        before = {path.relative_to(self.data).as_posix(): path.read_bytes()
                  for path in self.data.rglob('*') if path.is_file()}
        self.assertEqual(self.start().returncode, 0)
        self.assertEqual(self.called.read_text(), 'redis-server\n--appendonly\nyes\n')
        self.assertFalse((self.data / '.agent-line-write-probe').exists())
        self.assertEqual(before, {path.relative_to(self.data).as_posix(): path.read_bytes()
                                  for path in self.data.rglob('*') if path.is_file()})

    def test_unwritable_dataset_refuses_before_entrypoint(self):
        self.dataset()
        self.data.chmod(0o555)
        try:
            result = self.start()
            self.assertEqual(result.returncode, 78)
            self.assertFalse(self.called.exists())
            self.assertFalse((self.data / '.agent-line-write-probe').exists())
        finally:
            self.data.chmod(0o700)

    def test_compose_cannot_skip_the_guard(self):
        compose = (ROOT / 'deploy/sim/compose.yaml').read_text()
        self.assertIn("entrypoint: ['/bin/sh', '/usr/local/bin/agent-line-redis-start.sh']", compose)
        self.assertIn('source: ./redis-start.sh', compose)
        self.assertIn("command: ['redis-server', '--appendonly', 'yes']", compose)
