"""Copy one existing main draft through the actual browser, then save explicitly.

Only a new --execute state permits two authoring POSTs. Every subsequent call
is GET/SQLite read only, even with --execute. No publish, run, model or restart.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

import httpx
from playwright.async_api import async_playwright, expect

from agent_line.adapters.sim import SimClient
from agent_line.sim_authoring import SimAuthoringClient, decode_document
from agent_line.store import canonical
from probes.qwenpaw_memory.native_roundtrip import save
from probes.sim.publication_capability_browser import CHROME

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT/'var/workflow-copy-20261022/browser-native'
BASE = 'http://10.30.0.28:3193'
PROJECT = 'native-memory-roundtrip'
SOURCE = '1b2a278a-f28f-4bb2-b637-f79d045449fc'


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def task_digest(path):
    with sqlite3.connect(Path(path).as_uri()+'?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        return digest([dict(row) for row in db.execute('SELECT * FROM tasks ORDER BY id')])


def configuration():
    path = ROOT/'var/adapter/service.json'
    cfg = json.loads(path.read_text())
    def locate(value):
        return Path(value) if Path(value).is_absolute() else path.parent/value
    auth = json.loads(locate(cfg['authoringFile']).read_text())
    credential = next(c for c in auth['credentials'] if PROJECT in c['projectScopes'] and c['expiresAt'] > time.time())
    sim_auth = json.loads(locate(cfg['sim']['authFile']).read_text())
    client = SimClient(sim_auth['baseUrl'], sim_auth['apiKey'], client=httpx.Client(timeout=15, trust_env=False))
    native = SimAuthoringClient(client, auth['workspaces'][PROJECT])
    return cfg, locate(cfg['database']), credential['token'], native, client


def observe(state, database, native, token):
    assert task_digest(database) == state['tasksSha256'], 'main_tasks_changed'
    assert all(file_digest(path) == value for path, value in state['configurationHashes'].items()), 'configuration_changed'
    assert digest(native.get_state(SOURCE)) == state['sourceStateSha256'], 'source_state_changed'
    assert digest(native.versions(SOURCE)) == state['sourceVersionsSha256'], 'source_versions_changed'
    result = dict(sourceUnchanged=True, oldTasksUnchanged=True, configurationUnchanged=True,
                  newModelTasks=0, newRoleOperations=0)
    with httpx.Client(timeout=10, trust_env=False) as http:
        operations = []
        for intent in state['operations']:
            kind = 'save' if intent['path'].endswith('/save') else 'create'
            response = http.get(BASE+'/extension/v1/authoring/projects/'+PROJECT+'/operations/'+intent['operationId'],
                                params={'kind':kind}, headers={'Authorization':'Bearer '+token})
            assert response.status_code == 200, 'original_operation_unavailable'
            operation = response.json()
            assert (operation['projectId'] == PROJECT and operation['kind'] == kind
                    and operation['operationId'] == intent['operationId']), 'original_operation_identity_mismatch'
            if operation['status'] == 'accepted' and state.get('copyWorkflowId'):
                assert operation['receipt']['workflowId'] == state['copyWorkflowId'], 'original_workflow_identity_mismatch'
            operations.append(dict(kind=kind,status=operation['status'],outcome=(operation.get('receipt') or {}).get('outcome')))
        result['originalOperations'] = operations
        if state['status'] in ('browser_saved_and_reopened', 'passed_actual_copy_save_reopen'):
            assert operations == [dict(kind='create',status='accepted',outcome='created'),
                                  dict(kind='save',status='accepted',outcome='saved')], 'original_operations_not_confirmed'
    if state.get('copyWorkflowId'):
        workflow = state['copyWorkflowId']
        detail = native.get_detail(workflow)
        current = native.get_state(workflow)
        result['copiedDocumentMatches'] = decode_document(current) == state['sourceDocument']
        result['newWorkflowIdentity'] = workflow != SOURCE
        result['copyUnpublished'] = detail['isDeployed'] is False and native.versions(workflow)['data'] == []
        result['nativeIdNamespaceRemapped'] = set(current['blocks']).isdisjoint(native.get_state(SOURCE)['blocks'])
        assert all(result[k] for k in ('copiedDocumentMatches','newWorkflowIdentity','copyUnpublished','nativeIdNamespaceRemapped'))
    return result


async def execute(state, token):
    errors = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=CHROME, args=['--no-sandbox'])
        try:
            context = await browser.new_context(viewport={'width':1440,'height':1000})
            async def record(route):
                req = route.request
                if req.method == 'POST':
                    body = req.post_data_json
                    expected = ('/workflows' if not state['operations']
                                else '/workflows/'+state.get('copyWorkflowId', 'missing')+'/save')
                    if len(state['operations']) >= 2 or not req.url.endswith(expected):
                        await route.abort()
                        raise RuntimeError('refuse_unplanned_mutation')
                    state['operations'].append(dict(path=req.url.removeprefix(BASE),
                                                    operationId=body['operationId'],intentAt=time.time()))
                    save(OUT/'state.json',state)
                elif req.method != 'GET':
                    await route.abort()
                    raise RuntimeError('refuse_unplanned_method')
                await route.continue_()
            await context.route('**/extension/v1/**',record)
            async def connect(page):
                page.on('pageerror',lambda error:errors.append(type(error).__name__))
                await page.goto(BASE+'/workbench/editor.html')
                await page.locator('#token').fill(token)
                await page.locator('#login').click()
                await expect(page.locator('#connection')).to_have_text('编辑服务已连接')
                await page.locator('#project').select_option(PROJECT)
                await expect(page.locator('#refresh')).to_be_enabled()
            page = await context.new_page()
            await connect(page)
            await page.locator('.workflow-choice').filter(has_text=state['sourceName']).click()
            await expect(page.locator('#flow-heading')).to_have_text(state['sourceName'])
            await page.locator('#copy-details summary').click()
            await page.locator('#copy-name').fill(state['copyName'])
            async with page.expect_response(lambda r:r.request.method=='POST' and r.url.endswith('/workflows')) as event:
                await page.locator('#copy-submit').click()
            created = await (await event.value).json()
            save(OUT/'create-receipt.json',created)
            assert created['status']=='accepted' and created['receipt']['outcome']=='created'
            state['copyWorkflowId']=created['receipt']['workflowId']
            save(OUT/'state.json',state)
            await expect(page.locator('#notice')).to_contain_text('尚未保存到 Sim')
            await expect(page.locator('#flow-heading')).to_have_text(state['copyName'])
            assert await page.locator('#published-bindings a').count()==0
            assert len(state['operations'])==1
            async with page.expect_response(lambda r:r.request.method=='POST' and r.url.endswith('/save')) as event:
                await page.locator('#save').click()
            saved = await (await event.value).json()
            save(OUT/'save-receipt.json',saved)
            assert saved['status']=='accepted' and saved['receipt']['outcome']=='saved'
            await expect(page.locator('#save')).to_be_disabled()
            assert len(state['operations'])==2
            await page.screenshot(path=str(OUT/'desktop.png'),full_page=True)
            await page.close()
            # Entirely new page: no local copy or pending operation is reused.
            page = await context.new_page()
            await connect(page)
            await page.locator('.workflow-choice').filter(has_text=state['copyName']).click()
            await expect(page.locator('#flow-heading')).to_have_text(state['copyName'])
            await expect(page.locator('#node-list button')).to_have_count(len(state['sourceDocument']['nodes']))
            assert await page.locator('#published-bindings a').count()==0
            await page.set_viewport_size({'width':390,'height':844})
            await page.screenshot(path=str(OUT/'mobile.png'),full_page=True)
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            assert await page.evaluate('localStorage.length+sessionStorage.length')==0
            await page.locator('#logout').click()
            assert await page.locator('#copy-name').input_value()==''
            assert await page.locator('#canvas').text_content()==''
            assert not errors
            state.update(status='browser_saved_and_reopened',browserReopened=True,apiPosts=2,
                         modelTasks=0,publishCalls=0,runCalls=0,noHorizontalOverflow=True,logoutCleared=True)
            save(OUT/'state.json',state)
        finally:
            await browser.close()
            for screenshot in OUT.glob('*.png'):
                screenshot.chmod(0o600)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute',action='store_true')
    args=parser.parse_args()
    cfg,database,token,native,client=configuration()
    try:
        state_path=OUT/'state.json'
        if state_path.exists():
            state=json.loads(state_path.read_text())
            report=observe(state,database,native,token)
            save(OUT/'read-only-observation.json',dict(report,mode='observe_only',checkedAt=time.time()))
            print(json.dumps(dict(mode='observe_only',**report)))
            return
        if not args.execute:
            print(json.dumps(dict(mode='not_started',newSubmissions=0)))
            return
        OUT.mkdir(parents=True,mode=0o700,exist_ok=False)
        os.chmod(OUT.parent,0o700)
        baseline=json.loads((ROOT/'var/inflight-adapter-restart-20261022/runtime/state.json').read_text())
        source=native.get_state(SOURCE)
        document=decode_document(source)
        state=dict(status='prepared',sourceWorkflowId=SOURCE,sourceName=native.get_detail(SOURCE)['name'],
                   sourceDocument=document,sourceStateSha256=digest(source),sourceVersionsSha256=digest(native.versions(SOURCE)),
                   tasksSha256=task_digest(database),configurationHashes={path:file_digest(path) for path in baseline['configurationHashes']},
                   copyName='主流程副本验证-'+uuid.uuid4().hex[:12],operations=[],createdAt=time.time())
        save(state_path,state)
        asyncio.run(execute(state,token))
        report=observe(state,database,native,token)
        state['status']='passed_actual_copy_save_reopen'
        save(state_path,state)
        save(OUT/'report.json',dict(report,browserReopened=True,apiPosts=2,publishCalls=0,runCalls=0,
                                   pageMocks=0,sameHostLanOnly=True,checkedAt=time.time()))
        print(json.dumps(dict(status=state['status'],**report)))
    finally:
        client._client.close()


if __name__=='__main__':
    try:
        main()
    except Exception as error:
        if OUT.is_dir():
            save(OUT/'incomplete.json',dict(errorType=type(error).__name__,replayPolicy='observe_original_only'))
        print(json.dumps(dict(status='incomplete_observe_original',errorType=type(error).__name__)))
        raise SystemExit(1)
