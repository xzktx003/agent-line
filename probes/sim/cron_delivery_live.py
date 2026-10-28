#!/usr/bin/env python3
"""One real supercronic minute, reused published Start/End, zero model calls.

Default is read-only. Existing state is never reinstalled or resubmitted, even
with --execute. A failed cleanup leaves evidence requiring explicit reconciliation.
SIGUSR2 gracefully reloads the existing scheduler; a signal receipt alone is not
proof: the original timer's HTTP request and Sim terminal result must be observed.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import httpx
import uvicorn
from fastapi.responses import JSONResponse

from agent_line.adapters.sim import SimClient
from agent_line.authoring_api import AuthoringAPI, AuthoringAccess
from agent_line.service import ServiceConfig, create_app
from agent_line.sim_authoring import SimAuthoringClient
from agent_line.sim_journal import SimJournal
from agent_line.store import TaskStore, canonical
from agent_line.triggers import TriggerAPI, event_key
from agent_line.workflows import WorkflowGateway
from deploy.sim.trigger_cron import prepare, private_read, verify_prepared
from probes.full_workflow.parallel_roles import main_snapshot
from probes.sim.authoring_probe import write
from probes.sim.triggers_live import PROJECT, TriggerProbe, private_file, require

DATA = ROOT / 'var/sim-cron-delivery'
SOURCE = ROOT / 'var/sim-triggers-live'
CONTAINER = 'agent-line-cron-1'
IMAGE = 'ghcr.io/simstudioai/cron@sha256:9cbf8de37f3ac7b3d18d62e9783aab5962ef202a9a17636d64e6fa1dd3c9dbcc'
MOUNT = '/agent-line-triggers'
CRONTAB = '/etc/sim/crontab'
KIND = 'sim-cron-delivery-v1'


def sha(value):
    return hashlib.sha256(value).hexdigest()


def docker(*args, data=None):
    result = subprocess.run(['docker', *args], input=data, capture_output=True, timeout=40)
    require(result.returncode == 0, 'docker_command_failed')
    return result.stdout + (result.stderr if args[0] == 'logs' else b'')


def inspect_container(run=docker):
    # Never inspect the complete environment, Config.Cmd, or running argv.
    fmt = '{{json .Id}}\n{{json .Config.Image}}\n{{json .Config.Labels}}\n{{json .State.Running}}\n{{json .NetworkSettings.Networks}}'
    lines = run('inspect', '--format', fmt, CONTAINER).decode().splitlines()
    require(len(lines) == 5, 'container_inspection_incomplete')
    identity, image, labels, running, networks = map(json.loads, lines)
    require(isinstance(identity, str) and len(identity) == 64 and image == IMAGE
            and running is True and labels.get('com.docker.compose.project') == 'agent-line'
            and labels.get('com.docker.compose.service') == 'cron'
            and len(networks) == 1, 'existing_cron_ownership_mismatch')
    gateway = next(iter(networks.values())).get('Gateway')
    require(isinstance(gateway, str) and gateway, 'container_gateway_missing')
    comm = run('exec', identity, 'cat', '/proc/1/comm').decode().strip()
    require(comm == 'supercronic', 'existing_pid1_not_supercronic')
    # This exact image has no credentials in PID 1's command; do not print it.
    cmd = run('exec', identity, 'cat', '/proc/1/cmdline').split(b'\0')
    require(cmd == [b'/usr/local/bin/supercronic', b'-passthrough-logs', b'/etc/sim/crontab', b''], 'unexpected_scheduler_command')
    uid = run('exec', identity, 'id', '-u').decode().strip()
    gid = run('exec', identity, 'id', '-g').decode().strip()
    require(uid == '1001' and gid == '1001', 'unexpected_cron_runtime_uid_gid')
    return {'id': identity, 'image': image, 'gateway': gateway, 'uid': 1001, 'gid': 1001}


def assert_same_container(identity, run=docker):
    current = inspect_container(run)
    require(current == identity, 'container_changed_refuse_mutation')


def remote_bytes(identity, path, run=docker):
    run('exec', identity['id'], 'sh', '-c', 'test -f "$1" && test ! -L "$1"', 'check', path)
    return run('exec', identity['id'], 'cat', path)


def root_reader(run):
    def read(*args):
        require(args[0] == 'exec', 'root_reader_exec_only')
        return run('exec', '--user', '0', *args[1:])
    return read


def verify_remote_directory(identity, generated, run=docker, *, owner='1001:1001'):
    run('exec', identity['id'], 'sh', '-c', 'test -d "$1" && test ! -L "$1"', 'check', MOUNT)
    require(run('exec', identity['id'], 'stat', '-c', '%u:%g:%a', MOUNT).decode().strip()
            == owner + ':700', 'remote_generated_directory_owner_or_mode_changed')
    expected = {p.name: p.read_bytes() for p in Path(generated).iterdir()}
    names = run('exec', identity['id'], 'ls', '-A1', MOUNT).decode().splitlines()
    require(set(names) == set(expected), 'remote_directory_contents_changed')
    for name, contents in expected.items():
        require(name.replace('.', '').replace('-', '').isalnum(), 'unexpected_generated_filename')
        path = MOUNT + '/' + name
        run('exec', identity['id'], 'sh', '-c', 'test -f "$1" && test ! -L "$1"', 'check', path)
        require(remote_bytes(identity, path, run) == contents, 'remote_generated_file_changed')
        metadata = run('exec', identity['id'], 'stat', '-c', '%u:%g:%a', path).decode().strip()
        require(metadata == owner + ':' + ('700' if name == 'send.sh' else '600'),
                'remote_generated_owner_or_mode_changed')


def backup_journal(source, target):
    require(not Path(target).exists(), 'new_journal_already_exists')
    with sqlite3.connect(Path(source).resolve().as_uri() + '?mode=ro', uri=True) as incoming:
        with sqlite3.connect(target) as outgoing:
            incoming.backup(outgoing)
    Path(target).chmod(0o600)


class CronProbe:
    def __init__(self, directory, state, auth, *, run=docker, sim_factory=SimClient):
        self.directory, self.state, self.auth = Path(directory), state, auth
        self.run, self.sim_factory = run, sim_factory
        self.lock = threading.RLock()

    def save(self):
        with self.lock:
            write(self.directory / 'state.json', self.state)

    def once(self, key, operation):
        with self.lock:
            require(key not in self.state['operations'], 'operation_exists_no_retry')
            self.state['operations'][key] = {'status': 'intent', 'at': time.time()}
            self.save()
        result = operation()
        with self.lock:
            self.state['operations'][key].update(status='received', at=time.time())
            self.save()
        return result

    def mutation(self, key, *args):
        assert_same_container(self.state['container'], self.run)
        return self.once(key, lambda: self.run(*args))

    def request(self, request):
        if request.method == 'GET':
            return
        expected = '/api/v2/workflows/' + self.state['workflowId'] + '/execute'
        require(request.method == 'POST' and request.url.path == expected
                and request.headers.get('X-Run-Id') == self.state['runId'], 'unexpected_native_mutation')
        with self.lock:
            require(not self.state['nativeIntents'], 'native_intent_exists_no_retry')
            self.state['nativeIntents'].append({'method': 'POST', 'path': expected,
                'runId': self.state['runId'], 'bodySha256': sha(request.content), 'status': 'intent'})
            self.save()

    def response(self, response):
        if response.request.method != 'GET':
            with self.lock:
                self.state['nativeIntents'][0].update(status='received', httpStatus=response.status_code)
                self.save()

    @contextmanager
    def transport(self, *, writing=False):
        sim = self.sim_factory(self.auth['baseUrl'], self.auth['apiKey'])
        if writing:
            sim._client.event_hooks['request'].append(self.request)
            sim._client.event_hooks['response'].append(self.response)
        else:
            sim._client.event_hooks['request'].append(
                lambda request: require(request.method == 'GET', 'observation_must_not_mutate'))
        try:
            yield sim
        finally:
            sim.close()

    def verify_native(self, sim, *, poll=False):
        # Reuse exact original deployment + End marker proof, never authoring IO.
        return TriggerProbe.verify_native(self, sim, poll=poll)

    def app(self, sim, token):
        tasks = TaskStore(self.directory / 'tasks.sqlite')
        journal = SimJournal(self.directory / 'journal.sqlite')
        projects = {PROJECT: {'workspace': str(self.directory / 'workspace')}}
        authoring = AuthoringAPI(AuthoringAccess({}), journal,
            {PROJECT: SimAuthoringClient(sim, self.auth['workspaceId'])}, projects=projects)
        gateway = WorkflowGateway(sim, journal, {}, installation_id=self.state['installationId'])
        triggers = TriggerAPI(gateway, {self.state['triggerId']: {'projectId': PROJECT,
            'bindingId': self.state['bindingId'], 'type': 'schedule'}}, projects=projects)
        config = ServiceConfig(tokens={token: {PROJECT}}, projects=projects,
                               bindings={}, controlled_execution='unknown')
        app = create_app(tasks, config, workflows=gateway, authoring=authoring, triggers=triggers)

        @app.middleware('http')
        async def audit(request, call_next):
            if request.method != 'POST':
                return await call_next(request)
            expected_path = '/extension/v1/triggers/' + self.state['triggerId'] + '/schedule'
            if request.headers.get('authorization') != 'Bearer ' + token or request.url.path != expected_path:
                return JSONResponse({'detail': 'unauthorized'}, status_code=401)
            try:
                body = json.loads(await request.body())
            except ValueError:
                return JSONResponse({'detail': 'invalid'}, status_code=422)
            if body != {'slot': self.state['slot'], 'input': self.state['input']}:
                return JSONResponse({'detail': 'unexpected occurrence'}, status_code=409)
            with self.lock:
                self.state['deliveries'].append({'slot': body['slot'], 'input': body['input'],
                                                'status': 'intent', 'at': time.time()})
                index = len(self.state['deliveries']) - 1
                self.save()
            response = await call_next(request)
            with self.lock:
                self.state['deliveries'][index].update(status='received', httpStatus=response.status_code)
                self.save()
            return response
        return app

    def install(self):
        identity = self.state['container']
        generated = self.directory / 'generated'
        require(remote_bytes(identity, CRONTAB, self.run) == (self.directory / 'original.crontab').read_bytes(),
                'original_crontab_changed_before_install')
        self.mutation('copy-private-jobs', 'cp', str(generated), identity['id'] + ':' + MOUNT)
        verify_remote_directory(identity, generated, root_reader(self.run),
                                owner=f'{os.getuid()}:{os.getgid()}')
        self.mutation('own-private-jobs', 'exec', '--user', '0', identity['id'],
                      'chown', '-R', '1001:1001', MOUNT)
        verify_remote_directory(identity, generated, self.run)
        # Parse before replacing; test mode never fires jobs.
        self.mutation('validate-crontab', 'exec', identity['id'], '/usr/local/bin/supercronic', '-test', MOUNT + '/crontab')
        self.mutation('install-crontab', 'cp', str(generated / 'crontab'), identity['id'] + ':' + CRONTAB)
        require(remote_bytes(identity, CRONTAB, root_reader(self.run)) == (generated / 'crontab').read_bytes(),
                'copied_crontab_changed_refuse_chown')
        self.mutation('own-installed-crontab', 'exec', '--user', '0', identity['id'],
                      'chown', '1001:1001', CRONTAB)
        require(self.run('exec', identity['id'], 'stat', '-c', '%u:%g:%a', CRONTAB).decode().strip()
                == '1001:1001:600', 'installed_crontab_owner_or_mode_changed')
        require(remote_bytes(identity, CRONTAB, self.run) == (generated / 'crontab').read_bytes(), 'installed_bytes_differ')
        self.mutation('reload-installed', 'kill', '--signal', 'USR2', identity['id'])

    def reload_reads(self):
        # Capture only; raw official job logs may contain sensitive argv.
        contents = self.run('logs', '--since', str(self.state.get('startedAt', 0)),
                            '--tail', '10000', self.state['container']['id'])
        return sum(b'read crontab: /etc/sim/crontab' in line for line in contents.splitlines())

    def wait_original_reload(self, before):
        deadline, next_update = time.monotonic() + 900, 0
        while time.monotonic() < deadline:
            if self.reload_reads() > before:
                assert_same_container(self.state['container'], self.run)
                require(remote_bytes(self.state['container'], CRONTAB, self.run)
                        == (self.directory / 'original.crontab').read_bytes(), 'restored_crontab_changed')
                self.run('exec', self.state['container']['id'], '/usr/local/bin/supercronic', '-test', CRONTAB)
                return
            if time.monotonic() >= next_update:
                print(json.dumps({'phase': 'waiting_existing_supercronic_restore_reload'}), flush=True)
                next_update = time.monotonic() + 30
            time.sleep(1)
        raise RuntimeError('original_reload_not_observed_keep_evidence')

    def remove_local_credentials(self):
        require(self.state.get('listenerStopped') is True
                and self.state.get('cleanup') == 'restored_original_crontab_removed_private_jobs',
                'cleanup_not_complete_keep_credentials')
        generated = verify_prepared(self.directory / 'generated', self.directory / 'original.crontab')
        manifest = private_file(generated / 'manifest.json')
        token = self.directory / 'token'
        require(sha(private_read(token)) == self.state['tokenSha256'], 'temporary_token_changed_refuse_remove')
        self.state['preparedManifest'] = manifest
        self.save()
        for path in generated.iterdir():
            path.unlink()
        generated.rmdir()
        token.unlink()
        self.state['localCredentialsRemoved'] = True
        self.save()

    def cleanup(self):
        identity, operations = self.state['container'], self.state['operations']
        generated = self.directory / 'generated'
        if 'install-crontab' in operations:
            assert_same_container(identity, self.run)
            require(remote_bytes(identity, CRONTAB, self.run) == (generated / 'crontab').read_bytes(),
                    'crontab_changed_refuse_restore')
            self.mutation('restore-crontab', 'cp', str(self.directory / 'original.crontab'), identity['id'] + ':' + CRONTAB)
            require(remote_bytes(identity, CRONTAB, root_reader(self.run))
                    == (self.directory / 'original.crontab').read_bytes(), 'copied_restore_changed_refuse_chown')
            mode, owner = self.state['originalCrontabMode'], self.state['originalCrontabOwner']
            require(mode == '644' and owner == '0:0', 'untrusted_original_metadata')
            self.mutation('restore-crontab-mode', 'exec', '--user', '0', identity['id'], 'chmod', mode, CRONTAB)
            self.mutation('restore-crontab-owner', 'exec', '--user', '0', identity['id'], 'chown', owner, CRONTAB)
            require(self.run('exec', identity['id'], 'stat', '-c', '%u:%g:%a', CRONTAB).decode().strip()
                    == owner + ':' + mode, 'restored_crontab_owner_or_mode_changed')
            require(remote_bytes(identity, CRONTAB, self.run) == (self.directory / 'original.crontab').read_bytes(),
                    'restored_bytes_differ')
            before = self.reload_reads()
            self.mutation('reload-original', 'kill', '--signal', 'USR2', identity['id'])
            self.wait_original_reload(before)
            self.state['originalReloadObserved'] = True
            self.save()
        if 'copy-private-jobs' in operations:
            assert_same_container(identity, self.run)
            verify_remote_directory(identity, generated, self.run)
            self.mutation('remove-private-jobs', 'exec', '--user', '0', identity['id'], 'rm', '-rf', MOUNT)
            require(self.run('exec', identity['id'], 'sh', '-c', 'test ! -e "$1" && test ! -L "$1"', 'check', MOUNT) == b'',
                    'private_jobs_remain')
        self.state['cleanup'] = 'restored_original_crontab_removed_private_jobs'
        self.save()


@contextmanager
def execution_window(probe, listener, address, port):
    """Keep native transport open until the HTTP server has drained and stopped."""
    handles = {}
    with probe.transport(writing=True) as sim:
        try:
            yield sim, handles
        finally:
            try:
                probe.cleanup()
            finally:
                server, thread = handles.get('server'), handles.get('thread')
                if server is not None: server.should_exit = True
                if thread is not None:
                    thread.join(timeout=20)
                    require(not thread.is_alive(), 'temporary_uvicorn_still_running')
                listener.close()
                with socket.socket() as check:
                    check.settimeout(1)
                    require(check.connect_ex((address, port)) != 0, 'temporary_listener_still_open')
                probe.state['listenerStopped'] = True
                probe.save()


def run_probe(*, execute=False, directory=DATA, source=SOURCE, auth_path=None,
              run=docker, sim_factory=SimClient, snapshot=None):
    require(type(execute) is bool, 'explicit_execute_boolean_required')
    directory, source = Path(directory), Path(source)
    state_path = directory / 'state.json'
    if not state_path.exists() and not execute:
        return {'status': 'not_executed', 'nativeWritesThisInvocation': 0}
    auth = private_file(auth_path or ROOT / 'var/sim/probe-auth.json')
    snapshot = snapshot or (lambda: main_snapshot(ROOT))
    if state_path.exists():
        state = private_file(state_path)
        require(state.get('kind') == KIND and state['baseUrl'] == auth['baseUrl'].rstrip('/')
                and state['workspaceId'] == auth['workspaceId'], 'original_origin_changed')
        probe = CronProbe(directory, state, auth, run=run, sim_factory=sim_factory)
        with probe.transport() as sim:
            native = probe.verify_native(sim)
        return {'status': 'observation_only', 'nativeWritesThisInvocation': 0,
                'native': native, 'cleanup': state.get('cleanup', 'incomplete')}
    require(not directory.exists(), 'partial_directory_requires_reconciliation')
    original = private_file(source / 'state.json')
    proof = private_file(source / 'root-check.json')
    require(proof.get('status') == 'passed_real_trigger_ingress_read_only'
            and proof['workflowId'] == original['workflowId'] and proof['publication'] == original['publication']
            and original['baseUrl'] == auth['baseUrl'].rstrip('/') and original['workspaceId'] == auth['workspaceId'],
            'reusable_original_publication_not_verified')
    identity = inspect_container(run)
    original_bytes = remote_bytes(identity, CRONTAB, run)
    require(original_bytes == (ROOT / 'upstream/sim/docker/crontab').read_bytes(), 'official_crontab_not_exact')
    require(run('exec', identity['id'], 'sh', '-c', 'test ! -e "$1" && test ! -L "$1"', 'check', MOUNT) == b'',
            'existing_managed_directory_refuse_overwrite')
    mode = run('exec', identity['id'], 'stat', '-c', '%a', CRONTAB).decode().strip()
    require(mode == '644', 'unexpected_original_mode')
    owner = run('exec', identity['id'], 'stat', '-c', '%u:%g', CRONTAB).decode().strip()
    require(owner == '0:0', 'unexpected_original_owner')
    nonce = uuid.uuid4().hex
    slot = (datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=3)).strftime('%Y-%m-%dT%H:%M:00Z')
    installation, trigger = 'agent-line-cron-' + nonce, 'cron-' + nonce
    rid = str(uuid.uuid5(uuid.NAMESPACE_URL, canonical([installation, PROJECT, original['workflowId'],
        event_key(trigger, 'schedule', slot), original['bindingId']])))
    state = {key: original[key] for key in ('workflowId', 'bindingId', 'publication', 'marker', 'workspaceId', 'baseUrl')}
    state.update(schemaVersion=1, kind=KIND, slot=slot, runId=rid, installationId=installation,
        triggerId=trigger, input={'cronProbe': nonce}, container=identity, originalCrontabMode=mode, originalCrontabOwner=owner,
        originalCrontabSha256=sha(original_bytes), mainSnapshot=snapshot(), runs={rid: {'runId': rid}},
        operations={}, nativeIntents=[], deliveries=[], startedAt=time.time())
    # All preflight network reads precede creating the immutable execution state.
    preview = CronProbe(directory, state, auth, run=run, sim_factory=sim_factory)
    with preview.transport() as sim:
        state['runs'] = {}
        preview.verify_native(sim)
        state['runs'] = {rid: {'runId': rid}}
    with socket.socket() as listener:
        listener.bind((identity['gateway'], 0)); listener.listen(128)
        listener.setblocking(False)
        address, port = listener.getsockname()
        require(address == identity['gateway'], 'listener_host_changed')
        directory.mkdir(mode=0o700)
        write(state_path, state)
        token = secrets.token_urlsafe(48)
        write(directory / 'source-config.json', {'schemaVersion': 1, 'adapterBaseUrl': f'http://{address}:{port}',
            'tokenFile': 'token', 'jobs': [{'triggerId': trigger, 'cron': '* * * * *', 'input': state['input'], 'onceSlot': slot}]})
        fd = os.open(directory / 'token', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as handle: handle.write(token)
        state['tokenSha256'] = sha(token.encode())
        write(state_path, state)
        fd = os.open(directory / 'original.crontab', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as handle: handle.write(original_bytes)
        prepare(directory / 'source-config.json', directory / 'generated', trusted_http_hosts=(address,))
        backup_journal(source / 'journal.sqlite', directory / 'journal.sqlite')
        (directory / 'workspace').mkdir(mode=0o700)
        TaskStore.initialize(directory / 'tasks.sqlite')
        probe = CronProbe(directory, state, auth, run=run, sim_factory=sim_factory)
        with execution_window(probe, listener, address, port) as (sim, handles):
            app = probe.app(sim, token)
            server = uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False, lifespan='on',
                                                       timeout_graceful_shutdown=15))
            thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
            handles.update(server=server, thread=thread)
            thread.start()
            deadline = time.monotonic() + 15
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.05)
            require(server.started, 'temporary_listener_not_ready')
            with httpx.Client(base_url=f'http://{address}:{port}', headers={'Authorization': 'Bearer ' + token},
                              timeout=15, trust_env=False) as http:
                health = http.get('/extension/v1/health').json()
                require(health['controlledExecution'] == 'unknown' and health['readyForExecution'] is False,
                        'machine_capability_changed')
                probe.install()
                deadline = datetime.strptime(slot, '%Y-%m-%dT%H:%M:00Z').replace(tzinfo=timezone.utc).timestamp() + 90
                next_update = 0
                while time.time() < deadline:
                    if state['deliveries'] and state['deliveries'][0].get('httpStatus') == 202:
                        break
                    if time.monotonic() >= next_update:
                        print(json.dumps({'phase': 'waiting_original_supercronic_minute', 'slot': slot}), flush=True)
                        next_update = time.monotonic() + 30
                    time.sleep(0.5)
                require(len(state['deliveries']) == 1 and state['deliveries'][0].get('httpStatus') == 202,
                        'original_timer_delivery_not_received_no_resubmit')
                native = probe.verify_native(sim, poll=True)
                require(native['runs'][rid]['verified'] is True, 'actual_timer_run_not_verified')
                response = http.post('/extension/v1/triggers/' + trigger + '/schedule',
                                     json={'slot': slot, 'input': state['input']})
                require(response.status_code == 202 and response.json()['runId'] == rid
                        and len(state['nativeIntents']) == 1
                        and state['nativeIntents'][0].get('httpStatus') == 202,
                        'same_slot_replay_created_another_native_run')
                state['checks'] = {'actualSupercronicMinute': True, 'sameSlotReplaySameRun': True,
                    'nativeExecuteCount': 1, 'native': native, 'controlledExecution': 'unknown', 'modelCalls': 0}
                probe.save()
        probe.remove_local_credentials()
        require(snapshot() == state['mainSnapshot'], 'main_configuration_or_guard_changed')
        with sqlite3.connect((directory / 'tasks.sqlite').resolve().as_uri() + '?mode=ro', uri=True) as db:
            require(db.execute('select count(*) from tasks').fetchone()[0] == 0
                    and db.execute('select count(*) from workspace_leases').fetchone()[0] == 0, 'zero_model_probe_created_tasks')
        report = {'status': 'passed_actual_supercronic_zero_model', 'checks': state['checks'],
                  'cleanup': state['cleanup'], 'listenerStopped': state['listenerStopped'],
                  'originalReloadObserved': state.get('originalReloadObserved') is True,
                  'localCredentialsRemoved': state.get('localCredentialsRemoved') is True,
                  'mainSnapshotUnchanged': True, 'nativeWritesThisInvocation': 1}
        write(directory / 'report.json', report)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    try:
        result = run_probe(execute=args.execute)
        print(json.dumps({'status': result['status'], 'nativeWritesThisInvocation': result['nativeWritesThisInvocation']}))
        return 0
    except Exception as exc:
        print(json.dumps({'status': 'incomplete', 'errorType': type(exc).__name__, 'replayPermitted': False}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
