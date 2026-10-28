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
        await page.goto(BASE+'/qwenpaw/')
        await page.evaluate('(token)=>localStorage.setItem("qwenpaw_auth_token",token)', token)
        await page.reload()
        await page.wait_for_timeout(4000)
        report['qwenURL']=page.url
        report['qwenTitle']=await page.title()
        await page.screenshot(path=str(OUT/'qwen-prefixed.png'),full_page=True)
        sim=json.loads((ROOT/'var/sim/probe-account.json').read_text())
        login=await context.request.post(BASE+'/api/auth/sign-in/email',data={k:sim[k] for k in ['email','password']},headers={'Origin':BASE})
        report['simLoginStatus']=login.status
        workspace=json.loads((ROOT/'var/sim/probe-auth.json').read_text())['workspaceId']
        simpage=await context.new_page()
        response=await simpage.goto(BASE+'/workspace/'+workspace)
        report['simFramePolicy']=response.headers.get('x-frame-options')
        await simpage.wait_for_timeout(5000)
        report['simURL']=simpage.url
        report['simBodyExcerpt']=(await simpage.locator('body').inner_text())[:160]
        await simpage.screenshot(path=str(OUT/'sim-root.png'),full_page=True)
        # Transport check only, not evidence of production plugin embedding.
        report['socketHandshake']=await page.evaluate('''()=>new Promise(resolve=>{const s=new WebSocket(location.origin.replace('http','ws')+'/socket.io/?EIO=4&transport=websocket');const t=setTimeout(()=>{s.close();resolve(false)},5000);s.onmessage=e=>{clearTimeout(t);resolve(e.data.startsWith('0{'));s.close()};s.onerror=()=>{clearTimeout(t);resolve(false)}})''')
        await browser.close()
    (OUT/'browser-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False))

asyncio.run(main())
