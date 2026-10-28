"""Sim CLI must never repair missing persistent data during an ordinary start."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1] / 'deploy/sim'
SECRET_NAMES = ('POSTGRES_PASSWORD', 'BETTER_AUTH_SECRET', 'ENCRYPTION_KEY',
                'API_ENCRYPTION_KEY', 'INTERNAL_API_SECRET', 'CRON_SECRET')


class SimStartupGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.deploy = self.root / 'deploy/sim'
        self.deploy.mkdir(parents=True)
        for name in ('prepare.py', 'run.sh', 'compose.yaml', 'triggers.compose.yaml'):
            shutil.copyfile(SOURCE / name, self.deploy / name)
        (self.deploy / 'run.sh').chmod(0o700)
        self.state = self.root / 'var/sim'
        self.capture = self.root / 'docker-calls'
        tools = self.root / 'tools'
        tools.mkdir()
        docker = tools / 'docker'
        docker.write_text('''#!/usr/bin/python3
import json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ["DOCKER_CAPTURE"], "a") as f:
    f.write(json.dumps(args) + "\\n")
state = pathlib.Path(os.environ["DOCKER_CAPTURE"] + ".state")
cid = "b" * 64
if args[0] == "compose" and "run" in args and "--detach" in args:
    env = pathlib.Path(args[args.index("--env-file") + 1])
    state.write_text(json.dumps({"id": cid, "name": "/" + args[args.index("--name") + 1],
        "labels": {"com.docker.compose.project": "agent-line", "com.docker.compose.service": "db",
                   "com.docker.compose.oneoff": "True"}, "running": True,
        "mounts": [{"Type": "bind", "Source": str(env.parent / "postgres"),
                    "Destination": "/var/lib/postgresql/data"}]}))
    print(cid)
elif args[:2] == ["container", "ls"]:
    if state.exists(): print(cid)
elif args[0] == "inspect":
    print(state.read_text())
elif args[0] == "stop":
    state.unlink()
''')
        docker.chmod(0o700)
        self.env = {key: value for key, value in os.environ.items()
                    if key not in ('AGENT_LINE_SIM_TRIGGER_CRON_DIR', 'AGENT_LINE_SIM_TRIGGER_CRON_OWNER')}
        self.env.update(PATH=str(tools) + ':' + os.environ['PATH'], DOCKER_CAPTURE=str(self.capture))

    def run_cli(self, *arguments):
        return subprocess.run([str(self.deploy / 'run.sh'), *arguments],
                              cwd=self.root, env=self.env, capture_output=True)

    def prepare(self, *arguments):
        return subprocess.run(['/usr/bin/python3', str(self.deploy / 'prepare.py'), *arguments],
                              cwd=self.root, env=self.env, capture_output=True)

    def existing(self):
        self.state.mkdir(parents=True, mode=0o700)
        for name in ('postgres', 'redis', 'uploads'):
            (self.state / name).mkdir(mode=0o700)
        env = self.state / 'sim.env'
        env.write_text(''.join(f'{name}={"a" * 64}\n' for name in SECRET_NAMES))
        env.chmod(0o600)
        return env

    def assert_no_docker(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse(self.capture.exists(), result.stderr)
        self.assertNotIn(b'a' * 64, result.stdout + result.stderr)

    def test_default_prepare_does_not_create_missing_state_or_secret(self):
        result = self.prepare()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.state.exists())
        self.assertFalse((self.root / 'var').exists())

    def test_start_with_missing_environment_does_not_initialize_or_call_docker(self):
        self.state.mkdir(parents=True)
        sentinel = self.state / 'existing-data'
        sentinel.write_text('retain')
        self.assert_no_docker(self.run_cli('up', '-d'))
        self.assertEqual([p.name for p in self.state.iterdir()], ['existing-data'])
        self.assertEqual(sentinel.read_text(), 'retain')

    def test_private_permission_error_is_rejected_without_chmod(self):
        env = self.existing()
        env.chmod(0o644)
        before = env.read_bytes()
        self.assert_no_docker(self.run_cli('up', '-d'))
        self.assertEqual(env.stat().st_mode & 0o777, 0o644)
        self.assertEqual(env.read_bytes(), before)

    def test_missing_data_directory_does_not_get_recreated(self):
        self.existing()
        for missing in ('postgres', 'redis', 'uploads'):
            with self.subTest(directory=missing):
                path = self.state / missing
                path.rmdir()
                self.assert_no_docker(self.run_cli('up', '-d'))
                self.assertFalse(path.exists())
                path.mkdir(mode=0o700)

    def test_restore_marker_rejects_all_starting_commands_without_docker(self):
        self.existing()
        for directory in (self.root, self.root / 'var', self.state):
            marker = directory / 'RESTORE-DO-NOT-START.json'
            marker.write_text('{}')
            for command in ('up', 'start', 'restart', 'create', 'run', 'watch', 'scale', 'unpause'):
                with self.subTest(marker=directory.name, command=command):
                    self.assert_no_docker(self.run_cli(command))
            marker.unlink()

    def test_read_or_stop_commands_keep_data_and_can_inspect_restore_state(self):
        env = self.existing()
        original = env.read_bytes()
        (self.state / 'postgres').rmdir()
        (self.root / 'RESTORE-DO-NOT-START.json').write_text('{}')
        for arguments in (('config', '--quiet'), ('ps', '-a'), ('stop',)):
            result = self.run_cli(*arguments)
            self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in self.capture.read_text().splitlines()]
        self.assertEqual([call[-len(args):] for call, args in zip(calls, (('config','--quiet'),('ps','-a'),('stop',)))],
                         [['config', '--quiet'], ['ps', '-a'], ['stop']])
        self.assertEqual(env.read_bytes(), original)
        self.assertFalse((self.state / 'postgres').exists())

    def test_initialize_is_explicit_and_never_replaces_existing_directory(self):
        result = self.run_cli('init')
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in self.capture.read_text().splitlines()]
        self.assertEqual(calls[0], ['ps', '-aq', '--filter', 'label=com.docker.compose.project=agent-line'])
        self.assertTrue(any(call[0] == 'stop' for call in calls))
        self.capture.unlink()
        self.assertEqual(set(p.name for p in self.state.iterdir()), {'sim.env', 'postgres', 'redis', 'uploads'})
        env = self.state / 'sim.env'
        self.assertEqual(env.stat().st_mode & 0o777, 0o600)
        original = env.read_bytes()
        self.assertNotEqual(original.splitlines()[0].split(b'=')[1], original.splitlines()[1].split(b'=')[1])
        self.assert_no_docker(self.run_cli('init'))
        self.assertEqual(env.read_bytes(), original)
        env.unlink()
        self.assert_no_docker(self.run_cli('init'))
        self.assertFalse(env.exists())

    def test_required_secret_missing_or_empty_is_rejected_sanitized(self):
        env = self.existing()
        for value in ('', 'CRON_SECRET=\n', 'CRON_SECRET=\'\'\n', 'CRON_SECRET= # missing\n', 'CRON_SECRET="" # missing\n', 'CRON_SECRET=${EXTERNAL_SECRET}\n'):
            env.write_text(''.join(f'{name}={"a" * 64}\n' for name in SECRET_NAMES[:-1]) + value)
            self.assert_no_docker(self.run_cli('up'))

    def test_symlink_environment_or_data_is_rejected(self):
        env = self.existing()
        saved = self.root / 'saved.env'
        env.rename(saved)
        env.symlink_to(saved)
        self.assert_no_docker(self.run_cli('up'))
        env.unlink()
        saved.rename(env)
        pg = self.state / 'postgres'
        pg.rmdir()
        target = self.root / 'external-pg'
        target.mkdir()
        pg.symlink_to(target, target_is_directory=True)
        self.assert_no_docker(self.run_cli('up'))
        self.assertEqual(list(target.iterdir()), [])

    def test_initialization_under_restore_marker_is_rejected_without_files(self):
        (self.root / 'RESTORE-DO-NOT-START.json').write_text('{}')
        self.assert_no_docker(self.run_cli('init'))
        self.assertFalse(self.state.exists())

    def test_start_normal_preserves_files_and_invokes_same_compose(self):
        env = self.existing()
        original = env.read_bytes()
        result = self.run_cli('up', '-d')
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [json.loads(line) for line in self.capture.read_text().splitlines()]
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][:3], ['ps', '--all', '--no-trunc'])
        self.assertEqual(calls[1], ['compose', '--project-name', 'agent-line', '--env-file', str(env),
                                    '-f', str(self.deploy / 'compose.yaml'), 'up', '-d'])
        self.assertEqual(env.read_bytes(), original)

    def test_unwritable_state_does_not_call_docker_or_leave_a_probe(self):
        env = self.existing()
        original = env.read_bytes()
        names = sorted(path.name for path in self.state.iterdir())
        self.state.chmod(0o555)
        try:
            self.assert_no_docker(self.run_cli('up', '-d'))
            self.assertFalse((self.state / '.agent-line-write-probe').exists())
            self.assertEqual(sorted(path.name for path in self.state.iterdir()), names)
        finally:
            self.state.chmod(0o700)
        self.assertEqual(env.read_bytes(), original)

    def test_state_probe_symlink_does_not_call_docker(self):
        self.existing()
        target = self.root / 'outside-state-probe'
        target.write_text('keep\n')
        link = self.state / '.agent-line-write-probe'
        link.symlink_to(target)
        self.assert_no_docker(self.run_cli('up', '-d'))
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_text(), 'keep\n')

    def test_nonregular_or_duplicate_secret_file_is_rejected_without_blocking(self):
        env = self.existing()
        original = env.read_bytes()
        env.write_bytes(original + b'CRON_SECRET=another-secret\n')
        self.assert_no_docker(self.run_cli('up'))
        env.unlink()
        os.mkfifo(env, mode=0o600)
        result = subprocess.run([str(self.deploy / 'run.sh'), 'up'],
                                cwd=self.root, env=self.env, capture_output=True, timeout=5)
        self.assert_no_docker(result)
        self.assertTrue(env.is_fifo())

    def test_parent_symlink_is_rejected_without_mutating_its_target(self):
        self.existing()
        moved = self.root / 'saved-var'
        (self.root / 'var').rename(moved)
        (self.root / 'var').symlink_to(moved, target_is_directory=True)
        original = (moved / 'sim/sim.env').read_bytes()
        self.assert_no_docker(self.run_cli('up'))
        self.assertEqual((moved / 'sim/sim.env').read_bytes(), original)

    def test_global_flags_and_extra_init_arguments_cannot_bypass_preflight(self):
        self.assert_no_docker(self.run_cli('--profile', 'other', 'up'))
        self.assert_no_docker(self.run_cli('init', 'extra'))
        self.assertFalse((self.root / 'var').exists())

    def test_read_commands_with_missing_secret_never_create_files(self):
        for command in ('config', 'ps', 'stop'):
            self.assert_no_docker(self.run_cli(command))
            self.assertFalse((self.root / 'var').exists())

    def test_every_data_bind_disables_automatic_source_creation(self):
        compose = (self.deploy / 'compose.yaml').read_text()
        optional = (self.deploy / 'triggers.compose.yaml').read_text()
        self.assertEqual(compose.count('create_host_path: false'), 7)
        self.assertEqual(optional.count('create_host_path: false'), 2)
        self.assertNotIn("volumes: ['../../var/sim/", compose)
