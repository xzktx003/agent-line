"""The normal database entrypoint cannot turn a missing cluster into a new one."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PostgresStartGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / 'pgdata'
        self.data.mkdir()
        self.called = self.root / 'official-entrypoint-called'
        entrypoint = self.root / 'entrypoint'
        entrypoint.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$ENTRYPOINT_CAPTURE"\n')
        entrypoint.chmod(0o700)
        source = (ROOT / 'deploy/sim/postgres-start.sh').read_text()
        self.assertEqual(source.count('/usr/local/bin/docker-entrypoint.sh'), 1)
        self.guard = self.root / 'guard.sh'
        self.guard.write_text(source.replace('/usr/local/bin/docker-entrypoint.sh', str(entrypoint)))

    def start(self):
        return subprocess.run(['/bin/sh', str(self.guard), 'postgres'],
                              env={**os.environ, 'PGDATA': str(self.data),
                                   'ENTRYPOINT_CAPTURE': str(self.called)},
                              capture_output=True, timeout=5)

    def cluster(self):
        for name in ('global', 'base', 'pg_wal'):
            (self.data / name).mkdir()
        (self.data / 'PG_VERSION').write_text('17\n')
        (self.data / 'global/pg_control').write_bytes(b'fixture-control')

    def test_empty_root_fails_before_official_initialization(self):
        result = self.start()
        self.assertEqual(result.returncode, 78)
        self.assertFalse(self.called.exists())
        self.assertEqual(list(self.data.iterdir()), [])

    def test_incomplete_or_replaced_cluster_cannot_initialize(self):
        self.cluster()
        for name in ('PG_VERSION', 'global/pg_control', 'base', 'pg_wal'):
            with self.subTest(missing=name):
                path = self.data / name
                saved = self.root / 'saved'
                path.rename(saved)
                result = self.start()
                self.assertEqual(result.returncode, 78)
                self.assertFalse(path.exists())
                self.assertFalse(self.called.exists())
                saved.rename(path)

    def test_empty_or_symlinked_control_files_are_rejected(self):
        self.cluster()
        for name in ('PG_VERSION', 'global/pg_control'):
            path = self.data / name
            original = path.read_bytes()
            path.write_bytes(b'')
            self.assertEqual(self.start().returncode, 78)
            target = self.root / 'external-file'
            target.write_bytes(original)
            path.unlink()
            path.symlink_to(target)
            self.assertEqual(self.start().returncode, 78)
            path.unlink()
            path.write_bytes(original)
        self.assertFalse(self.called.exists())

    def test_existing_cluster_delegates_unchanged_to_official_entrypoint(self):
        self.cluster()
        before = {str(p.relative_to(self.data)): p.read_bytes()
                  for p in self.data.rglob('*') if p.is_file()}
        self.assertEqual(self.start().returncode, 0)
        self.assertEqual(self.called.read_text(), 'postgres\n')
        self.assertEqual(before, {str(p.relative_to(self.data)): p.read_bytes()
                                 for p in self.data.rglob('*') if p.is_file()})

    def test_writable_cluster_removes_the_durability_probe(self):
        self.cluster()
        self.assertEqual(self.start().returncode, 0)
        self.assertFalse((self.data / '.agent-line-write-probe').exists())
        self.assertFalse((self.data / '.agent-line-write-probe').is_symlink())
        self.assertTrue(self.called.exists())

    def test_unwritable_cluster_refuses_before_initialization(self):
        self.cluster()
        before = {path: path.read_bytes() for path in self.data.rglob('*') if path.is_file()}
        self.data.chmod(0o555)
        try:
            result = self.start()
            self.assertEqual(result.returncode, 78)
            self.assertFalse(self.called.exists())
            self.assertFalse((self.data / '.agent-line-write-probe').exists())
        finally:
            self.data.chmod(0o700)
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_probe_symlink_is_not_followed_or_removed(self):
        self.cluster()
        target = self.root / 'outside-probe'
        target.write_text('keep\n')
        link = self.data / '.agent-line-write-probe'
        link.symlink_to(target)
        result = self.start()
        self.assertEqual(result.returncode, 78)
        self.assertFalse(self.called.exists())
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_text(), 'keep\n')

    def test_symlinked_global_directory_is_not_an_existing_local_cluster(self):
        self.cluster()
        saved = self.root / 'saved-global'
        (self.data / 'global').rename(saved)
        (self.data / 'global').symlink_to(saved, target_is_directory=True)
        self.assertEqual(self.start().returncode, 78)
        self.assertFalse(self.called.exists())

    def test_compose_cannot_skip_guard_on_container_restart(self):
        compose = (ROOT / 'deploy/sim/compose.yaml').read_text().split('\n  db:\n', 1)[1]
        self.assertIn("entrypoint: ['/bin/sh', '/usr/local/bin/agent-line-postgres-start.sh']", compose)
        self.assertIn('source: ./postgres-start.sh', compose)
        self.assertIn('read_only: true', compose)
        self.assertIn('create_host_path: false', compose)

