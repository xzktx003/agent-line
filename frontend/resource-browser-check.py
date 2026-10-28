"""Real browser, mocked API fixtures: verifies presentation, not executor shutdown."""
import asyncio
import json
from pathlib import Path
from playwright.async_api import async_playwright
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'var/gateway'
CHROME='/data01/home/zukang.xu/.cache/ms-playwright/chromium_headless_shell-1223/chrome-headless-shell-linux64/chrome-headless-shell'

async def main():
    cases=[]
    fixture={'taskId':'fixture-task','projectId':'presentation-test','roleBindingId':'fixture-role','executionStatus':'canceled','verificationStatus':'pending','memoryStatus':'not_requested'}
    async with async_playwright() as p:
        browser=await p.chromium.launch(executable_path=CHROME,args=['--no-sandbox'])
        page=await browser.new_page(viewport={'width':1440,'height':1100})
        async def respond(route):
            path=route.request.url.split('/extension/v1')[-1]
            data=fixture if path.startswith('/node-tasks/fixture-task') else {'tasks':[],'workflows':[],'readyForExecution':False,'status':'ok'}
            await route.fulfill(status=200,json=data)
        await page.route('**/extension/v1/**',respond)
        await page.goto('http://127.0.0.1:3193/workbench/')
        await page.locator('#token').fill('mock-presentation-token')
        await page.locator('#login-form button[type=submit]').click()
        await page.wait_for_function("()=>document.querySelector('#connection-badge').textContent==='服务已连接'")
        async def query(expected):
            await page.locator('#task-id').fill('fixture-task')
            await page.locator('#task-form button[type=submit]').click()
            await page.wait_for_function("()=>!document.querySelector('#task-form button').hasAttribute('aria-busy')")
            text=await page.locator('#task-result').inner_text()
            assert expected in text
            assert '取消请求不等于执行资源已退出' in text
            values=await page.locator('#task-result dd').all_text_contents()
            return values
        for value,expected in [(False,'执行资源退出待确认'),(True,'执行资源已退出'),('true','执行资源退出未确认')]:
            fixture['resourcesReleased']=value
            values=await query(expected)
            assert '执行已结束' in values
            assert ('执行资源已退出' in values)==(value is True)
            cases.append({'resourcesReleased':value,'label':expected,'passed':True})
        del fixture['resourcesReleased']
        values=await query('执行资源退出未确认')
        assert '执行资源已退出' not in values
        cases.append({'resourcesReleased':'missing','passed':True})
        fixture['projectId']='<img src=x onerror="window.__xss=true">'
        fixture['executionStatus']='<svg onload="window.__xss=true">'
        await query('执行资源退出未确认')
        assert await page.locator('#task-result img,#task-result svg').count()==0
        assert await page.evaluate('window.__xss===undefined')
        assert '<img' in await page.locator('#task-result').inner_text()
        cases.append({'name':'XSS rendered as text','passed':True})
        fixture.update(projectId='presentation-test',executionStatus='running',resourcesReleased=False)
        await query('执行资源退出待确认')
        await page.locator('#cancel-task').click()
        fixture['executionStatus']='cancel_requested'
        await page.locator('#confirm-cancel').click()
        await page.wait_for_function("()=>document.querySelector('#notice').textContent.includes('已提交取消请求')")
        assert '资源退出尚未确认' in await page.locator('#notice').inner_text()
        cases.append({'name':'cancel receipt does not imply resource exit','passed':True})
        fixture['executionStatus']='canceled'
        await query('执行资源退出待确认')
        await page.locator('#task-result').scroll_into_view_if_needed()
        await page.screenshot(path=str(OUT/'resources-pending.png'),full_page=True)
        fixture['resourcesReleased']=True
        await query('执行资源已退出')
        await page.screenshot(path=str(OUT/'resources-exited.png'),full_page=True)
        await browser.close()
    report={'evidenceType':'real browser with mocked API, not real resource release','checks':cases}
    (OUT/'resources-browser-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False))

asyncio.run(main())
