"""Fixture-only browser probe for registered-task execution queue explanations."""
import asyncio
import json
import mimetypes
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, expect
from probes.sim.publication_capability_browser import CHROME as DEFAULT_CHROME

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'var/execution-queue-20261022/ui'
ORIGIN = 'https://execution-queue-fixture.test'
CHROME = os.environ.get('CHROME', DEFAULT_CHROME)
BASELINE = ROOT / 'var/execution-queue-20261022/baseline-assets'

REASONS = ('awaiting_checks', 'admission_closed', 'workspace_in_use',
           'role_in_use', 'earlier_registered_task', 'state_unknown')
EXPECTED = {
    'awaiting_checks': '正在等待执行准入检查',
    'admission_closed': '执行准入当前关闭',
    'workspace_in_use': '工作区正由其他任务使用',
    'role_in_use': '角色正由其他任务使用',
    'earlier_registered_task': '前序登记任务尚未处理',
    'state_unknown': '等待原因暂时无法确认',
}


def task(task_id, queue_marker='valid', status='registered'):
    value = {'taskId': task_id, 'projectId': 'fixture-project', 'roleBindingId': 'fixture-role',
             'executionStatus': status, 'verificationStatus': 'pending', 'memoryStatus': 'not_requested'}
    if queue_marker == 'valid':
        value['executionQueue'] = {'policy': 'serial_fifo', 'state': 'waiting',
                                   'reason': task_id.removeprefix('reason-')}
    elif queue_marker == 'malformed':
        value['executionQueue'] = {'policy': '<img src=x onerror=alert(1)>', 'state': 'started',
                                   'reason': '<script>alert(1)</script>'}
    elif queue_marker == 'active':
        value['executionQueue'] = {'policy': 'serial_fifo', 'state': 'waiting', 'reason': 'role_in_use'}
    return value


FIXTURES = [task('reason-' + reason) for reason in REASONS]
FIXTURES += [task('malformed-queue-field', 'malformed'),
             task('active-queue-is-ignored', 'active', 'running'),
             task('legacy-field-absent', 'absent')]
BY_ID = {item['taskId']: item for item in FIXTURES}


async def capture(browser, baseline, width, screenshot_name):
    context = await browser.new_context(viewport={'width': width, 'height': 1000})
    errors, dialogs, api_calls, unexpected = [], [], [], []

    async def route(r):
        request = r.request
        parsed = urlsplit(request.url)
        assert parsed.netloc == 'execution-queue-fixture.test', f'Unexpected network host: {parsed.netloc}'
        path = parsed.path
        if path.startswith('/extension/v1/'):
            api_calls.append((request.method, path))
            assert request.method == 'GET', f'Unexpected API write: {request.method} {path}'
            if path.endswith('/health'):
                data = {'storage': 'available', 'controlledExecution': 'confirmed',
                        'readyForExecution': False}
            elif path.endswith('/workflows'):
                data = {'workflows': []}
            elif path.endswith('/node-tasks'):
                data = {'tasks': FIXTURES, 'nextCursor': None}
            elif '/node-tasks/' in path:
                task_id = path.rsplit('/', 1)[-1]
                data = BY_ID[task_id]
            else:
                raise AssertionError(f'Unexpected API endpoint: {path}')
            await r.fulfill(json=data)
            return
        if path.startswith('/workbench/'):
            name = path.removeprefix('/workbench/') or 'index.html'
            root = BASELINE if baseline and name == 'app.js' else ROOT / 'frontend'
            file = (root / name).resolve()
            assert file.parent == root and file.is_file(), f'Unexpected asset request: {path}'
            await r.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file)[0] or 'application/octet-stream')
            return
        unexpected.append(request.url)
        raise AssertionError(f'Unexpected request (models/native APIs are forbidden): {request.url}')

    await context.route('**/*', route)
    page = await context.new_page()
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('dialog', lambda dialog: dialogs.append(dialog.message))
    await page.goto(ORIGIN + '/workbench/')
    await page.locator('#token').fill('fixture-only-credential')
    await page.locator('#login-form button').click()
    await expect(page.locator('#connection-badge')).to_have_text('服务已连接')
    await expect(page.locator('#task-list li')).to_have_count(len(FIXTURES))
    if screenshot_name:
        await page.screenshot(path=str(OUT / screenshot_name), full_page=True)
    return context, page, errors, dialogs, api_calls, unexpected


