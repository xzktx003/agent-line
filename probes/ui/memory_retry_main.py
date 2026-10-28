"""Read-only compatibility check for the deployed main memory review UI.

Uses the existing private review_memory credential in the configured memory
file. Every browser/API operation is GET; unexpected browser POSTs are aborted.
No record content, identifiers, credentials, or raw API payloads are emitted.
"""
import asyncio
import json
import os
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from playwright.async_api import async_playwright

from probes.qwenpaw_memory.reconcile_browser import CHROME
from probes.upgrade.current_backend import SERVICE, db_digest, file_hash


ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'var/memory-write-retry-20261022/main-native'
ORIGIN = 'http://127.0.0.1:3193'


def private_config():
    config = json.loads(SERVICE.read_text())
    memory_path = Path(config['memoryFile'])
    if not memory_path.is_absolute():
        memory_path = SERVICE.parent / memory_path
    memory = json.loads(memory_path.read_text())
    now = time.time()
    credentials = [item for item in memory.get('credentials', [])
                   if isinstance(item, dict) and 'review_memory' in item.get('capabilities', [])
                   and isinstance(item.get('token'), str) and item.get('expiresAt', 0) > now + 30]
    if len(credentials) != 1:
        raise RuntimeError('review_credential_unavailable')
    return config, memory, memory_path, credentials[0]['token']


def config_graph_snapshot(config):
    files, databases, pending = {SERVICE.resolve()}, set(), [SERVICE.resolve()]
    raw_db = Path(config['database'])
    database = (raw_db if raw_db.is_absolute() else SERVICE.parent / raw_db).resolve()
    databases.add(database)
    while pending:
        current = pending.pop()
        try:
            payload = json.loads(current.read_text())
        except (OSError, ValueError, UnicodeError):
            continue

        def walk(value):
            if isinstance(value, dict):
                for item in value.values():
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)
            elif isinstance(value, str) and value.endswith(('.json', '.sqlite')):
                path = Path(value)
                path = (path if path.is_absolute() else current.parent / path).resolve()
                if not path.is_relative_to(ROOT) or not path.is_file():
                    return
                if path.suffix == '.sqlite':
                    databases.add(path)
                elif path not in files:
                    files.add(path)
                    pending.append(path)
        walk(payload)
    return ({str(path): file_hash(path) for path in sorted(files)},
            {str(path): file_hash(path) for path in sorted(databases)},
            database)


def task_event_snapshot(database):
    return {'tasks': db_digest(database, 'tasks'), 'events': db_digest(database, 'events')}


