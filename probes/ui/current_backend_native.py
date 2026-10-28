"""Real LAN editor against reloaded main; GET-only, no mock API responses."""
import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import httpx
from playwright.async_api import async_playwright, expect
from probes.sim.publication_capability_browser import CHROME
from probes.qwenpaw_memory.native_roundtrip import save

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'var/backend-rollout-20261022/browser'
ORIGIN='http://10.30.0.28:3193'


def digest_tables(database):
    with sqlite3.connect(database.as_uri()+'?mode=ro',uri=True) as db:
        rows={table:db.execute('SELECT * FROM '+table+' ORDER BY rowid').fetchall() for table in ('tasks','events','workspace_leases')}
    return hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest()


async def main():
    config_path=ROOT/'var/adapter/service.json'
    config=json.loads(config_path.read_text())
    credentials=json.loads((config_path.parent/config['authoringFile']).read_text())['credentials']
    credential=credentials[0]; project=credential['projectScopes'][0]
    database=Path(config['database']); database=database if database.is_absolute() else config_path.parent/database
    before=digest_tables(database)
    with httpx.Client(timeout=20) as client:
        base=ORIGIN+'/extension/v1/authoring/projects/'+project
        headers={'Authorization':'Bearer '+credential['token']}
        result=client.get(base+'/workflows',headers=headers,params={'limit':1})
        assert result.status_code==200
        term=result.json()['workflows'][0]['name'] if result.json()['workflows'] else '局域网只读核对'
        result=client.get(base+'/archives',headers=headers)
        assert result.status_code==200
        archives=len(result.json()['archives'])
    OUT.mkdir(mode=0o700,exist_ok=True)
    reports=[]
    async with async_playwright() as playwright:
        browser=await playwright.chromium.launch(executable_path=CHROME,headless=True,args=['--no-sandbox'])
        try:
            for width in (1440,390):
                context=await browser.new_context(viewport={'width':width,'height':1000})
                errors,requests=[],[]
                async def readonly(route):
                    request=route.request
                    assert request.url.startswith(ORIGIN+'/'), 'Unexpected origin'
                    assert request.method=='GET', 'Native proof must not mutate'
                    requests.append(request.method)
                    await route.continue_()
                await context.route('**/*',readonly)
                page=await context.new_page()
                page.on('pageerror',lambda e:errors.append(type(e).__name__))
                await page.goto(ORIGIN+'/workbench/editor.html')
                await page.locator('#token').fill(credential['token'])
                await page.locator('#login').click()
                await page.locator('#project').select_option(project)
                await expect(page.locator('#workflow-search')).to_be_enabled()
                await page.locator('#workflow-search').fill(term)
                async with page.expect_response(lambda r:'/workflows?' in r.url and 'search=' in r.url) as response_info:
                    await page.locator('#workflow-search-submit').click()
                response=await response_info.value
                assert response.status==200
                payload=await response.json()
                assert payload['search']==term
                await expect(page.locator('.workflow-choice')).to_have_count(len(payload['workflows']))
                await page.locator('#refresh-archives').click()
                await expect(page.locator('#refresh-archives')).to_be_enabled()
                await expect(page.locator('.archive-choice')).to_have_count(archives)
                assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                file=OUT/f'editor-{width}.png'; await page.screenshot(path=str(file),full_page=True); file.chmod(0o600)
                await page.locator('#logout').click()
                await expect(page.locator('#token')).to_have_value('')
                await expect(page.locator('.workflow-choice')).to_have_count(0)
                assert not errors,errors
                reports.append({'width':width,'realApiSearch':True,'matchingRows':len(payload['workflows']),
                                'realArchiveDirectory':True,'archiveRows':archives,'getRequests':len(requests),
                                'mutations':0,'mockResponses':0,'logoutCleared':True,'bodyOverflow':False})
                await context.close()
        finally: await browser.close()
    report={'mode':'real_lan_browser_current_main','cases':reports,'originalTaskEventLeaseDigestUnchanged':before==digest_tables(database),
            'modelCalls':0,'otherLanDeviceVerified':False}
    assert report['originalTaskEventLeaseDigestUnchanged']
    save(OUT/'report.json',report); print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__': asyncio.run(main())
