"""Offline safety and real HTTP/journal wiring for the clock delivery probe."""
import copy
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

from fastapi.testclient import TestClient

from agent_line.sim_journal import SimJournal
from agent_line.store import TaskStore, canonical
from agent_line.triggers import event_key
from probes.sim.authoring_probe import write
from deploy.sim.trigger_cron import prepare
from probes.sim.cron_delivery_live import (
    CRONTAB, IMAGE, KIND, MOUNT, CronProbe, backup_journal, inspect_container, run_probe, sha, execution_window,
)
from probes.sim.triggers_live import PROJECT, run_probe as source_probe
from tests.test_live_trigger_probe import NativeFixture


class DockerFixture:
    def __init__(self):
        self.identity = 'a' * 64
        self.image = IMAGE
        self.labels = {'com.docker.compose.project': 'agent-line', 'com.docker.compose.service': 'cron'}
        self.files = {CRONTAB: b'# original\n'}
        self.modes = {CRONTAB: '644'}
        self.owners = {CRONTAB: '0:0'}
        self.directories = {}
        self.uid = self.gid = '1001'
        self.calls = []; self.reads = 1

    def __call__(self, *args):
        self.calls.append(args)
        if args[0] == 'inspect':
            return ('\n'.join(json.dumps(v) for v in [self.identity, self.image, self.labels, True,
                     {'agent-line_default': {'Gateway': '172.20.0.1'}}]) + '\n').encode()
        if args[0] == 'kill': self.reads += 1; return b''
        if args[0] == 'logs': return b'read crontab: /etc/sim/crontab\n' * self.reads
        if args[0] == 'cp':
            local = Path(args[1]); target = args[2].split(':', 1)[1]
            paths = list(local.iterdir()) if local.is_dir() else [local]
            if local.is_dir():
                self.directories[target] = {'owner': f'{os.getuid()}:{os.getgid()}', 'mode': '700'}
            for path in paths:
                destination = target + '/' + path.name if local.is_dir() else target
                self.files[destination] = path.read_bytes()
                self.modes[destination] = oct(stat.S_IMODE(path.stat().st_mode))[2:]
                self.owners[destination] = f'{os.getuid()}:{os.getgid()}'
            return b''
        root = args[:4] == ('exec', '--user', '0', self.identity)
        if args[:2] == ('exec', self.identity) or root:
            command = args[4:] if root else args[2:]
            if command == ('id', '-u'): return (self.uid + '\n').encode()
            if command == ('id', '-g'): return (self.gid + '\n').encode()
            if command == ('cat', '/proc/1/comm'): return b'supercronic\n'
            if command == ('cat', '/proc/1/cmdline'):
                return b'/usr/local/bin/supercronic\0-passthrough-logs\0/etc/sim/crontab\0'
            if command[0] == 'cat':
                path = command[1]
                if not root and self.owners[path] != '1001:1001' and self.modes[path] == '600':
                    raise RuntimeError('fixture_permission_denied')
                return self.files[path]
            if command[0] == 'stat':
                path = command[-1]
                owner = self.directories[path]['owner'] if path in self.directories else self.owners[path]
                mode = self.directories[path]['mode'] if path in self.directories else self.modes[path]
                return {'%a': mode, '%u:%g': owner, '%u:%g:%a': owner + ':' + mode}[command[-2]].encode() + b'\n'
            if command[0] == 'ls':
                return ('\n'.join(p.removeprefix(MOUNT + '/') for p in self.files if p.startswith(MOUNT + '/')) + '\n').encode()
            if command[0] == '/usr/local/bin/supercronic': return b''
            if command[0] == 'chmod':
                assert root or self.owners[command[2]] == '1001:1001'
                self.modes[command[2]] = command[1]; return b''
            if command[0] == 'chown':
                assert root
                if command[1] == '-R':
                    assert command == ('chown', '-R', '1001:1001', MOUNT)
                    self.directories[MOUNT]['owner'] = '1001:1001'
                    for path in self.owners:
                        if path.startswith(MOUNT + '/'): self.owners[path] = '1001:1001'
                else: self.owners[command[2]] = command[1]
                return b''
            if command[0] == 'rm':
                assert root
                self.directories.pop(MOUNT)
                self.files = {p: b for p, b in self.files.items() if not p.startswith(MOUNT + '/')}
                return b''
            if command[0] == 'sh': return b''
        raise AssertionError('Unexpected Docker call ' + repr(args))


class CronDeliveryProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / 'new'
        self.auth = self.root / 'auth.json'
        self.remote = NativeFixture()
        self.auth_data = {'workspaceId': 'fixture-workspace', 'baseUrl': 'http://sim.fixture', 'apiKey': 'fixture-secret'}
        write(self.auth, self.auth_data)

    def test_execution_transport_closes_after_cleanup_stop_join_and_listener_close(self):
        for fail_body in (False, True):
            events = []
            probe = self.probe()
            def factory(base, key):
                sim = self.remote.factory(base, key)
                original_close = sim.close
                def close():
                    events.append('client-close'); original_close()
                sim.close = close
                return sim
            probe.sim_factory = factory
            probe.cleanup = Mock(side_effect=lambda: events.append('cron-cleanup'))
            probe.save = Mock(side_effect=lambda: events.append('state-save'))
            class Server:
                def __setattr__(self, name, value):
                    if name == 'should_exit' and value is True:
                        events.append('server-stop')
                    object.__setattr__(self, name, value)
            thread = Mock()
            thread.join.side_effect = lambda timeout: events.append('server-join')
            thread.is_alive.return_value = False
            listener = Mock(); listener.close.side_effect = lambda: events.append('listener-close')
            checker = Mock(); checker.connect_ex.return_value = 111
            with patch('probes.sim.cron_delivery_live.socket.socket') as sock:
                sock.return_value.__enter__.return_value = checker
                try:
                    with execution_window(probe, listener, '127.0.0.1', 12345) as (sim, handles):
                        handles.update(server=Server(), thread=thread)
                        events.append('body')
                        if fail_body: raise ValueError('simulated body failure')
                except ValueError:
                    self.assertTrue(fail_body)
            self.assertEqual(events, ['body', 'cron-cleanup', 'server-stop', 'server-join',
                                      'listener-close', 'state-save', 'client-close'])
            self.assertTrue(probe.state['listenerStopped'])

    def test_observation_transport_rejects_post_before_native_io(self):
        probe = self.probe()
        with probe.transport() as sim:
            with self.assertRaisesRegex(RuntimeError, 'observation_must_not_mutate'):
                sim._client.post('http://sim.fixture/api/v2/workflows')
        self.assertEqual(self.remote.calls, [])

    def test_default_does_not_open_credentials_or_docker_or_create_files(self):
        self.auth.unlink(); runner = Mock(side_effect=AssertionError('No docker'))
        result = run_probe(directory=self.directory, auth_path=self.auth, run=runner)
        self.assertEqual(result['status'], 'not_executed')
        self.assertFalse(self.directory.exists()); runner.assert_not_called()

    def test_container_ownership_and_pid1_exact_command(self):
        run = DockerFixture()
        identity = inspect_container(run)
        self.assertEqual(identity['gateway'], '172.20.0.1')
        self.assertFalse(any('env' in arg for call in run.calls for arg in call))
        for field, bad in (('image', 'other:image'), ('identity', 'short'), ('labels', {}), ('uid', '0'), ('gid', '0')):
            original = getattr(run, field); setattr(run, field, bad)
            with self.assertRaises(RuntimeError): inspect_container(run)
            setattr(run, field, original)

    def probe(self, state=None, runner=None):
        self.directory.mkdir(mode=0o700, exist_ok=True)
        state = state or {'operations': {}}
        return CronProbe(self.directory, state, self.auth_data, run=runner or DockerFixture(), sim_factory=self.remote.factory)

    def test_once_records_intent_before_io_and_never_repeats_unknown(self):
        probe = self.probe()
        def lost():
            self.assertEqual(json.loads((self.directory / 'state.json').read_text())['operations']['write']['status'], 'intent')
            raise TimeoutError('private error must not be emitted')
        with self.assertRaises(TimeoutError): probe.once('write', lost)
        operation = Mock()
        with self.assertRaisesRegex(RuntimeError, 'no_retry'): probe.once('write', operation)
        operation.assert_not_called()

    def staged(self):
        run = DockerFixture(); identity = inspect_container(run)
        generated = self.directory / 'generated'; self.directory.mkdir(mode=0o700); generated.mkdir(mode=0o700)
        original = self.directory / 'original.crontab'; original.write_bytes(run.files[CRONTAB]); original.chmod(0o600)
        for name, data in [('crontab', b'# original\n# managed\n'), ('send.sh', b'exit 0\n'), ('0.curl', b'private')]:
            path = generated / name; path.write_bytes(data); path.chmod(0o700 if name == 'send.sh' else 0o600)
        state = {'operations': {}, 'container': identity, 'originalCrontabMode': '644', 'originalCrontabOwner': '0:0'}
        return self.probe(state, run), run

    def test_install_parse_reload_restore_and_remove_exact_files(self):
        probe, run = self.staged(); probe.install()
        self.assertEqual(run.owners[CRONTAB], '1001:1001')
        self.assertEqual(run.modes[CRONTAB], '600')
        self.assertEqual(run.directories[MOUNT], {'owner': '1001:1001', 'mode': '700'})
        self.assertTrue(all(run.owners[p] == '1001:1001' for p in run.files if p.startswith(MOUNT + '/')))
        self.assertEqual(run.files[CRONTAB], b'# original\n# managed\n')
        probe.cleanup()
        self.assertEqual(run.files, {CRONTAB: b'# original\n'})
        self.assertEqual(run.modes[CRONTAB], '644')
        self.assertEqual(run.owners[CRONTAB], '0:0')
        self.assertIn(('exec', '--user', '0', run.identity, 'chown', '-R', '1001:1001', MOUNT), run.calls)
        self.assertEqual(probe.state['operations']['own-private-jobs']['status'], 'received')
        self.assertEqual(probe.state['operations']['own-installed-crontab']['status'], 'received')
        self.assertEqual(len([c for c in run.calls if c[0] == 'kill']), 2)
        self.assertTrue(all(v['status'] == 'received' for v in probe.state['operations'].values()))

    def test_copied_unknown_directory_is_verified_as_root_before_any_chown(self):
        probe, run = self.staged()
        def tampering(*args):
            result = run(*args)
            if args[0] == 'cp' and Path(args[1]).is_dir():
                run.files[MOUNT + '/0.curl'] = b'unknown content'
            return result
        probe.run = tampering
        with self.assertRaisesRegex(RuntimeError, 'file_changed'): probe.install()
        self.assertFalse(any('chown' in call for call in run.calls))
        self.assertNotIn('own-private-jobs', probe.state['operations'])
        self.assertEqual(run.files[CRONTAB], b'# original\n')

    def test_unknown_chown_intent_is_not_replayed(self):
        probe, run = self.staged()
        def lose_chown(*args):
            if 'chown' in args:
                raise TimeoutError('lost chown reply')
            return run(*args)
        probe.run = lose_chown
        with self.assertRaises(TimeoutError): probe.install()
        self.assertEqual(probe.state['operations']['own-private-jobs']['status'], 'intent')
        before = len([call for call in run.calls if call[0] == 'cp'])
        with self.assertRaisesRegex(RuntimeError, 'no_retry'): probe.install()
        self.assertEqual(len([call for call in run.calls if call[0] == 'cp']), before)

    def test_cleanup_refuses_changed_crontab_or_generated_files_or_container(self):
        probe, run = self.staged(); probe.install()
        run.files[CRONTAB] = b'external modification'
        with self.assertRaisesRegex(RuntimeError, 'refuse_restore'): probe.cleanup()
        self.assertEqual(run.files[CRONTAB], b'external modification')
        self.assertIn(MOUNT + '/0.curl', run.files)
        run.files[CRONTAB] = b'# original\n# managed\n'
        run.files[MOUNT + '/0.curl'] = b'other owner'
        with self.assertRaisesRegex(RuntimeError, 'file_changed'): probe.cleanup()
        self.assertIn(MOUNT + '/0.curl', run.files)
        run.identity = 'b' * 64
        before = len([c for c in run.calls if c[0] in ('cp', 'kill')])
        with self.assertRaisesRegex(RuntimeError, 'container_changed'): probe.cleanup()
        self.assertEqual(len([c for c in run.calls if c[0] in ('cp', 'kill')]), before)

    def credential_probe(self):
        self.directory.mkdir(mode=0o700)
        original = self.directory / 'original.crontab'; original.write_bytes(b'# original\n')
        original.chmod(0o600)
        token = self.directory / 'token'; token.write_text('a' * 40); token.chmod(0o600)
        config = self.directory / 'source-config.json'
        write(config, {'schemaVersion': 1, 'adapterBaseUrl': 'http://127.0.0.1:3193', 'tokenFile': 'token',
                       'jobs': [{'triggerId': 'once', 'cron': '* * * * *', 'input': {}}]})
        prepare(config, self.directory / 'generated', original)
        state = {'operations': {}, 'listenerStopped': True,
                 'cleanup': 'restored_original_crontab_removed_private_jobs', 'tokenSha256': sha(token.read_bytes())}
        return self.probe(state)

    def test_local_credentials_removed_only_after_stop_and_precise_manifest_match(self):
        probe = self.credential_probe()
        probe.state['listenerStopped'] = False
        with self.assertRaisesRegex(RuntimeError, 'keep_credentials'): probe.remove_local_credentials()
        self.assertTrue((self.directory / 'token').exists())
        probe.state['listenerStopped'] = True
        probe.remove_local_credentials()
        self.assertFalse((self.directory / 'token').exists())
        self.assertFalse((self.directory / 'generated').exists())
        self.assertTrue(probe.state['localCredentialsRemoved'])
        self.assertIn('0.curl', probe.state['preparedManifest']['files'])
        self.assertNotIn('a' * 40, (self.directory / 'state.json').read_text())

    def test_local_credential_tamper_refuses_any_deletion(self):
        probe = self.credential_probe()
        target = self.directory / 'generated/0.curl'
        target.write_text('changed by another operator')
        with self.assertRaises(ValueError): probe.remove_local_credentials()
        self.assertTrue((self.directory / 'token').exists())
        self.assertEqual(target.read_text(), 'changed by another operator')

    def test_journal_backup_preserves_source_and_rejects_existing_target(self):
        source = self.root / 'source.sqlite'; target = self.root / 'backup.sqlite'
        SimJournal.initialize(source); original = source.read_bytes()
        backup_journal(source, target)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        with self.assertRaises(RuntimeError): backup_journal(source, target)

    def build_http_probe(self):
        source = self.root / 'source'
        source_probe(execute=True, directory=source, auth_path=self.auth,
                     sim_factory=self.remote.factory, snapshot=lambda: {})
        original = json.loads((source / 'state.json').read_text())
        self.directory.mkdir(mode=0o700)
        backup_journal(source / 'journal.sqlite', self.directory / 'journal.sqlite')
        TaskStore.initialize(self.directory / 'tasks.sqlite')
        (self.directory / 'workspace').mkdir(mode=0o700)
        state = {key: original[key] for key in ('workflowId', 'bindingId', 'publication', 'marker', 'workspaceId', 'baseUrl')}
        slot, installation, trigger = '2026-10-22T08:00:00Z', 'new-installation', 'new-cron'
        rid = str(uuid.uuid5(uuid.NAMESPACE_URL, canonical([installation, PROJECT, state['workflowId'],
            event_key(trigger, 'schedule', slot), state['bindingId']])))
        state.update(kind=KIND, operations={}, nativeIntents=[], deliveries=[], slot=slot, input={'test': True},
                     installationId=installation, triggerId=trigger, runId=rid, runs={rid: {'runId': rid}})
        return self.probe(state), state

    def test_real_http_and_copied_catalog_start_one_run_duplicate_does_not_execute(self):
        probe, state = self.build_http_probe(); count = len(self.remote.writes())
        with probe.transport(writing=True) as sim, TestClient(probe.app(sim, 'private-token')) as http:
            headers = {'Authorization': 'Bearer private-token'}
            path = '/extension/v1/triggers/new-cron/schedule'
            body = {'slot': state['slot'], 'input': state['input']}
            first = http.post(path, json=body, headers=headers)
            self.assertEqual(first.status_code, 202)
            self.assertEqual(first.json()['runId'], state['runId'])
            duplicate = http.post(path, json=body, headers=headers)
            self.assertEqual(duplicate.json(), first.json())
            self.assertEqual(len(self.remote.writes()), count + 1)
            self.assertEqual(len(state['nativeIntents']), 1)
            self.assertEqual(len(state['deliveries']), 2)
            self.assertTrue(probe.verify_native(sim)['runs'][state['runId']]['verified'])
            self.assertEqual(http.post(path, json=body).status_code, 401)
            self.assertEqual(http.post(path, json=body | {'slot': '2026-10-22T08:01:00Z'}, headers=headers).status_code, 409)
            self.assertEqual(len(self.remote.writes()), count + 1)

    def test_existing_state_is_read_only_even_with_execute_and_no_docker(self):
        probe, state = self.build_http_probe()
        with probe.transport(writing=True) as sim, TestClient(probe.app(sim, 'private-token')) as http:
            http.post('/extension/v1/triggers/new-cron/schedule', json={'slot': state['slot'], 'input': state['input']},
                      headers={'Authorization': 'Bearer private-token'})
        saved = (self.directory / 'state.json').read_bytes(); before = len(self.remote.writes())
        runner = Mock(side_effect=AssertionError('No docker writes'))
        for execute in (False, True):
            result = run_probe(execute=execute, directory=self.directory, auth_path=self.auth,
                               run=runner, sim_factory=self.remote.factory)
            self.assertEqual(result['status'], 'observation_only')
            self.assertEqual((self.directory / 'state.json').read_bytes(), saved)
        self.assertEqual(len(self.remote.writes()), before); runner.assert_not_called()


if __name__ == '__main__':
    unittest.main()