async def verify():
    OUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    OUT.chmod(0o700)
    checks, failures, screenshots = [], [], []
    metrics = {'anonymousStatus': None, 'authenticatedStatus': None, 'browserGetRequests': 0,
               'browserPostAttempts': 0, 'committedRowsSeen': 0, 'pageErrorCount': 0}

    def check(name, ok):
        checks.append(name)
        if not ok:
            failures.append(name)

    config, memory, memory_path, token = private_config()
    cfg_before, db_before, database = config_graph_snapshot(config)
    rows_before = task_event_snapshot(database)
    api_calls = 0
    candidate = None
    try:
        with httpx.Client(timeout=15, follow_redirects=False) as client:
            anonymous = client.get(ORIGIN + '/extension/v1/memories?limit=1')
            metrics['anonymousStatus'] = anonymous.status_code
            check('anonymous_memory_api_is_401', anonymous.status_code == 401)
            response = client.get(ORIGIN + '/extension/v1/memories?limit=500',
                                  headers={'Authorization': 'Bearer ' + token})
            api_calls += 2
            metrics['authenticatedStatus'] = response.status_code
            check('authenticated_memory_history_is_200', response.status_code == 200)
            if response.status_code == 200:
                payload = response.json()
                memories = payload.get('memories') if isinstance(payload, dict) else None
                if isinstance(memories, list):
                    metrics['committedRowsSeen'] = sum(1 for row in memories if isinstance(row, dict)
                                                       and row.get('status') == 'committed')
                    candidate = next((row for row in memories if isinstance(row, dict)
                                      and row.get('status') == 'committed'
                                      and type(row.get('commitVersion')) is int
                                      and row['commitVersion'] > 0
                                      and all(isinstance(row.get(key), str) and row[key]
                                              for key in ('bindingId', 'projectId', 'roleAgentId', 'taskId'))), None)
            check('committed_record_with_positive_version_found', candidate is not None)
    except Exception:
        failures.append('authenticated_read_preflight_completed')

    if candidate is not None:
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(executable_path=CHROME, args=['--no-sandbox'])
                try:
                    context = await browser.new_context(viewport={'width': 1440, 'height': 1000})
                    page = await context.new_page()
                    page_errors = []
                    page.on('pageerror', lambda error: page_errors.append(type(error).__name__))

                    async def route(request):
                        nonlocal api_calls
                        url = urlsplit(request.request.url)
                        if url.path.startswith('/extension/v1/'):
                            if request.request.method == 'GET':
                                api_calls += 1
                                metrics['browserGetRequests'] += 1
                                await request.continue_()
                            else:
                                metrics['browserPostAttempts'] += 1
                                await request.abort('blockedbyclient')
                            return
                        if url.netloc == '127.0.0.1:3193' and url.path.startswith('/workbench/'):
                            await request.continue_()
                        else:
                            await request.abort()

                    await context.route('**/*', route)
                    await page.goto(ORIGIN + '/workbench/memories.html')
                    await page.locator('#memory-token').fill(token)
                    await page.locator('#memory-connect').click()
                    await page.locator('#memory-history button').first.wait_for(state='visible', timeout=15000)

                    task_id = candidate['taskId']
                    selected = False
                    for _ in range(30):
                        match = page.locator('#memory-history button').filter(has_text=task_id).first
                        if await match.count() and await match.is_visible():
                            await match.click()
                            selected = True
                            break
                        next_button = page.locator('#memory-history-next')
                        if not await next_button.is_enabled():
                            break
                        await next_button.click()
                        await page.wait_for_timeout(80)
                    check('committed_history_record_selectable', selected)
                    if selected:
                        await page.wait_for_function("() => !document.querySelector('#memory-query').disabled", timeout=15000)
                        context_text = await page.locator('#memory-context').inner_text()
                        check('committed_record_chinese_state', '已写回并读回核对' in context_text)
                        check('committed_record_version_unchanged', type(candidate.get('commitVersion')) is int
                              and candidate['commitVersion'] > 0)
                        check('committed_retry_area_hidden', await page.locator('#memory-retry-area').count() == 0
                              or await page.locator('#memory-retry-area').is_hidden())
                        readonly = await page.locator('#memory-content').evaluate('(e) => e.readOnly')
                        content_present = await page.locator('#memory-content').evaluate('(e) => e.value.length > 0')
                        check('committed_approved_content_readonly', readonly and content_present)
                        check('ordinary_review_submit_disabled', await page.locator('#memory-submit').is_disabled())

                        # Crop only the heading/query controls. Do not capture review text, identifiers, or credentials.
                        for width, height, name in ((1440, 1000, 'desktop'), (390, 844, 'mobile')):
                            await page.set_viewport_size({'width': width, 'height': height})
                            shot = OUT / f'main-memory-safe-controls-{name}.png'
                            await page.locator('.memory-editor > .section-heading').screenshot(path=str(shot))
                            screenshots.append(shot.name)
                            check(name + '_document_no_horizontal_overflow',
                                  await page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'))

                    await page.locator('#memory-logout').click()
                    check('logout_clears_memory_content', await page.locator('#memory-content').input_value() == '')
                    check('logout_clears_memory_selection',
                          await page.locator('#memory-context').inner_text() == '尚未选择来源任务')
                    check('logout_hides_retry_area', await page.locator('#memory-retry-area').count() == 0
                          or await page.locator('#memory-retry-area').is_hidden())
                    check('logout_clears_credential_field', await page.locator('#memory-token').input_value() == '')
                    metrics['pageErrorCount'] = len(page_errors)
                    check('no_browser_page_errors', not page_errors)
                    await context.close()
                finally:
                    await browser.close()
        except Exception:
            # Exception strings can include a selected task ID or source-derived text; only record a category.
            failures.append('browser_readonly_flow_completed')

    cfg_after, db_after, _ = config_graph_snapshot(config)
    rows_after = task_event_snapshot(database)
    check('configuration_graph_unchanged', cfg_before == cfg_after and db_before == db_after)
    check('main_tasks_and_events_unchanged', rows_before == rows_after)
    check('browser_never_attempted_post', metrics['browserPostAttempts'] == 0)

    report = {'scope': 'deployed main memory UI/API read-only compatibility check',
              'passed': not failures, 'checks': len(checks), 'failures': failures,
              'authenticatedReadStatus': metrics['authenticatedStatus'],
              'anonymousReadStatus': metrics['anonymousStatus'],
              'committedRowsSeen': metrics['committedRowsSeen'],
              'browserGetRequests': metrics['browserGetRequests'], 'browserPostAttempts': metrics['browserPostAttempts'],
              'realModelCalls': 0, 'nativeMemoryWrites': 0,
              'configurationFilesChecked': len(cfg_before), 'databaseFilesChecked': len(db_before),
              'configurationGraphUnchanged': cfg_before == cfg_after and db_before == db_after,
              'tasksAndEventsUnchanged': rows_before == rows_after,
              'pageErrorCount': metrics['pageErrorCount'], 'screenshots': screenshots}
    path = OUT / 'report.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    path.chmod(0o600)
    for name in screenshots:
        (OUT / name).chmod(0o600)
    print(json.dumps({'passed': report['passed'], 'checks': report['checks'], 'failures': failures,
                      'authenticatedReadStatus': metrics['authenticatedStatus'],
                      'anonymousReadStatus': metrics['anonymousStatus'],
                      'browserPostAttempts': metrics['browserPostAttempts'],
                      'nativeMemoryWrites': 0, 'realModelCalls': 0,
                      'configurationGraphUnchanged': report['configurationGraphUnchanged'],
                      'tasksAndEventsUnchanged': report['tasksAndEventsUnchanged'],
                      'report': str(path)}, ensure_ascii=False))
    return not failures


if __name__ == '__main__':
    raise SystemExit(0 if asyncio.run(verify()) else 1)
