"""Task-center fixture regression against local static assets; no real API writes.

Run --baseline before the UI edit to preserve the expected red report. Every
/extension/v1 request is intercepted, including unexpected mutation attempts.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
from urllib.parse import unquote, urlsplit

from playwright.async_api import async_playwright, expect

from probes.qwenpaw_memory.reconcile_browser import CHROME


ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'var/task-state-context-20261022/browser-fixture'
ORIGIN = 'http://127.0.0.1:3193'
TOKEN = 'task-center-fixture-only-not-a-real-service-credential'
CONTEXT = {
    '流程标识': ('workflow', 'id'), '发布版本': ('workflow', 'revision'),
    '运行标识': ('workflow', 'runId'), '节点标识': ('workflow', 'nodeId'),
    '节点激活': ('workflow', 'nodeExecutionId'), '本次尝试': ('workflow', 'attemptId'),
    '原生执行任务': ('executionIdentity', 'upstreamTaskId'),
    'Codex 会话': ('executionIdentity', 'codexThreadId'),
    'Codex 回合': ('executionIdentity', 'codexTurnId'),
}


def fixture(task_id, verification, memory, *, execution='succeeded'):
    return {
        'taskId': task_id, 'projectId': 'fixture-project', 'roleBindingId': 'fixture-binding',
        'executionStatus': execution, 'upstreamSubmissionStatus': 'accepted', 'version': 4,
        'verificationStatus': verification, 'memoryStatus': memory, 'resourcesReleased': True,
        'workflow': {'id': 'fixture-flow', 'revision': '7', 'runId': 'fixture-run',
                     'nodeId': 'fixture-node', 'nodeExecutionId': 'fixture-activation',
                     'attemptId': 'fixture-attempt'},
        'executionIdentity': {'upstreamTaskId': 'fixture-native-task',
                              'codexThreadId': 'fixture-codex-thread', 'codexTurnId': 'fixture-codex-turn'},
        'roleRevision': 'qwenpaw-role-v1:' + 'a' * 64,
        'output': {'text': 'Fixture output; no coding task was submitted.'}, 'evidence': [],
    }


async def run(baseline=False):
    OUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    OUT.chmod(0o700)
    prefix = 'baseline-' if baseline else ''
    cases = [
        ('failed', 'failed', '验证失败', '记忆写回失败'),
        ('pending', 'pending', '等待验证', '记忆等待核对'),
        ('canceled', 'deleting', '已取消验收', '删除处理中'),
        ('passed', 'deleted', '验证通过', '已删除'),
        ('passed', 'needs_review', '验证通过', '需要审核'),
        ('succeeded', 'succeeded', '未识别状态', '未识别状态'),
        ('future-verification', 'future-memory', '未识别状态', '未识别状态'),
        ('passed', 'committed', '验证通过', '已写入'),
    ]
    tasks = [fixture('fixture-task-' + str(i), v, m,
                     execution='future-execution' if i == 6 else 'succeeded')
             for i, (v, m, _, _) in enumerate(cases)]
    missing = fixture('fixture-missing-context', None, None)
    missing.update(workflow={}, executionIdentity={})
    missing.pop('roleRevision')
    tasks.append(missing)
    long = fixture('fixture-long-context', 'passed', 'committed')
    for label, (group, field) in CONTEXT.items():
        long[group][field] = 'fixture-' + field + '-' + 'x' * 180
    long['workflow']['id'] = '<img src=x onerror="window.fixtureXSS=true">'
    long['roleRevision'] = 'qwenpaw-role-v1:' + 'b' * 64
    tasks.append(long)
    by_id = {task['taskId']: task for task in tasks}
    checks, failures, mutations, unexpected, errors, requests, screenshots = [], [], [], [], [], [], []
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
                    requests.append({'method': r.request.method, 'path': path})
                    if r.request.method != 'GET':
                        mutations.append(path)
                        await r.fulfill(status=405, json={'detail': 'Fixture prohibits all writes'})
                        return
                    if path == '/extension/v1/health':
                        data = {'storage': 'available', 'controlledExecution': 'confirmed',
                                'readyForExecution': True, 'reconciliationRequired': False}
                    elif path == '/extension/v1/workflows':
                        data = {'workflows': []}
                    elif path == '/extension/v1/node-tasks':
                        data = {'tasks': tasks, 'nextCursor': None}
                    elif path.removeprefix('/extension/v1/node-tasks/') in by_id:
                        data = by_id[path.rsplit('/', 1)[1]]
                        if slow:
                            arrived.set()
                            await release.wait()
                    else:
                        unexpected.append(path)
                        await r.fulfill(status=404, json={'detail': 'Unknown fixture path'})
                        return
                    await r.fulfill(status=200, json=data)
                    if slow and path.startswith('/extension/v1/node-tasks/'):
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
            await page.locator('#token').fill(TOKEN)
            await page.locator('#login-form button').click()
            await expect(page.locator('#task-list li')).to_have_count(len(tasks))

            async def query(task):
                await page.locator('#task-id').fill(task['taskId'])
                await page.locator('#task-form button').click()
                await expect(page.locator('#task-result dl dd').first).to_have_text(task['taskId'])
                return await page.locator('#task-result').evaluate(
                    '(e) => Object.fromEntries(Array.from(e.querySelectorAll("dl > dt"), '
                    'dt => [dt.textContent, dt.nextElementSibling.textContent]))')

            async def expand_trace(name):
                trace = page.locator('#task-result .task-trace')
                if await trace.count() != 1:
                    check(name + '_trace_exists', False)
                    return
                summary = trace.locator('summary')
                await summary.focus()
                check(name + '_summary_keyboard_focus', await summary.evaluate('(e) => document.activeElement === e'))
                before = len(requests)
                if not await trace.evaluate('(e) => e.open'):
                    await summary.press('Enter')
                check(name + '_keyboard_opens_trace', await trace.evaluate('(e) => e.open'))
                await summary.click()
                check(name + '_manual_collapse', not await trace.evaluate('(e) => e.open'))
                await summary.press('Enter')
                check(name + '_keyboard_reopens_trace', await trace.evaluate('(e) => e.open'))
                await page.evaluate('() => new Promise(resolve => requestAnimationFrame(resolve))')
                check(name + '_trace_toggle_does_not_request', len(requests) == before)

            for i, (_, _, verification, memory) in enumerate(cases):
                task = tasks[i]
                row = page.locator('#task-list li').nth(i)
                states = await row.locator('.task-states > span').all_text_contents()
                check(f'list_{i}_verification', verification in states[1], states[1])
                check(f'list_{i}_memory', memory in states[2], states[2])
                expected_execution = '未识别状态' if i == 6 else '执行成功'
                check(f'list_{i}_execution_independent', expected_execution in states[0], states[0])
                rows = await query(task)
                check(f'detail_{i}_verification', verification in rows.get('验证状态', ''), rows.get('验证状态'))
                check(f'detail_{i}_memory', memory in rows.get('记忆回写', ''), rows.get('记忆回写'))
                check(f'detail_{i}_execution_independent', expected_execution in rows.get('执行状态', ''), rows.get('执行状态'))
                if i == 0:
                    await expand_trace('detail')
                    for label, (group, field) in CONTEXT.items():
                        check('detail_context_' + field, rows.get(label) == str(task[group][field]), rows.get(label))
                    check('detail_role_revision', rows.get('角色版本') == task['roleRevision'], rows.get('角色版本'))
                    for width, height, name in ((1440, 1000, 'desktop'), (390, 844, 'mobile')):
                        await page.set_viewport_size({'width': width, 'height': height})
                        filename = prefix + 'task-result-' + name + '.png'
                        await page.locator('#task-result').screenshot(path=str(OUT / filename))
                        screenshots.append(filename)
                        check(name + '_result_no_horizontal_overflow', await page.locator('#task-result').evaluate(
                            '(e) => e.scrollWidth <= e.clientWidth + 1'))
                        summary = page.locator('#task-result .task-trace > summary')
                        check(name + '_trace_summary_touch_height', await summary.count() == 1 and
                              await summary.evaluate('(e) => e.getBoundingClientRect().height >= 44'))

            rows = await query(missing)
            for label, (_, field) in CONTEXT.items():
                check('missing_' + field + '_not_inferred', rows.get(label) == '尚无记录', rows.get(label))
            check('missing_verification_not_success', rows.get('验证状态') == '尚无记录', rows.get('验证状态'))
            check('missing_memory_not_success', rows.get('记忆回写') == '尚无记录', rows.get('记忆回写'))

            rows = await query(long)
            await expand_trace('long_context')
            for label, (group, field) in CONTEXT.items():
                check('long_context_' + field, rows.get(label) == str(long[group][field]), rows.get(label))
            check('xss_is_literal_text', await page.locator('#task-result img').count() == 0
                  and await page.evaluate('window.fixtureXSS !== true'))
            check('identity_does_not_invent_role_or_session', not any(
                key in rows for key in ('roleAgentId', 'roleSessionId', '正式角色标识', '角色会话标识')))
            for width, height, name in ((1440, 1000, 'desktop'), (390, 844, 'mobile')):
                await page.set_viewport_size({'width': width, 'height': height})
                check(name + '_long_result_no_horizontal_overflow', await page.locator('#task-result').evaluate(
                    '(e) => e.scrollWidth <= e.clientWidth + 1'))
                filename = prefix + 'long-task-result-' + name + '.png'
                await page.locator('#task-result').screenshot(path=str(OUT / filename))
                screenshots.append(filename)
                summary = page.locator('#task-result .task-trace > summary')
                check(name + '_long_trace_summary_touch_height', await summary.count() == 1 and
                      await summary.evaluate('(e) => e.getBoundingClientRect().height >= 44'))

            slow = True
            await page.locator('#task-id').fill(tasks[0]['taskId'])
            await page.locator('#task-form button').click()
            await asyncio.wait_for(arrived.wait(), timeout=5)
            await page.locator('#logout').click()
            release.set()
            await asyncio.wait_for(delivered.wait(), timeout=5)
            await page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
            check('logout_clears_task_and_late_response', '尚未选择节点任务' in await page.locator('#task-result').inner_text()
                  and await page.locator('#task-result dl').count() == 0)
            check('logout_clears_list', await page.locator('#task-list li').count() == 0)
            check('logout_clears_token', await page.locator('#token').input_value() == '')
            check('credentials_not_persisted', await page.evaluate('localStorage.length + sessionStorage.length') == 0)
            check('no_execution_or_memory_posts', not mutations, mutations)
            check('only_explicit_api_fixtures', not unexpected, unexpected)
            check('no_browser_errors', not errors, errors)
            await context.close()
        finally:
            release.set()
            await browser.close()
    report = {'scope': 'intercepted API fixtures against real local static assets only',
              'baseline': baseline, 'passed': not failures, 'checks': len(checks), 'failures': failures,
              'fixtureTasks': len(tasks), 'fixtureRequests': len(requests), 'mutationRequests': mutations,
              'realAPIRequests': 0, 'nativeTasksSubmitted': 0, 'mockResponses': len(requests),
              'pageErrors': errors, 'screenshots': screenshots}
    path = OUT / (prefix + 'report.json')
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    path.chmod(0o600)
    for name in screenshots:
        os.chmod(OUT / name, 0o600)
    print(json.dumps({key: report[key] for key in ('scope', 'baseline', 'passed', 'checks', 'mutationRequests',
                                                  'realAPIRequests', 'nativeTasksSubmitted')}
                     | {'failedChecks': [item['check'] for item in failures], 'report': str(path)}, ensure_ascii=False))
    return not failures


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', action='store_true')
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(run(args.baseline)) else 1)
