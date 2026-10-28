"""Mock-only role revision UI checks against real local static assets; no writes."""
import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT/'.omx/state/role-revision-ui'
CHROME = '/data01/home/zukang.xu/.cache/ms-playwright/chromium_headless_shell-1223/chrome-headless-shell-linux64/chrome-headless-shell'
MESSAGES = {
    'role_revision_required': '角色版本尚未固定，任务尚未启动。请核对服务端角色绑定。',
    'role_revision_mismatch': '角色配置已变化，任务尚未启动。请核对原发布版本与角色配置。',
    'role_revision_unavailable': '暂时无法核对角色配置，任务尚未启动。请检查 QwenPaw 连接后查询原任务。',
}


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    tasks = [dict(taskId='mock-'+code, projectId='模拟项目', roleBindingId='模拟角色',
                  executionStatus='registered', verificationStatus='pending', memoryStatus='pending',
                  resourcesReleased=False, submissionBlock=code) for code in MESSAGES]
    writes, errors, requests = [], [], []
    delay = asyncio.Event(); arrived = asyncio.Event(); slow = False
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=CHROME, args=['--no-sandbox'])
        try:
            context = await browser.new_context(viewport={'width':1440,'height':1000})
            async def route(request):
                nonlocal slow
                req = request.request; path = urlsplit(req.url).path
                if req.method not in ('GET', 'HEAD'):
                    writes.append({'method':req.method,'path':path}); await request.abort(); return
                if path.startswith('/extension/v1/'):
                    requests.append(path)
                    if path.endswith('/health'): data = dict(storage='available', controlledExecution='unknown', readyForExecution=False, reconciliationRequired=False)
                    elif path.endswith('/workflows'): data = {'workflows':[]}
                    elif path.endswith('/node-tasks'): data = {'tasks':tasks,'nextCursor':None}
                    elif '/node-tasks/' in path:
                        data = next(t for t in tasks if path.endswith('/'+t['taskId']))
                        if slow:
                            arrived.set(); await delay.wait()
                    else:
                        await request.abort(); return
                    await request.fulfill(status=200, content_type='application/json', body=json.dumps(data)); return
                if path.startswith('/workbench/'):
                    await request.continue_(); return
                await request.abort()
            await context.route('**/*', route)
            page = await context.new_page(); page.on('pageerror', lambda err: errors.append(str(err)))
            await page.goto('http://127.0.0.1:3193/workbench/')
            await page.locator('#token').fill('mock-browser-token-not-a-real-credential')
            await page.locator('#login-form button').click()
            await expect(page.locator('#task-list li')).to_have_count(3)
            for task in tasks:
                await expect(page.locator('#task-list')).to_contain_text(MESSAGES[task['submissionBlock']])
                await page.locator('#task-list button').filter(has_text=task['taskId']).click()
                await expect(page.locator('#task-result')).to_contain_text(MESSAGES[task['submissionBlock']])
                await expect(page.locator('#task-result')).to_contain_text('尚未启动，无执行资源')
                assert '执行资源已退出' not in await page.locator('#task-result').inner_text()
            await page.locator('#tasks').scroll_into_view_if_needed()
            await page.screenshot(path=str(OUT/'desktop.png'), full_page=True)
            await page.locator('#tasks').screenshot(path=str(OUT/'desktop-tasks.png'))
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            await page.set_viewport_size({'width':390,'height':844})
            await page.screenshot(path=str(OUT/'mobile.png'), full_page=True)
            await page.locator('#tasks').screenshot(path=str(OUT/'mobile-tasks.png'))
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            slow = True
            await page.locator('#task-form button').click()
            await asyncio.wait_for(arrived.wait(), 5)
            await page.locator('#logout').click()
            delay.set()
            await expect(page.locator('#task-result')).to_contain_text('尚未选择节点任务')
            await page.wait_for_timeout(300)
            await expect(page.locator('#task-list li')).to_have_count(0)
            assert 'mock-role_' not in await page.locator('#task-result').inner_text()
            assert await page.locator('#token').input_value() == ''
            assert await page.evaluate('localStorage.length + sessionStorage.length') == 0
            assert not writes and not errors
            report = dict(mode='mock_responses_real_static_ui', passed=True, businessWrites=writes,
                          pageErrors=errors, reads=requests, desktopWidth=1440, mobileWidth=390,
                          checks=['three_list_messages','three_detail_messages','never_claims_resource_exit',
                                  'no_horizontal_overflow','logout_clears','late_response_isolated','no_automatic_mutation'])
            (OUT/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print('Mock role revision browser checks passed; no business writes.')
        finally:
            delay.set(); await browser.close()


if __name__ == '__main__': asyncio.run(main())
