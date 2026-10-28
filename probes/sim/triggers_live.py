#!/usr/bin/env python3
"""Real Sim trigger ingress, constant End result, and zero model calls.

Only a fresh --execute invocation may mutate Sim. Once state.json exists every
invocation is observation-only, even with --execute. Durable outer requests and
native transport intentions precede IO; neither uncertain operation is retried.
There is no listener, scheduler, executor, QwenPaw login, or memory interaction.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from agent_line.adapters.sim import SimClient, segment
from agent_line.approvals import TrustedPrincipal
from agent_line.authoring_api import AuthoringAPI, AuthoringAccess
from agent_line.service import ServiceConfig, create_app
from agent_line.sim_authoring import SimAuthoringClient, compile_document, decode_document
from agent_line.sim_journal import SimJournal
from agent_line.store import TaskStore, canonical
from agent_line.triggers import TriggerAPI, event_key
from agent_line.workflows import WorkflowGateway
from probes.full_workflow.parallel_roles import main_snapshot
from probes.sim.authoring_probe import write

DATA = ROOT / 'var/sim-triggers-live'
PROJECT = 'trigger-live-project'
INSTALLATION = 'agent-line-trigger-live-v1'
KIND = 'sim-triggers-live'


def require(condition, code):
    if not condition:
        raise RuntimeError(code)


def private_file(path):
    path = Path(path)
    info = path.lstat()
    require(not path.is_symlink() and stat.S_ISREG(info.st_mode)
            and info.st_uid == os.getuid() and not info.st_mode & 0o077
            and info.st_size <= 1048576, 'private_regular_file_required')
    return json.loads(path.read_text())


def document(marker):
    return {'schemaVersion': 1, 'variables': {}, 'nodes': [
        {'id': 'start', 'type': 'start', 'name': '开始',
         'position': {'x': 0, 'y': 0}, 'config': {}},
        {'id': 'end', 'type': 'end', 'name': '结束',
         'position': {'x': 250, 'y': 0}, 'config': {'output': {'marker': marker}}}],
        'edges': [{'id': 'direct', 'source': 'start', 'target': 'end'}]}


def trigger_run_id(workflow, binding, trigger, kind, identity):
    # Same stable original identity as WorkflowGateway.start, recorded before IO.
    value = canonical([INSTALLATION, PROJECT, workflow,
                       event_key(trigger, kind, identity), binding])
    return str(uuid.uuid5(uuid.NAMESPACE_URL, value))


class TriggerProbe:
    def __init__(self, auth, directory, state, *, read_only, sim_factory=SimClient):
        self.auth, self.directory, self.state = auth, Path(directory), state
        self.path = self.directory / 'state.json'
        self.read_only, self.sim_factory = read_only, sim_factory
        self.calls = []
        require(state.get('kind') == KIND and state.get('schemaVersion') == 1
                and state.get('workspaceId') == segment(auth['workspaceId'])
                and state.get('baseUrl') == auth['baseUrl'].rstrip('/'),
                'state_origin_or_workspace_changed')

    def save(self):
        write(self.path, self.state)

    def record_request(self, request):
        call = {'method': request.method, 'path': request.url.path}
        self.calls.append(call)
        if request.method == 'GET':
            return
        require(not self.read_only, 'observation_must_not_mutate')
        require(request.method in ('POST', 'PUT'), 'unexpected_native_mutation')
        # The HTTP transport sees every authoring/gateway mutation. No API key or
        # authorization header is persisted; only public path, body hash and ID.
        key = hashlib.sha256(canonical([request.method, request.url.path,
            hashlib.sha256(request.content).hexdigest(), request.headers.get('X-Run-Id')]).encode()).hexdigest()
        require(key not in self.state['nativeIntents'], 'native_intent_exists_no_retry')
        self.state['nativeIntents'][key] = {**call, 'status': 'intent',
            'bodySha256': hashlib.sha256(request.content).hexdigest(),
            'runId': request.headers.get('X-Run-Id'), 'recordedAt': time.time()}
        request.extensions['agent_line_trigger_intent'] = key
        self.save()  # fsync both file and directory before network transmission.

    def record_response(self, response):
        key = response.request.extensions.get('agent_line_trigger_intent')
        if key is not None:
            self.state['nativeIntents'][key].update(
                status='received', httpStatus=response.status_code, receivedAt=time.time())
            self.save()

    @contextmanager
    def transport(self):
        sim = self.sim_factory(self.auth['baseUrl'], self.auth['apiKey'])
        sim._client.event_hooks['request'].append(self.record_request)
        sim._client.event_hooks['response'].append(self.record_response)
        try:
            yield sim
        finally:
            sim.close()

    @contextmanager
    def application(self):
        require(not self.read_only, 'observation_does_not_open_service')
        # Reopening these handles and all API objects is intentional. Nothing is
        # initialized here, and only the new isolated stores are ever opened.
        tasks = TaskStore(self.directory / 'tasks.sqlite')
        journal = SimJournal(self.directory / 'journal.sqlite')
        projects = {PROJECT: {'workspace': str(self.directory / 'workspace')}}
        editor_token, runner_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        actor = TrustedPrincipal('trigger-live-editor', frozenset({PROJECT}),
            frozenset({'edit_workflows', 'publish_workflows'}), time.time() + 3600)
        with self.transport() as sim:
            remote = SimAuthoringClient(sim, self.auth['workspaceId'])
            authoring = AuthoringAPI(AuthoringAccess({editor_token: actor}), journal,
                                    {PROJECT: remote}, projects=projects)
            gateway = WorkflowGateway(sim, journal, {}, installation_id=INSTALLATION)
            binding = self.state.get('bindingId')
            triggers = TriggerAPI(gateway, {
                key: {'projectId': PROJECT, 'bindingId': binding, 'type': kind}
                for key, kind in (('hook', 'webhook'), ('timer', 'schedule'))
            } if binding else {}, projects=projects)
            config = ServiceConfig(tokens={runner_token: {PROJECT}}, projects=projects,
                                   bindings={}, controlled_execution='unknown')
            app = create_app(tasks, config, workflows=gateway, authoring=authoring, triggers=triggers)
            with TestClient(app) as http:
                yield http, {'Authorization': 'Bearer ' + editor_token}, {
                    'Authorization': 'Bearer ' + runner_token}, sim, journal

    def once(self, key, http, path, body, headers, *, expected=202):
        require(not self.read_only, 'observation_must_not_mutate')
        require(key not in self.state['steps'], 'outer_intent_exists_no_retry')
        self.state['steps'][key] = {'status': 'intent', 'path': path,
                                   'body': body, 'recordedAt': time.time()}
        self.save()
        response = http.post(path, json=body, headers=headers)
        result = response.json()
        self.state['steps'][key].update(status='received', httpStatus=response.status_code,
                                       result=result, receivedAt=time.time())
        self.save()
        require(response.status_code == expected, 'local_http_receipt_unexpected')
        return result

    def get(self, http, path, headers):
        response = http.get(path, headers=headers)
        require(response.status_code == 200, 'local_http_read_failed')
        return response.json()

    def create_and_publish(self):
        base = '/extension/v1/authoring/projects/' + PROJECT + '/workflows'
        with self.application() as (http, editor, _, _, _):
            for kind, expected in (('create', 'created'), ('save', 'saved'), ('publish', 'published')):
                if kind == 'create':
                    path, body = base, {'operationId': 'create-' + self.state['nonce'],
                        'name': 'agent-line-triggers-live', 'description': '零模型薄触发接线验收'}
                else:
                    path = base + '/' + self.state['workflowId']
                    draft = self.get(http, path, editor)
                    body = {'operationId': kind + '-' + self.state['nonce'],
                            'expectedStateSha256': draft['stateSha256']}
                    if kind == 'save':
                        body['document'] = document(self.state['marker'])
                    path += '/' + kind
                receipt = self.once(kind, http, path, body, editor)
                require(receipt.get('status') == 'accepted'
                        and receipt.get('receipt', {}).get('outcome') == expected,
                        'authoring_operation_not_confirmed_no_retry')
                result = receipt['receipt']
                if kind == 'create':
                    self.state['workflowId'] = segment(result['workflowId'])
                elif kind == 'publish':
                    self.state.update(bindingId=result['bindingId'], publication={
                        key: result[key] for key in ('revision', 'deploymentVersionId', 'graphSha256')})
                self.save()

    def native_writes(self):
        return sum(call['method'] != 'GET' for call in self.calls)

    def send(self, http, runner, label, trigger, kind, identity, inputs, *, expected=202):
        field = 'eventId' if kind == 'webhook' else 'slot'
        if expected == 202:
            rid = trigger_run_id(self.state['workflowId'], self.state['bindingId'], trigger, kind, identity)
            self.state['runs'].setdefault(rid, {'runId': rid, 'trigger': trigger,
                                             'kind': kind, field: identity, 'input': inputs})
            self.save()  # Original run identity survives even a lost local HTTP receipt.
        return self.once(label, http, '/extension/v1/triggers/' + trigger + '/' + kind,
                         {field: identity, 'input': inputs}, runner, expected=expected)

    def accepted(self, result, trigger, kind, identity):
        expected = trigger_run_id(self.state['workflowId'], self.state['bindingId'], trigger, kind, identity)
        require(result.get('status') == 'accepted' and result.get('runId') == expected
                and result.get('safeToResubmit') is False and result.get('verifiedComplete') is False,
                'trigger_not_accepted_no_retry')

    def trigger_cases(self):
        event, slot, next_slot = self.state['eventId'], *self.state['slots']
        inputs = {'marker': self.state['marker']}
        with self.application() as (http, _, runner, _, _):
            health = self.get(http, '/extension/v1/health', runner)
            require(health['controlledExecution'] == 'unknown' and health['readyForExecution'] is False,
                    'machine_capability_must_remain_unknown')
            before = self.native_writes()
            hook = self.send(http, runner, 'hook-first', 'hook', 'webhook', event, inputs)
            self.accepted(hook, 'hook', 'webhook', event)
            require(self.native_writes() == before + 1, 'hook_must_execute_once')
            duplicate = self.send(http, runner, 'hook-duplicate', 'hook', 'webhook', event, inputs)
            require(duplicate == hook and self.native_writes() == before + 1, 'hook_duplicate_wrote')
            self.send(http, runner, 'hook-conflict', 'hook', 'webhook', event,
                      {'marker': self.state['marker'], 'changed': True}, expected=409)
            require(self.native_writes() == before + 1, 'hook_conflict_wrote')
            timer = self.send(http, runner, 'timer-first', 'timer', 'schedule', slot, inputs)
            self.accepted(timer, 'timer', 'schedule', slot)
            duplicate = self.send(http, runner, 'timer-duplicate', 'timer', 'schedule', slot, inputs)
            require(duplicate == timer and self.native_writes() == before + 2, 'slot_duplicate_wrote')
            second = self.send(http, runner, 'timer-next', 'timer', 'schedule', next_slot, inputs)
            self.accepted(second, 'timer', 'schedule', next_slot)
            require(second['runId'] != timer['runId'] and self.native_writes() == before + 3,
                    'new_slot_did_not_execute_once')
        # Actual API objects, TaskStore and SimJournal are rebuilt from disk.
        before = self.native_writes()
        with self.application() as (http, _, runner, sim, journal):
            cases = [('hook', 'webhook', event, hook), ('timer', 'schedule', slot, timer),
                     ('timer', 'schedule', next_slot, second)]
            for index, (trigger, kind, identity, original) in enumerate(cases):
                replayed = self.send(http, runner, 'restart-' + str(index), trigger, kind, identity, inputs)
                require(replayed == original and self.native_writes() == before, 'restart_replay_wrote')
                frozen = journal.get_publication(self.state['workflowId'], original['runId'])
                require(frozen['bindingId'] == self.state['bindingId']
                        and frozen['projectId'] == PROJECT
                        and frozen['publication'] == self.state['publication'], 'frozen_publication_changed')
            self.state['checks'] = {'webhookDuplicate': True, 'webhookConflict409': True,
                'scheduleDuplicate': True, 'nextUtcSlot': True, 'reopenedApiReplay': True,
                'frozenPublication': True, 'controlledExecution': 'unknown'}
            self.save()
            return self.verify_native(sim, poll=True)

    def verify_native(self, sim, *, poll=False):
        remote = SimAuthoringClient(sim, self.auth['workspaceId'])
        workflow = self.state.get('workflowId')
        observed = {}
        if not workflow:
            return {'runs': {}, 'publicationVerified': False}
        detail = remote.get_detail(workflow)
        require(detail['id'] == workflow, 'original_workflow_changed')
        publication = self.state.get('publication')
        if publication:
            version = remote.get_version(workflow, publication['revision'])
            require(version.get('id') == publication['deploymentVersionId']
                    and version.get('isActive') is True
                    and compile_document(decode_document(version['state']), workflow)
                    == compile_document(document(self.state['marker']), workflow),
                    'original_publication_changed')
        for rid in self.state['runs']:
            data = None
            for _ in range(80 if poll else 1):
                value = sim.observe(workflow, rid)
                data = value.get('response', {}).get('data')
                if data and data.get('status') in ('completed', 'failed', 'cancelled'):
                    break
                if poll:
                    time.sleep(0.25)
            if not data or data.get('status') != 'completed':
                observed[rid] = {'status': data.get('status') if data else 'unknown', 'verified': False}
                continue
            require(data.get('workflowId') == workflow and data.get('runId') == rid
                    and (data.get('output') or {}).get('data') == {'marker': self.state['marker']},
                    'original_end_result_mismatch')
            response = sim._client.get(sim.base_url + '/api/v2/logs/' + rid,
                                      headers={'x-api-key': sim._key}, follow_redirects=False)
            require(response.status_code == 200, 'original_run_log_unavailable')
            envelope = response.json()
            log = envelope.get('data') if isinstance(envelope, dict) else None
            require(isinstance(log, dict) and envelope.get('success') is not False
                    and 'error' not in envelope and publication is not None
                    and log.get('runId') == rid and log.get('workflowId') == workflow
                    and log.get('deploymentVersionId') == publication['deploymentVersionId'],
                    'original_run_deployment_mismatch')
            observed[rid] = {'status': 'completed', 'verified': True,
                'deploymentVersionId': log['deploymentVersionId'], 'marker': self.state['marker']}
        return {'runs': observed, 'publicationVerified': publication is not None}


def run_probe(*, execute=False, directory=DATA, auth_path=None, sim_factory=SimClient,
              snapshot=None):
    require(type(execute) is bool, 'explicit_execute_boolean_required')
    directory = Path(directory)
    state_path = directory / 'state.json'
    # This early return makes default invocation safe even without credentials.
    if not state_path.exists() and not execute:
        return {'status': 'not_executed', 'requiresExecute': True, 'nativeWritesThisInvocation': 0}
    if directory.exists():
        info = directory.lstat()
        require(not directory.is_symlink() and stat.S_ISDIR(info.st_mode)
                and info.st_uid == os.getuid() and not info.st_mode & 0o077,
                'private_probe_directory_required')
    auth = private_file(auth_path or ROOT / 'var/sim/probe-auth.json')
    snapshot = snapshot or (lambda: main_snapshot(ROOT))
    before = snapshot()
    existed = state_path.exists()
    if existed:
        state = private_file(state_path)
    else:
        require(not directory.exists() or not any(directory.iterdir()),
                'partial_directory_requires_reconciliation')
        directory.mkdir(mode=0o700, parents=False, exist_ok=True)
        nonce = uuid.uuid4().hex
        minute = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        state = {'schemaVersion': 1, 'kind': KIND, 'workspaceId': segment(auth['workspaceId']),
            'baseUrl': auth['baseUrl'].rstrip('/'), 'nonce': nonce, 'marker': 'triggers-live-' + nonce,
            'eventId': 'event-' + nonce,
            'slots': [(minute + timedelta(minutes=i)).strftime('%Y-%m-%dT%H:%M:00Z') for i in (0, 1)],
            'steps': {}, 'nativeIntents': {}, 'runs': {}, 'mainSnapshot': before}
        write(state_path, state)
        (directory / 'workspace').mkdir(mode=0o700)
        TaskStore.initialize(directory / 'tasks.sqlite')
        SimJournal.initialize(directory / 'journal.sqlite')
    probe = TriggerProbe(auth, directory, state, read_only=existed, sim_factory=sim_factory)
    try:
        if existed:
            require(state['mainSnapshot'] == before, 'main_snapshot_changed_since_original_probe')
            with probe.transport() as sim:
                native = probe.verify_native(sim)
            report = {'status': 'observation_only', 'nativeWritesThisInvocation': probe.native_writes(),
                'originalRunCount': len(state['runs']), 'native': native,
                'incompleteOuterIntents': [key for key, value in state['steps'].items()
                                           if value.get('status') != 'received'],
                'incompleteNativeIntents': [key for key, value in state['nativeIntents'].items()
                                            if value.get('status') != 'received']}
        else:
            probe.create_and_publish()
            native = probe.trigger_cases()
            require(len(native['runs']) == 3 and all(r['verified'] for r in native['runs'].values()),
                    'native_runs_not_complete_no_resubmit')
            require(probe.native_writes() == 6, 'unexpected_native_write_count')
            with sqlite3.connect((directory / 'tasks.sqlite').resolve().as_uri() + '?mode=ro', uri=True) as db:
                require(db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
                        and db.execute('SELECT count(*) FROM workspace_leases').fetchone()[0] == 0,
                        'zero_model_probe_created_tasks')
            report = {'status': 'passed_live_zero_model', 'nativeWritesThisInvocation': 6,
                'nativeExecuteCount': 3, 'nativeCreateCount': 1, 'nativeSaveCount': 1,
                'nativePublishCount': 1, 'native': native, 'checks': state['checks'],
                'modelCalls': 0, 'nodeTaskCount': 0, 'activeLeaseCount': 0,
                'scope': 'Explicit event ingress only; no scheduler or autonomous event source'}
        require(snapshot() == before, 'main_configuration_or_guard_changed')
        report['mainSnapshotUnchanged'] = True
        require(not existed or probe.native_writes() == 0, 'observation_must_not_mutate')
        write(directory / ('observation.json' if existed else 'report.json'), report)
        return report
    except Exception as exc:
        write(directory / 'failure.json', {'status': 'incomplete', 'errorType': type(exc).__name__,
            'code': getattr(exc, 'code', 'probe_check_failed'),
            'nativeWritesThisInvocation': probe.native_writes(), 'replayPermitted': False})
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    try:
        result = run_probe(execute=args.execute)
        print(json.dumps({key: result[key] for key in ('status', 'nativeWritesThisInvocation')}))
        return 0
    except Exception as exc:
        print(json.dumps({'status': 'incomplete', 'errorType': type(exc).__name__,
                          'code': getattr(exc, 'code', 'probe_check_failed'), 'replayPermitted': False}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
