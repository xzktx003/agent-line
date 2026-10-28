import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

EXECUTOR_DIR = Path(__file__).resolve().parents[1] / 'deploy' / 'executor'
sys.path.insert(0, str(EXECUTOR_DIR))
import launcher

from tests import test_execution_readiness as readiness_test_module


class SandboxHelperLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.package = self.root / 'vendor' / 'codex-linux'
        self.binary = self.package / 'bin' / 'codex'
        self.binary.parent.mkdir(parents=True)
        self.binary.write_text('codex')
        self.binary.chmod(0o755)
        self.workspace = self.root / 'workspace'
        self.home = self.root / 'home'
        self.workspace.mkdir()
        self.home.mkdir()
        auth = self.home / 'auth.json'
        auth.write_text('{"token":"test"}')
        auth.chmod(0o600)
        (self.home / 'config.toml').write_text('model = "test"\n')
        (self.home / 'config.toml').chmod(0o600)
        (self.home / 'state_5.sqlite').write_bytes(b'not-a-real-database')
        (self.home / 'state_5.sqlite').chmod(0o600)
        self.helper = self.package / 'codex-resources' / 'bwrap'

    def _config(self):
        config = self.root / 'config.json'
        config.write_text(json.dumps({'workspace': str(self.workspace), 'codex_home': str(self.home),
            'binary': str(self.binary), 'image': 'sha256:' + 'a' * 64}))
        config.chmod(0o600)
        return config

    def test_present_helper_is_validated_and_mounted_readonly_on_path(self):
        self.helper.parent.mkdir()
        self.helper.write_text('official helper')
        self.helper.chmod(0o775)
        self.assertEqual(launcher._sandbox_helper(self.binary), self.helper)
        with patch.object(launcher, 'CONFIG', self._config()):
            arguments = launcher.command(['app-server'])
        mounts = [arguments[index + 1] for index, value in enumerate(arguments[:-1]) if value == '--mount']
        self.assertIn(f'type=bind,src={self.helper},dst=/usr/local/bin/bwrap,readonly', mounts)

    def test_absent_helper_keeps_legacy_mount_list(self):
        self.assertIsNone(launcher._sandbox_helper(self.binary))
        with patch.object(launcher, 'CONFIG', self._config()):
            arguments = launcher.command(['app-server'])
        self.assertFalse(any('dst=/usr/local/bin/bwrap' in value for value in arguments))

    def test_present_invalid_helper_is_rejected(self):
        self.helper.parent.mkdir()
        self.helper.write_text('helper')
        self.helper.chmod(0o777)
        with self.assertRaises(RuntimeError):
            launcher._sandbox_helper(self.binary)
        self.helper.chmod(0o600)
        with self.assertRaises(RuntimeError):
            launcher._sandbox_helper(self.binary)
        self.helper.unlink()
        self.helper.symlink_to(self.binary)
        with self.assertRaises(RuntimeError):
            launcher._sandbox_helper(self.binary)
        self.helper.unlink()
        resources = self.package / 'codex-resources'
        resources.rename(self.package / 'resources-real')
        resources.symlink_to(self.package / 'resources-real', target_is_directory=True)
        with self.assertRaises(RuntimeError):
            launcher._sandbox_helper(self.binary)
        resources.unlink()
        (self.package / 'resources-real').rename(resources)
        self.helper.write_text('helper')
        self.helper.chmod(0o775)
        with patch.object(launcher.os, 'getuid', return_value=os.getuid() + 1):
            with self.assertRaises(RuntimeError):
                launcher._sandbox_helper(self.binary)

    def test_helper_mount_path_with_comma_is_rejected(self):
        self.helper.parent.mkdir()
        self.helper.write_text('helper')
        self.helper.chmod(0o755)
        comma_package = self.root / 'comma,package'
        comma_binary = comma_package / 'bin' / 'codex'
        comma_binary.parent.mkdir(parents=True)
        comma_binary.write_text('codex')
        comma_binary.chmod(0o755)
        comma_helper = comma_package / 'codex-resources' / 'bwrap'
        comma_helper.parent.mkdir()
        comma_helper.write_text('helper')
        comma_helper.chmod(0o755)
        with self.assertRaises(RuntimeError):
            launcher._sandbox_helper(comma_binary)

    def _mounts(self, arguments):
        return [arguments[index + 1] for index, value in enumerate(arguments[:-1]) if value == '--mount']

    def test_only_this_workspace_session_directory_is_mounted_over_shared_sessions(self):
        with patch.object(launcher, 'CONFIG', self._config()):
            arguments = launcher.command(['app-server'])
        mounts = self._mounts(arguments)
        self.assertNotIn(f'type=bind,src={self.home},dst=/codex-home', mounts)
        view_mounts = [value for value in mounts if value.endswith(',dst=/codex-home')]
        self.assertEqual(len(view_mounts), 1)
        view_dir = Path(view_mounts[0].removeprefix('type=bind,src=').removesuffix(',dst=/codex-home'))
        self.assertTrue(view_dir.is_relative_to(self.home / 'views'))
        self.assertEqual((view_dir / 'config.toml').read_text(), 'model = "test"\n')
        self.assertFalse((view_dir / 'state_5.sqlite').exists())
        session_mounts = [value for value in mounts if value.endswith(',dst=/codex-home/sessions')]
        self.assertEqual(len(session_mounts), 1)
        source = session_mounts[0].removeprefix('type=bind,src=').removesuffix(',dst=/codex-home/sessions')
        session_dir = Path(source)
        self.assertTrue(session_dir.is_relative_to(self.home / 'sessions' / 'projects'))
        self.assertNotEqual(session_dir, self.home / 'sessions')
        self.assertEqual(session_dir.stat().st_mode & 0o777, 0o700)
        self.assertLess(mounts.index(view_mounts[0]), mounts.index(session_mounts[0]))
        self.assertIn(f'type=bind,src={self.home / "auth.json"},dst=/codex-home/auth.json,readonly', mounts)
        self.assertNotIn(f'type=bind,src={self.home / "state_5.sqlite"},dst=/codex-home/state_5.sqlite', mounts)
        self.assertFalse(any('state_5.sqlite' in value for value in mounts))
        other = self.root / 'other-workspace'
        other.mkdir()
        config = self.root / 'config.json'
        config.write_text(json.dumps({'workspace': str(other), 'codex_home': str(self.home),
            'binary': str(self.binary), 'image': 'sha256:' + 'a' * 64}))
        with patch.object(launcher, 'CONFIG', config):
            other_mounts = self._mounts(launcher.command(['app-server']))
        other_session = [value for value in other_mounts if value.endswith(',dst=/codex-home/sessions')]
        self.assertEqual(len(other_session), 1)
        self.assertNotEqual(other_session[0], session_mounts[0])

    def test_installation_and_model_cache_are_writable_copies_inside_the_view(self):
        installation = self.home / 'installation_id'
        installation.write_text('install-id\n')
        installation.chmod(0o644)
        cache = self.home / 'models_cache.json'
        cache.write_text('{"models":[]}\n')
        cache.chmod(0o644)
        with patch.object(launcher, 'CONFIG', self._config()):
            arguments = launcher.command(['app-server'])
        mounts = self._mounts(arguments)
        self.assertIn(f'type=bind,src={self.home / "auth.json"},dst=/codex-home/auth.json,readonly', mounts)
        self.assertFalse(any('installation_id' in value or 'models_cache.json' in value for value in mounts))
        view_mount = next(value for value in mounts if value.endswith(',dst=/codex-home'))
        view = Path(view_mount.removeprefix('type=bind,src=').removesuffix(',dst=/codex-home'))
        self.assertEqual((view / 'installation_id').read_text(), 'install-id\n')
        self.assertEqual((view / 'models_cache.json').read_text(), '{"models":[]}\n')
        self.assertEqual((view / 'installation_id').stat().st_mode & 0o777, 0o600)
        self.assertEqual((view / 'models_cache.json').stat().st_mode & 0o777, 0o600)
        (view / 'models_cache.json').write_text('{"models":["kept"]}\n')
        with patch.object(launcher, 'CONFIG', self._config()):
            launcher.command(['app-server'])
        self.assertEqual((view / 'models_cache.json').read_text(), '{"models":["kept"]}\n')
        cache.chmod(0o666)
        with patch.object(launcher, 'CONFIG', self._config()):
            with self.assertRaises(RuntimeError):
                launcher.command(['app-server'])

    def test_symlinked_project_session_directory_is_rejected(self):
        leaf_parent = self.home / 'sessions' / 'projects'
        leaf_parent.mkdir(parents=True)
        digest = launcher.hashlib.sha256(str(self.workspace.resolve()).encode()).hexdigest()
        (leaf_parent / digest).symlink_to(self.workspace, target_is_directory=True)
        with patch.object(launcher, 'CONFIG', self._config()):
            with self.assertRaises(RuntimeError):
                launcher.command(['app-server'])



    def _write_config(self, payload):
        config = self.root / 'config.json'
        config.write_text(json.dumps(payload))
        config.chmod(0o600)
        return config

    def test_task_field_mounts_only_its_directory_and_splits_sessions(self):
        fields = self.workspace / 'task-fields'
        alpha = fields / 'alpha'
        beta = fields / 'beta'
        alpha.mkdir(parents=True)
        beta.mkdir()
        (fields / 'sibling-secret.txt').write_text('secret\n')
        base = {'workspace': str(self.workspace), 'codex_home': str(self.home),
                'binary': str(self.binary), 'image': 'sha256:' + 'a' * 64}
        with patch.object(launcher, 'CONFIG', self._write_config({**base, 'taskField': 'alpha'})):
            mounts = self._mounts(launcher.command(['app-server']))
        cover = f',dst={self.workspace}/task-fields'
        cover_mounts = [value for value in mounts if value.endswith(cover)]
        self.assertEqual(len(cover_mounts), 1)
        cover_src = Path(cover_mounts[0].removeprefix('type=bind,src=').removesuffix(cover))
        self.assertTrue(cover_src.is_relative_to(self.home / 'task-field-covers'))
        self.assertTrue(cover_src.is_dir())
        self.assertEqual(list(cover_src.iterdir()), [cover_src / 'alpha'])
        field_mount = f'type=bind,src={alpha},dst={self.workspace}/task-fields/alpha'
        self.assertIn(field_mount, mounts)
        self.assertLess(mounts.index(f'type=bind,src={self.workspace},dst={self.workspace}'), mounts.index(cover_mounts[0]))
        self.assertLess(mounts.index(cover_mounts[0]), mounts.index(field_mount))
        self.assertFalse(any(str(beta) in value or 'sibling-secret' in value for value in mounts))
        alpha_session = next(value for value in mounts if value.endswith(',dst=/codex-home/sessions'))
        alpha_view = next(value for value in mounts if value.endswith(',dst=/codex-home'))
        with patch.object(launcher, 'CONFIG', self._write_config(base)):
            plain = self._mounts(launcher.command(['app-server']))
        self.assertFalse(any('task-fields' in value for value in plain))
        self.assertNotEqual(alpha_session, next(value for value in plain if value.endswith(',dst=/codex-home/sessions')))
        self.assertNotEqual(alpha_view, next(value for value in plain if value.endswith(',dst=/codex-home')))
        with patch.object(launcher, 'CONFIG', self._write_config({**base, 'taskField': 'beta'})):
            beta_mounts = self._mounts(launcher.command(['app-server']))
        self.assertIn(f'type=bind,src={beta},dst={self.workspace}/task-fields/beta', beta_mounts)
        self.assertFalse(any(str(alpha) in value for value in beta_mounts))
        self.assertNotEqual(alpha_session, next(value for value in beta_mounts if value.endswith(',dst=/codex-home/sessions')))

    def test_task_field_rejects_unsafe_segments_and_linked_directories(self):
        fields = self.workspace / 'task-fields'
        real = fields / 'alpha'
        real.mkdir(parents=True)
        (fields / 'linked').symlink_to(real, target_is_directory=True)
        base = {'workspace': str(self.workspace), 'codex_home': str(self.home),
                'binary': str(self.binary), 'image': 'sha256:' + 'a' * 64}
        for value in ('', '../alpha', 'alpha/beta', 'alpha,beta', '.', '..'):
            with patch.object(launcher, 'CONFIG', self._write_config({**base, 'taskField': value})):
                with self.assertRaises(RuntimeError):
                    launcher.command(['app-server'])
        with patch.object(launcher, 'CONFIG', self._write_config({**base, 'taskField': 'linked'})):
            with self.assertRaises(RuntimeError):
                launcher.command(['app-server'])
        with patch.object(launcher, 'CONFIG', self._write_config({**base, 'taskField': 'missing'})):
            with self.assertRaises(RuntimeError):
                launcher.command(['app-server'])


