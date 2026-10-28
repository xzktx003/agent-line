"""One explicit owned main draft: real UI create/save/archive/read-only audit.

Every POST intent is persisted before forwarding. Reopening never replays a
browser mutation; original operations can be read from the main journal.
"""
import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit
import httpx
from playwright.async_api import async_playwright, expect
from probes.sim.publication_capability_browser import CHROME
from probes.qwenpaw_memory.native_roundtrip import save

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'var/backend-rollout-20261022/main-archive'
ORIGIN='http://10.30.0.28:3193'
NAME='归档界面真实验收 20261022'


def digest(database):
    with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True) as db:
        values={t:db.execute('SELECT * FROM '+t+' ORDER BY rowid').fetchall() for t in ('tasks','events','workspace_leases')}
    return hashlib.sha256(json.dumps(values,sort_keys=True).encode()).hexdigest()


async def main(resume_empty_preflight=False):
    config_path=ROOT/'var/adapter/service.json'; cfg=json.loads(config_path.read_text())
    credential=json.loads((config_path.parent/cfg['authoringFile']).read_text())['credentials'][0]
    project=credential['projectScopes'][0]
    state_path=OUT/'state.json'
    if state_path.exists() and not (resume_empty_preflight and json.loads(state_path.read_text())=={'phase':'started','operations':[]}):
        old=json.loads(state_path.read_text()); statuses=[]
        with httpx.Client(timeout=20) as client:
            for operation in old['operations']:
                response=client.get(ORIGIN+'/extension/v1/authoring/projects/'+project+'/operations/'+operation['body']['operationId'],
                    params={'kind':operation['kind']},headers={'Authorization':'Bearer '+credential['token']})
                statuses.append({'kind':operation['kind'],'http':response.status_code,
                                 'status':response.json().get('status') if response.status_code==200 else 'unknown'})
        print(json.dumps({'mode':'original_operations_observation_only','phase':old['phase'],'operations':statuses,'mutations':0}))
        return
    OUT.mkdir(mode=0o700,exist_ok=True)
    database=Path(cfg['database']); database=database if database.is_absolute() else config_path.parent/database
    before=digest(database)
    state={'phase':'started','operations':[]}; save(state_path,state)
    errors,reads=[],[]
    prefix='/extension/v1/authoring/projects/'+project+'/workflows'
    async with async_playwright() as playwright:
        browser=await playwright.chromium.launch(executable_path=CHROME,headless=True,args=['--no-sandbox'])
        try:
            context=await browser.new_context(viewport={'width':1440,'height':1000})
            async def route(r):
                req=r.request; path=urlsplit(req.url).path
                assert req.url.startswith(ORIGIN+'/')
                if req.method=='POST':
                    assert path==prefix or path.startswith(prefix+'/') and path.endswith(('/save','/archive'))
                    kind='create' if path==prefix else path.rsplit('/',1)[-1]
                    assert kind not in [o['kind'] for o in state['operations']], 'Never send a second mutation of the same stage'
                    body=req.post_data_json
                    assert body.get('operationId')
                    state['operations'].append({'kind':kind,'body':body})
                    state['phase']=kind+'_intent'; save(state_path,state)
                else:
                    assert req.method=='GET'; reads.append(path)
                await r.continue_()
            await context.route('**/*',route)
            page=await context.new_page(); page.on('pageerror',lambda e:errors.append(type(e).__name__))
            await page.goto(ORIGIN+'/workbench/editor.html')
            await page.locator('#token').fill(credential['token']); await page.locator('#login').click()
            await page.locator('#project').select_option(project)
            await page.locator('#create-form').locator('..').evaluate('e=>{if(e.tagName==="DETAILS") e.open=true}')
            await page.locator('#workflow-name').fill(NAME)
            await page.locator('#workflow-description').fill('本仓库功能验收专用草稿；仅保存与归档，不发布、不运行、不调用模型')
            await page.locator('#create').click()
            await expect(page.locator('#flow-heading')).to_have_text(NAME)
            await expect(page.locator('#save')).to_be_enabled()
            await page.locator('#save').click()
            await expect(page.locator('#save')).to_be_disabled()
            await expect(page.locator('#archive-workflow')).to_be_enabled()
            page.once('dialog',lambda dialog:dialog.accept())
            await page.locator('#archive-workflow').click()
            choice=page.locator('.archive-choice').filter(has_text=NAME)
            await expect(choice).to_have_count(1)
            await choice.click()
            await expect(page.locator('#archive-audit-summary')).to_contain_text('归档已确认')
            await expect(page.locator('#archive-audit-heading')).to_have_text('归档前审计 · '+NAME)
            assert not await page.locator('.workflow-choice').filter(has_text=NAME).count()
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            file=OUT/'archive-main.png'; await page.screenshot(path=str(file),full_page=True); file.chmod(0o600)
            await page.locator('#logout').click(); await expect(page.locator('#archive-audit')).to_be_hidden()
            assert not errors, errors
            await context.close()
        finally: await browser.close()
    assert [o['kind'] for o in state['operations']]==['create','save','archive']
    assert before==digest(database), 'Original task/event/lease rows changed'
    state['phase']='complete'; save(state_path,state)
    report={'mode':'real_main_lan_browser_native_archive','phase':'complete','nativeApiPostKinds':['create','save','archive'],
            'postCount':3,'mockResponses':0,'getRequests':len(reads),'archivedAuditRead':True,'removedFromActiveDirectory':True,
            'originalTaskEventLeaseDigestUnchanged':True,'modelCalls':0,'workflowExecutions':0,'publications':0,
            'logoutCleared':True,'otherLanDeviceVerified':False}
    save(OUT/'report.json',report); print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(); parser.add_argument('--resume-empty-preflight',action='store_true')
    args=parser.parse_args(); asyncio.run(main(args.resume_empty_preflight))
