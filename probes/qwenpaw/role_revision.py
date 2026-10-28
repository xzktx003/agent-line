#!/usr/bin/env python3
"""Actual isolated QwenPaw configuration drift; no real model or Codex calls.

The executor binary is a private sentinel that records unexpected invocation and
exits. It cannot execute Codex. This proves admission checks, not model execution.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import httpx
from probes.upgrade.qwenpaw_runtime import Runtime
from probes.upgrade.qwenpaw_persistence import NEW, save

ROLE = 'revision-fixture-parent'
TARGET = 'revision-fixture-executor'


def run(attempt):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', attempt):
        raise ValueError('Invalid attempt')
    out = ROOT / 'var/upgrade-runtime' / attempt
    if out.exists():
        old = json.loads((out / 'report.json').read_text())
        print(json.dumps({'mode': 'existing_report_only', 'phase': old['phase'], 'verified': old.get('verified', False)}))
        return old
    out.mkdir(mode=0o700)
    report = {'phase': 'prepared', 'verified': False, 'intents': {}, 'requests': [],
              'scope': 'live_isolated_config_and_plugin_admission_only',
              'realModelExecution': False, 'realCodexExecution': False, 'productionChanged': False}
    path = out / 'report.json'
    persist = lambda: save(path, report)
    persist()
    runtime = Runtime(ROOT, NEW, out / 'installation')
    runtime.initialize()
    instance = runtime.instance
    token_file = instance / 'execution-token'
    token_file.write_text(secrets.token_urlsafe(40)); token_file.chmod(0o600)
    workspace = instance / 'executor-workspace'; workspace.mkdir(mode=0o700)
    codex_home = instance / 'codex-home'; codex_home.mkdir(mode=0o700)
    sentinel = instance / 'unexpected-executor-invocation'
    launcher = instance / 'sentinel-launcher'
    launcher.write_text('#!/usr/bin/env python3\nfrom pathlib import Path\nPath(' + repr(str(sentinel)) + ').touch()\nraise SystemExit(99)\n')
    launcher.chmod(0o700)
    control = instance / 'sentinel-control.py'; control.write_text('raise SystemExit(99)\n'); control.chmod(0o700)
    receipt_db = instance / 'receipts.sqlite'
    spec = importlib.util.spec_from_file_location('revision_probe_bridge', ROOT / 'plugins/agent-line/execution_bridge.py')
    bridge = importlib.util.module_from_spec(spec); spec.loader.exec_module(bridge)
    bridge.ReceiptLedger.initialize(receipt_db)
    runtime._run(['-c', '''import asyncio
from qwenpaw.providers.provider_manager import ProviderManager
from qwenpaw.providers.provider import ProviderInfo, ModelInfo
asyncio.run(ProviderManager.get_instance().add_custom_provider(ProviderInfo(
    id='revision-fixture', name='No network fixture', base_url='http://127.0.0.1:9/v1',
    api_key='not-a-real-key', enabled=True, extra_models=[
        ModelInfo(id='fixture-a',name='Fixture A'), ModelInfo(id='fixture-b',name='Fixture B')],
    chat_model='OpenAIChatModel')))
'''])

    def once(name, operation):
        if name in report['intents']:
            raise RuntimeError('Observe original intent; never repeat a write')
        report['intents'][name] = 'intent'; persist()
        result = operation()
        report['intents'][name] = 'received'; persist()
        return result

    def connect():
        client = httpx.Client(base_url=runtime.start(), timeout=30, trust_env=False)
        response = client.post('/api/auth/login', json=runtime.credentials)
        response.raise_for_status(); payload = response.json()
        token = payload.get('token') or payload.get('access_token')
        if not token:
            client.close(); raise RuntimeError('Authentication unavailable')
        client.headers.update({'Authorization': 'Bearer ' + token, 'X-Agent-Id': ROLE,
                               'X-Agent-Line-Service-Token': token_file.read_text().strip()})
        # AuthMiddleware may return 401 before asynchronous plugin registration.
        # Wait on the actual authenticated route, never infer readiness from it.
        for _ in range(120):
            health = client.get('/api/agent-line/health')
            if health.status_code == 200:
                return client
            if health.status_code != 404:
                client.close(); raise RuntimeError('Plugin health unavailable')
            time.sleep(.5)
        client.close(); raise RuntimeError('Authenticated plugin readiness deadline')

    def request(client, method, route, body=None, expected=200):
        response = client.request(method, route, json=body)
        report['requests'].append({'method': method, 'path': route, 'status': response.status_code}); persist()
        if response.status_code != expected:
            raise RuntimeError('Unexpected fixture HTTP status: ' + str(response.status_code))
        return response.json()

    def pin(client):
        value = request(client, 'GET', '/api/agent-line/execution-binding')['roleRevisions'][ROLE]
        if not re.fullmatch(r'qwenpaw-role-v1:[0-9a-f]{64}', value):
            raise RuntimeError('No valid server revision')
        return value

    def wait_not_starting(client, identifier):
        # Native toggle/delete reject queued startup. Observe the same owned
        # role before the one explicit write; never retry a rejected mutation.
        for _ in range(120):
            agents = request(client, 'GET', '/api/agents')['agents']
            matches = [item for item in agents if item['id'] == identifier]
            if len(matches) != 1:
                raise RuntimeError('Owned fixture role unavailable')
            if matches[0]['startup_status'] not in ('queued', 'starting'):
                return
            time.sleep(.5)
        raise RuntimeError('Owned fixture startup observation deadline')

    client = None
    try:
        client = connect()
        once('parent', lambda: request(client, 'POST', '/api/agents', {
            'id': ROLE, 'name': '修订验证角色', 'backend': 'qwenpaw', 'skill_names': [],
            'active_model': {'provider_id': 'revision-fixture', 'model': 'fixture-a'}}, 201))
        once('executor', lambda: request(client, 'POST', '/api/agents', {
            'id': TARGET, 'name': '不可执行的验证后端', 'backend': 'codex', 'workspace_dir': str(workspace),
            'backend_settings': {'binary': str(launcher), 'model': 'fixture-model'}}, 201))
        client.close(); client = None; runtime.stop()
        config = {'managed_role_ids': [ROLE], 'codex_target_agent_id': TARGET, 'execution_bridge': {
            'receipt_db': str(receipt_db), 'service_token_file': str(token_file), 'target_agent_id': TARGET,
            'workspace': str(workspace), 'codex_home': str(codex_home),
            'container_control': str(control), 'container_launcher': str(launcher)}}
        runtime._run(['-c', 'import json\nfrom qwenpaw.config.utils import load_config,save_config\n'
            + 'cfg=load_config();cfg.plugins["agent-line"]=' + repr(config) + ';save_config(cfg)\n'])
        client = connect(); original = pin(client)
        report['originalRevision'] = original
        body = {'taskId': 'revision-task', 'roleAgentId': ROLE, 'roleSessionId': 'agent-line-revision-task', 'roleRevision': original}
        once('bind_original', lambda: request(client, 'POST', '/api/agent-line/delegations/bind', body))
        profile = request(client, 'GET', '/api/agents/' + ROLE)
        profile['name'] = '同一身份的新名称'
        once('rename', lambda: request(client, 'PUT', '/api/agents/' + ROLE, profile))
        assert pin(client) == original
        once('native_memory', lambda: request(client, 'PUT', '/api/workspace/memory/revision-fixture.md?section=digest',
            {'content': '# 原生记忆\n\n只改记忆正文，不改角色行为。\n'}))
        assert pin(client) == original
        report['renameAndNativeMemoryKeepRevision'] = True
        report['disabledAdmissionChecks'] = {}
        # Toggle only this isolated probe's owned roles through the official API.
        # A disabled parent must not be selected as the request workspace itself.
        client.headers['X-Agent-Id'] = 'default'
        for label, identifier in (('parent', ROLE), ('executor', TARGET)):
            wait_not_starting(client, identifier)
            once('disable_' + label, lambda: request(client, 'PATCH',
                '/api/agents/' + identifier + '/toggle', {'enabled': False}))
            request(client, 'GET', '/api/agent-line/execution-binding', expected=409)
            refused = {**body, 'taskId': 'disabled-' + label,
                       'roleSessionId': 'agent-line-disabled-' + label}
            once('reject_disabled_bind_' + label, lambda: request(client, 'POST',
                '/api/agent-line/delegations/bind', refused, 409))
            once('reject_disabled_execution_' + label, lambda: request(client, 'POST',
                '/api/agent-line/executions', {'taskId': body['taskId'],
                    'roleSessionId': body['roleSessionId'], 'instruction': 'Must not start'}, 409))
            with sqlite3.connect(receipt_db) as db:
                assert db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0] == 0
                assert db.execute('SELECT COUNT(*) FROM role_bindings').fetchone()[0] == 1
            assert not sentinel.exists()
            once('reenable_' + label, lambda: request(client, 'PATCH',
                '/api/agents/' + identifier + '/toggle', {'enabled': True}))
            assert pin(client) == original
            report['disabledAdmissionChecks'][label] = {
                'descriptorStatus': 409, 'newBindingStatus': 409, 'newExecutionStatus': 409,
                'receiptCount': 0, 'sameRevisionAfterReenable': True}
            persist()
        client.headers['X-Agent-Id'] = ROLE
        for name, update in (
            ('model', lambda p: p.update(active_model={'provider_id': 'revision-fixture', 'model': 'fixture-b'})),
            ('permissions', lambda p: p.update(approval_level='STRICT')),
        ):
            profile = request(client, 'GET', '/api/agents/' + ROLE); update(profile)
            before = pin(client)
            once('change_' + name, lambda: request(client, 'PUT', '/api/agents/' + ROLE, profile))
            assert pin(client) != before
            refused = {**body, 'taskId': 'rejected-' + name, 'roleSessionId': 'agent-line-rejected-' + name}
            once('reject_bind_' + name, lambda: request(client, 'POST', '/api/agent-line/delegations/bind', refused, 409))
        soul = request(client, 'GET', '/api/workspace/files/SOUL.md')
        content = soul['content']
        before = pin(client)
        once('prompt', lambda: request(client, 'PUT', '/api/workspace/files/SOUL.md', {'content': content + '\n固定提示已调整。\n'}))
        assert pin(client) != before
        once('reject_execution', lambda: request(client, 'POST', '/api/agent-line/executions', {
            'taskId': body['taskId'], 'roleSessionId': body['roleSessionId'], 'instruction': 'Must not start'}, 409))
        with sqlite3.connect(receipt_db) as db:
            assert db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0] == 0
            assert db.execute('SELECT role_revision FROM role_bindings WHERE task_id=?', (body['taskId'],)).fetchone()[0] == original
        client.close(); client = None; runtime.stop()
        client = connect()
        once('reject_after_restart', lambda: request(client, 'POST', '/api/agent-line/executions', {
            'taskId': body['taskId'], 'roleSessionId': body['roleSessionId'], 'instruction': 'Must not start'}, 409))
        with sqlite3.connect(receipt_db) as db:
            assert db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0] == 0
        assert not sentinel.exists()
        client.headers['X-Agent-Id'] = 'default'
        wait_not_starting(client, ROLE)
        once('delete_owned_parent', lambda: request(client, 'DELETE', '/api/agents/' + ROLE))
        request(client, 'GET', '/api/agents/' + ROLE, expected=404)
        request(client, 'GET', '/api/agent-line/execution-binding', expected=409)
        once('reject_deleted_parent_bind', lambda: request(client, 'POST',
            '/api/agent-line/delegations/bind', {**body, 'taskId': 'deleted-parent',
                'roleSessionId': 'agent-line-deleted-parent'}, 409))
        with sqlite3.connect(receipt_db) as db:
            assert db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0] == 0
            assert db.execute('SELECT COUNT(*) FROM role_bindings').fetchone()[0] == 1
        assert not sentinel.exists()
        report['deletedParentRejectsNewAdmission'] = True
        report.update(phase='complete', verified=True, noExecutionClaims=True, sentinelInvoked=False,
                      oldPinSurvivesHostRestart=True, source=runtime.verify_source())
        persist()
        print(json.dumps({k: report[k] for k in ('phase', 'verified', 'scope', 'noExecutionClaims', 'sentinelInvoked')}))
        return report
    except BaseException as error:
        report.update(phase='failed', failureType=type(error).__name__); persist()
        raise
    finally:
        if client is not None: client.close()
        runtime.stop()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('--attempt', required=True)
    run(parser.parse_args().attempt)
