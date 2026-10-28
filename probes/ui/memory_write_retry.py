"""Browser fixture regression for explicit, versioned memory-write retry.

All /extension/v1/memories traffic is intercepted. No real memory write,
execution submission, authenticated API, or model request is made.
"""
import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import unquote, urlsplit

from playwright.async_api import async_playwright

from probes.qwenpaw_memory.reconcile_browser import CHROME

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'var/memory-write-retry-20261022'
ORIGIN = 'http://127.0.0.1:3193'
TOKEN = 'fixture-memory-retry-only-not-a-real-credential'
CONTENT = '只保留原审核批准的经验内容；禁止生成新审核正文。'

BINDINGS = [
    {'bindingId': 'fixture-role-a', 'projectId': 'fixture-project-a', 'roleBindingId': 'fixture-binding-a',
     'roleAgentId': 'fixture-agent-a'},
    {'bindingId': 'fixture-role-b', 'projectId': 'fixture-project-b', 'roleBindingId': 'fixture-binding-b',
     'roleAgentId': 'fixture-agent-b'},
]


def source(binding, task_id):
    return {'taskId': task_id, 'projectId': binding['projectId'], 'roleAgentId': binding['roleAgentId'],
            'executionStatus': 'succeeded', 'verificationStatus': 'passed', 'resourcesReleased': True,
            'workflowRunId': 'fixture-run-' + task_id, 'workflow': {'runId': 'fixture-run-' + task_id},
            'outputPreview': 'fixture source result', 'evidencePreview': 'fixture evidence',
            'outputTruncated': False, 'evidenceTruncated': False}


def record(binding, task_id, *, status='failed', reason='official_write_rejected', version=1,
           content=CONTENT, deletion=None):
    value = {'bindingId': binding['bindingId'], 'projectId': binding['projectId'],
             'roleBindingId': binding['roleBindingId'], 'roleAgentId': binding['roleAgentId'],
             'taskId': task_id, 'actor': '模拟审核人', 'decisionId': 'fixture-original-decision-' + task_id,
             'content': content, 'contentSha256': 'c' * 64, 'status': status, 'reason': reason,
             'commitVersion': version, 'createdAt': 1792656000,
             'source': {'executionStatus': 'succeeded', 'verificationStatus': 'passed',
                        'evidenceSha256': 'e' * 64, 'workflow': {'runId': 'fixture-run-' + task_id}},
             'events': []}
    if deletion is not None:
        value['deletion'] = {'status': deletion}
    return value


