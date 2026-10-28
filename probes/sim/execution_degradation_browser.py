"""Offline compatibility warning against real static UI; no business writes."""
import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / '.omx/state/execution-degradation-ui'
CHROME = '/data01/home/zukang.xu/.cache/ms-playwright/chromium_headless_shell-1223/chrome-headless-shell-linux64/chrome-headless-shell'
REASON = '执行接口与当前插件不兼容，已停止新执行。历史记录仍可查看，请核对扩展与 QwenPaw 版本。'


async def main(*, capture_reference=False):
    OUT.mkdir(parents=True, exist_ok=True)
    writes, errors = [], []
    mode = 'blocked'
    task = dict(taskId='mock-history-compatibility', projectId='模拟项目', roleBindingId='模拟角色',
                executionStatus='succeeded', verificationStatus='passed', memoryStatus='not_requested',
                resourcesReleased=True, output={'summary': '模拟历史记录，未调用模型'})
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=CHROME, args=['--no-sandbox'])
        try:
            context = await browser.new_context(viewport={'width': 1440, 'height': 1000})

            async def route(request):
                req = request.request
                path = urlsplit(req.url).path
                if req.method not in ('GET', 'HEAD'):
                    writes.append({'method': req.method, 'path': path})
                    await request.abort()
                    return
                if path.startswith('/extension/v1/'):
                    if path.endswith('/health'):
                        data = dict(storage='available', controlledExecution='unsupported' if mode == 'blocked' else 'unknown',
                                    readyForExecution=False, reconciliationRequired=False)
                        if mode == 'blocked':
                            data['executionBlockReason'] = REASON
                        if mode == 'invalid':
                            data['executionBlockReason'] = {'invalid': True}
                    elif path.endswith('/workflows'):
                        data = {'workflows': [{'bindingId': 'mock-binding', 'projectId': '模拟项目'}]}
                    elif path.endswith('/node-tasks'):
                        data = {'tasks': [task], 'nextCursor': None}
                    elif path.endswith('/node-tasks/' + task['taskId']):
                        data = task
                    else:
                        await request.abort()
                        return
                    await request.fulfill(status=200, content_type='application/json', body=json.dumps(data))
                    return
                if path.startswith('/workbench/'):
                    await request.continue_()
                    return
                await request.abort()

            await context.route('**/*', route)
            page = await context.new_page()
            page.on('pageerror', lambda err: errors.append(str(err)))
            await page.goto('http://127.0.0.1:3193/workbench/')
            await page.locator('#token').fill('mock-ui-credential-not-a-real-secret')
            await page.locator('#login-form button').click()
            await expect(page.locator('#task-list li')).to_have_count(1)
            if capture_reference:
                await page.screenshot(path=str(OUT / 'reference-desktop.png'), full_page=True)
                await page.set_viewport_size({'width': 390, 'height': 844})
                await page.screenshot(path=str(OUT / 'reference-mobile.png'), full_page=True)
                print('Reference captured; compatibility response is a fixture, no business writes.')
                return

            start = page.locator('#start-form button[type=submit]')
            await expect(start).to_be_disabled()
            await expect(page.locator('#notice')).to_contain_text(REASON)
            await expect(page.locator('#admission-state')).to_have_text('已停止新执行')
            await expect(page.locator('#start-feedback')).to_contain_text(REASON)
            assert '可独立查询与启动' not in await page.locator('#notice').inner_text()
            await page.locator('#task-list button').click()
            await expect(page.locator('#task-result')).to_contain_text(task['taskId'])
            await expect(page.locator('#task-form button')).to_be_enabled()
            await page.locator('#new-key').click()
            await expect(page.locator('#start-feedback')).to_contain_text(REASON)
            # Even a scripted form submission must stop before HTTP mutation.
            await page.locator('#start-form').evaluate("f => f.dispatchEvent(new Event('submit', {bubbles:true, cancelable:true}))")
            await expect(start).to_be_disabled()
            await page.locator('#refresh-health').click()
            await expect(page.locator('#notice')).to_contain_text(REASON)
            await expect(start).to_be_disabled()
            await page.locator('#connection').scroll_into_view_if_needed()
            await page.screenshot(path=str(OUT / 'desktop.png'), full_page=True)
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            await page.set_viewport_size({'width': 390, 'height': 844})
            await page.screenshot(path=str(OUT / 'mobile.png'), full_page=True)
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')

            mode = 'invalid'
            await page.locator('#refresh-health').click()
            await expect(page.locator('#notice')).to_contain_text('执行能力信息异常')
            await expect(start).to_be_disabled()
            await page.locator('#logout').click()
            await expect(page.locator('#admission-state')).to_have_text('尚未核验')
            assert REASON not in await page.locator('#start-feedback').inner_text()
            mode = 'legacy'
            await page.locator('#token').fill('mock-second-credential')
            await page.locator('#login-form button').click()
            await expect(start).to_be_enabled()
            await expect(page.locator('#notice')).to_contain_text('受控节点执行尚未开放')
            assert REASON not in await page.locator('#start-feedback').inner_text()
            await page.locator('#logout').click()
            assert await page.locator('#token').input_value() == ''
            assert await page.evaluate('localStorage.length + sessionStorage.length') == 0
            assert not writes and not errors
            report = dict(mode='mock_health_and_history_real_static_ui', passed=True, businessWrites=writes,
                          pageErrors=errors, checks=[
                              'reason_visible', 'start_disabled', 'history_readable', 'refresh_preserves_warning',
                              'scripted_submit_never_posts', 'new_key_does_not_hide_block',
                              'invalid_reason_blocks', 'logout_clears', 'legacy_health_still_works',
                              'desktop_mobile_no_overflow', 'no_persistent_credentials'])
            (OUT / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print('Compatibility UI fixture checks passed; no business writes.')
        finally:
            await browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture-reference', action='store_true')
    args = parser.parse_args()
    asyncio.run(main(capture_reference=args.capture_reference))
