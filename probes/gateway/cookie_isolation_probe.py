"""Two legitimate independent browser sessions plus concurrent anonymous requests."""
import asyncio,json
from pathlib import Path
from playwright.async_api import async_playwright
ROOT=Path(__file__).resolve().parents[2]
BASE='http://10.30.0.28:3194'
CHROME='/data01/home/zukang.xu/.cache/ms-playwright/chromium_headless_shell-1223/chrome-headless-shell-linux64/chrome-headless-shell'
async def main():
    account=json.loads((ROOT/'var/sim/probe-account.json').read_text())
    async with async_playwright() as p:
        browser=await p.chromium.launch(executable_path=CHROME,args=['--no-sandbox'])
        contexts=[await browser.new_context() for _ in range(3)]
        for context in contexts[:2]:
            r=await context.request.post(BASE+'/api/auth/sign-in/email',data={k:account[k] for k in ['email','password']},headers={'Origin':BASE})
            assert r.status==200
        async def session(context):
            r=await context.request.get(BASE+'/api/auth/get-session')
            assert r.status==200
            return await r.json()
        initial=await asyncio.gather(*(session(c) for c in contexts))
        assert initial[0]['session']['id']!=initial[1]['session']['id']
        assert initial[2] is None
        pages=[await c.new_page() for c in contexts]
        for _ in range(5):
            results=await asyncio.gather(*(session(c) for c in contexts))
            assert results[0]['session']['id']==initial[0]['session']['id']
            assert results[1]['session']['id']==initial[1]['session']['id']
            assert results[2] is None
        await asyncio.gather(*(page.goto(BASE+'/workspace') for page in pages))
        await pages[2].locator('input[name=email]').wait_for(timeout=15000)
        assert '/login' in pages[2].url
        for page in pages[:2]:
            await page.get_by_text('New workflow',exact=True).wait_for(timeout=15000)
        for context in contexts[:2]:
            await context.request.post(BASE+'/api/auth/sign-out',data={},headers={'Origin':BASE})
        await browser.close()
    report={'passed':True,'independentAuthenticatedSessions':2,'anonymousSessionRemainsNull':True,'concurrentRounds':5,'anonymousBrowserShowsLogin':True,'authenticatedBrowsersShowWorkspace':True}
    (ROOT/'var/gateway/cookie-isolation-report.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report))
asyncio.run(main())
