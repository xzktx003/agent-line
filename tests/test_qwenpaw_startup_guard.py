"""Normal restarts must never bootstrap replacement roles or credentials."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.test_qwenpaw_manager import module


class StartupGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / 'var/qwenpaw'
        self.secrets = self.root / 'var/qwenpaw-secrets'
        self.workspace = self.root / 'external/workspace'
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in {'ROOT': self.root, 'DATA': self.data,
                            'SECRETS': self.secrets,
                            'STATE': self.data / 'agent-line-process.json',
                            'CREDENTIALS': self.secrets / 'local-login.json',
                            'LOG': self.data / 'service.log'}.items():
            self.stack.enter_context(patch.object(module, name, value))
        self.spawn = self.stack.enter_context(patch.object(module.subprocess, 'Popen'))
        self.run = self.stack.enter_context(patch.object(module.subprocess, 'run'))

    def private_json(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    def installation(self):
        self.data.mkdir(parents=True, mode=0o700)
        self.secrets.mkdir(mode=0o700)
        self.workspace.mkdir(parents=True)
        self.config = {'agents': {'profiles': {
            'role': {'id': 'role', 'workspace_dir': str(self.workspace)}}},
            'plugins': {'agent-line': {
                'execution_bridge': {'receipt_db': str(self.data / 'receipts.sqlite')},
                'memory_governance': {'state_file': str(self.data / 'governance.sqlite')}}}}
        self.private_json(self.data / 'config.json', self.config)
        self.private_json(self.workspace / 'agent.json', {
            'id': 'role', 'workspace_dir': str(self.workspace), 'name': '中文角色'})
        self.private_json(self.secrets / 'local-login.json', {'username': 'local', 'password': 'test-only'})
        self.private_json(self.secrets / 'auth.json', {
            'user': {'username': 'local', 'password_hash': 'hash', 'password_salt': 'salt'},
            'jwt_secret': 'encrypted-test-placeholder'})
        for path in (self.secrets / '.master_key', self.data / 'receipts.sqlite',
                     self.data / 'governance.sqlite'):
            path.write_bytes(b'existing-private-content')
            path.chmod(0o600)

    def snapshot(self):
        return {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mode)
                for p in self.root.rglob('*') if p.is_file() and not p.is_symlink()}

    def assert_start_blocked(self):
        before = self.snapshot()
        with self.assertRaises((SystemExit, ValueError)):
            module.start()
        self.assertEqual(self.spawn.call_count, 0)
        self.assertEqual(self.run.call_count, 0)
        self.assertFalse((self.data / 'service.log').exists())
        self.assertFalse((self.data / 'agent-line-process.json').exists())
        self.assertEqual(before, self.snapshot())

    def test_missing_core_files_block_before_spawn_and_do_not_recreate(self):
        self.installation()
        paths = [self.data / 'config.json', self.workspace / 'agent.json',
                 self.secrets / 'auth.json', self.secrets / '.master_key',
                 self.secrets / 'local-login.json', self.data / 'receipts.sqlite',
                 self.data / 'governance.sqlite']
        for path in paths:
            with self.subTest(path=path.name):
                content = path.read_bytes()
                path.unlink()
                self.assert_start_blocked()
                path.write_bytes(content)
                path.chmod(0o600)

    def test_readonly_core_file_or_workspace_never_starts(self):
        self.installation()
        for path in (self.data / 'config.json', self.workspace / 'agent.json',
                     self.data / 'receipts.sqlite', self.secrets, self.workspace):
            with self.subTest(path=path.name):
                mode = path.stat().st_mode & 0o777
                path.chmod(mode & ~0o222)
                self.assert_start_blocked()
                path.chmod(mode)

    def test_empty_or_mismatched_role_and_unregistered_auth_block(self):
        self.installation()
        for path, replacement in (
            (self.data / 'config.json', {'agents': {'profiles': {}}}),
            (self.workspace / 'agent.json', {'id': 'replacement', 'workspace_dir': str(self.workspace)}),
            (self.workspace / 'agent.json', {'id': 'role', 'workspace_dir': str(self.root / 'wrong')}),
            (self.secrets / 'auth.json', {'user': None}),
            (self.secrets / 'auth.json', {'user': {'username': 'other', 'password_hash': 'h', 'password_salt': 's'}, 'jwt_secret': 'j'}),
        ):
            with self.subTest(path=path.name, replacement=replacement):
                original = path.read_bytes()
                self.private_json(path, replacement)
                self.assert_start_blocked()
                path.write_bytes(original)

    def test_restore_markers_always_block_init_and_start_without_writes(self):
        self.installation()
        for parent in (self.root, self.root / 'var', self.data, self.secrets, self.workspace):
            marker = parent / 'RESTORE-DO-NOT-START.json'
            marker.write_text('{}')
            with self.subTest(parent=parent):
                self.assert_start_blocked()
                before = self.snapshot()
                with self.assertRaises((SystemExit, ValueError)):
                    module.init()
                self.assertEqual(before, self.snapshot())
                self.run.assert_not_called()
            marker.unlink()

    def test_existing_partial_install_never_calls_init_or_replaces_secrets(self):
        self.secrets.mkdir(parents=True)
        self.private_json(self.secrets / 'auth.json', {'user': {'username': 'keep'}})
        before = self.snapshot()
        with self.assertRaises((SystemExit, ValueError)):
            module.init()
        self.run.assert_not_called()
        self.assertFalse(self.data.exists())
        self.assertEqual(before, self.snapshot())

    def test_existing_healthy_install_cannot_be_force_reinstalled_by_init(self):
        self.installation()
        before = self.snapshot()
        with self.assertRaises((SystemExit, ValueError)):
            module.init()
        self.run.assert_not_called()
        self.assertEqual(before, self.snapshot())

    def test_empty_or_invalid_core_config_and_loose_secrets_block(self):
        self.installation()
        for path in (self.data / 'config.json', self.workspace / 'agent.json',
                     self.secrets / 'auth.json', self.secrets / '.master_key',
                     self.secrets / 'local-login.json'):
            original = path.read_bytes()
            for content in (b'', b'invalid-json'):
                # The master key is opaque: only emptiness, permissions and
                # existence are checked, without replacing native crypto.
                if path.name == '.master_key' and content:
                    continue
                with self.subTest(path=path.name, content=content):
                    path.write_bytes(content)
                    self.assert_start_blocked()
            path.write_bytes(original)
        for path in (self.secrets, self.secrets / 'auth.json',
                     self.secrets / '.master_key', self.secrets / 'local-login.json'):
            mode = path.stat().st_mode & 0o777
            with self.subTest(path=path.name, mode='public'):
                path.chmod(mode | 0o044)
                self.assert_start_blocked()
                path.chmod(mode)

    def test_empty_log_and_missing_process_receipt_do_not_replace_roles(self):
        self.installation()
        module.LOG.touch(mode=0o600)
        before = self.snapshot()
        module.start_preflight()
        self.assertEqual(before, self.snapshot())
        self.assertFalse(module.STATE.exists())

    def test_healthy_running_install_retains_process_and_all_bytes(self):
        self.installation()
        self.private_json(module.STATE, {'pid': 123, 'start_ticks': '1', 'port': 1234})
        before = self.snapshot()
        with patch.object(module, 'live', return_value=True):
            module.start()
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.spawn.call_count, 0)

    def test_fresh_empty_directories_supported_and_dangling_marker_blocks(self):
        self.data.mkdir(parents=True)
        self.secrets.mkdir()
        marker = self.root / 'RESTORE-DO-NOT-START.json'
        marker.symlink_to(self.root / 'missing-marker-target')
        with self.assertRaises(ValueError):
            module.init()
        self.assertEqual(self.run.call_count, 0)
        marker.unlink()
        module.init()
        self.assertEqual(self.run.call_count, 3)

    def test_existing_live_state_does_not_bypass_missing_config(self):
        self.installation()
        self.private_json(module.STATE, {'pid': 1})
        (self.data / 'config.json').unlink()
        before = self.snapshot()
        with patch.object(module, 'live', return_value=True) as live:
            with self.assertRaises((SystemExit, ValueError)):
                module.start()
            live.assert_not_called()
        self.assertEqual(before, self.snapshot())
        self.spawn.assert_not_called()

    def test_start_preflight_is_readonly_and_allows_current_role_edits(self):
        self.installation()
        before = self.snapshot()
        module.start_preflight()
        self.assertEqual(before, self.snapshot())
        self.private_json(self.workspace / 'agent.json', {
            'id': 'role', 'workspace_dir': str(self.workspace),
            'name': '新名称', 'backend': 'remelight', 'running': {'model': 'future-model'}})
        role2 = self.root / 'external/role2'
        self.private_json(role2 / 'agent.json', {'id': 'role2', 'workspace_dir': str(role2)})
        self.config['agents']['profiles']['role2'] = {'id': 'role2', 'workspace_dir': str(role2)}
        self.private_json(self.data / 'config.json', self.config)
        module.start_preflight()
        del self.config['agents']['profiles']['role2']
        self.private_json(self.data / 'config.json', self.config)
        module.start_preflight()

    def test_symlink_file_or_parent_or_install_root_rejected(self):
        self.installation()
        path = self.workspace / 'agent.json'
        real = path.with_name('saved-agent.json')
        path.rename(real)
        path.symlink_to(real)
        self.assert_start_blocked()
        path.unlink()
        real.rename(path)
        real_workspace = self.workspace.with_name('original')
        self.workspace.rename(real_workspace)
        self.workspace.symlink_to(real_workspace, target_is_directory=True)
        self.assert_start_blocked()
        self.workspace.unlink()
        real_workspace.rename(self.workspace)
        real_data = self.data.with_name('original-data')
        self.data.rename(real_data)
        self.data.symlink_to(real_data, target_is_directory=True)
        self.assert_start_blocked()

    def test_fresh_init_uses_official_cli_and_does_not_start_app(self):
        module.init()
        self.assertEqual(self.run.call_count, 3)
        self.assertEqual(self.run.call_args_list[0].args[0][1:4], ['init', '--defaults', '--accept-security'])
        self.spawn.assert_not_called()
        self.assertEqual((self.secrets / 'local-login.json').stat().st_mode & 0o777, 0o600)
