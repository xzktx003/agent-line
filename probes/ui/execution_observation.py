"""Observation-state browser fixtures; all API traffic is intercepted.

Use --baseline before the frontend change. This probe never starts or cancels
real work and never calls a real execution or memory endpoint.
"""
import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import unquote, urlsplit

from playwright.async_api import async_playwright, expect

from probes.ui.task_state_context import CHROME, ORIGIN, fixture


ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'var/inflight-adapter-restart-20261022/browser-fixture'


async def run(baseline=False):
    OUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    OUT.chmod(0o700)
    prefix = 'baseline-' if baseline else ''
    task = fixture('fixture-observation-task', 'pending', 'not_requested', execution='accepted')
    task.update(resourcesReleased=False, executionObservation={'status': 'unknown', 'updatedAt': 1700000000.5})
    checks, failures, requests, mutations, errors, unexpected, screenshots = [], [], [], [], [], [], []
    arrived, release, delivered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    slow = False

    def check(name, condition, actual=None):
        checks.append(name)
        if not condition:
            failures.append({'check': name, **({'actual': actual} if actual is not None else {})})

    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=CHROME, args=['--no-sandbox'])
        try:
            context = await browser.new_context(viewport={'width': 1440, 'height': 1000})

            async def route(r):
                url = urlsplit(r.request.url)
                path = unquote(url.path)
                if path.startswith('/extension/v1/'):
                    requests.append((r.request.method, path))
                    if r.request.method != 'GET':
                        mutations.append(path)
                        await r.fulfill(status=405, json={'detail': 'Fixture prohibits all writes'})
                        return
                    if path == '/extension/v1/health':
                        data = {'storage': 'available', 'controlledExecution': 'confirmed', 'readyForExecution': True}
                    elif path == '/extension/v1/workflows':
                        data = {'workflows': []}
                    elif path == '/extension/v1/node-tasks':
                        data = {'tasks': [task], 'nextCursor': None}
                    elif path == '/extension/v1/node-tasks/' + task['taskId']:
                        if slow:
                            arrived.set()
                            await release.wait()
                        data = task
                    elif path == '/extension/v1/node-tasks/fixture-missing-task':
                        await r.fulfill(status=404, json={'detail': 'Fixture task not found'})
                        return
                    else:
                        unexpected.append(path)
                        await r.fulfill(status=404, json={'detail': 'Unknown fixture request'})
                        return
                    # An oversized JSON number tests JS Infinity without invalid JSON.
                    await r.fulfill(status=200, content_type='application/json', body=json.dumps(data))
                    if slow and path.endswith('/' + task['taskId']):
                        delivered.set()
                    return
                if url.netloc == '127.0.0.1:3193' and path.startswith('/workbench/'):
                    await r.continue_()
                    return
                await r.abort()

            await context.route('**/*', route)
            page = await context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            await page.goto(ORIGIN + '/workbench/')
            await page.locator('#token').fill('fixture-observation-only-not-a-real-credential')
            await page.locator('#login-form button').click()
            await expect(page.locator('#task-list li')).to_have_count(1)

            async def query():
                await page.locator('#task-id').fill(task['taskId'])
                await page.locator('#task-form button').click()
                await expect(page.locator('#task-result > dl dd').first).to_have_text(task['taskId'])
                return await page.locator('#task-result').evaluate(
                    '(e) => Object.fromEntries(Array.from(e.querySelectorAll("dl > dt"), '
                    'dt => [dt.textContent, dt.nextElementSibling.textContent]))')

            async def refresh():
                previous = len(requests)
                await page.locator('#refresh-tasks').click()
                await expect(page.locator('#task-list li')).to_have_count(1)
                check('fixture_refresh_requested_' + str(previous), len(requests) > previous)
                return await page.locator('#task-list li').inner_text()

            rows = await query()
            content = await page.locator('#task-result').inner_text()
            check('accepted_remains_accepted', rows.get('执行状态') == '已接收', rows.get('执行状态'))
            check('detail_unknown_observation', '核对结果未知' in rows.get('最近核对', ''), rows.get('最近核对'))
            check('list_unknown_observation', '最近核对' in await page.locator('#task-list').inner_text()
                  and '核对结果未知' in await page.locator('#task-list').inner_text())
            check('unknown_explains_preserved_state_and_no_resubmit', '已记录' in content
                  and ('不变' in content or '保持' in content) and '原任务' in content
                  and any(word in content for word in ('重投', '重复提交', '重新提交', '重新投递')))
            for width, height, name in ((1440, 1000, 'desktop'), (390, 844, 'mobile')):
                await page.set_viewport_size({'width': width, 'height': height})
                filename = prefix + 'task-result-' + name + '.png'
                await page.locator('#task-result').screenshot(path=str(OUT / filename))
                screenshots.append(filename)
                check(name + '_result_no_overflow', await page.locator('#task-result').evaluate(
                    '(e) => e.scrollWidth <= e.clientWidth + 1'))

            task['executionObservation'] = {'status': 'observed', 'updatedAt': 1700000001}
            rows = await query()
            check('same_task_observed_clears_unknown', '已取得原执行记录' in rows.get('最近核对', '')
                  and '核对结果未知' not in await page.locator('#task-result').inner_text())
            check('same_task_identity_preserved', rows.get('任务标识') == task['taskId']
                  and rows.get('Codex 会话') == task['executionIdentity']['codexThreadId'])
            listing = await refresh()
            check('list_observed_replaces_unknown', '已取得原执行记录' in listing and '核对结果未知' not in listing)

            task.update(executionStatus='succeeded', executionObservation={'status': 'unknown', 'updatedAt': 1700000002})
            rows = await query()
            check('stored_success_does_not_hide_unknown_observation', rows.get('执行状态') == '执行成功'
                  and '核对结果未知' in rows.get('最近核对', ''))

            task.pop('executionObservation')
            rows = await query()
            check('missing_observation_does_not_invent_state', '最近核对' not in rows
                  and '核对结果未知' not in await page.locator('#task-result').inner_text())
            check('missing_observation_keeps_list_experience', '最近核对' not in await refresh())

            invalid = [None, [], 'unknown', {}, {'status': 'future', 'updatedAt': 1},
                       {'status': '__proto__', 'updatedAt': 1}, {'status': 'constructor', 'updatedAt': 1},
                       {'status': 'toString', 'updatedAt': 1}, {'status': 'observed', 'updatedAt': '1'},
                       {'status': 'observed', 'updatedAt': None}, {'status': 'observed'},
                       {'status': 'observed', 'updatedAt': 10 ** 400},
                       {'status': '<img src=x onerror="window.fixtureXSS=true">', 'updatedAt': 1}]
            for index, value in enumerate(invalid):
                task['executionObservation'] = value
                rows = await query()
                check('invalid_' + str(index) + '_detail_requires_confirmation',
                      '核对状态待确认' in rows.get('最近核对', ''), rows.get('最近核对'))
                listing = await refresh()
                check('invalid_' + str(index) + '_list_requires_confirmation', '核对状态待确认' in listing)
            check('invalid_observation_cannot_execute_markup', await page.locator('#task-result img').count() == 0
                  and await page.evaluate('window.fixtureXSS !== true'))

            task['executionObservation'] = {'status': 'unknown', 'updatedAt': 0}
            rows = await query()
            check('finite_zero_timestamp_is_valid', '核对结果未知' in rows.get('最近核对', ''))
            await page.locator('#task-id').fill('fixture-missing-task')
            await page.locator('#task-form button').click()
            await expect(page.locator('#task-result')).to_contain_text('未能读取任务')
            check('404_clears_old_detail', await page.locator('#task-result dl').count() == 0
                  and '核对结果未知' not in await page.locator('#task-result').inner_text())

            slow = True
            await page.locator('#task-id').fill(task['taskId'])
            await page.locator('#task-form button').click()
            await asyncio.wait_for(arrived.wait(), timeout=5)
            await page.locator('#logout').click()
            release.set()
            await asyncio.wait_for(delivered.wait(), timeout=5)
            await page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
            check('logout_late_response_cannot_restore_detail', await page.locator('#task-result dl').count() == 0
                  and '尚未选择节点任务' in await page.locator('#task-result').inner_text())
            check('logout_clears_list_and_token', await page.locator('#task-list li').count() == 0
                  and await page.locator('#token').input_value() == '')
            check('no_credential_storage', await page.evaluate('localStorage.length + sessionStorage.length') == 0)
            check('no_posts_or_other_mutations', not mutations, mutations)
            check('no_unhandled_api', not unexpected, unexpected)
            check('no_page_errors', not errors, errors)
            await context.close()
        finally:
            release.set()
            await browser.close()
    report = {'scope': 'API-intercepted browser fixtures only; no native execution evidence',
              'baseline': baseline, 'passed': not failures, 'checks': len(checks), 'failures': failures,
              'fixtureRequests': len(requests), 'mockResponses': len(requests), 'realAPIRequests': 0,
              'mutationRequests': mutations, 'nativeTasksSubmitted': 0, 'pageErrors': errors, 'screenshots': screenshots}
    path = OUT / (prefix + 'report.json')
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    path.chmod(0o600)
    for name in screenshots:
        (OUT / name).chmod(0o600)
    print(json.dumps({'passed': report['passed'], 'checks': len(checks),
                      'failedChecks': [x['check'] for x in failures], 'realAPIRequests': 0,
                      'mutationRequests': mutations, 'report': str(path)}, ensure_ascii=False))
    return not failures


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', action='store_true')
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(run(args.baseline)) else 1)
