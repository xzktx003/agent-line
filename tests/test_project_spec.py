import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_line.test_executor import DockerTestExecutor
from deploy.project.spec import RESERVED_WORKSPACE_NAMES, normalize_spec


class ProjectSpecTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / 'installation'
        self.workspace = self.base / 'project'
        self.home = self.root / 'codex-home'
        for path in (self.root, self.workspace, self.home):
            path.mkdir(mode=0o700)
        self.binary = self.base / 'codex'
        self.sidecar = self.base / 'codex-code-mode-host'
        for path in (self.binary, self.sidecar):
            path.write_text('#!/bin/sh\nexit 0\n')
            path.chmod(0o755)
        self.auth = self.base / 'sim-auth.json'
        self.auth.write_text(json.dumps({'baseUrl': 'http://127.0.0.1:3000',
                                        'apiKey': 'private-sim-secret'}))
        self.auth.chmod(0o600)
        self.spec = {
            'schemaVersion': 1,
            'project': {'id': 'my-project', 'name': '我的项目', 'workspace': str(self.workspace)},
            'coordinator': {'id': 'project-parent', 'name': '协调', 'providerId': 'local', 'modelId': 'configured'},
            'executor': {'id': 'project-codex', 'name': '执行', 'model': 'explicit-model',
                         'sandbox': 'workspace-write', 'approvalPolicy': 'on-request',
                         'binary': str(self.binary), 'imageId': 'sha256:' + 'a' * 64,
                         'codexHome': str(self.home)},
            'qwenpaw': {'baseURL': 'http://127.0.0.1:52403/'},
            'sim': {'authFile': str(self.auth), 'workspaceId': 'workspace-1'},
            'verification': {'policyId': 'project-tests', 'imageId': 'sha256:' + 'b' * 64,
                             'tests': [{'id': 'unit', 'argv': ['python3', '-m', 'unittest']}]},
            'memoryQuery': '项目经验',
        }

    def normalize(self, value=None):
        return normalize_spec(self.spec if value is None else value, root=self.root)

    def snapshot(self):
        return {str(path.relative_to(self.base)): (path.lstat().st_mode,
                path.read_bytes() if path.is_file() else None)
                for path in self.base.rglob('*')}

    def test_existing_executor_identity_and_independent_copy_without_disk_or_network_writes(self):
        before = self.snapshot()
        original = copy.deepcopy(self.spec)
        with patch('subprocess.Popen', side_effect=AssertionError('No processes')):
            result = self.normalize()
        expected = DockerTestExecutor(self.home, 'sha256:' + 'b' * 64).environment_id
        self.assertEqual(result['verificationPolicy']['testEnvironmentId'], expected)
        self.assertEqual(result['verificationPolicy']['artifacts'], [])
        self.assertEqual(result['verificationPolicy']['tests'][0]['timeoutSeconds'], 60)
        self.assertEqual(result['qwenpaw']['baseURL'], 'http://127.0.0.1:52403')
        self.assertNotIn('private-sim-secret', json.dumps(result))
        self.assertEqual(self.spec, original)
        result['verificationPolicy']['tests'][0]['argv'].append('other')
        self.assertEqual(self.spec, original)
        self.assertEqual(before, self.snapshot())

    def test_exact_schemas_and_integer_version(self):
        for version in (True, 1.0, '1', 2):
            with self.subTest(version=version):
                value = copy.deepcopy(self.spec); value['schemaVersion'] = version
                with self.assertRaises(ValueError): self.normalize(value)
        for key in ('project', 'coordinator', 'executor', 'qwenpaw', 'sim', 'verification'):
            value = copy.deepcopy(self.spec); value[key]['unexpected'] = 'value'
            with self.subTest(key=key), self.assertRaises(ValueError): self.normalize(value)
        value = copy.deepcopy(self.spec); value['readiness'] = True
        with self.assertRaises(ValueError): self.normalize(value)

    def test_names_models_queries_and_identifiers_are_explicit(self):
        for section, key in (('project', 'id'), ('project', 'name'), ('coordinator', 'providerId'),
                             ('coordinator', 'modelId'), ('executor', 'model')):
            for invalid in ('', ' padded ', None, 3, 'line\nbreak'):
                value = copy.deepcopy(self.spec); value[section][key] = invalid
                with self.subTest(section=section, key=key, invalid=invalid), self.assertRaises(ValueError):
                    self.normalize(value)
        for identifier in ('default', 'x', 'a' * 65, 'a/b', '-bad', 'bad_'):
            value = copy.deepcopy(self.spec); value['coordinator']['id'] = identifier
            with self.subTest(identifier=identifier), self.assertRaises(ValueError): self.normalize(value)
        value = copy.deepcopy(self.spec); value['executor']['id'] = value['coordinator']['id']
        with self.assertRaises(ValueError): self.normalize(value)

    def test_optional_team_requires_explicit_distinct_role_ids_and_models(self):
        value = copy.deepcopy(self.spec)
        value['team'] = {
            'planner': {'id': 'project-planner', 'name': '规划', 'providerId': 'local', 'modelId': 'plan-model'},
            'reviewer': {'id': 'project-reviewer', 'name': '审查', 'providerId': 'local', 'modelId': 'review-model'}}
        self.assertEqual(self.normalize(value)['team'], value['team'])
        for key in ('planner', 'reviewer'):
            malformed = copy.deepcopy(value); del malformed['team'][key]['modelId']
            with self.subTest(key=key), self.assertRaises(ValueError): self.normalize(malformed)
        for duplicate in ('project-parent', 'project-codex', 'project-planner'):
            malformed = copy.deepcopy(value); malformed['team']['reviewer']['id'] = duplicate
            with self.subTest(duplicate=duplicate), self.assertRaises(ValueError): self.normalize(malformed)

    def test_registered_workspace_must_be_existing_canonical_and_owned(self):
        for path in ('relative', str(self.base / 'missing')):
            self.spec['project']['workspace'] = path
            with self.assertRaises((ValueError, OSError)): self.normalize()
        alias = self.base / 'alias'; alias.symlink_to(self.workspace, target_is_directory=True)
        self.spec['project']['workspace'] = str(alias)
        with self.assertRaises(ValueError): self.normalize()
        self.spec['project']['workspace'] = str(self.workspace)
        with patch('deploy.project.spec.os.getuid', return_value=os.getuid() + 1):
            with self.assertRaises(ValueError): self.normalize()

    def test_workspace_cannot_contain_or_be_contained_by_installation_or_codex_home(self):
        for path in (self.root, self.home, self.base):
            self.spec['project']['workspace'] = str(path)
            with self.subTest(path=path), self.assertRaises(ValueError): self.normalize()
        nested = self.workspace / 'home'; nested.mkdir(mode=0o700)
        self.spec['project']['workspace'] = str(self.workspace)
        self.spec['executor']['codexHome'] = str(nested)
        with self.assertRaises(ValueError): self.normalize()

    def test_private_codex_home_required(self):
        self.home.chmod(0o755)
        with self.assertRaises(ValueError): self.normalize()

    def test_fresh_codex_home_allows_only_private_regular_auth_without_reading_secrets(self):
        auth = self.home / 'auth.json'
        auth.write_text('private-codex-auth-content')
        auth.chmod(0o600)
        before = self.snapshot()
        result = self.normalize()
        self.assertNotIn('private-codex-auth-content', json.dumps(result))
        self.assertEqual(before, self.snapshot())
        auth.chmod(0o644)
        with self.assertRaises(ValueError): self.normalize()
        auth.unlink()
        auth.mkdir(mode=0o700)
        with self.assertRaises(ValueError): self.normalize()
        auth.rmdir()
        auth.symlink_to(self.auth)
        with self.assertRaises(ValueError): self.normalize()

    def test_fresh_codex_home_rejects_old_runtime_config_and_unknown_files(self):
        for name in ('sessions', 'archived_sessions', 'state_5.sqlite', 'config.toml',
                     'plugins', 'unknown'):
            path = self.home / name
            path.write_text('previous state')
            path.chmod(0o600)
            before = self.snapshot()
            with self.subTest(name=name), self.assertRaises(ValueError): self.normalize()
            self.assertEqual(before, self.snapshot())
            path.unlink()

    def test_fresh_process_normalization_never_imports_qwenpaw_or_bootstraps_native_state(self):
        source = '''
import importlib.abc, json, sys
class BlockNative(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'qwenpaw' or fullname.startswith('qwenpaw.'):
            raise AssertionError('Native package import forbidden in preflight')
assert not any(n == 'qwenpaw' or n.startswith('qwenpaw.') for n in sys.modules)
sys.meta_path.insert(0, BlockNative())
from deploy.project.spec import normalize_spec
spec, root = json.load(sys.stdin)
normalized = normalize_spec(spec, root=root)
assert normalized['project']['id'] == spec['project']['id']
assert not any(n == 'qwenpaw' or n.startswith('qwenpaw.') for n in sys.modules)
print('native_imports=0')
'''
        before = self.snapshot()
        result = subprocess.run([sys.executable, '-c', source],
                                input=json.dumps([self.spec, str(self.root)]),
                                capture_output=True, text=True, timeout=30,
                                cwd=Path(__file__).resolve().parents[1], check=True)
        self.assertEqual(result.stdout.strip(), 'native_imports=0')
        self.assertEqual(before, self.snapshot())

    def test_docker_mount_separator_in_workspace_or_home_is_rejected(self):
        comma = self.base / 'with,comma'; comma.mkdir(mode=0o700)
        for section, key in (('project', 'workspace'), ('executor', 'codexHome')):
            value = copy.deepcopy(self.spec); value[section][key] = str(comma)
            with self.subTest(section=section), self.assertRaises(ValueError): self.normalize(value)

    def test_all_native_reserved_names_including_dangling_symlinks_rejected(self):
        for name in RESERVED_WORKSPACE_NAMES:
            path = self.workspace / name
            path.symlink_to(self.base / 'missing-target')
            with self.subTest(name=name), self.assertRaises(ValueError): self.normalize()
            path.unlink()

    def test_business_project_instructions_and_code_are_preserved(self):
        for name in ('AGENTS.md', 'SOUL.md', 'README.md', 'app.py'):
            (self.workspace / name).write_text('existing project content')
        before = self.snapshot()
        self.normalize()
        self.assertEqual(before, self.snapshot())

    def test_executable_owned_binary_and_sidecar_are_required(self):
        for path in (self.binary, self.sidecar):
            for mode in (0o644, 0o777):
                path.chmod(mode)
                with self.subTest(path=path.name, mode=mode), self.assertRaises(ValueError): self.normalize()
            path.chmod(0o755)
        self.sidecar.unlink()
        with self.assertRaises(OSError): self.normalize()

    def test_images_and_actual_codex_policies_are_required(self):
        for section in ('executor', 'verification'):
            for image in ('latest', 'sha256:abc', 'sha256:' + 'A' * 64):
                value = copy.deepcopy(self.spec); value[section]['imageId'] = image
                with self.subTest(section=section, image=image), self.assertRaises(ValueError): self.normalize(value)
        for key in ('sandbox', 'approvalPolicy'):
            value = copy.deepcopy(self.spec); value['executor'][key] = 'anything'
            with self.assertRaises(ValueError): self.normalize(value)

    def test_qwenpaw_loopback_only_without_embedded_credentials_or_paths(self):
        for origin in ('http://10.0.0.1:5000', 'http://example.com', 'http://localhost/api',
                       'http://user:password@127.0.0.1', 'http://127.0.0.1/?key=secret',
                       'http://127.0.0.1/#fragment'):
            self.spec['qwenpaw']['baseURL'] = origin
            with self.subTest(origin=origin), self.assertRaises(ValueError): self.normalize()
        for origin in ('http://localhost:5000', 'http://127.0.0.2:5000', 'http://[::1]:5000',
                       'https://127.0.0.1:5000', 'http://127.0.0.1', 'http://127.0.0.1:0'):
            self.spec['qwenpaw']['baseURL'] = origin
            with self.subTest(origin=origin), self.assertRaises(ValueError): self.normalize()
        self.spec['qwenpaw']['baseURL'] = 'http://127.0.0.1:5000'
        self.assertEqual(self.normalize()['qwenpaw']['baseURL'], self.spec['qwenpaw']['baseURL'])

    def test_sim_credentials_exact_private_json_and_no_secret_in_failure(self):
        self.auth.chmod(0o644)
        with self.assertRaises(ValueError): self.normalize()
        self.auth.chmod(0o600)
        for content in ('{"baseUrl":"http://localhost","apiKey":"private-sim-secret","extra":1}',
                        '{"baseUrl":"http://localhost","apiKey":"a","apiKey":"private-sim-secret"}',
                        '{"baseUrl":"http://private-sim-secret@localhost","apiKey":"private-sim-secret"}'):
            self.auth.write_text(content)
            with self.assertRaises(ValueError) as caught: self.normalize()
            self.assertNotIn('private-sim-secret', str(caught.exception))

    def test_test_policy_reuses_frozen_verifier_validation_and_failure_is_read_only(self):
        for tests in ([], [{'id': 'bad', 'argv': []}],
                      [{'id': 'unit', 'argv': ['true'], 'timeoutSeconds': True}],
                      [{'id': 'unit', 'argv': ['true'], 'shell': True}]):
            self.spec['verification']['tests'] = tests
            before = self.snapshot()
            with self.subTest(tests=tests), self.assertRaises(ValueError): self.normalize()
            self.assertEqual(before, self.snapshot())


if __name__ == '__main__':
    unittest.main()
