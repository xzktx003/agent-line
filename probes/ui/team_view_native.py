"""Real browser persistence proof against an isolated local TeamView API.

Only actual, existing profile identifiers are referenced. The isolated project
registry and fake workflow client confer no canonical project ownership and no
native execution capability. No Sim, model, QwenPaw API, or native Agent call is
made. The same private SimJournal is reopened across a full Uvicorn restart.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import threading
import time
import uuid
from urllib.parse import urlsplit

import httpx
import uvicorn
from fastapi import FastAPI
from playwright.async_api import async_playwright, expect

from agent_line.approvals import TrustedPrincipal
from agent_line.authoring_api import AuthoringAPI, AuthoringAccess
from agent_line.sim_journal import SimJournal
from agent_line.store import TaskStore, canonical
from agent_line.workbench import mount_workbench
from probes.qwenpaw_memory.reconcile_browser import CHROME

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'var/team-view-20261022/native'
PROFILE_CONFIG = ROOT / 'var/luna-native-candidate-20261022/proxy-fixed/install/var/qwenpaw/config.json'
PROFILE_WORKSPACES = PROFILE_CONFIG.parent / 'workspaces'
PROJECT = 'isolated-team-view-proof'
BASE = 'http://127.0.0.1:32981'
PORT = 32981
TOKEN = None
SUBJECT = 'isolated team graph browser proof'
ROLE_IDS = ('default', 'luna-proxy-parent', 'luna-proxy-codex')


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    with os.fdopen(os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600), 'w') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)


def protected_snapshot():
    service_path = ROOT / 'var/adapter/service.json'
    service = json.loads(service_path.read_text())
    config_paths = [service_path]
    for key in ('authoringFile',):
        if service.get(key):
            config_paths.append((service_path.parent / service[key]).resolve())
    sim = service.get('sim') or {}
    if sim.get('authFile'):
        config_paths.append((service_path.parent / sim['authFile']).resolve())
    config_hashes = {str(path.relative_to(ROOT)): file_hash(path) for path in config_paths if path.is_file()}
    database = Path(service['database'])
    if not database.is_absolute(): database = service_path.parent / database
    journal = (service_path.parent / sim['journal']).resolve()
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        tasks = [dict(row) for row in db.execute('SELECT * FROM tasks ORDER BY id')]
        events = [tuple(row) for row in db.execute('SELECT * FROM events ORDER BY rowid')]
    with sqlite3.connect(journal.as_uri() + '?mode=ro', uri=True) as db:
        sim_operations = db.execute('SELECT * FROM operations ORDER BY workflow,run,context').fetchall()
    recorded = json.loads((ROOT / 'var/execution-queue-20261022/main-rollout/state.json').read_text())['eventDigest']
    task_event_sha = hashlib.sha256(json.dumps(events, sort_keys=True).encode()).hexdigest()
    assert task_event_sha == recorded, 'main_task_event_digest_differs_from_protected_rollout_record'
    return {'mainTaskRowsSha256': digest(tasks), 'mainTaskEventsSha256': task_event_sha,
            'mainTaskEventsRecordedSha256': recorded,
            'mainSimOperationsSha256': digest(sim_operations),
            'mainConfigurationSha256': config_hashes}


def profile_evidence():
    data = json.loads(PROFILE_CONFIG.read_text())
    profiles = data.get('agents', {}).get('profiles', [])
    assert all(role in profiles for role in ROLE_IDS), 'selected_profile_id_missing'
    paths = [PROFILE_CONFIG]
    for role in ROLE_IDS:
        agent = PROFILE_WORKSPACES / role / 'agent.json'
        if agent.is_file(): paths.append(agent)
    return {str(path.relative_to(ROOT)): file_hash(path) for path in paths}, list(ROLE_IDS)


class NoSimOrNativeCalls:
    def __getattr__(self, name):
        def refused(*args, **kwargs):
            raise AssertionError('unexpected simulated/native workflow client call: ' + name)
        return refused


class Server:
    def __init__(self, journal_path, task_path, token):
        self.journal = SimJournal(journal_path)
        self.tasks = TaskStore(task_path)
        self.access = AuthoringAccess({token: TrustedPrincipal(
            SUBJECT, frozenset({PROJECT}), frozenset({'edit_workflows'}), time.time() + 900)})
        bindings = {f'profile-binding-{i}': {'projectId': PROJECT, 'roleAgentId': role,
                    'enabled': True} for i, role in enumerate(ROLE_IDS)}
        # This registry exists only inside the private test process/directory.
        api = AuthoringAPI(self.access, self.journal, {PROJECT: NoSimOrNativeCalls()},
                           projects={PROJECT: {'name': '隔离项目目录', 'workspace': str(OUT)}},
                           role_bindings=bindings, task_store=self.tasks)
        self.app = FastAPI()
        self.app.include_router(api.router())
        mount_workbench(self.app, ROOT / 'frontend')
        self.config = uvicorn.Config(self.app, host='127.0.0.1', port=PORT, log_level='error',
                                     access_log=False, lifespan='off')
        self.server = uvicorn.Server(self.config)
        self.thread = None

    def start(self):
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, name='team-view-isolated-uvicorn', daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.server.started:
                return
            if not self.thread.is_alive(): raise RuntimeError('isolated_server_failed_to_start')
            time.sleep(0.05)
        raise TimeoutError('isolated_server_start_timeout')

    def stop(self):
        if self.thread and self.thread.is_alive():
            self.server.should_exit = True
            self.thread.join(timeout=10)
            if self.thread.is_alive():
                self.server.force_exit = True
                self.thread.join(timeout=3)
            if self.thread.is_alive(): raise TimeoutError('isolated_server_failed_to_stop')


async def connect(page):
    await page.goto(BASE + '/workbench/team.html')
    await page.locator('#team-token').fill(TOKEN)
    await page.locator('#team-connect').click()
    await expect(page.locator('#team-connection')).to_contain_text('已连接')
    await page.locator('#team-project').select_option(PROJECT)
    await expect(page.locator('#team-content')).to_be_visible()


async def main():
    global TOKEN
    OUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(OUT, 0o700)
    profile_hashes, role_ids = profile_evidence()
    before = protected_snapshot()
    journal_path = OUT / 'team-journal.sqlite'
    task_path = OUT / 'tasks.sqlite'
    if not journal_path.exists(): SimJournal.initialize(journal_path)
    if not task_path.exists(): TaskStore.initialize(task_path)
    TOKEN = 'isolated-team-view-' + uuid.uuid4().hex + uuid.uuid4().hex
    server = Server(journal_path, task_path, TOKEN)
    writes, errors = [], []
    operation_rows = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=CHROME, args=['--no-sandbox'])
        try:
            context = await browser.new_context(viewport={'width': 1440, 'height': 1000})
            async def record(route):
                req = route.request
                if '/extension/v1/authoring/' not in req.url:
                    await route.continue_(); return
                if req.method == 'POST':
                    body = req.post_data_json
                    expected = '/extension/v1/authoring/projects/' + PROJECT + '/team'
                    if urlsplit(req.url).path != expected or len(writes) >= 2:
                        await route.abort(); raise RuntimeError('refuse_unplanned_team_mutation')
                    writes.append({'body': body, 'at': time.time()})
                    save(OUT / 'operations.json', writes)
                elif req.method != 'GET':
                    await route.abort(); raise RuntimeError('refuse_unplanned_http_method')
                await route.continue_()
            await context.route('**/extension/v1/authoring/**', record)
            page = await context.new_page()
            page.on('pageerror', lambda error: errors.append(type(error).__name__))
            server.start()
            await connect(page)
            assert await page.locator('#team-equivalent').get_by_text(role_ids[0]).count() == 1
            assert await page.locator('#team-equivalent').get_by_text(role_ids[1]).count() == 1
            assert await page.locator('#team-equivalent').get_by_text(role_ids[2]).count() == 1
            first_edges = [{'sourceAgentId': role_ids[0], 'targetAgentId': role_ids[1]},
                           {'sourceAgentId': role_ids[1], 'targetAgentId': role_ids[2]}]
            for source, target in ((role_ids[0], role_ids[1]), (role_ids[1], role_ids[2])):
                await page.locator('#edge-source').select_option(source)
                await page.locator('#edge-target').select_option(target)
                await page.locator('#edge-add').click()
            async with page.expect_response(lambda r: r.request.method == 'POST' and r.url.endswith('/team')) as event:
                await page.locator('#team-save').click()
            first = await (await event.value).json()
            await expect(page.locator('#team-notice')).to_contain_text('保存')
            await expect(page.locator('#team-save')).to_be_disabled()
            assert writes[0]['body'] == {'operationId': writes[0]['body']['operationId'],
                                         'expectedRevision': 0, 'edges': first_edges}
            operation_rows.append({'revision': first['revision'], 'edges': first['edges']})
            await page.screenshot(path=str(OUT / 'first-save-desktop.png'), full_page=True)
            await page.close()
            server.stop()
            server = None

            # A new application object and listener reopen the same private SQLite stores.
            server = Server(journal_path, task_path, TOKEN)
            server.start()
            page = await context.new_page()
            page.on('pageerror', lambda error: errors.append(type(error).__name__))
            await connect(page)
            await expect(page.locator('#team-state')).to_contain_text('团队版本 1')
            for edge in first_edges:
                await expect(page.locator('#edge-staging')).to_contain_text(edge['sourceAgentId'])
                await expect(page.locator('#edge-staging')).to_contain_text(edge['targetAgentId'])
            await page.locator('#edge-staging button').first.click()
            await page.locator('#edge-source').select_option(role_ids[2])
            await page.locator('#edge-target').select_option(role_ids[0])
            await page.locator('#edge-add').click()
            second_edges = [{'sourceAgentId': role_ids[2], 'targetAgentId': role_ids[0]},
                            {'sourceAgentId': role_ids[1], 'targetAgentId': role_ids[2]}]
            async with page.expect_response(lambda r: r.request.method == 'POST' and r.url.endswith('/team')) as event:
                await page.locator('#team-save').click()
            second = await (await event.value).json()
            assert second['revision'] == 2 and second['edges'] == second_edges
            assert writes[1]['body'] == {'operationId': writes[1]['body']['operationId'],
                                         'expectedRevision': 1, 'edges': second_edges}
            operation_rows.append({'revision': second['revision'], 'edges': second['edges']})
            await page.set_viewport_size({'width': 390, 'height': 844})
            await page.screenshot(path=str(OUT / 'second-save-mobile.png'), full_page=True)
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert await page.evaluate('localStorage.length + sessionStorage.length === 0')
            assert not errors, errors
            await page.locator('#team-logout').click()
            assert await page.locator('#team-token').input_value() == ''
            assert await page.locator('#team-content').is_hidden()
            await page.close()
            server.stop(); server = None
        finally:
            if server is not None: server.stop()
            await browser.close()
    with SimJournal(journal_path).connect() as db:
        history = db.execute("SELECT run,input,receipt FROM operations WHERE context='team-view' ORDER BY rowid").fetchall()
    decoded = [{'operationId': row['run'], 'input': json.loads(row['input']), 'receipt': json.loads(row['receipt'])}
               for row in history]
    assert len(decoded) == 2
    assert [item['receipt']['revision'] for item in decoded] == [1, 2]
    after = protected_snapshot()
    assert before == after, 'main task/events/config changed during isolated proof'
    profile_hashes_after, role_ids_after = profile_evidence()
    assert role_ids_after == role_ids and profile_hashes_after == profile_hashes, 'native_profile_configuration_changed'
    with SimJournal(journal_path).connect() as db:
        contexts = [row[0] for row in db.execute('SELECT context FROM operations')]
    with TaskStore(task_path).connect() as db:
        isolated_task_count = db.execute('SELECT count(*) FROM tasks').fetchone()[0]
    assert contexts and set(contexts) == {'team-view'}
    assert isolated_task_count == 0
    report = {'passed': True, 'mode': 'real_loopback_browser_isolated_authoring_api_restart',
              'projectRegistry': 'isolated_test_metadata_only_no_canonical_ownership_or_execution_grant',
              'actualExistingProfileIds': role_ids, 'profileConfigSha256Before': profile_hashes,
              'profileConfigSha256After': profile_hashes_after,
              'teamOperations': [{'operationId': row['operationId'], 'revision': row['receipt']['revision'],
                                  'edges': row['receipt']['edges']} for row in decoded],
              'serverFullRestartBetweenSaves': True, 'sameTeamJournalReopened': True,
              'protectedMainStateBefore': before, 'protectedMainStateAfter': after,
              'mainStatePreserved': True, 'fakeSimClientCalls': 0, 'simCalls': 0,
              'isolatedNonTeamJournalOperations': 0, 'isolatedTaskRows': isolated_task_count,
              'nativeAgentCalls': 0, 'modelCalls': 0, 'newAgentMetadataCreated': False,
              'threeFormalRolesRecreated': False, 'browserPageErrors': errors,
              'screenshots': ['first-save-desktop.png', 'second-save-mobile.png']}
    save(OUT / 'report.json', report)
    for file in OUT.glob('*.png'): file.chmod(0o600)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as error:
        if OUT.is_dir(): save(OUT / 'incomplete.json', {'errorType': type(error).__name__,
                                                         'replayPolicy': 'do_not_replay_mutations; inspect_original_team_operations'})
        print(json.dumps({'status': 'incomplete', 'errorType': type(error).__name__}))
        raise SystemExit(1)
