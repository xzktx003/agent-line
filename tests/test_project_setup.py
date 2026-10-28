import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from deploy.project.setup import ROOT, install, native_environment, preflight, public_status, write_new
from deploy.project.native import secure_role_workspace
from agent_line.store import TaskStore


PIN = 'qwenpaw-role-v1:' + 'a' * 64


class ProjectSetupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / 'installation'
        self.workspace = self.base / 'project'
        self.home = self.base / 'codex-home'
        for path in (self.root, self.workspace, self.home):
            path.mkdir(mode=0o700)
        for name in ('var', 'var/qwenpaw', 'var/qwenpaw-secrets'):
            (self.root / name).mkdir(mode=0o700)
        for name in ('launcher.py', 'control.py', 'lifecycle.py'):
            target = self.root / 'deploy/executor' / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / 'deploy/executor' / name, target)
            target.chmod(0o755)
        shutil.copytree(ROOT / 'plugins/agent-line', self.root / 'plugins/agent-line',
                        ignore=shutil.ignore_patterns('__pycache__'))
        write_new(self.root / 'var/qwenpaw/config.json', {'agents': {'profiles': {}}, 'plugins': {}})
        write_new(self.root / 'var/qwenpaw-secrets/local-login.json',
                  {'username': 'fixture', 'password': 'private-host-password'})
        self.binary = self.base / 'codex'
        for path in (self.binary, self.base / 'codex-code-mode-host'):
            path.write_text('#!/bin/sh\nexit 0\n'); path.chmod(0o755)
        self.auth = self.base / 'sim-auth.json'
        write_new(self.auth, {'baseUrl': 'http://127.0.0.1:3000', 'apiKey': 'private-sim-key'})
        self.spec = {'schemaVersion': 1,
            'project': {'id': 'business-project', 'name': '实际业务项目', 'workspace': str(self.workspace)},
            'coordinator': {'id': 'business-parent', 'name': '开发协调', 'providerId': 'fixture', 'modelId': 'chosen-model'},
            'executor': {'id': 'business-codex', 'name': '项目执行', 'model': 'explicit-codex-model',
                'sandbox': 'workspace-write', 'approvalPolicy': 'on-request', 'binary': str(self.binary),
                'imageId': 'sha256:' + 'a' * 64, 'codexHome': str(self.home)},
            'qwenpaw': {'baseURL': 'http://127.0.0.1:65199'},
            'sim': {'authFile': str(self.auth), 'workspaceId': 'workspace-fixture'},
            'verification': {'policyId': 'business-tests', 'imageId': 'sha256:' + 'b' * 64,
                'tests': [{'id': 'business-unit', 'argv': ['python3', '-m', 'unittest', 'discover', '-s', 'tests']}]},
            'memoryQuery': '本项目的构建规范和已经审核的经验'}
        self.source = self.base / 'spec.json'; write_new(self.source, self.spec)
        self.calls = []
        self.socket_patch = patch('deploy.project.setup.socket.create_connection', side_effect=ConnectionRefusedError)
        self.socket_patch.start(); self.addCleanup(self.socket_patch.stop)

    def mock_native(self, root, arguments):
        self.calls.append(arguments)
        if Path(arguments[0]).name == 'native.py':
            spec = json.loads((root / 'var/adapter/project-spec.json').read_text())
            revisions = {'developer': PIN, 'planner': 'qwenpaw-role-v1:' + 'b' * 64,
                         'reviewer': 'qwenpaw-role-v1:' + 'c' * 64}
            revisions = {key: value for key, value in revisions.items()
                         if key == 'developer' or key in (spec.get('team') or {})}
            write_new(root / 'var/adapter/native-role.json', {'roleRevision': PIN,
                'roleRevisions': revisions})

    def snapshot(self):
        return {str(path.relative_to(self.base)): (path.stat().st_mode, hashlib.sha256(path.read_bytes()).hexdigest())
                for path in self.base.rglob('*') if path.is_file() and not path.is_symlink()}

    def test_check_is_read_only_and_does_not_import_native_manager_or_run_commands(self):
        before = self.snapshot()
        with patch('subprocess.run', side_effect=AssertionError('no native commands')):
            spec, digest = preflight(self.source, root=self.root)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(spec['project']['id'], 'business-project')
        self.assertEqual(digest, hashlib.sha256(self.source.read_bytes()).hexdigest())

    def test_install_builds_existing_configs_and_stores_without_readiness_or_model_execution(self):
        result = install(self.source, root=self.root, run_native=self.mock_native)
        self.assertEqual(result, {'phase': 'configured', 'projectId': 'business-project',
            'controlledExecution': 'unknown', 'nativeExecutionVerified': False,
            'serviceStarted': False, 'readinessGenerated': False})
        directory = self.root / 'var/adapter'
        service = json.loads((directory / 'service.json').read_text())
        self.assertEqual(service['projects']['business-project']['workspace'], str(self.workspace))
        policy = service['bindings']['developer']['verificationPolicy']
        self.assertEqual(policy['tests'][0]['argv'], self.spec['verification']['tests'][0]['argv'])
        self.assertEqual(policy['artifacts'], [])
        self.assertFalse((directory / 'readiness.json').exists())
        self.assertEqual(TaskStore(directory / 'tasks.sqlite').list_tasks({'business-project'})['tasks'], [])
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.calls[0][2:4], ['plugin', 'install'])
        self.assertIn('chosen-model', self.calls[1])
        for path in (self.root / 'var').rglob('*'):
            if path.is_file(): self.assertEqual(path.stat().st_mode & 0o777, 0o600, path)
        credentials = [next(iter(service['tokens']))]
        for name in ('authoring', 'approvals', 'memory'):
            credentials.append(json.loads((directory / (name + '.json')).read_text())['credentials'][0]['token'])
        self.assertEqual(len(set(credentials)), 4)
        self.assertNotIn('private-host-password', json.dumps(result))
        self.assertNotIn('private-sim-key', json.dumps(result))

    def test_fresh_team_install_wires_three_existing_binding_schemas_and_preserves_shared_ledgers(self):
        self.spec['team'] = {
            'planner': {'id': 'business-planner', 'name': '规划', 'providerId': 'fixture', 'modelId': 'planner-model'},
            'reviewer': {'id': 'business-reviewer', 'name': '审查', 'providerId': 'fixture', 'modelId': 'reviewer-model'}}
        self.source.write_text(json.dumps(self.spec))
        install(self.source, root=self.root, run_native=self.mock_native)
        directory = self.root / 'var/adapter'
        service = json.loads((directory / 'service.json').read_text())
        self.assertEqual(set(service['bindings']), {'planner', 'developer', 'reviewer'})
        execution = json.loads((directory / 'execution.json').read_text())
        self.assertEqual({row['roleBindingId'] for row in execution['gateways']}, set(service['bindings']))
        memory = json.loads((directory / 'memory.json').read_text())
        self.assertEqual(set(memory['bindings']), set(service['bindings']))
        self.assertEqual(len({row['ledgerPath'] for row in execution['gateways']}), 1)
        self.assertEqual({row['commitLedgerPath'] for row in memory['bindings'].values()},
                         {str(directory / 'memory-commits.sqlite')})
        self.assertEqual(len([call for call in self.calls if call[:2] == ['-m', 'qwenpaw']]), 4)
        self.assertEqual(json.loads((self.root / 'var/single-project-setup.json').read_text())['roleRevision'], PIN)

    def test_native_role_workspace_permissions_are_applied_to_every_team_role(self):
        workspaces = []
        for role in ('planner', 'developer', 'reviewer'):
            workspace = self.root / 'var/qwenpaw/workspaces' / role
            workspace.mkdir(parents=True, mode=0o755)
            child = workspace / 'state.json'
            child.write_text('{}')
            child.chmod(0o644)
            workspaces.append((workspace, child))
        for workspace, child in workspaces:
            secure_role_workspace(workspace)
            self.assertEqual(workspace.stat().st_mode & 0o777, 0o700)
            self.assertEqual(child.stat().st_mode & 0o777, 0o600)
        alias = workspaces[0][0] / 'alias'
        alias.symlink_to(workspaces[1][1])
        with self.assertRaisesRegex(ValueError, 'Unexpected alias'):
            secure_role_workspace(workspaces[0][0])

    def test_generated_configs_assemble_in_real_runtime_with_fixture_remote_and_unknown_rejects_tasks(self):
        from agent_line.runtime import load_app
        install(self.source, root=self.root, run_native=self.mock_native)
        directory = self.root / 'var/adapter'
        service = json.loads((directory / 'service.json').read_text())
        token = next(iter(service['tokens']))
        requests = []
        def remote(request):
            requests.append((request.method, request.url.path))
            if request.url.path == '/api/auth/login':
                return httpx.Response(200, json={'access_token': 'fixture-host-token'})
            if request.url.path == '/api/agent-line/execution-binding':
                return httpx.Response(200, json={'schemaVersion': 1, 'ownedContainer': True,
                    'workspace': str(self.workspace), 'managedRoleIds': ['business-parent'],
                    'targetAgentId': 'business-codex', 'roleRevisions': {'business-parent': PIN}})
            if request.url.path == '/api/agents':
                return httpx.Response(200, json={'agents': [{'id': 'business-parent', 'backend': 'qwenpaw', 'enabled': True}]})
            raise AssertionError('Unexpected synthetic native request')
        real_client = httpx.Client
        def factory(**kwargs):
            return real_client(transport=httpx.MockTransport(remote), **kwargs)
        with patch('httpx.Client', side_effect=factory):
            app = load_app(directory / 'service.json')
            with TestClient(app) as client:
                headers = {'Authorization': 'Bearer ' + token}
                health = client.get('/extension/v1/health', headers=headers)
                self.assertEqual(health.status_code, 200)
                self.assertEqual(health.json()['controlledExecution'], 'unknown')
                self.assertFalse(health.json()['readyForExecution'])
                body = {'schemaVersion': '1.0', 'idempotencyKey': 'no-execution',
                    'workflow': {'id': 'workflow', 'revision': '1', 'runId': 'run', 'nodeId': 'node',
                                 'nodeExecutionId': 'activation', 'attemptId': '1'},
                    'projectId': 'business-project', 'roleBindingId': 'developer', 'input': {},
                    'memoryPolicyId': 'native-role', 'outputSchemaId': 'task-result'}
                self.assertEqual(client.post('/extension/v1/node-tasks', json=body, headers=headers).status_code, 503)
        self.assertEqual(TaskStore(directory / 'tasks.sqlite').list_tasks({'business-project'})['tasks'], [])
        self.assertTrue(all(path in ('/api/auth/login', '/api/agent-line/execution-binding', '/api/agents')
                            for _, path in requests))

    def test_existing_installation_is_never_repeated_or_reassigned(self):
        install(self.source, root=self.root, run_native=self.mock_native)
        before = self.snapshot()
        with self.assertRaises(ValueError): install(self.source, root=self.root, run_native=self.mock_native)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(len(self.calls), 3)

    def test_empty_old_directory_and_old_bridge_configuration_rejected_without_mutation(self):
        for name in ('adapter', 'executor'):
            path = self.root / 'var' / name; path.mkdir()
            before = self.snapshot()
            with self.assertRaises(ValueError): preflight(self.source, root=self.root)
            self.assertEqual(before, self.snapshot()); path.rmdir()
        path = self.root / 'var/qwenpaw/config.json'
        value = json.loads(path.read_text()); value['plugins']['agent-line'] = {'execution_bridge': {}}
        path.write_text(json.dumps(value))
        with self.assertRaises(ValueError): preflight(self.source, root=self.root)
        self.assertFalse(self.calls)

    def test_failure_remains_incomplete_and_next_invocation_never_retries_native_write(self):
        def fail(root, arguments):
            self.calls.append(arguments)
            raise ValueError('private-native-detail')
        with self.assertRaises(ValueError): install(self.source, root=self.root, run_native=fail)
        self.assertEqual(public_status(self.root)['phase'], 'incomplete')
        before = self.snapshot()
        with self.assertRaises(ValueError): install(self.source, root=self.root, run_native=fail)
        self.assertEqual(before, self.snapshot()); self.assertEqual(len(self.calls), 1)
        self.assertNotIn('private-native-detail', (self.root / 'var/single-project-setup.json').read_text())

    def test_live_or_unproven_recorded_host_rejected(self):
        path = self.root / 'var/qwenpaw/agent-line-process.json'
        for pid in (os.getpid(), True, '123'):
            if path.exists(): path.unlink()
            write_new(path, {'pid': pid})
            before = self.snapshot()
            with self.assertRaises(ValueError): preflight(self.source, root=self.root)
            self.assertEqual(before, self.snapshot())

    def test_actual_listener_blocks_offline_installation(self):
        self.socket_patch.stop()
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0)); listener.listen()
            value = copy.deepcopy(self.spec)
            value['qwenpaw']['baseURL'] = 'http://127.0.0.1:' + str(listener.getsockname()[1])
            self.source.write_text(json.dumps(value))
            before = self.snapshot()
            with self.assertRaises(ValueError): preflight(self.source, root=self.root)
            self.assertEqual(before, self.snapshot())

    def test_listener_timeout_or_other_network_error_does_not_allow_install(self):
        for error in (TimeoutError(), OSError('unproven')):
            with patch('deploy.project.setup.socket.create_connection', side_effect=error):
                before = self.snapshot()
                with self.assertRaises(ValueError): preflight(self.source, root=self.root)
                self.assertEqual(before, self.snapshot())

    def test_unsupported_native_endpoint_is_rejected_before_listener_check(self):
        self.spec['qwenpaw']['baseURL'] = 'https://127.0.0.1:443'
        self.source.write_text(json.dumps(self.spec))
        with patch('deploy.project.setup.socket.create_connection', side_effect=ConnectionRefusedError) as connection:
            with self.assertRaises(ValueError): preflight(self.source, root=self.root)
        connection.assert_not_called()

    def test_child_keyring_identity_does_not_inherit_main_account_and_persisted_override_is_rejected(self):
        with patch.dict(os.environ, {'QWENPAW_KEYRING_ACCOUNT': 'main-installation', 'CODEX_HOME': '/main-home'}):
            env = native_environment(self.root)
        self.assertNotEqual(env['QWENPAW_KEYRING_ACCOUNT'], 'main-installation')
        self.assertNotIn('CODEX_HOME', env)
        self.assertEqual(env['QWENPAW_SECRET_DIR'], str(self.root / 'var/qwenpaw-secrets'))
        before = self.snapshot()
        with patch.dict(os.environ, {'QWENPAW_KEYRING_ACCOUNT': 'main-installation'}):
            with self.assertRaises(ValueError): preflight(self.source, root=self.root)
        self.assertEqual(before, self.snapshot())
        path = self.root / 'var/qwenpaw-secrets/envs.json'
        write_new(path, {'QWENPAW_KEYRING_ACCOUNT': 'main-installation'})
        before = self.snapshot()
        with self.assertRaises(ValueError): preflight(self.source, root=self.root)
        self.assertEqual(before, self.snapshot())

    def test_exclusive_marker_writes_do_not_follow_alias_or_replace_existing_intent(self):
        path = self.base / 'intent.json'; write_new(path, {'phase': 'original'})
        with self.assertRaises(FileExistsError): write_new(path, {'phase': 'replacement'})
        self.assertEqual(json.loads(path.read_text()), {'phase': 'original'})
        alias = self.base / 'alias.json'; alias.symlink_to(path)
        with self.assertRaises(FileExistsError): write_new(alias, {'phase': 'replacement'})

    def test_real_official_offline_roles_preserve_code_and_native_memory_without_model_calls(self):
        # Official init/config/CLI run in a fresh child environment. This test
        # uses the official offline starter catalog, never a credential/model.
        source = ('from qwenpaw.config.config import Config; '
                  'from qwenpaw.config.utils import save_config; '
                  'c=Config(); c.agents.profiles={}; c.agents.agent_order=[]; save_config(c)')
        guard = self.base / 'network-guard'; guard.mkdir()
        attempted = self.base / 'socket-attempted'
        (guard / 'sitecustomize.py').write_text('import socket\nfrom pathlib import Path\n'
            'def deny(*args, **kwargs):\n'
            '    Path(' + repr(str(attempted)) + ').write_text("socket attempted")\n'
            '    raise AssertionError("Offline native setup must not connect")\n'
            'socket.socket.connect=deny\nsocket.create_connection=deny\n')
        environment = native_environment(self.root); environment['PYTHONPATH'] = str(guard)
        subprocess.run([sys.executable, '-c', source], env=environment,
                       check=True, capture_output=True, timeout=30)
        self.spec['coordinator'].update(providerId='kilo', modelId='kilo-auto/free')
        self.source.write_text(json.dumps(self.spec))
        business = {'AGENTS.md': 'existing business instructions\n',
                    'README.md': 'existing project readme\n', 'app.py': 'print("project")\n'}
        for name, content in business.items(): (self.workspace / name).write_text(content)
        with patch.dict(os.environ, {'PYTHONPATH': str(guard)}):
            result = install(self.source, root=self.root)
        self.assertEqual(result['phase'], 'configured')
        config = json.loads((self.root / 'var/qwenpaw/config.json').read_text())
        parent = json.loads((self.root / 'var/qwenpaw/workspaces/business-parent/agent.json').read_text())
        target = json.loads((self.workspace / 'agent.json').read_text())
        self.assertEqual(config['plugins']['agent-line']['managed_role_ids'], ['business-parent'])
        self.assertEqual((self.root / 'var/qwenpaw/plugins/agent-line/plugin.py').read_bytes(),
                         (self.root / 'plugins/agent-line/plugin.py').read_bytes())
        for path in (self.root / 'var/qwenpaw/plugins/agent-line').rglob('*'):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700 if path.is_dir() else 0o600)
        self.assertEqual(parent['backend'], 'qwenpaw')
        self.assertEqual(parent['running']['memory_manager_backend'], 'remelight')
        enabled = {name for name, tool in parent['tools']['builtin_tools'].items() if tool['enabled']}
        self.assertEqual(enabled, {'agent_line_delegate', 'agent_line_execution_status'})
        self.assertEqual(target['backend'], 'codex')
        self.assertEqual(target['backend_settings']['model'], 'explicit-codex-model')
        self.assertEqual(target['backend_settings']['approval_policy'], 'on-request')
        self.assertEqual(target['workspace_dir'], str(self.workspace))
        self.assertIsInstance(target.get('channels'), dict)
        self.assertTrue(target['channels']['console']['enabled'])
        self.assertTrue(all(not channel['enabled'] for name, channel in target['channels'].items()
                            if name != 'console'))
        self.assertEqual(set(p.name for p in self.workspace.iterdir()), {*business, 'agent.json'})
        for name, content in business.items(): self.assertEqual((self.workspace / name).read_text(), content)
        record = json.loads((self.root / 'var/adapter/native-role.json').read_text())
        self.assertRegex(record['roleRevision'], r'^qwenpaw-role-v1:[0-9a-f]{64}$')
        self.assertFalse(record['modelExecutionVerified'])
        self.assertFalse((self.root / 'var/adapter/readiness.json').exists())
        self.assertEqual(TaskStore(self.root / 'var/adapter/tasks.sqlite').list_tasks({'business-project'})['tasks'], [])
        self.assertFalse(attempted.exists(), 'Official offline preparation attempted a network connection')
