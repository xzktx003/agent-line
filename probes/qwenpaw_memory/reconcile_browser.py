"""Explicit fixture browser checks; never sends a native memory mutation."""
import argparse
import asyncio
import json
from pathlib import Path
import time

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[2]
CHROME = '/data01/home/zukang.xu/.cache/ms-playwright/chromium_headless_shell-1223/chrome-headless-shell-linux64/chrome-headless-shell'


async def run(baseline=False):
    out = ROOT / 'var/memory-reconciliation-20261022/browser-fixture'
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    binding = {'bindingId': 'fixture-memory', 'projectId': 'fixture-project',
               'roleBindingId': 'fixture-role', 'roleAgentId': 'fixture-native-role'}
    task = {'taskId': 'fixture-task', 'executionStatus': 'succeeded', 'verificationStatus': 'passed',
            'resourcesReleased': True, 'memoryStatus': 'pending', 'workflowRunId': 'fixture-run'}
    record = {**binding, 'taskId': task['taskId'], 'actor': '界面验收身份（模拟）',
              'decisionId': 'fixture-original-decision', 'content': '已核实的任务经验；等待原生写回读回核对。',
              'contentSha256': 'a' * 64, 'status': 'pending', 'reason': 'readback_unknown',
              'createdAt': time.time(), 'source': {'workflow': {'runId': 'fixture-run'},
                  'executionStatus': 'succeeded', 'verificationStatus': 'passed', 'evidenceSha256': 'b' * 64},
              'events': []}
    checks = []
    errors = []
    state = {'record': record, 'posts': [], 'mode': 'pending'}
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=CHROME, args=['--no-sandbox'])
        try:
            context = await browser.new_context(viewport={'width': 1440, 'height': 1000})
            page = await context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))

            async def route(r):
                req = r.request
                suffix = req.url.split('/extension/v1/memories', 1)[1].split('?', 1)[0]
                if req.method == 'POST':
                    assert suffix == '/fixture-memory/fixture-task/reconcile', 'browser tried to submit memory'
                    assert req.post_data_json == {}
                    state['posts'].append(suffix)
                    if state['mode'] == 'disconnect':
                        return await r.abort('connectionreset')
                    if state['mode'] == 'unauthorized':
                        return await r.fulfill(status=401, json={'detail': '需要认证'})
                    if state['mode'] == 'conflict':
                        return await r.fulfill(status=409, json={'detail': '审核不一致'})
                    if state['mode'] == 'committed':
                        state['record']['status'] = 'committed'
                    if state['mode'] == 'mismatch':
                        return await r.fulfill(json={**state['record'], 'taskId': 'another-task', 'status': 'committed'})
                    return await r.fulfill(json=state['record'])
                if suffix == '/bindings':
                    return await r.fulfill(json={'bindings': [binding]})
                if suffix == '/sources':
                    return await r.fulfill(json={'tasks': [task], 'nextCursor': None})
                if suffix.startswith('/sources/'):
                    return await r.fulfill(json={**task, 'projectId': binding['projectId'], 'roleAgentId': binding['roleAgentId'],
                        'workflow': {'runId': 'fixture-run'}, 'outputPreview': '{"result":"模拟来源，执行成功"}',
                        'evidencePreview': '[]', 'outputTruncated': False, 'evidenceTruncated': False})
                if suffix == '':
                    return await r.fulfill(json={'memories': [state['record']], 'nextCursor': None})
                return await r.fulfill(json=state['record'])

            await context.route('**/extension/v1/memories**', route)
            await page.goto('http://127.0.0.1:3193/workbench/memories.html')
            await page.locator('#memory-token').fill('fixture-only-credential')
            await page.locator('#memory-connect').click()
            await page.locator('#memory-sources button').first.click()
            await page.wait_for_function("() => !document.querySelector('#memory-query').disabled")
            assert await page.locator('#memory-content').evaluate('(e) => e.readOnly')
            assert await page.locator('#memory-submit').is_disabled()
            for width, height, name in [(1440, 1000, 'desktop'), (390, 844, 'mobile')]:
                await page.set_viewport_size({'width': width, 'height': height})
                assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                await page.screenshot(path=str(out / f'{"baseline" if baseline else "pending"}-{name}.png'), full_page=True)
            if baseline:
                assert not state['posts']
                print(json.dumps({'baseline': True, 'nativeMutations': 0, 'screenshots': 2}))
                return
            button = page.locator('#memory-reconcile')
            assert await button.is_visible() and await button.is_enabled()
            assert await button.evaluate('(e) => e.getBoundingClientRect().height >= 44')
            await button.focus()
            assert await button.evaluate('(e) => document.activeElement === e')
            await button.evaluate('(e) => {e.click(); e.click();}')
            await page.wait_for_function("() => !document.querySelector('#memory-query').disabled")
            assert len(state['posts']) == 1
            assert '等待核对' in await page.locator('#memory-context').inner_text()
            checks += ['pending_visible_readonly', 'keyboard_focus_and_touch_height', 'double_click_one_reconciliation', 'still_pending_can_observe_again']
            for mode in ('disconnect', 'conflict', 'mismatch'):
                state['mode'] = mode
                await button.click()
                await page.wait_for_function("() => !document.querySelector('#memory-query').disabled")
                assert await button.is_enabled()
                assert '等待核对' in await page.locator('#memory-context').inner_text()
                assert await page.locator('#memory-submit').is_disabled()
                checks.append(mode + '_does_not_forge_commit_or_allow_new_approval')
            state['mode'] = 'committed'
            await button.click()
            await page.wait_for_function("() => !document.querySelector('#memory-query').disabled")
            assert await button.is_hidden()
            assert '已写回并读回核对' in await page.locator('#memory-context').inner_text()
            checks.append('committed_hides_reconciliation_and_keeps_content_readonly')
            for status in ('approved', 'needs_review', 'deleting', 'deleted'):
                state['record']['status'] = status
                if status in ('deleting', 'deleted'):
                    state['record'].update(content='', deletion={'status': status})
                await page.locator('#memory-query').click()
                await page.wait_for_function("() => !document.querySelector('#memory-query').disabled")
                assert await button.is_hidden()
                assert await page.locator('#memory-submit').is_disabled()
                checks.append(status + '_cannot_trigger_rewrite')
            state['record'].pop('deletion', None)
            state['record'].update(status='pending', content='原审核正文')
            await page.locator('#memory-query').click()
            await page.wait_for_function("() => !document.querySelector('#memory-query').disabled")
            state['mode'] = 'unauthorized'
            await button.click()
            await page.wait_for_function("() => !document.querySelector('#memory-login').hidden")
            assert await page.locator('#memory-content').input_value() == ''
            assert await button.is_hidden()
            assert await page.evaluate('localStorage.length === 0 && sessionStorage.length === 0')
            checks.append('expired_auth_clears_content_and_reconciliation')
            assert not errors
            report = {'verified': True, 'fixtureOnly': True, 'nativeMutations': 0, 'checks': checks,
                      'reconcilePOSTs': len(state['posts']), 'browserErrors': errors}
            (out / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(json.dumps(report, ensure_ascii=False))
            await context.close()
        finally:
            await browser.close()


if __name__ == '__main__':
    args = argparse.ArgumentParser()
    args.add_argument('--baseline', action='store_true')
    asyncio.run(run(args.parse_args().baseline))