class SandboxHelperFingerprintTests(unittest.TestCase):
    def setUp(self):
        self.fixture = readiness_test_module.FingerprintTest()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        binary = self.fixture.root / 'bin' / 'codex'
        self.helper = binary.parent.parent / 'codex-resources' / 'bwrap'

    def fingerprint(self):
        return self.fixture.fingerprint()

    def test_absence_preserves_old_fingerprint_shape_and_helper_changes_freshness(self):
        before = self.fingerprint()
        self.assertIsNone(readiness_test_module.readiness._optional_sandbox_helper(
            self.fixture.root / 'bin' / 'codex'))
        self.helper.parent.mkdir()
        self.helper.write_text('bundled bwrap')
        self.helper.chmod(0o775)
        added = self.fingerprint()
        self.assertNotEqual(before, added)
        added_helper = readiness_test_module.readiness._optional_sandbox_helper(
            self.fixture.root / 'bin' / 'codex')
        self.assertIsNotNone(added_helper)
        self.helper.write_text('changed bwrap')
        changed = self.fingerprint()
        changed_helper = readiness_test_module.readiness._optional_sandbox_helper(
            self.fixture.root / 'bin' / 'codex')
        self.assertNotEqual(added_helper[1], changed_helper[1])
        future = self.fixture.fixture.now + 100
        os.utime(self.helper, (future, future))
        touched = self.fingerprint()
        self.assertNotEqual(changed, touched)
        self.assertGreater(touched['sourcesLatestAt'], changed['sourcesLatestAt'])

    def test_present_unsafe_helper_invalidates_fingerprint(self):
        self.helper.parent.mkdir()
        self.helper.write_text('bundled bwrap')
        self.helper.chmod(0o777)
        with self.assertRaises(ValueError):
            self.fingerprint()

