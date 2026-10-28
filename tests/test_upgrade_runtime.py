"""Runtime source selection and process isolation without starting QwenPaw."""
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('upgrade_runtime', ROOT / 'probes/upgrade/qwenpaw_runtime.py')
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)


class UpgradeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'scripts').mkdir()
        shutil.copyfile(ROOT / 'scripts/prepare_upgrade_candidate.py', self.root / 'scripts/prepare_upgrade_candidate.py')
        self.instance = self.root / 'var/upgrade-runtime/probe'
        self.commit = 'a' * 40
        with patch.object(module.Runtime, '_verify_candidate'):
            self.runtime = module.Runtime(self.root, self.commit, self.instance)
        self.runtime._verify_candidate = Mock()

    def initialized(self):
        r = self.runtime; r._prepare_directories()
        (r.data / 'config.json').write_text('{}')
        r._private(r.login_path, {'username': 'scratch', 'password': 'scratch-only'})

    def test_outside_or_symlinked_storage_is_rejected_before_import(self):
        for outside in (self.root / 'var/qwenpaw', self.root / 'var/upgrade-runtime'):
            with self.assertRaises(ValueError): module.Runtime(self.root, self.commit, outside)
        self.instance.parent.mkdir(parents=True)
        self.instance.symlink_to(self.root)
        with self.assertRaises(ValueError): module.Runtime(self.root, self.commit, self.instance)

    def test_environment_excludes_parent_credentials_and_runtime(self):
        with patch.dict(module.os.environ, {'PYTHONPATH': '/wrong', 'OPENAI_API_KEY': 'not-real',
                         'CODEX_HOME': '/formal', 'QWENPAW_RUNTIME_ID': 'formal', 'QWENPAW_DESKTOP': '1'}):
            env = self.runtime.environment()
        self.assertEqual(env['PYTHONPATH'], str(self.runtime.candidate / 'src'))
        self.assertEqual(env['QWENPAW_WORKING_DIR'], str(self.runtime.data))
        self.assertEqual(env['QWENPAW_SECRET_DIR'], str(self.runtime.secret_dir))
        self.assertEqual(env['PYTHONDONTWRITEBYTECODE'], '1')
        self.assertEqual(env['QWENPAW_DISABLE_KEYRING'], 'true')
        for key in ('OPENAI_API_KEY', 'CODEX_HOME', 'QWENPAW_RUNTIME_ID', 'QWENPAW_DESKTOP'):
            self.assertNotIn(key, env)

    def test_candidate_requires_ready_state_and_clean_verifier(self):
        r = self.runtime; r.candidate.parent.mkdir(parents=True)
        remote = r.checker.OFFICIAL_REMOTES['qwenpaw']
        state = {'status': 'ready', 'commit': self.commit, 'repository': 'qwenpaw', 'remote': remote,
                 'path': str(r.candidate.relative_to(self.root))}
        path = r.candidate.with_name(r.candidate.name + '.state.json')
        path.write_text(json.dumps(state))
        with patch.object(r.checker, 'verify') as verify:
            module.Runtime._verify_candidate(r)
            verify.assert_called_once_with(r.candidate, remote, self.commit)
        state['status'] = 'failed'; path.write_text(json.dumps(state))
        with self.assertRaises(ValueError): module.Runtime._verify_candidate(r)

    def test_source_resolution_must_use_exact_candidate(self):
        r = self.runtime
        with patch.object(r, '_run', return_value=json.dumps({'modulePath': '/installed/qwenpaw/__init__.py', 'version': '2.3.0b1'})):
            with self.assertRaises(RuntimeError): r.verify_source()
        path = r.candidate / 'src/qwenpaw/__init__.py'
        with patch.object(r, '_run', return_value=json.dumps({'modulePath': str(path), 'version': '2.3.0b1', 'dependencyVersions': [{'requirement': 'click>=8', 'installed': '8.1'}], 'dependencyErrors': []})):
            result = r.verify_source()
        self.assertEqual(result['commit'], self.commit)
        self.assertEqual(result['dependencies'], 'shared-existing-venv-not-upgraded')

    def test_initialize_uses_official_cli_and_preserves_credentials(self):
        r = self.runtime
        with patch.object(r, 'verify_source'), patch.object(r, '_run', return_value='') as run:
            r.initialize()
            credentials = r.credentials
            self.assertEqual(r.login_path.stat().st_mode & 0o777, 0o600)
            self.assertIn(['-m', 'qwenpaw', 'init', '--defaults', '--accept-security'], [call.args[0] for call in run.call_args_list])
            r.initialize()
            self.assertEqual(r.credentials, credentials)
        self.assertTrue((r.instance / 'initialized.json').exists())
        installed = [call.args[0] for call in run.call_args_list]
        self.assertIn(['-m', 'qwenpaw', 'plugin', 'install', str(r.root / 'plugins/agent-line'), '--force'], installed)

    def test_explicit_plugin_directory_is_the_only_install_source(self):
        plugin = self.root / 'isolated-plugin'
        plugin.mkdir()
        (plugin / 'plugin.json').write_text('{"id": "agent-line"}')
        with patch.object(module.Runtime, '_verify_candidate'):
            runtime = module.Runtime(self.root, self.commit, self.root / 'var/upgrade-runtime/custom', plugin)
        runtime._verify_candidate = Mock()
        with patch.object(runtime, 'verify_source'), patch.object(runtime, '_run', return_value='') as run:
            runtime.initialize()
        installed = [call.args[0] for call in run.call_args_list]
        self.assertIn(['-m', 'qwenpaw', 'plugin', 'install', str(plugin.resolve()), '--force'], installed)
        self.assertNotIn(str(self.root / 'plugins/agent-line'), [part for args in installed for part in args])

    def test_stop_refuses_live_unowned_pid(self):
        self.initialized(); r = self.runtime
        r._private(r.state_path, {'pid': 123, 'startTicks': 'old'})
        with patch.object(r, '_alive', return_value=True), patch.object(r, '_owned', return_value=False), patch.object(module.os, 'kill') as kill:
            with self.assertRaises(RuntimeError): r.stop()
            kill.assert_not_called()

    def test_stop_only_signals_owned_identity_and_waits(self):
        self.initialized(); r = self.runtime
        r._private(r.state_path, {'pid': 123, 'startTicks': 'owned'})
        with patch.object(r, '_alive', side_effect=[True, False]), patch.object(r, '_owned', return_value=True), patch.object(module.os, 'kill') as kill:
            self.assertTrue(r.stop())
            kill.assert_called_once_with(123, module.signal.SIGTERM)

    def test_start_restored_data_without_process_or_marker_uses_source_only(self):
        self.initialized(); r = self.runtime
        original = Path.read_text
        def read(path, *args, **kwargs):
            if str(path) == '/proc/987654321/stat': return ' '.join(['x'] * 21 + ['birth'])
            return original(path, *args, **kwargs)
        process = Mock(pid=987654321)
        with patch.object(r, 'verify_source'), patch.object(r, '_alive', return_value=True), patch.object(module.subprocess, 'Popen', return_value=process) as popen, patch.object(Path, 'read_text', read), patch.object(module.httpx, 'get', return_value=Mock(status_code=401)):
            url = r.start()
        self.assertTrue(url.startswith('http://127.0.0.1:'))
        command = popen.call_args.args[0]
        self.assertEqual(command[:5], [str(r.python), '-B', '-m', 'qwenpaw', 'app'])
        self.assertEqual(popen.call_args.kwargs['env']['PYTHONPATH'], str(r.candidate / 'src'))
        self.assertEqual(json.loads(r.state_path.read_text())['commit'], self.commit)

    def test_recycled_or_dead_pid_is_not_signaled(self):
        self.initialized(); r = self.runtime
        r._private(r.state_path, {'pid': 123, 'startTicks': 'old'})
        with patch.object(r, '_alive', return_value=False), patch.object(module.os, 'kill') as kill:
            self.assertFalse(r.stop()); kill.assert_not_called()

    def test_incompatible_or_missing_dependency_report_is_rejected(self):
        r = self.runtime
        for errors in ([{'name': 'missing', 'reason': 'missing'}], [{'name': 'old', 'reason': 'version_mismatch'}], None):
            value = {'modulePath': str(r.candidate / 'src/qwenpaw/__init__.py'), 'version': '2.3.0b1',
                     'dependencyVersions': [{'requirement': 'click>=8', 'installed': '7'}], 'dependencyErrors': errors}
            with self.subTest(errors=errors), patch.object(r, '_run', return_value=json.dumps(value)):
                with self.assertRaises(RuntimeError): r.verify_source()
