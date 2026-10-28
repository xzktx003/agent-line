"""Fixture-only browser probe for execution limits and business-attempt explanations."""
import asyncio
import json
import mimetypes
import os
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'var/execution-limits-20261022/ui'
ORIGIN = 'https://execution-limits-fixture.test'
CHROME = os.environ.get('CHROME', '/data01/home/zukang.xu/.cache/ms-playwright/chromium_headless_shell-1223/chrome-headless-shell')


def projection(task_id, *, attempts=2, maximum=3, allowed=True, deadline=1792684800,
              seconds=30, usage=None):
    return {'taskId': task_id,
            'scope': {'projectId': 'fixture-project', 'workflowId': 'fixture-workflow',
                      'workflowRevision': 7, 'runId': 'fixture-run', 'nodeId': 'fixture-node'},
            'limitSeconds': seconds, 'deadlineAt': deadline,
            'attemptsUsed': attempts, 'maxBusinessAttempts': maximum,
            'admissionAllowed': allowed,
            'usage': usage if usage is not None else {'tokens': None, 'modelCalls': None, 'cost': None}}


def task(task_id, limits_marker='normal', status='registered'):
    value = {'taskId': task_id, 'projectId': 'fixture-project', 'roleBindingId': 'fixture-role',
             'executionStatus': status, 'verificationStatus': 'pending', 'memoryStatus': 'not_requested'}
    valid = projection(task_id)
    if limits_marker == 'normal':
        value['executionLimits'] = valid
    elif limits_marker == 'exhausted':
        value['executionLimits'] = projection(task_id, attempts=3, allowed=False)
        value['executionQueue'] = {'policy': 'serial_fifo', 'state': 'waiting',
                                   'reason': 'business_attempt_limit'}
    elif limits_marker == 'unknown':
        value['executionLimits'] = {'state': 'unknown'}
    elif limits_marker == 'infinity':
        # JSON.parse accepts an overflowing JSON number (1e999) as Infinity;
        # keep the API fixture valid JSON while avoiding Python's nonstandard Infinity literal.
        valid['limitSeconds'] = 'INFINITY_SENTINEL'
        value['executionLimits'] = valid
    elif limits_marker == 'string':
        valid['attemptsUsed'] = '2'
        value['executionLimits'] = valid
    elif limits_marker == 'nan':
        valid['deadlineAt'] = 'NaN'
        value['executionLimits'] = valid
    elif limits_marker == 'null-usage':
        valid['usage'] = None
        value['executionLimits'] = valid
    elif limits_marker == 'missing-usage':
        del valid['usage']
        value['executionLimits'] = valid
    elif limits_marker == 'malicious':
        value['executionLimits'] = {'state': '<img src=x onerror=alert(1)>'}
    return value


FIXTURES = [task('normal-2-of-3'), task('exhausted-business-attempts', 'exhausted'),
            task('unknown-limits', 'unknown'), task('invalid-infinity', 'infinity'),
            task('invalid-string', 'string'), task('invalid-nan', 'nan'),
            task('invalid-null-usage', 'null-usage'), task('invalid-missing-usage', 'missing-usage'),
            task('malicious-limits', 'malicious'), task('legacy-no-limits', 'absent')]
BY_ID = {item['taskId']: item for item in FIXTURES}
EXPECTED_VALID = '本节点在此运行中已提交 2/3 次业务尝试；本任务时限 30 秒，从提交开始计算。token、模型调用次数和费用未知。超时将请求停止，实际退出须等待回执。'
EXPECTED_INVALID = '执行限制与用量暂时无法核对，不能视为剩余额度充足。'


