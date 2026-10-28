"""Real browser evidence; credentials stay in memory and are never printed."""
import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'var/gateway'
BASE='http://10.30.0.28:3194'
CHROME='/data01/home/zukang.xu/.cache/ms-playwright/chromium_headless_shell-1223/chrome-headless-shell-linux64/chrome-headless-shell'

async def main():
    OUT.mkdir(exist_ok=True)
    report={}
    async with async_playwright() as p:
        browser=await p.chromium.launch(executable_path=CHROME, args=['--no-sandbox'])
        context=await browser.new_context(viewport={'width':1440,'height':1000})
        qwen=json.loads((ROOT/'var/qwenpaw-secrets/local-login.json').read_text())
        login=await context.request.post(BASE+'/qwenpaw/api/auth/login',data=qwen)
        report['qwenLoginStatus']=login.status
        auth=await login.json()
        token=auth.get('access_token') or auth.get('token')
        if not token:
            raise RuntimeError('Qwen login did not return token')
        page=await context.new_page()
        async def accept_discard(dialog):
            await dialog.accept()
        page.on('dialog', accept_discard)
        await page.goto(BASE+'/qwenpaw/')
        await page.evaluate('(token)=>localStorage.setItem("qwenpaw_auth_token",token)', token)
        await page.reload()
        await page.wait_for_timeout(4000)
        report['qwenURL']=page.url
        report['qwenTitle']=await page.title()
        await page.screenshot(path=str(OUT/'qwen-prefixed.png'),full_page=True)
        await page.get_by_text('Console account', exact=True).click()
        await page.wait_for_timeout(1000)
        await page.get_by_text('Settings', exact=True).click()
        await page.wait_for_timeout(1500)
        await page.get_by_text('工作流工作台', exact=True).first.click()
        await page.wait_for_timeout(1000)
        await page.get_by_role('button',name='打开原版画布').click()
        await page.wait_for_timeout(2500)
        frame=page.frame_locator('iframe[title="Sim 原版工作流编辑器"]')
        report['pluginMenuURL']=page.url
        report['iframeLoginVisible']=await frame.locator('input[name="email"]').is_visible()
        sim=json.loads((ROOT/'var/sim/probe-account.json').read_text())
        await frame.locator('input[name="email"]').fill(sim['email'])
        await frame.locator('input[name="password"]').fill(sim['password'])
        await frame.get_by_role('button',name='Sign in',exact=True).click()
        await frame.get_by_text('New workflow',exact=True).wait_for(timeout=20000)
        await page.wait_for_timeout(2500)
        report['iframeEditorVisible']=await frame.get_by_text('New workflow',exact=True).is_visible()
        report['iframeSameOrigin']=all(f.url.startswith(BASE) for f in page.frames if f!=page.main_frame)
        await page.screenshot(path=str(OUT/'plugin-sim-editor.png'),full_page=True)
        # Select an existing isolated graph without editing or submitting any execution.
        await frame.get_by_text('Agent Line G0 condition',exact=True).click()
        await page.wait_for_timeout(1000)
        report['conditionCanvasVisible']=await frame.locator('.react-flow').count()>0
        await page.get_by_role('button',name='重新加载画布',exact=True).click()
        await frame.get_by_text('New workflow',exact=True).wait_for(timeout=20000)
        report['reloadKeepsSimSession']=True
        await page.get_by_role('button',name='关闭画布',exact=True).click()
        report['closeRemovesIframe']=await page.locator('iframe').count()==0
        await page.get_by_role('button',name='打开原版画布',exact=True).click()
        await frame.get_by_text('New workflow',exact=True).wait_for(timeout=20000)
        # Official Sim logout endpoint, no fabricated cookie manipulation.
        logout=await context.request.post(BASE+'/api/auth/sign-out',data={},headers={'Origin':BASE})
        report['simLogoutStatus']=logout.status
        await page.get_by_role('button',name='重新加载画布',exact=True).click()
        await frame.locator('input[name="email"]').wait_for(timeout=20000)
        report['simLogoutShowsLogin']=True
        report['qwenStillLoggedIn']=await page.get_by_role('button',name='关闭画布',exact=True).is_visible()
        await page.screenshot(path=str(OUT/'plugin-sim-logged-out.png'),full_page=True)
        # Login again then use Qwen's real menu logout; Sim remains a separate session.
        await frame.locator('input[name="email"]').fill(sim['email'])
        await frame.locator('input[name="password"]').fill(sim['password'])
        await frame.get_by_role('button',name='Sign in',exact=True).click()
        await frame.get_by_text('New workflow',exact=True).wait_for(timeout=20000)
        await page.get_by_text('Console account',exact=True).click()
        await page.get_by_text('Logout',exact=True).click()
        await page.wait_for_timeout(1000)
        report['qwenLogoutURL']=page.url
        report['qwenLogoutRemovesIframe']=await page.locator('iframe').count()==0
        simpage=await context.new_page()
        await simpage.goto(BASE+'/workspace')
        await simpage.get_by_text('New workflow',exact=True).wait_for(timeout=20000)
        report['qwenLogoutDoesNotLogoutSim']=True
        await context.request.post(BASE+'/api/auth/sign-out',data={},headers={'Origin':BASE})
        await browser.close()
    (OUT/'plugin-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False))

asyncio.run(main())
