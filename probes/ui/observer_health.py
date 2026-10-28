"""Intercepted browser regression probe for the execution observer summary."""
import argparse
import asyncio
import json
import os
from pathlib import Path
from urllib.parse import unquote, urlsplit

from playwright.async_api import async_playwright

from probes.qwenpaw_memory.reconcile_browser import CHROME

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'var/worker-health-ui-20261022'
ORIGIN = 'http://127.0.0.1:3193'
TOKEN = 'observer-health-fixture-only-not-a-real-credential'


def health(observer=None, *, ready=True, block=None):
    value = {'storage': 'available', 'controlledExecution': 'confirmed', 'readyForExecution': ready}
    if observer is not None:
        value['executionObserver'] = observer
    if block is not None:
        value['executionBlockReason'] = block
    return value


async def run(baseline=False):
    OUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    OUT.chmod(0o700)
    prefix = 'baseline-' if baseline else ''
    current = health()
    health_status = 200
    health_plan = []
    checks, failures, requests, mutations, unexpected, errors, screenshots = [], [], [], [], [], [], []

    def check(name, ok, actual=None):
        checks.append(name)
        if not ok:
            failures.append({'check': name, **({'actual': actual} if actual is not None else {})})

    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=CHROME, args=['--no-sandbox'])
        try:
            context = await browser.new_context(viewport={'width': 1440, 'height': 1000})

            async def route(r):
                url, path = urlsplit(r.request.url), unquote(urlsplit(r.request.url).path)
                if baseline and url.netloc == '127.0.0.1:3193':
                    asset = {'/workbench/': 'index.html', '/workbench/index.html': 'index.html',
                             '/workbench/app.js': 'app.js', '/workbench/styles.css': 'styles.css'}.get(path)
                    if asset:
                        body = (OUT / 'baseline-assets' / asset).read_bytes()
                        content_type = 'text/javascript' if asset.endswith('.js') else 'text/css' if asset.endswith('.css') else 'text/html'
                        await r.fulfill(status=200, body=body, content_type=content_type)
                        return
                if path.startswith('/extension/v1/'):
                    requests.append((r.request.method, path))
                    if r.request.method != 'GET':
                        mutations.append(path)
                        await r.fulfill(status=405, json={'detail': 'Fixture prohibits writes'})
                    elif path == '/extension/v1/health':
                        response = health_plan.pop(0) if health_plan else (health_status, current, None)
                        code, body, gate = response
                        if gate is not None:
                            gate[0].set()
                            await gate[1].wait()
                        await r.fulfill(status=code, body=json.dumps(body, ensure_ascii=False, allow_nan=True),
                                         content_type='application/json')
                    elif path == '/extension/v1/workflows':
                        await r.fulfill(status=200, json={'workflows': []})
                    elif path == '/extension/v1/node-tasks':
                        await r.fulfill(status=200, json={'tasks': [], 'nextCursor': None})
                    else:
                        unexpected.append(path)
                        await r.fulfill(status=404, json={'detail': 'Unknown fixture request'})
                    return
                if url.netloc == '127.0.0.1:3193' and path.startswith('/workbench/'):
                    await r.continue_()
                else:
                    await r.abort()

            await context.route('**/*', route)
            page = await context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            await page.goto(ORIGIN + '/workbench/')
            await page.locator('#token').fill(TOKEN)
            await page.locator('#login-form button').click()
            await page.locator('#connected-actions').wait_for(state='visible')

            async def snapshot():
                return await page.evaluate('''() => { const root=document.querySelector('#observer-health'); return {
                  exists:!!root, hidden:!root||root.hidden,
                  state:document.querySelector('#observer-state')?.textContent||'',
                  hint:document.querySelector('#observer-hint')?.textContent||'',
                  admission:document.querySelector('#admission-state')?.textContent||'',
                  startDisabled:document.querySelector('#start-form button[type=submit]')?.disabled,
                  text:root?.innerText||''}; }''')

            async def refresh(value):
                nonlocal current, health_status
                current = value
                health_status = 200
                before = sum(path == '/extension/v1/health' for _, path in requests)
                await page.locator('#refresh-health').click()
                for _ in range(100):
                    await page.wait_for_timeout(20)
                    if sum(path == '/extension/v1/health' for _, path in requests) > before:
                        break
                await page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')

            async def state_case(name, data, label, ready=True, hint_terms=()):
                await refresh(health(data, ready=ready))
                s = await snapshot()
                check(name + '_visible', s['exists'] and not s['hidden'], s)
                check(name + '_label', s['state'] == label, s['state'])
                expected = '允许新节点任务' if ready else '节点执行尚未开放'
                check(name + '_admission_preserved', s['admission'] == expected, s['admission'])
                if hint_terms:
                    check(name + '_hint_meaning', any(x in s['hint'] for x in hint_terms), s['hint'])
                return s

            s = await snapshot()
            check('legacy_field_hidden', not s['exists'] or s['hidden'], s)
            s = await state_case('healthy_ready_false', {
                'status': 'healthy', 'lastScanCompletedAt': 1792656000,
                'secondsSinceCompletedScanOrStart': 2, 'observationWindowSeconds': 30,
            }, '巡检正常', ready=False)
            check('healthy_does_not_open_admission', s['admission'] != '允许新节点任务', s)
            await state_case('degraded', {'status': 'degraded', 'lastScanCompletedAt': 1792656000,
                'secondsSinceCompletedScanOrStart': 1, 'observationWindowSeconds': 30}, '巡检遇到错误', hint_terms=('刷新', '原任务'))
            await state_case('delayed', {'status': 'delayed', 'lastScanCompletedAt': 1792655900,
                'secondsSinceCompletedScanOrStart': 100, 'observationWindowSeconds': 30}, '巡检完成延迟', hint_terms=('刷新', '原任务'))
            await state_case('stopped', {'status': 'stopped', 'lastScanCompletedAt': 1792655900,
                'secondsSinceCompletedScanOrStart': 100, 'observationWindowSeconds': 30}, '巡检线程已停止', hint_terms=('刷新', '原任务'))
            await state_case('starting', {'status': 'starting', 'lastScanCompletedAt': None,
                'secondsSinceCompletedScanOrStart': 3, 'observationWindowSeconds': 30}, '等待首轮巡检', hint_terms=('首次扫描',))
            await state_case('not_started', {'status': 'not_started', 'lastScanCompletedAt': None,
                'secondsSinceCompletedScanOrStart': None, 'observationWindowSeconds': 30}, '巡检尚未启动', hint_terms=('刷新',))
            await state_case('not_configured', {'status': 'not_configured'}, '未配置执行巡检', ready=False)

            bad = [None, [], 'healthy', {}, {'status': 'healthy'},
                   {'status': '__proto__', 'lastScanCompletedAt': 1, 'secondsSinceCompletedScanOrStart': 1, 'observationWindowSeconds': 1},
                   {'status': 'prototype', 'lastScanCompletedAt': 1, 'secondsSinceCompletedScanOrStart': 1, 'observationWindowSeconds': 1},
                   {'status': '<img src=x onerror="window.observerXSS=true">', 'lastScanCompletedAt': 1, 'secondsSinceCompletedScanOrStart': 1, 'observationWindowSeconds': 1},
                   {'status': 'healthy', 'lastScanCompletedAt': '1', 'secondsSinceCompletedScanOrStart': 1, 'observationWindowSeconds': 30},
                   {'status': 'healthy', 'lastScanCompletedAt': 1, 'secondsSinceCompletedScanOrStart': 1, 'observationWindowSeconds': 0},
                   {'status': 'healthy', 'lastScanCompletedAt': None, 'secondsSinceCompletedScanOrStart': 1, 'observationWindowSeconds': 30},
                   {'status': 'degraded', 'lastScanCompletedAt': 1, 'secondsSinceCompletedScanOrStart': None, 'observationWindowSeconds': 30},
                   {'status': 'healthy', 'lastScanCompletedAt': float('inf'), 'secondsSinceCompletedScanOrStart': 1, 'observationWindowSeconds': 30},
                   {'status': 'healthy', 'lastScanCompletedAt': 1, 'secondsSinceCompletedScanOrStart': float('inf'), 'observationWindowSeconds': 30},
                   {'status': 'healthy', 'lastScanCompletedAt': 1, 'secondsSinceCompletedScanOrStart': 1, 'observationWindowSeconds': float('inf')},
                   {'status': 'healthy', 'lastScanCompletedAt': -1, 'secondsSinceCompletedScanOrStart': -1, 'observationWindowSeconds': 30}]
            for i, value in enumerate(bad):
                await refresh(health(value))
                s = await snapshot()
                check(f'invalid_{i}_not_healthy', s['state'] != '巡检正常', s)
                check(f'invalid_{i}_schema_label', s['state'] in ('巡检状态待核对', '未配置执行巡检', ''), s['state'])
            check('malicious_status_is_text_only', await page.locator('#observer-health img').count() == 0
                  and await page.evaluate('window.observerXSS !== true'))

            await refresh(health())
            s = await snapshot()
            check('missing_field_hidden_after_refresh', not s['exists'] or s['hidden'], s)

            # A failed manual refresh must discard the previous healthy snapshot.
            await state_case('healthy_before_failed_refresh', {
                'status': 'healthy', 'lastScanCompletedAt': 1792656000,
                'secondsSinceCompletedScanOrStart': 2, 'observationWindowSeconds': 30,
            }, '巡检正常')
            health_status = 503
            before = sum(path == '/extension/v1/health' for _, path in requests)
            await page.locator('#refresh-health').click()
            for _ in range(100):
                await page.wait_for_timeout(20)
                if sum(path == '/extension/v1/health' for _, path in requests) > before:
                    break
            await page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
            s = await snapshot()
            check('failed_refresh_clears_healthy', s['state'] != '巡检正常', s)
            check('failed_refresh_shows_unknown', s['state'] in ('巡检状态待核对', '') or s['hidden'], s)
            health_status = 200

            # Two concurrent refreshes return in reverse order; the later request must win.
            first_arrived, second_arrived, release_first = asyncio.Event(), asyncio.Event(), asyncio.Event()
            release_second = asyncio.Event()
            release_second.set()
            health_plan[:] = [(200, health({'status': 'degraded', 'lastScanCompletedAt': 1792656000,
                'secondsSinceCompletedScanOrStart': 2, 'observationWindowSeconds': 30}), (first_arrived, release_first)),
                (200, health({'status': 'stopped', 'lastScanCompletedAt': 1792656000,
                'secondsSinceCompletedScanOrStart': 2, 'observationWindowSeconds': 30}), (second_arrived, release_second))]
            before = sum(path == '/extension/v1/health' for _, path in requests)
            await page.evaluate("""() => { const b=document.querySelector('#refresh-health');
              b.dispatchEvent(new MouseEvent('click',{bubbles:true}));
              b.dispatchEvent(new MouseEvent('click',{bubbles:true})); }""")
            await asyncio.wait_for(first_arrived.wait(), timeout=5)
            await asyncio.wait_for(second_arrived.wait(), timeout=5)
            # The second response is released immediately; wait until it has rendered, then release first.
            await page.wait_for_timeout(100)
            release_first.set()
            for _ in range(100):
                await page.wait_for_timeout(20)
                if sum(path == '/extension/v1/health' for _, path in requests) >= before + 2:
                    break
            await page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
            s = await snapshot()
            check('latest_concurrent_refresh_wins', s['state'] == '巡检线程已停止', s['state'])
            health_plan.clear()

            await state_case('screenshot', {'status': 'healthy', 'lastScanCompletedAt': 1792656000,
                'secondsSinceCompletedScanOrStart': 2, 'observationWindowSeconds': 30}, '巡检正常')
            for w, h, name in ((1440, 1000, 'desktop'), (390, 844, 'mobile')):
                await page.set_viewport_size({'width': w, 'height': h})
                path = OUT / f'{prefix}observer-health-{name}.png'
                await page.screenshot(path=str(path), full_page=True)
                screenshots.append(path.name)
                check(name + '_no_horizontal_overflow', await page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'))
                check(name + '_buttons_touch_size', await page.locator('button:visible').evaluate_all(
                    '(xs) => xs.every(e => e.getBoundingClientRect().height >= 44)'))

            # A response arriving after logout must not restore any observer snapshot.
            arrived, release = asyncio.Event(), asyncio.Event()
            health_plan[:] = [(200, health({'status': 'healthy', 'lastScanCompletedAt': 1792656000,
                'secondsSinceCompletedScanOrStart': 2, 'observationWindowSeconds': 30}), (arrived, release))]
            await page.evaluate("document.querySelector('#refresh-health').dispatchEvent(new MouseEvent('click',{bubbles:true}))")
            await asyncio.wait_for(arrived.wait(), timeout=5)
            await page.locator('#logout').click()
            release.set()
            await page.wait_for_timeout(100)
            s = await snapshot()
            check('logout_clears_observer_state', not s['exists'] or s['hidden'] or not s['state'], s)
            check('late_health_response_cannot_restore_after_logout', not s['exists'] or s['hidden'] or not s['state'], s)
            check('logout_clears_credential', await page.locator('#token').input_value() == '')
            check('health_only_gets', all(m == 'GET' for m, pth in requests if pth == '/extension/v1/health'), requests)
            check('no_api_mutations', not mutations, mutations)
            check('no_unexpected_api', not unexpected, unexpected)
            check('no_page_errors', not errors, errors)
            await context.close()
        finally:
            await browser.close()

    report = {'scope': 'intercepted local browser fixtures only', 'baseline': baseline,
              'passed': not failures, 'checks': len(checks), 'failures': failures,
              'fixtureRequests': len(requests), 'realAPIRequests': 0,
              'mutationRequests': mutations, 'nativeTasksSubmitted': 0,
              'pageErrors': errors, 'screenshots': screenshots}
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
