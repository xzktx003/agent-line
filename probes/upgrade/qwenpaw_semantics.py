#!/usr/bin/env python3
"""Isolated QwenPaw commit upgrade; Sim remains the same external installation."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import httpx
from fastapi.testclient import TestClient
from probes.upgrade.qwenpaw_runtime import Runtime
from probes.upgrade.qwenpaw_persistence import OLD, NEW, copy_offline, save
from agent_line.adapters.sim import SimClient
from agent_line.authoring_api import AuthoringAPI, AuthoringAccess
from agent_line.approvals import TrustedPrincipal
from agent_line.published_workflows import PublishedWorkflowCatalog
from agent_line.service import ServiceConfig, create_app
from agent_line.sim_authoring import SimAuthoringClient, compile_document
from agent_line.sim_journal import SimJournal
from agent_line.store import TaskStore

ROLE = 'upgrade-semantics-role'
PROJECT = 'upgrade-semantics'
MEMORY = '/api/workspace/memory/upgrade-semantics.md?section=digest'
CONTENT = '# 原生升级验收\n\n固定事实 SEMANTICS_NATIVE_cobalt_orchard。\n'


def document():
    def node(i, kind, config):
        return dict(id=i, type=kind, name=i, position={'x': 20, 'y': 20}, config=config)
    return {'schemaVersion': 1, 'variables': {}, 'nodes': [
        node('start', 'start', {}),
        node('choice', 'condition', {'rules': [{'id': 'yes', 'operator': 'eq', 'field': 'input.approved', 'value': True}]}),
        node('accepted', 'end', {'output': {'branch': 'accepted'}}),
        node('rejected', 'end', {'output': {'branch': 'rejected'}})],
        'edges': [{'id': 'begin', 'source': 'start', 'target': 'choice'},
                  {'id': 'yes_path', 'source': 'choice', 'target': 'accepted', 'branch': 'yes'},
                  {'id': 'no_path', 'source': 'choice', 'target': 'rejected', 'branch': 'else'}]}


def call(client, method, path, body=None):
    response = client.request(method, path, json=body)
    if response.status_code >= 400:
        raise RuntimeError('HTTP failed: ' + str(response.status_code))
    return response.json()


@contextmanager
def host(runtime):
    base = runtime.start()
    with httpx.Client(base_url=base, timeout=30) as client:
        assert client.get('/api/agent-line/health').status_code == 401
        login = call(client, 'POST', '/api/auth/login', runtime.credentials)
        token = login.get('token') or login.get('access_token')
        assert token
        client.headers.update({'Authorization': 'Bearer ' + token, 'X-Agent-Id': ROLE})
        deadline = time.monotonic() + 60
        while client.get('/api/agent-line/health').status_code == 404 and time.monotonic() < deadline:
            time.sleep(.2)
        yield client


def plugin_hashes(runtime, expected=None):
    expected = Path(expected) if expected else ROOT / 'plugins/agent-line/dist/index.js'
    candidates = list(runtime.data.rglob('plugins/agent-line/dist/index.js'))
    if len(candidates) != 1:
        raise RuntimeError('Installed plugin frontend not uniquely located')
    digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    assert digest(candidates[0]) == digest(expected), 'Installed frontend differs from extension source'
    return {'frontendSha256': digest(expected)}


def inspect_host(runtime, client, frontend=None):
    health = call(client, 'GET', '/api/agent-line/health')
    assert health.get('scope') == 'plugin-registration-only'
    roles = call(client, 'GET', '/api/agents')['agents']
    found = [r for r in roles if r['id'] == ROLE]
    assert len(found) == 1
    role = found[0]
    memory = call(client, 'GET', MEMORY)['content']
    assert memory.strip() == CONTENT.strip()
    return {'role': {k: role.get(k) for k in ('id', 'name', 'backend', 'description')},
            'memorySha256': hashlib.sha256(memory.strip().encode()).hexdigest(),
            'anonymousDenied': True, 'pluginAuthenticated': True, **plugin_hashes(runtime, frontend)}


class Extension:
    def __init__(self, instance, settings, initialize=False):
        self.path = instance / 'extension'
        if initialize:
            self.path.mkdir(mode=0o700)
            TaskStore.initialize(self.path / 'tasks.sqlite')
            SimJournal.initialize(self.path / 'journal.sqlite')
            save(self.path / 'access.json', {'author': secrets.token_urlsafe(32), 'service': secrets.token_urlsafe(32)})
        access = json.loads((self.path / 'access.json').read_text())
        self.sim = SimClient(settings['baseUrl'], settings['apiKey'])
        self.native = SimAuthoringClient(self.sim, settings['workspaceId'])
        self.journal = SimJournal(self.path / 'journal.sqlite')
        tasks = TaskStore(self.path / 'tasks.sqlite')
        projects = {PROJECT: {'workspace': str(instance)}}
        credentials = AuthoringAccess({access['author']: TrustedPrincipal('upgrade-probe', frozenset({PROJECT}), frozenset({'edit_workflows', 'publish_workflows'}), time.time()+86400)})
        self.api = AuthoringAPI(credentials, self.journal, {PROJECT: self.native}, projects=projects)
        app = create_app(tasks, ServiceConfig(tokens={access['service']: {PROJECT}}, projects=projects, bindings={}), authoring=self.api)
        self.http = TestClient(app)
        self.http.headers.update({'Authorization': 'Bearer ' + access['author']})

    def close(self):
        self.http.close()
        self.sim.close()

    def request(self, method, path='', body=None):
        return call(self.http, method, '/extension/v1/authoring/projects/'+PROJECT+'/workflows'+path, body)

    def inspect(self, state):
        detail = self.request('GET', '/'+state['workflowId'])
        assert compile_document(detail['document'], state['workflowId']) == compile_document(document(), state['workflowId'])
        assert detail['publishedBindings'] == [{'bindingId': state['publication']['bindingId'], 'revision': state['publication']['revision']}]
        version = self.native.get_version(state['workflowId'], state['publication']['revision'])
        assert version['id'] == state['publication']['deploymentVersionId']
        catalog = PublishedWorkflowCatalog(self.api).bindings({PROJECT})
        binding = catalog[state['publication']['bindingId']]
        history = self.sim.observe(state['workflowId'], state['runId'])
        assert history['status'] == 'completed'
        native = history.get('response', {})
        native = native.get('data', native)
        assert native.get('output', {}).get('data') == {'branch': 'accepted'}
        recorded = self.journal.get(state['workflowId'], state['runId'])
        assert recorded['status'] == 'accepted'
        return {'document': detail['document'], 'publication': state['publication'], 'binding': binding,
                'history': history, 'journal': recorded}


def once(state, path, name, operation):
    """Persist before each write. Any prior intent forbids another request."""
    if name in state['intents']:
        raise RuntimeError('Existing intent: observe only, never resubmit')
    state['intents'][name] = 'intent'
    save(path, state)
    value = operation()
    state['intents'][name] = {'received': value}
    save(path, state)
    return value


def run(attempt, old=None, new=None, plugin=None):
    old = old or OLD
    new = new or NEW
    frontend = Path(plugin) / 'dist/index.js' if plugin else None
    if not re.fullmatch('[A-Za-z0-9_-]{1,80}', attempt):
        raise ValueError('Invalid attempt')
    directory = ROOT / 'var/upgrade-runtime' / attempt
    if directory.exists():
        # No host restart or Sim mutation on rerun; original state remains authoritative.
        report = json.loads((directory / 'report.json').read_text())
        print(json.dumps({'phase': report['phase'], 'verified': report.get('verified', False), 'mode': 'existing_report_only'}))
        return report
    directory.mkdir(mode=0o700)
    state = {'phase': 'initializing', 'verified': False, 'intents': {}, 'oldCommit': old, 'newCommit': new, 'plugin': str(plugin) if plugin else 'plugins/agent-line', 'productionManifestChanged': False,
             'runId': str(uuid.uuid4()), 'observations': [], 'productionChanged': False,
             'simRestored': False, 'modelExecutionValidated': False, 'nativeRecallValidated': False,
             'nineCompatibilityProbesComplete': False, 'scope': 'isolated_qwenpaw_and_extension_snapshot_with_unchanged_external_sim'}
    path = directory / 'report.json'
    save(path, state)
    settings = json.loads((ROOT / 'var/sim/probe-auth.json').read_text())
    instance = directory / 'installation'
    active = None
    extension = None
    try:
        active = Runtime(ROOT, old, instance, plugin)
        active.initialize()
        print('phase: old runtime initialized', flush=True)
        with host(active) as client:
            once(state, path, 'role', lambda: call(client, 'POST', '/api/agents', {'id': ROLE, 'name': '升级语义验收角色', 'description': '隔离测试，不调用模型', 'backend': 'qwenpaw', 'language': 'zh', 'skill_names': []}))
            once(state, path, 'memory', lambda: call(client, 'PUT', MEMORY, {'content': CONTENT}))
            old_host = inspect_host(active, client, frontend)
            extension = Extension(instance, settings, initialize=True)
            created = once(state, path, 'create', lambda: extension.request('POST', body={'operationId': 'create-'+uuid.uuid4().hex, 'name': '升级语义隔离验收 '+attempt, 'description': '无模型条件分支；Sim固定不回滚'}))
            state['workflowId'] = created['receipt']['workflowId']; save(path, state)
            detail = extension.request('GET', '/'+state['workflowId'])
            saved = once(state, path, 'save', lambda: extension.request('POST', '/'+state['workflowId']+'/save', {'operationId': 'save-'+uuid.uuid4().hex, 'expectedStateSha256': detail['stateSha256'], 'document': document()}))
            assert saved['receipt']['outcome'] == 'saved'
            detail = extension.request('GET', '/'+state['workflowId'])
            published = once(state, path, 'publish', lambda: extension.request('POST', '/'+state['workflowId']+'/publish', {'operationId': 'publish-'+uuid.uuid4().hex, 'expectedStateSha256': detail['stateSha256']}))
            assert published['receipt']['outcome'] == 'published'
            state['publication'] = published['receipt']; save(path, state)
            once(state, path, 'execute', lambda: extension.journal.execute(extension.sim, state['workflowId'], state['runId'], {'input': {'approved': True}}))
            for _ in range(30):
                if extension.sim.observe(state['workflowId'], state['runId'])['status'] == 'completed': break
                time.sleep(.5)
            initial = extension.inspect(state)
            state['observations'].append({'phase': 'old', 'host': old_host, 'extension': initial, 'source': active.verify_source()})
        extension.close(); extension = None
        active.stop(); active = None
        copy_offline(instance, directory / 'offline-backup')
        state['phase'] = 'backup_complete'; save(path, state)
        print('phase: old verified and offline backup complete', flush=True)
        active = Runtime(ROOT, new, instance, plugin)
        with host(active) as client:
            assert inspect_host(active, client, frontend) == old_host
            extension = Extension(instance, settings)
            assert extension.inspect(state) == initial
            state['observations'].append({'phase': 'new', 'source': active.verify_source(), 'hostAndExtensionMatch': True})
        extension.close(); extension = None
        active.stop(); active = None
        state['phase'] = 'upgrade_verified'; save(path, state)
        print('phase: new version semantics verified', flush=True)
        instance.rename(directory / 'post-upgrade-installation')
        copy_offline(directory / 'offline-backup', instance)
        active = Runtime(ROOT, old, instance, plugin)
        with host(active) as client:
            assert inspect_host(active, client, frontend) == old_host
            extension = Extension(instance, settings)
            assert extension.inspect(state) == initial
            state['observations'].append({'phase': 'restored_old', 'source': active.verify_source(), 'hostAndExtensionMatch': True})
        extension.close(); extension = None
        active.stop(); active = None
        state.update(phase='complete', verified=True, offlineSnapshotRestored=True)
        save(path, state)
        print(json.dumps({'phase': state['phase'], 'verified': True, 'workflowId': state['workflowId'], 'report': str(path)}))
        return state
    except BaseException as error:
        state['failureType'] = type(error).__name__
        save(path, state)
        raise
    finally:
        if extension is not None: extension.close()
        if active is not None: active.stop()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--attempt', required=True)
    parser.add_argument('--old', default=OLD)
    parser.add_argument('--new', default=NEW)
    parser.add_argument('--plugin')
    args = parser.parse_args()
    run(args.attempt, args.old, args.new, args.plugin)