async def run(baseline=False, visual_only=False):
    OUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    OUT.chmod(0o700)
    prefix = 'baseline-' if baseline else 'visual-current-' if visual_only else ''
    tasks = {}
    records = {}
    for binding in BINDINGS:
        for task_id in ['retry-main', 'retry-pending', 'retry-intent', 'retry-deleting', 'retry-deleted',
                        'retry-bad-version', 'retry-missing-version', 'retry-zero-version', 'retry-negative-version',
                        'retry-bad-reason', 'retry-missing-content', 'retry-mismatch-identity', 'retry-other']:
            task = source(binding, task_id)
            tasks[(binding['bindingId'], task_id)] = task
            records[(binding['bindingId'], task_id)] = record(binding, task_id)
    records[(BINDINGS[0]['bindingId'], 'retry-pending')].update(status='pending', reason='readback_unknown')
    records[(BINDINGS[0]['bindingId'], 'retry-intent')].update(status='intent', reason='submission_uncertain')
    records[(BINDINGS[0]['bindingId'], 'retry-deleting')].update(status='failed', deletion='deleting')
    records[(BINDINGS[0]['bindingId'], 'retry-deleted')].update(status='failed', deletion='deleted')
    records[(BINDINGS[0]['bindingId'], 'retry-bad-version')]['commitVersion'] = True
    records[(BINDINGS[0]['bindingId'], 'retry-missing-version')].pop('commitVersion')
    records[(BINDINGS[0]['bindingId'], 'retry-zero-version')]['commitVersion'] = 0
    records[(BINDINGS[0]['bindingId'], 'retry-negative-version')]['commitVersion'] = -1
    records[(BINDINGS[0]['bindingId'], 'retry-bad-reason')]['reason'] = 'readback_unknown'
    records[(BINDINGS[0]['bindingId'], 'retry-missing-content')].pop('content')
    records[(BINDINGS[0]['bindingId'], 'retry-mismatch-identity')]['roleAgentId'] = 'other-fixture-role'

    state = {'records': records, 'retry_mode': 'timeout', 'retry_requests': [],
             'reconcile_requests': [], 'review_requests': [], 'other_posts': [],
             'retry_arrived': asyncio.Event(), 'retry_release': asyncio.Event(),
             'late_retry': False, 'mismatch_field': 'taskId', 'retry_response_version': 9,
             'unexpected': [], 'requests': [], 'errors': []}
    checks, failures, screenshots = [], [], []

    def check(name, ok, actual=None):
        checks.append(name)
        if not ok:
            failures.append({'check': name, **({'actual': actual} if actual is not None else {})})

    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=CHROME, args=['--no-sandbox'])
        try:
            context = await browser.new_context(viewport={'width': 1440, 'height': 1000})
            page = await context.new_page()
            page.on('pageerror', lambda error: state['errors'].append(str(error)))

            async def route(r):
                req = r.request
                url = urlsplit(req.url)
                path = unquote(url.path)
                if baseline and url.netloc == '127.0.0.1:3193':
                    asset = {'/workbench/memories.html': 'memories.html', '/workbench/memories.js': 'memories.js',
                             '/workbench/memories.css': 'memories.css', '/workbench/styles.css': 'styles.css'}.get(path)
                    if asset:
                        body = (OUT / 'baseline-assets' / asset).read_bytes()
                        content_type = 'text/javascript' if asset.endswith('.js') else 'text/css' if asset.endswith('.css') else 'text/html'
                        await r.fulfill(status=200, body=body, content_type=content_type)
                        return
                prefix_path = '/extension/v1/memories'
                if path.startswith(prefix_path):
                    suffix = path[len(prefix_path):]
                    state['requests'].append((req.method, suffix))
                    if req.method == 'GET':
                        if suffix == '/bindings':
                            return await r.fulfill(json={'bindings': BINDINGS})
                        if suffix.startswith('/sources/'):
                            parts = suffix.split('/')
                            binding_id, task_id = parts[2], parts[3]
                            item = tasks.get((binding_id, task_id))
                            if item is None:
                                return await r.fulfill(status=404, json={'detail': 'fixture source absent'})
                            return await r.fulfill(json=item)
                        if suffix == '/sources':
                            binding_id = urlsplit(req.url).query.split('bindingId=', 1)[-1].split('&', 1)[0]
                            binding_id = unquote(binding_id)
                            rows = [v for (b, _), v in tasks.items() if b == binding_id]
                            return await r.fulfill(json={'tasks': rows, 'nextCursor': None})
                        if suffix == '':
                            return await r.fulfill(json={'memories': list(records.values()), 'nextCursor': None})
                        if suffix.count('/') == 2:
                            _, binding_id, task_id = suffix.split('/', 2)
                            item = records.get((binding_id, task_id))
                            if item is None:
                                return await r.fulfill(status=404, json={'detail': 'fixture record absent'})
                            return await r.fulfill(json=item)
                        state['unexpected'].append((req.method, suffix))
                        return await r.fulfill(status=404, json={'detail': 'unknown fixture GET'})
                    try:
                        body = req.post_data_json
                    except Exception:
                        body = None
                    if suffix.endswith('/retry'):
                        state['retry_requests'].append({'suffix': suffix, 'body': body})
                        if state['retry_mode'] == 'timeout':
                            if state['late_retry']:
                                state['retry_arrived'].set()
                                await state['retry_release'].wait()
                                return await r.fulfill(json=records[(BINDINGS[0]['bindingId'], 'retry-main')])
                            return await r.abort('connectionreset')
                        if state['retry_mode'] == 'mismatch':
                            item = dict(records[(BINDINGS[0]['bindingId'], 'retry-main')])
                            item.update(status='committed')
                            item[state['mismatch_field']] = 'another-fixture-identity'
                            return await r.fulfill(json=item)
                        if state['retry_mode'] == 'failed':
                            item = dict(records[(BINDINGS[0]['bindingId'], 'retry-main')])
                            item.update(status='failed', reason='official_write_rejected',
                                         commitVersion=state['retry_response_version'])
                            records[(BINDINGS[0]['bindingId'], 'retry-main')] = item
                            return await r.fulfill(json=item)
                        if state['retry_mode'] == 'pending':
                            item = dict(records[(BINDINGS[0]['bindingId'], 'retry-main')])
                            item.update(status='pending', reason='adapter_outcome_unknown',
                                        commitVersion=(body or {}).get('expectedVersion', 0) + 1)
                            records[(BINDINGS[0]['bindingId'], 'retry-main')] = item
                            return await r.fulfill(json=item)
                        if state['retry_mode'] == 'committed':
                            item = dict(records[(BINDINGS[0]['bindingId'], 'retry-main')])
                            item.update(status='committed', reason='official_readback_matches',
                                        commitVersion=(body or {}).get('expectedVersion', 0) + 1)
                            records[(BINDINGS[0]['bindingId'], 'retry-main')] = item
                            return await r.fulfill(json=item)
                        state['unexpected'].append((req.method, suffix, body))
                        return await r.fulfill(status=400, json={'detail': 'no fixture mode'})
                    if suffix.endswith('/reconcile'):
                        state['reconcile_requests'].append({'suffix': suffix, 'body': body})
                        binding_id, task_id = suffix.strip('/').split('/')[:2]
                        return await r.fulfill(json=records[(binding_id, task_id)])
                    if suffix.endswith('/review'):
                        state['review_requests'].append(suffix)
                    state['other_posts'].append({'suffix': suffix, 'body': body})
                    return await r.fulfill(status=405, json={'detail': 'fixture blocks writes'})
                if url.netloc == '127.0.0.1:3193' and path.startswith('/workbench/'):
                    return await r.continue_()
                return await r.abort()

            await context.route('**/*', route)
            await page.goto(ORIGIN + '/workbench/memories.html')
            await page.locator('#memory-token').fill(TOKEN)
            await page.locator('#memory-connect').click()
            try:
                await page.locator('#memory-sources button').first.wait_for(state='visible', timeout=7000)
            except Exception:
                print(json.dumps({'diagnostic': 'source rows absent', 'requests': state['requests'],
                    'errors': state['errors'], 'notice': await page.locator('#memory-notice').inner_text(),
                    'sourcesMessage': await page.locator('#memory-sources-message').inner_text(),
                    'unexpected': state['unexpected'], 'binding': await page.locator('#memory-binding').input_value(),
                    'bindingOptions': await page.locator('#memory-binding').locator('option').all_text_contents()}, ensure_ascii=False))
                raise
            if baseline:
                await page.locator('#memory-sources button').filter(has_text='retry-main').first.click()
                await page.wait_for_function("() => !document.querySelector('#memory-query').disabled")
                for width, height, name in ((1440, 1000, 'desktop'), (390, 844, 'mobile')):
                    await page.set_viewport_size({'width': width, 'height': height})
                    shot = OUT / f'baseline-selected-memory-retry-{name}.png'
                    await page.screenshot(path=str(shot), full_page=True)
                    screenshots.append(shot.name)
                    detail = OUT / f'baseline-memory-retry-detail-{name}.png'
                    await page.locator('.memory-editor').screenshot(path=str(detail))
                    screenshots.append(detail.name)
                    check(name + '_old_ui_no_overflow', await page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'))
                check('baseline_has_no_retry_ui', await page.locator('#memory-retry-area').count() == 0)
                check('baseline_no_mutations', not state['retry_requests'] and not state['other_posts'])
            elif visual_only:
                await page.locator('#memory-sources button').filter(has_text='retry-main').first.click()
                await page.wait_for_function("() => !document.querySelector('#memory-query').disabled")
                check('visual_current_failed_retry_visible', await page.locator('#memory-retry-area').is_visible()
                      and '记忆写回失败' in await page.locator('#memory-context').inner_text())
                for width, height, name in ((1440, 1000, 'desktop'), (390, 844, 'mobile')):
                    await page.set_viewport_size({'width': width, 'height': height})
                    detail = OUT / f'memory-retry-detail-{name}.png'
                    await page.locator('.memory-editor').screenshot(path=str(detail))
                    screenshots.append(detail.name)
            else:
                await exercise(page, state, check, screenshots)
            check('no_review_posts', not state['review_requests'], state['review_requests'])
            check('no_unexpected_api', not state['unexpected'], state['unexpected'])
            check('no_page_errors', not state['errors'], state['errors'])
            check('all_calls_fixture_memory_get_or_post', all(m in ('GET', 'POST') for m, _ in state['requests']))
            await context.close()
        finally:
            state['retry_release'].set()
            await browser.close()

    report = {'scope': 'intercepted memory API fixtures only', 'baseline': baseline,
              'passed': not failures, 'checks': len(checks), 'failures': failures,
              'fixtureRequests': len(state['requests']), 'realAPIRequests': 0,
              'retryRequests': state['retry_requests'], 'reconcileRequests': state['reconcile_requests'],
              'reviewRequests': state['review_requests'], 'otherPosts': state['other_posts'],
              'nativeWrites': 0, 'modelCalls': 0, 'pageErrors': state['errors'], 'screenshots': screenshots}
    report_name = 'baseline-selected-report.json' if baseline else prefix + 'report.json'
    path = OUT / report_name
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    path.chmod(0o600)
    for name in screenshots:
        (OUT / name).chmod(0o600)
    print(json.dumps({'passed': report['passed'], 'checks': len(checks),
        'failedChecks': [x['check'] for x in failures], 'realAPIRequests': 0,
        'retryRequests': len(state['retry_requests']), 'report': str(path)}, ensure_ascii=False))
    return not failures


async def exercise(page, state, check, screenshots):
    async def select(task_id, binding_index=0):
        if binding_index:
            await page.locator('#memory-binding').select_option(BINDINGS[binding_index]['bindingId'])
            await page.wait_for_function("() => !document.querySelector('#memory-query').disabled")
        row = page.locator('#memory-sources button').filter(has_text=task_id).first
        await row.click()
        await page.wait_for_function("() => !document.querySelector('#memory-query').disabled")
        await page.locator('#memory-query').click()
        await page.wait_for_function("() => !document.querySelector('#memory-query').disabled")

    async def area_state():
        return await page.evaluate('''() => ({area: !!document.querySelector('#memory-retry-area'),
          hidden: !document.querySelector('#memory-retry-area') || document.querySelector('#memory-retry-area').hidden,
          label: document.querySelector('#memory-retry')?.textContent.trim() || '',
          disabled: document.querySelector('#memory-retry')?.disabled,
          context: document.querySelector('#memory-context')?.innerText || '',
          content: document.querySelector('#memory-content')?.value || '',
          readonly: !!document.querySelector('#memory-content')?.readOnly,
          submitDisabled: !!document.querySelector('#memory-submit')?.disabled,
          retryHelp: document.querySelector('#memory-retry-help')?.innerText || ''});''')

    await select('retry-main')
    s = await area_state()
    check('eligible_failed_retry_visible', s['area'] and not s['hidden'], s)
    check('retry_button_label', s['label'] == '重试记忆写回', s['label'])
    check('failed_label_keeps_source_execution_success', '记忆写回失败' in s['context'] and '执行成功' in s['context'], s['context'])
    check('original_approved_content_readonly', s['content'] == CONTENT and s['readonly'] and s['submitDisabled'], s)
    check('retry_help_explains_original_approved_content_and_no_new_coding',
          '原' in s['retryHelp'] and '已审核正文' in s['retryHelp'] and '编码任务' in s['retryHelp'], s['retryHelp'])
    check('reconcile_available_for_failed', await page.locator('#memory-reconcile').is_visible()
          and await page.locator('#memory-reconcile').is_enabled())

    for width, height, name in ((1440, 1000, 'desktop'), (390, 844, 'mobile')):
        await page.set_viewport_size({'width': width, 'height': height})
        shot = OUT / f'memory-retry-{name}.png'
        await page.screenshot(path=str(shot), full_page=True)
        screenshots.append(shot.name)
        check(name + '_no_horizontal_overflow', await page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'))
        check(name + '_buttons_touch_size', await page.locator('button:visible').evaluate_all(
            '(xs) => xs.every(e => e.getBoundingClientRect().height >= 44)'))
    await page.set_viewport_size({'width': 1440, 'height': 1000})

    button = page.locator('#memory-retry')
    await button.focus()
    check('retry_keyboard_focus_and_touch_height', await button.evaluate('(e) => document.activeElement === e && e.getBoundingClientRect().height >= 44'))
    state['retry_mode'] = 'timeout'
    await button.click()
    await page.wait_for_timeout(150)
    check('retry_posts_expected_version_only', len(state['retry_requests']) == 1
          and state['retry_requests'][0]['body'] == {'expectedVersion': 1}, state['retry_requests'])
    check('timeout_stays_failed_and_disables_same_version', (await area_state())['disabled'] is True)
    await page.locator('#memory-query').click()
    await page.wait_for_function("() => !document.querySelector('#memory-query').disabled")
    check('requery_old_failed_version_does_not_resubmit', len(state['retry_requests']) == 1
          and (await area_state())['disabled'] is True)

    # Reconciliation is the existing observation operation and remains available for failed writes.
    await page.locator('#memory-reconcile').click()
    await page.wait_for_function("() => !document.querySelector('#memory-query').disabled")
    check('failed_reconcile_is_empty_body_observation', len(state['reconcile_requests']) == 1
          and state['reconcile_requests'][0]['body'] == {}, state['reconcile_requests'])

    # A new server failure version permits a deliberate attempt; every identity mismatch is rejected.
    key = (BINDINGS[0]['bindingId'], 'retry-main')
    mismatch_fields = ['bindingId', 'taskId', 'roleAgentId', 'projectId', 'decisionId', 'contentSha256']
    for version, field in enumerate(mismatch_fields, start=2):
        state['records'][key] = record(BINDINGS[0], 'retry-main', version=version)
        await page.locator('#memory-query').click()
        await page.wait_for_function("() => !document.querySelector('#memory-query').disabled")
        s = await area_state()
        check(f'failed_version_{version}_permits_manual_retry', not s['disabled'], s)
        state['retry_mode'] = 'mismatch'
        state['mismatch_field'] = field
        await page.locator('#memory-retry').click()
        await page.wait_for_function("() => !document.querySelector('#memory-query').disabled")
        s = await area_state()
        check(f'mismatch_{field}_sent_version', state['retry_requests'][-1]['body'] == {'expectedVersion': version})
        check(f'mismatch_{field}_not_accepted', '已写回并读回核对' not in s['context'] and s['disabled'] is True, s)
        check(f'mismatch_{field}_attempt_not_repeated', len(state['retry_requests']) == version)

    # Invalid states, schemas, deletion markers, and mismatched source identities cannot retry.
    for task_id in ['retry-pending', 'retry-intent', 'retry-deleting', 'retry-deleted',
                    'retry-bad-version', 'retry-missing-version', 'retry-zero-version',
                    'retry-negative-version', 'retry-bad-reason',
                    'retry-mismatch-identity']:
        await select(task_id)
        s = await area_state()
        check(task_id + '_retry_hidden', not s['area'] or s['hidden'], s)
        check(task_id + '_review_stays_readonly', s['readonly'] and s['submitDisabled'], s)

    await select('retry-main')
    await page.locator('#memory-binding').select_option(BINDINGS[1]['bindingId'])
    await page.locator('#memory-sources button').first.wait_for(state='visible')
    check('identity_switch_clears_old_content', await page.locator('#memory-content').input_value() == ''
          and await page.locator('#memory-context').inner_text() == '尚未选择来源任务')
    await page.locator('#memory-binding').select_option(BINDINGS[0]['bindingId'])
    await page.locator('#memory-sources button').first.wait_for(state='visible')

    # Failure returned from an attempt is still failed; a new server version can be explicitly retried.
    await select('retry-main')
    state['records'][key] = record(BINDINGS[0], 'retry-main', version=8)
    await page.locator('#memory-query').click()
    await page.wait_for_function("() => !document.querySelector('#memory-query').disabled")
    state['retry_mode'] = 'failed'
    state['retry_response_version'] = 9
    await page.locator('#memory-retry').click()
    await page.wait_for_function("() => !document.querySelector('#memory-query').disabled")
    check('failed_retry_receipt_preserves_failure', '记忆写回失败' in (await area_state())['context'])
    await page.locator('#memory-query').click()
    await page.wait_for_function("() => !document.querySelector('#memory-query').disabled")
    check('failed_retry_new_version_can_be_manual', (await area_state())['disabled'] is False)
    check('retry_version_eight_sent', state['retry_requests'][-1]['body'] == {'expectedVersion': 8})

    # Pending is observation-only; committed is terminal and hides all retry UI.
    state['records'][key] = record(BINDINGS[0], 'retry-main', version=10)
    await page.locator('#memory-query').click()
    await page.wait_for_function("() => !document.querySelector('#memory-query').disabled")
    state['retry_mode'] = 'pending'
    await page.locator('#memory-retry').click()
    await page.wait_for_function("() => !document.querySelector('#memory-query').disabled")
    s = await area_state()
    check('pending_retry_response_observation_only', '等待核对' in s['context'] and s['hidden'], s)
    check('pending_does_not_retry_again', len(state['retry_requests']) == 9)
    check('pending_reconcile_available', await page.locator('#memory-reconcile').is_visible())
    await page.locator('#memory-reconcile').click()
    await page.wait_for_function("() => !document.querySelector('#memory-query').disabled")
    check('pending_reconcile_empty_body', state['reconcile_requests'][-1]['body'] == {})

    state['records'][key] = record(BINDINGS[0], 'retry-main', version=12)
    await page.locator('#memory-query').click()
    await page.wait_for_function("() => !document.querySelector('#memory-query').disabled")
    state['retry_mode'] = 'committed'
    await page.locator('#memory-retry').click()
    await page.wait_for_function("() => !document.querySelector('#memory-query').disabled")
    s = await area_state()
    check('committed_retry_receipt_hides_retry', s['hidden'] and '已写回并读回核对' in s['context'], s)
    check('committed_does_not_allow_new_retry', len(state['retry_requests']) == 10)

    # Log out during an in-flight retry: response must not restore the approved content/state.
    await select('retry-main')
    state['records'][key] = record(BINDINGS[0], 'retry-main', version=13)
    await page.locator('#memory-query').click()
    await page.wait_for_function("() => !document.querySelector('#memory-query').disabled")
    state['retry_mode'] = 'timeout'
    state['late_retry'] = True
    state['retry_arrived'] = asyncio.Event()
    state['retry_release'] = asyncio.Event()
    await page.locator('#memory-query').click()
    await page.wait_for_function("() => !document.querySelector('#memory-query').disabled")
    await page.locator('#memory-retry').click()
    await asyncio.wait_for(state['retry_arrived'].wait(), timeout=5)
    await page.locator('#memory-logout').click()
    state['retry_release'].set()
    await page.wait_for_timeout(150)
    check('logout_clears_content', await page.locator('#memory-content').input_value() == '')
    check('late_retry_cannot_restore_state_or_content', await page.locator('#memory-content').input_value() == ''
          and await page.locator('#memory-context').inner_text() == '尚未选择来源任务')
    check('logout_clears_token', await page.locator('#memory-token').input_value() == '')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', action='store_true')
    parser.add_argument('--visual-only', action='store_true', help='capture the current selected failed record without exercising retry')
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(run(args.baseline, args.visual_only)) else 1)