async def main():
    OUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    OUT.chmod(0o700)
    checks = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=CHROME, args=['--no-sandbox'])
        try:
            for width, label in ((1440, 'desktop'), (390, 'mobile')):
                context, page, errors, dialogs, api_calls, unexpected = await capture(
                    browser, False, width, f'{label}-final.png')
                list_text = await page.locator('#task-list').inner_text()
                for reason in REASONS:
                    assert EXPECTED[reason] in list_text, reason
                waiting = await page.locator('#task-list').locator('li').filter(
                    has=page.get_by_text('reason-awaiting_checks', exact=True)).inner_text()
                assert '串行先进先出；当前等待' in waiting
                assert '正在等待执行准入检查' in waiting
                assert '已准备' not in waiting and '已启动' not in waiting and '执行中' not in waiting
                malformed = await page.locator('#task-list').locator('li').filter(
                    has=page.get_by_text('malformed-', exact=False)).inner_text()
                assert '执行队列信息暂时无法核对，请查询原任务。' in malformed
                assert '串行先进先出' not in malformed
                assert '<script>alert(1)</script>' not in malformed
                assert '<img src=x' not in malformed
                active = await page.locator('#task-list').locator('li').filter(
                    has=page.get_by_text('active-queue-is-ignored', exact=True)).inner_text()
                assert '队列策略' not in active
                legacy = await page.locator('#task-list').locator('li').filter(
                    has=page.get_by_text('legacy-field-absent', exact=True)).inner_text()
                assert '队列策略' not in legacy
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert await page.locator('#task-list button').first.evaluate('(e) => e.getBoundingClientRect().height >= 44')
                for reason in REASONS:
                    await page.locator('#task-list button').filter(has_text='reason-' + reason).click()
                    await expect(page.locator('#task-result')).to_contain_text('执行队列')
                    await expect(page.locator('#task-result')).to_contain_text(EXPECTED[reason])
                    await expect(page.locator('#task-result')).to_contain_text('串行先进先出；当前等待')
                await page.locator('#task-list button').filter(has_text='reason-awaiting_checks').click()
                waiting_detail = await page.locator('#task-result > dl').inner_text()
                assert '正在等待执行准入检查' in waiting_detail
                assert '已准备' not in waiting_detail and '已启动' not in waiting_detail
                await page.locator('#task-list button').filter(has_text='active-queue-is-ignored').click()
                await expect(page.locator('#task-result')).not_to_contain_text('执行队列')
                await page.locator('#task-list button').filter(has_text='malformed-').click()
                await expect(page.locator('#task-result')).to_contain_text(
                    '执行队列信息暂时无法核对，请查询原任务。')
                detail_rows = await page.locator('#task-result > dl').inner_text()
                assert '串行先进先出' not in detail_rows
                assert 'executionQueue' not in detail_rows and '<script>alert(1)</script>' not in detail_rows
                assert not dialogs
                assert await page.locator('#cancel-task').is_visible()
                await page.locator('#logout').click()
                await expect(page.locator('#task-list')).to_be_empty()
                await expect(page.locator('#task-result')).to_contain_text('尚未选择节点任务')
                assert all(method == 'GET' for method, _ in api_calls)
                assert not unexpected and not errors and not dialogs
                checks.append({'viewport': width, 'reasons': len(REASONS), 'legacy': True,
                               'malformed': True, 'active_ignored': True, 'logout_clear': True,
                               'no_writes_or_external_calls': True, 'no_overflow': True, 'touch_target_44px': True})
                await context.close()
            # Keep baseline and final captures paired at both requested widths.
            for width, label in ((1440, 'desktop'), (390, 'mobile')):
                context, _, errors, dialogs, api_calls, unexpected = await capture(
                    browser, True, width, f'{label}-baseline.png')
                assert not errors and not dialogs and not unexpected and all(method == 'GET' for method, _ in api_calls)
                await context.close()
        finally:
            await browser.close()
    report = {'checks': checks, 'limitations': ['Fixture responses prove only browser rendering, not server queue correctness.']}
    (OUT / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False))


async def focused():
    OUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    OUT.chmod(0o700)
    checks = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=CHROME, args=['--no-sandbox'])
        try:
            for width, label in ((1440, 'desktop'), (390, 'mobile')):
                context, page, errors, dialogs, api_calls, unexpected = await capture(
                    browser, False, width, None)
                valid = await page.locator('#task-list').locator('li').filter(
                    has=page.get_by_text('reason-awaiting_checks', exact=True)).inner_text()
                malformed = await page.locator('#task-list').locator('li').filter(
                    has=page.get_by_text('malformed-queue-field', exact=True)).inner_text()
                assert '队列策略：串行先进先出；当前等待。原因：正在等待执行准入检查。' in valid
                assert '执行队列信息暂时无法核对，请查询原任务。' in malformed
                assert '串行先进先出' not in malformed
                await page.locator('#task-list button').filter(
                    has_text='reason-awaiting_checks').click()
                normal_detail = page.locator('#task-result > dl')
                await expect(normal_detail).to_contain_text('串行先进先出；当前等待')
                await expect(normal_detail).to_contain_text('正在等待执行准入检查')
                await normal_detail.screenshot(path=str(OUT / f'{label}-normal-detail.png'))
                await page.locator('#task-list button').filter(
                    has_text='malformed-queue-field').click()
                invalid_detail = page.locator('#task-result > dl')
                await expect(invalid_detail).to_contain_text(
                    '执行队列信息暂时无法核对，请查询原任务。')
                assert '串行先进先出' not in await invalid_detail.inner_text()
                assert '<script>alert(1)</script>' not in await invalid_detail.inner_text()
                await invalid_detail.screenshot(path=str(OUT / f'{label}-invalid-detail.png'))
                assert all(method == 'GET' for method, _ in api_calls)
                assert not errors and not dialogs and not unexpected
                checks.append({'viewport': width, 'normal_list_and_detail': True,
                               'invalid_list_and_detail_no_policy_claim': True,
                               'no_writes_or_external_calls': True})
                await context.close()
        finally:
            await browser.close()
    report = {'focused_checks': checks}
    (OUT / 'focused-report.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    asyncio.run(focused() if '--focused' in sys.argv else main())