async def run():
    OUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    OUT.chmod(0o700)
    checks = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=CHROME, args=['--no-sandbox'])
        try:
            for width, label in ((1440, 'desktop'), (390, 'mobile')):
                context = await browser.new_context(viewport={'width': width, 'height': 1000})
                errors, dialogs, api_calls, unexpected = [], [], [], []

                async def route(r):
                    request = r.request
                    parsed = urlsplit(request.url)
                    assert parsed.netloc == 'execution-limits-fixture.test', f'Unexpected host: {request.url}'
                    path = parsed.path
                    if path.startswith('/extension/v1/'):
                        api_calls.append((request.method, path))
                        assert request.method == 'GET', f'Unexpected API write: {request.method} {path}'
                        if path.endswith('/health'):
                            data = {'storage': 'available', 'controlledExecution': 'unknown',
                                    'readyForExecution': False}
                        elif path.endswith('/workflows'):
                            data = {'workflows': []}
                        elif path.endswith('/node-tasks'):
                            data = {'tasks': FIXTURES, 'nextCursor': None}
                        elif '/node-tasks/' in path:
                            data = BY_ID[path.rsplit('/', 1)[-1]]
                        else:
                            raise AssertionError(f'Unexpected API endpoint: {path}')
                        body = json.dumps(data, ensure_ascii=False, allow_nan=False)
                        body = body.replace('"limitSeconds": "INFINITY_SENTINEL"', '"limitSeconds": 1e999')
                        await r.fulfill(body=body, content_type='application/json')
                        return
                    if path.startswith('/workbench/'):
                        name = path.removeprefix('/workbench/') or 'index.html'
                        file = (ROOT / 'frontend' / name).resolve()
                        assert file.parent == ROOT / 'frontend' and file.is_file(), f'Unexpected asset: {path}'
                        await r.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file)[0] or 'application/octet-stream')
                        return
                    unexpected.append(request.url)
                    raise AssertionError(f'Unexpected request (external services/models forbidden): {request.url}')

                await context.route('**/*', route)
                page = await context.new_page()
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('dialog', lambda dialog: dialogs.append(dialog.message))
                await page.goto(ORIGIN + '/workbench/')
                await page.locator('#token').fill('fixture-only-credential')
                await page.locator('#login-form button').click()
                await expect(page.locator('#connection-badge')).to_have_text('服务已连接')
                await expect(page.locator('#task-list li')).to_have_count(len(FIXTURES))
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                exhausted_list = await page.locator('#task-list').inner_text()
                assert '本节点在此运行中的业务尝试额度已用尽' in exhausted_list

                async def detail(task_id):
                    await page.locator('#task-id').fill(task_id)
                    await page.locator('#task-form button[type=submit]').click()
                    await expect(page.locator('#task-result > dl')).to_contain_text(task_id)
                    return page.locator('#task-result > dl')

                normal = await detail('normal-2-of-3')
                await expect(normal).to_contain_text(EXPECTED_VALID)
                normal_text = await normal.inner_text()
                assert '费用 0' not in normal_text and '已停止' not in normal_text
                await normal.screenshot(path=str(OUT / f'{label}-normal.png'))

                exhausted = await detail('exhausted-business-attempts')
                await expect(exhausted).to_contain_text('3/3 次业务尝试')
                exhausted_text = await exhausted.inner_text()
                assert '费用未知' in exhausted_text and '已停止' not in exhausted_text
                assert '本节点在此运行中的业务尝试额度已用尽' in exhausted_text
                await exhausted.screenshot(path=str(OUT / f'{label}-exhausted.png'))

                unknown = await detail('unknown-limits')
                await expect(unknown).to_contain_text(EXPECTED_INVALID)
                await unknown.screenshot(path=str(OUT / f'{label}-unknown.png'))
                malformed_ids = ['invalid-infinity', 'invalid-string', 'invalid-nan',
                                 'invalid-null-usage', 'invalid-missing-usage', 'malicious-limits']
                for task_id in malformed_ids:
                    rendered = await detail(task_id)
                    await expect(rendered).to_contain_text(EXPECTED_INVALID)
                    assert '<img' not in await rendered.inner_text()
                await detail('legacy-no-limits')
                legacy_text = await page.locator('#task-result > dl').inner_text()
                assert '执行限制' not in legacy_text and '业务尝试' not in legacy_text
                await page.locator('#logout').click()
                await expect(page.locator('#task-list')).to_be_empty()
                await expect(page.locator('#task-result')).to_contain_text('尚未选择节点任务')
                assert await page.locator('#token').input_value() == ''
                assert all(method == 'GET' for method, _ in api_calls)
                assert not errors and not dialogs and not unexpected
                checks.append({'viewport': width, 'normal_2_of_3': True, 'business_attempts_exhausted': True,
                               'unknown_and_malformed_rejected': len(malformed_ids) + 1,
                               'legacy_field_omitted': True, 'logout_clears_page': True,
                               'no_overflow': True, 'no_api_writes_external_calls_or_page_errors': True})
                await context.close()
        finally:
            await browser.close()
    report = {'checks': checks,
              'limitations': ['Fixture responses verify browser rendering and validation only; they do not prove server-side limits or actual task termination.']}
    report_path = OUT / 'report.json'
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    report_path.chmod(0o600)
    for output in OUT.iterdir():
        if output.is_file():
            output.chmod(0o600)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    asyncio.run(run())
