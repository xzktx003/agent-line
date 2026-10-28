"""Explicit initialization owns one temporary db container, never service data."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / 'deploy/sim/prepare.py'
CID = 'a' * 64
SECRET = 'private-bootstrap-secret'


class SimExplicitBootstrapTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('sim_prepare_bootstrap', SOURCE)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.module.ROOT = self.root
        self.module.STATE = self.root / 'var/sim'
        self.calls = []
        self.name = None
        self.exists = False
        self.running = False

    def docker(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        self.assertTrue(kwargs['capture_output'])
        self.assertGreater(kwargs['timeout'], 0)
        if argv[1] == 'ps':
            return subprocess.CompletedProcess(argv, 0, '', SECRET)
        if argv[1] == 'compose':
            self.name = argv[argv.index('--name') + 1]
            self.exists = self.running = True
            return subprocess.CompletedProcess(argv, 0, CID + '\n', SECRET)
        if argv[1] == 'inspect':
            payload = {'id': CID, 'name': '/' + self.name,
                       'labels': {'com.docker.compose.project': 'agent-line',
                                  'com.docker.compose.service': 'db',
                                  'com.docker.compose.oneoff': 'True'},
                       'running': self.running,
                       'mounts': [{'Type': 'bind', 'Source': str(self.module.STATE / 'postgres'),
                                   'Destination': '/var/lib/postgresql/data'}]}
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload), SECRET)
        if argv[1] == 'exec':
            return subprocess.CompletedProcess(argv, 0, SECRET, SECRET)
        if argv[1] == 'stop':
            self.running = self.exists = False
            return subprocess.CompletedProcess(argv, 0, CID, SECRET)
        if argv[1:3] == ['container', 'ls']:
            return subprocess.CompletedProcess(argv, 0, CID + '\n' if self.exists else '', SECRET)
        self.fail('Unexpected Docker operation: ' + str(argv[:3]))

    def initialize(self, handler=None):
        with patch.object(self.module.subprocess, 'run', side_effect=handler or self.docker):
            self.module.initialize()

    def test_explicit_init_uses_fixed_compose_and_proves_final_postgres_before_cleanup(self):
        self.initialize()
        argv = next(argv for argv, _ in self.calls if argv[1] == 'compose')
        self.assertEqual(argv[:8], ['docker', 'compose', '--project-name', 'agent-line',
                                   '--env-file', str(self.module.STATE / 'sim.env'),
                                   '-f', str(self.root / 'deploy/sim/compose.yaml')])
        self.assertEqual(argv[-4:], ['--entrypoint', 'docker-entrypoint.sh', 'db', 'postgres'])
        for flag in ('--detach', '--rm', '--no-deps', '--pull', 'never'):
            self.assertIn(flag, argv)
        probe = next(call for call, _ in self.calls if call[1] == 'exec')
        self.assertEqual(probe[2], CID)
        self.assertIn('/proc/1/comm', probe[-1])
        self.assertIn('pg_isready -U agent_line -d agent_line_sim', probe[-1])
        self.assertFalse(self.exists)
        self.assertEqual((self.module.STATE / 'sim.env').stat().st_mode & 0o777, 0o600)

    def test_existing_installation_never_calls_docker_or_changes_files(self):
        self.module.STATE.mkdir(parents=True)
        original = self.module.STATE / 'sentinel'
        original.write_text('keep')
        with self.assertRaisesRegex(ValueError, '安装目录已存在'):
            self.initialize()
        self.assertEqual(self.calls, [])
        self.assertEqual(original.read_text(), 'keep')

    def test_start_failure_after_creation_recovers_only_owned_name_and_keeps_private_state(self):
        def fail_start(argv, **kwargs):
            result = self.docker(argv, **kwargs)
            if argv[1] == 'compose':
                return subprocess.CompletedProcess(argv, 1, SECRET, SECRET)
            return result
        with self.assertRaisesRegex(ValueError, '初始化失败') as raised:
            self.initialize(fail_start)
        self.assertNotIn(SECRET, str(raised.exception))
        self.assertFalse(self.exists)
        self.assertTrue((self.module.STATE / 'sim.env').exists())
        count = len(self.calls)
        with self.assertRaisesRegex(ValueError, '安装目录已存在'):
            self.initialize()
        self.assertEqual(len(self.calls), count)

    def test_invalid_cid_does_not_become_docker_target(self):
        def invalid(argv, **kwargs):
            result = self.docker(argv, **kwargs)
            if argv[1] == 'compose':
                result.stdout = SECRET
            return result
        with self.assertRaisesRegex(ValueError, '初始化失败'):
            self.initialize(invalid)
        self.assertFalse(any(SECRET in argv for argv, _ in self.calls))
        self.assertFalse(self.exists)

    def test_readiness_timeout_stops_owned_container(self):
        def never_ready(argv, **kwargs):
            result = self.docker(argv, **kwargs)
            if argv[1] == 'exec':
                result.returncode = 1
            return result
        with patch.object(self.module, 'BOOTSTRAP_WAIT_SECONDS', 0):
            with self.assertRaisesRegex(ValueError, '初始化失败'):
                self.initialize(never_ready)
        self.assertFalse(self.exists)

    def test_timeout_exception_is_sanitized_and_attempts_owned_cleanup(self):
        def timeout(argv, **kwargs):
            result = self.docker(argv, **kwargs)
            if argv[1] == 'compose':
                raise subprocess.TimeoutExpired(argv, 1, SECRET, SECRET)
            return result
        with self.assertRaisesRegex(ValueError, '初始化失败') as raised:
            self.initialize(timeout)
        self.assertNotIn(SECRET, str(raised.exception))
        self.assertFalse(self.exists)

    def test_cleanup_failure_is_reported_and_never_force_removes(self):
        def cannot_stop(argv, **kwargs):
            if argv[1] == 'stop':
                self.calls.append((argv, kwargs))
                raise subprocess.TimeoutExpired(argv, 1, SECRET, SECRET)
            return self.docker(argv, **kwargs)
        with self.assertRaisesRegex(ValueError, '临时数据库容器未确认清理') as raised:
            self.initialize(cannot_stop)
        self.assertNotIn(SECRET, str(raised.exception))
        self.assertTrue(self.exists)
        self.assertFalse(any('--force' in argv or '-f' in argv[1:2] for argv, _ in self.calls))

    def test_foreign_container_labels_are_never_stopped(self):
        def foreign(argv, **kwargs):
            result = self.docker(argv, **kwargs)
            if argv[1] == 'inspect':
                value = json.loads(result.stdout)
                value['labels']['com.docker.compose.project'] = 'other'
                result.stdout = json.dumps(value)
            return result
        with self.assertRaisesRegex(ValueError, '临时数据库容器未确认清理'):
            self.initialize(foreign)
        self.assertFalse(any(argv[1] == 'stop' for argv, _ in self.calls))

    def test_old_compose_containers_reject_init_without_new_installation(self):
        def old_container(argv, **kwargs):
            self.calls.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, CID, SECRET)
        with self.assertRaisesRegex(ValueError, '已有 Compose 容器'):
            self.initialize(old_container)
        self.assertFalse(self.module.STATE.exists())
        self.assertEqual(len(self.calls), 1)

    def test_init_directory_lock_blocks_ordinary_start(self):
        def check_locked(argv, **kwargs):
            if argv[1] == 'exec':
                with self.assertRaisesRegex(ValueError, '首次数据库初始化尚未完成'):
                    self.module.check_start()
            return self.docker(argv, **kwargs)
        self.initialize(check_locked)
        with patch.object(self.module.subprocess, 'run', side_effect=self.docker):
            self.module.check_start()

    def test_mount_mismatch_never_stops_foreign_database(self):
        def foreign_mount(argv, **kwargs):
            result = self.docker(argv, **kwargs)
            if argv[1] == 'inspect':
                value = json.loads(result.stdout)
                value['mounts'][0]['Source'] = '/other/database'
                result.stdout = json.dumps(value)
            return result
        with self.assertRaisesRegex(ValueError, '临时数据库容器未确认清理'):
            self.initialize(foreign_mount)
        self.assertFalse(any(argv[1] == 'stop' for argv, _ in self.calls))

    def test_stopped_oneoff_is_removed_by_its_verified_cid(self):
        def persistent(argv, **kwargs):
            if argv[1] == 'stop':
                self.calls.append((argv, kwargs))
                self.running = False
                return subprocess.CompletedProcess(argv, 0, CID, SECRET)
            if argv[1] == 'rm':
                self.calls.append((argv, kwargs))
                self.assertEqual(argv[2:], [CID])
                self.exists = False
                return subprocess.CompletedProcess(argv, 0, CID, SECRET)
            return self.docker(argv, **kwargs)
        self.initialize(persistent)
        self.assertFalse(self.exists)
        self.assertTrue(any(argv[1] == 'rm' for argv, _ in self.calls))

    def test_failed_inspection_is_not_treated_as_container_absence(self):
        def unavailable(argv, **kwargs):
            result = self.docker(argv, **kwargs)
            if argv[1] == 'inspect':
                result.returncode = 1
            return result
        with self.assertRaisesRegex(ValueError, '临时数据库容器未确认清理'):
            self.initialize(unavailable)
        self.assertFalse(any(argv[1] == 'stop' for argv, _ in self.calls))

    def test_unparseable_inspection_is_sanitized(self):
        def unavailable(argv, **kwargs):
            result = self.docker(argv, **kwargs)
            if argv[1] == 'inspect':
                result.stdout = SECRET
            return result
        with self.assertRaisesRegex(ValueError, '临时数据库容器未确认清理') as raised:
            self.initialize(unavailable)
        self.assertNotIn(SECRET, str(raised.exception))

    def test_released_initialization_lock_does_not_allow_residual_oneoff(self):
        self.initialize()
        # Simulate a killed initializer: no Python lock remains, but its db does.
        for label in ('True', 'true', 'TRUE'):
            with self.subTest(label=label):
                with patch.object(self.module.subprocess, 'run', return_value=
                                  subprocess.CompletedProcess([], 0, CID + ' ' + label, SECRET)):
                    with self.assertRaisesRegex(ValueError, '临时数据库容器尚未排除'):
                        self.module.check_start()

    def test_unknown_oneoff_query_cannot_allow_start(self):
        self.initialize()
        for result in (subprocess.CompletedProcess([], 1, '', SECRET),
                       subprocess.CompletedProcess([], 0, CID, SECRET),
                       subprocess.CompletedProcess([], 0, CID + ' unknown', SECRET)):
            with self.subTest(output=result.stdout):
                with patch.object(self.module.subprocess, 'run', return_value=result):
                    with self.assertRaisesRegex(ValueError, '临时数据库容器尚未排除') as raised:
                        self.module.check_start()
                    self.assertNotIn(SECRET, str(raised.exception))
        with patch.object(self.module.subprocess, 'run', side_effect=OSError(SECRET)):
            with self.assertRaisesRegex(ValueError, '临时数据库容器尚未排除'):
                self.module.check_start()

    def test_regular_service_container_is_allowed_after_oneoff_query(self):
        self.initialize()
        with patch.object(self.module.subprocess, 'run', return_value=
                          subprocess.CompletedProcess([], 0, CID + ' False', SECRET)):
            self.module.check_start()

    def test_single_readiness_observation_timeout_keeps_original_cid(self):
        attempts = []
        def temporarily_unknown(argv, **kwargs):
            if argv[1] == 'exec':
                attempts.append(argv[2])
                if len(attempts) == 1:
                    raise subprocess.TimeoutExpired(argv, 5, SECRET, SECRET)
            return self.docker(argv, **kwargs)
        with patch.object(self.module.time, 'sleep'):
            self.initialize(temporarily_unknown)
        self.assertEqual(attempts, [CID, CID])
        self.assertEqual(sum(argv[1] == 'compose' for argv, _ in self.calls), 1)
        self.assertFalse(self.exists)
