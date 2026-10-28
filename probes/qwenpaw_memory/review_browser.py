"""Browser checks of review states and the real committed/deletion-pending record."""
import asyncio
import json
from pathlib import Path
import time

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[2]
BASE = 'http://127.0.0.1:3195'
DELETION = (ROOT / 'var/memory-review-http/deletion-report.json').exists()
OUT = ROOT / 'var/memory-review-http' / ('browser-deletion' if DELETION else 'browser')
CHROME = '/data01/home/zukang.xu/.cache/ms-playwright/chromium_headless_shell-1223/chrome-headless-shell-linux64/chrome-headless-shell'


async def main():
    OUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    checks = []
    errors = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=CHROME, args=['--no-sandbox'])
        try:
            context = await browser.new_context(viewport={'width': 1440, 'height': 1000})
            page = await context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            state = {'record': None, 'posts': [], 'unknown': False, 'unauthorized': False}
            binding = {'bindingId': 'binding', 'projectId': 'project',
                       'roleBindingId': 'role-binding', 'roleAgentId': 'role'}
            task = {'taskId': 'task-ui', 'executionStatus': 'succeeded',
                    'verificationStatus': 'passed', 'resourcesReleased': True,
                    'memoryStatus': 'not_requested', 'workflowRunId': 'run-ui'}
            async def route(request_route):
                request = request_route.request
                suffix = request.url.split('/extension/v1/memories', 1)[1].split('?', 1)[0]
                if state['unauthorized']:
                    await request_route.fulfill(status=401, json={'detail': '需要凭证'})
                elif suffix == '/bindings':
                    await request_route.fulfill(json={'bindings': [binding]})
                elif suffix == '/sources':
                    await request_route.fulfill(json={'tasks': [task], 'nextCursor': None})
                elif suffix.startswith('/sources/'):
                    await request_route.fulfill(json={**task, 'projectId': 'project', 'roleAgentId': 'role',
                        'workflow': {'runId': 'run-ui'}, 'outputPreview': '{"result":"可核对的任务结果"}',
                        'outputTruncated': False, 'evidencePreview': '[{"kind":"verification","status":"passed"}]',
                        'evidenceTruncated': False})
                elif suffix == '':
                    await request_route.fulfill(json={'memories': [state['record']] if state['record'] else [],
                                                       'nextCursor': None})
                elif request.method == 'POST':
                    body = request.post_data_json
                    state['posts'].append(body)
                    if state['unknown']:
                        await request_route.abort('connectionreset')
                        return
                    state['record'] = {**binding, 'taskId': task['taskId'], 'actor': '界面审核用户',
                        'decisionId': body['decisionId'], 'content': body['content'],
                        'status': 'committed', 'reason': 'official_readback_matches',
                        'contentSha256': 'a' * 64, 'createdAt': time.time(), 'events': [],
                        'source': {'workflow': {'runId': 'run-ui'}, 'verificationStatus': 'passed',
                                   'executionStatus': 'succeeded', 'evidenceSha256': 'b' * 64}}
                    await request_route.fulfill(status=202, json=state['record'])
                elif state['record'] is not None:
                    await request_route.fulfill(json=state['record'])
                else:
                    await request_route.fulfill(status=404, json={'detail': '尚无记录'})
            await context.route('**/extension/v1/memories**', route)
            await page.goto(BASE + '/workbench/memories.html')
            await page.locator('#memory-token').fill('mock-review-credential')
            await page.locator('#memory-connect').click()
            await page.locator('#memory-sources button').first.wait_for()
            await page.locator('#memory-sources button').first.click()
            await page.locator('#memory-content:not([disabled])').wait_for()
            content = '已核对的经验 <img src=x onerror="window.BAD=1">'
            await page.locator('#memory-content').fill(content)
            await page.locator('#memory-consent').check()
            await page.locator('#memory-submit').click()
            await page.wait_for_function("() => document.querySelector('#memory-content').readOnly")
            assert len(state['posts']) == 1
            assert set(state['posts'][0]) == {'decisionId', 'content'}
            assert await page.locator('#memory-context img').count() == 0
            assert await page.evaluate('window.BAD === undefined')
            assert await page.locator('#memory-submit').is_disabled()
            checks += ['browser_review_posts_once', 'request_contains_no_actor_or_scope',
                       'approved_content_readonly', 'content_not_interpreted_as_html']
            await page.screenshot(path=str(OUT / 'mock-desktop.png'), full_page=True)
            await page.set_viewport_size({'width': 390, 'height': 844})
            assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            await page.screenshot(path=str(OUT / 'mock-mobile.png'), full_page=True)
            checks.append('mobile_no_horizontal_overflow')
            state['record'].update(status='deleting', content='', deletion={'receipt': {'roleBlocked': True}})
            await page.locator('#memory-query').click()
            await page.wait_for_function("() => !document.querySelector('#memory-query').disabled")
            assert await page.locator('#memory-content').input_value() == ''
            assert '删除中，等待索引核对' in await page.locator('#memory-context').inner_text()
            checks.append('deletion_clears_visible_approved_content')
            state['record'] = None
            await page.locator('#memory-query').click()
            await page.wait_for_function("() => !document.querySelector('#memory-query').disabled")
            assert await page.locator('#memory-content').input_value() == ''
            assert await page.locator('#memory-submit').is_disabled()
            assert len(state['posts']) == 1
            checks.append('missing_record_after_deletion_does_not_revive_cached_approval')
            await page.locator('#memory-logout').click()
            assert await page.locator('#memory-content').input_value() == ''
            assert await page.locator('#memory-history button').count() == 0
            assert await page.evaluate('localStorage.length === 0 && sessionStorage.length === 0')
            checks.append('logout_clears_content_and_no_storage')

            # Reconnecting another identity must not revive private draft content.
            state['record'] = None
            await page.locator('#memory-token').fill('mock-other-credential')
            await page.locator('#memory-connect').click()
            await page.locator('#memory-sources button').first.wait_for()
            await page.locator('#memory-sources button').first.click()
            await page.wait_for_function("() => !document.querySelector('#memory-query').disabled")
            assert await page.locator('#memory-content').input_value() == ''
            assert await page.locator('#memory-submit').is_disabled()
            checks.append('reconnect_does_not_revive_prior_identity_content')
            await page.locator('#memory-logout').click()
            task['taskId'] = 'task-ui-unknown'
            state['unknown'] = True
            await page.locator('#memory-token').fill('mock-review-credential')
            await page.locator('#memory-connect').click()
            await page.locator('#memory-sources button').first.wait_for()
            await page.locator('#memory-sources button').first.click()
            await page.locator('#memory-content:not([disabled])').wait_for()
            await page.locator('#memory-content').fill('未知送达时保持原审核')
            await page.locator('#memory-consent').check()
            await page.locator('#memory-submit').click()
            await page.wait_for_function("() => document.querySelector('#memory-notice').textContent.includes('未知')")
            assert await page.locator('#memory-submit').is_disabled()
            count = len(state['posts'])
            await page.locator('#memory-query').click()
            await page.wait_for_timeout(100)
            assert len(state['posts']) == count
            assert await page.locator('#memory-content').input_value() == '未知送达时保持原审核'
            checks.append('unknown_delivery_preserves_draft_and_queries_without_repost')
            state['unauthorized'] = True
            await page.locator('#memory-refresh-history').click()
            await page.wait_for_function("() => !document.querySelector('#memory-login').hidden")
            assert await page.locator('#memory-content').input_value() == ''
            checks.append('unauthorized_clears_credential_and_content')
            await context.close()

            # A fresh context uses the actual API; no browser decision writes occur here.
            context = await browser.new_context(viewport={'width': 1440, 'height': 1000})
            page = await context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            config = json.loads((ROOT / 'var/memory-review-http/memory.json').read_text())
            await page.goto(BASE + '/workbench/memories.html')
            await page.locator('#memory-token').fill(config['credentials'][0]['token'])
            await page.locator('#memory-connect').click()
            await page.locator('#memory-history button').first.wait_for()
            await page.locator('#memory-history button').first.click()
            if DELETION:
                await page.wait_for_function("() => document.querySelector('#memory-context').textContent.includes('删除中，等待索引核对')")
                assert await page.locator('#memory-content').input_value() == ''
            else:
                await page.wait_for_function("() => document.querySelector('#memory-content').value.includes('HTTP_REVIEW_')")
            assert await page.locator('#memory-content').evaluate('(element)=>element.readOnly')
            assert await page.locator('#memory-submit').is_disabled()
            assert await page.locator('#memory-token').input_value() == ''
            await page.screenshot(path=str(OUT / 'real-desktop.png'), full_page=True)
            await page.set_viewport_size({'width': 390, 'height': 844})
            assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            await page.screenshot(path=str(OUT / 'real-mobile.png'), full_page=True)
            checks += [('real_authenticated_pending_deletion_has_no_content' if DELETION else 'real_authenticated_committed_record_visible'),
                       'real_record_readonly_and_credential_not_in_input']
            await page.locator('#memory-logout').click()
            assert await page.locator('#memory-content').input_value() == ''
            await context.close()
            assert not errors, errors
            report = {'verified': True, 'checks': checks, 'browserErrors': errors,
                      'realWritesInBrowser': 0, 'mockBehaviorExplicitlySeparate': True}
            (OUT / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(json.dumps(report, ensure_ascii=False))
        finally:
            await browser.close()


if __name__ == '__main__':
    asyncio.run(main())
