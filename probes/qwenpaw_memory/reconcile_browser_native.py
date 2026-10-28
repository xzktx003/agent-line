"""Real main-service browser reconciliation; no fixture routes or model calls."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import time

from playwright.async_api import async_playwright

from agent_line.store import TaskStore
from probes.qwenpaw_memory.native_roundtrip import save
from probes.qwenpaw_memory.reconcile_native import execution_hash
from probes.qwenpaw_memory.reconcile_browser import CHROME

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / 'var/memory-reconciliation-20261022'


async def main(execute=False):
    source = json.loads((DATA / 'native-receipt.json').read_text())
    assert source['phase'] == 'native_written_confirmation_pending'
    C = json.loads((ROOT / 'var/adapter/service.json').read_text())
    M = json.loads(Path(C['memoryFile']).read_text())
    token = next(c['token'] for c in M['credentials'] if 'native-memory-roundtrip' in c['projectScopes'])
    store = TaskStore(Path(C['database']))
    task_id = source['taskId']
    assert execution_hash(store.get(task_id)) == source['executionSha256']
    state_path = DATA / 'real-browser-state.json'
    existing = state_path.exists()
    state = json.loads(state_path.read_text()) if existing else {'phase': 'prepared', 'taskId': task_id}
    if not execute or existing:
        print(json.dumps({'mode': 'observation_only', 'newReconciliationRequests': 0,
                          'memoryStatus': store.get(task_id)['memoryStatus'], 'taskId': task_id}))
        return
    assert store.get(task_id)['memoryStatus'] == 'pending'
    out = DATA / 'browser-native'
    out.mkdir(mode=0o700)
    save(state_path, state)
    posts, errors = [], []
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=CHROME, args=['--no-sandbox'])
        try:
            context = await browser.new_context(viewport={'width': 1440, 'height': 1000})
            page = await context.new_page()
            page.on('pageerror', lambda error: errors.append(type(error).__name__))

            def request(req):
                if req.method == 'POST' and '/extension/v1/' in req.url:
                    assert req.url.endswith('/native-memory/' + task_id + '/reconcile')
                    assert req.post_data_json == {}
                    posts.append('original_memory_reconcile')

            page.on('request', request)
            await page.goto('http://10.30.0.28:3193/workbench/memories.html')
            await page.locator('#memory-token').fill(token)
            await page.locator('#memory-connect').click()
            await page.wait_for_function("() => !document.querySelector('#memory-binding').disabled")
            await page.locator('#memory-binding').select_option('native-memory')
            await page.locator('#memory-sources button[data-task-id="' + task_id + '"]').click()
            await page.wait_for_function("() => !document.querySelector('#memory-query').disabled")
            assert '等待核对' in await page.locator('#memory-context').inner_text()
            assert await page.locator('#memory-content').evaluate('(e) => e.readOnly')
            assert await page.locator('#memory-submit').is_disabled()
            assert await page.locator('#memory-token').input_value() == ''
            for width, height, name in [(1440, 1000, 'desktop'), (390, 844, 'mobile')]:
                await page.set_viewport_size({'width': width, 'height': height})
                assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                await page.screenshot(path=str(out / f'pending-{name}.png'), full_page=True)
            state.update(phase='reconciliation_intent', clickedAt=time.time())
            save(state_path, state)
            await page.locator('#memory-reconcile').click()
            await page.wait_for_function("() => document.querySelector('#memory-context').textContent.includes('已写回并读回核对')")
            assert len(posts) == 1 and await page.locator('#memory-reconcile').is_hidden()
            assert await page.locator('#memory-submit').is_disabled()
            assert hashlib.sha256((await page.locator('#memory-content').input_value()).strip().encode()).hexdigest() == source['contentSha256']
            for width, height, name in [(1440, 1000, 'desktop'), (390, 844, 'mobile')]:
                await page.set_viewport_size({'width': width, 'height': height})
                assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                await page.screenshot(path=str(out / f'committed-{name}.png'), full_page=True)
            await page.locator('#memory-logout').click()
            assert await page.locator('#memory-content').input_value() == ''
            assert await page.locator('#memory-reconcile').is_hidden()
            assert await page.evaluate('localStorage.length === 0 && sessionStorage.length === 0')
            assert not errors
            current = store.get(task_id)
            assert current['memoryStatus'] == 'committed' and execution_hash(current) == source['executionSha256']
            state.update(phase='real_browser_reconciled', verified=True, actualReconcilePOSTs=len(posts),
                         mockResponses=0, newCodingSubmissions=0, browserErrors=errors,
                         memoryStatus=current['memoryStatus'], executionUnchanged=True, completedAt=time.time())
            save(state_path, state)
            print(json.dumps({k: v for k, v in state.items() if k != 'clickedAt'}))
            await context.close()
        finally:
            await browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute', action='store_true')
    asyncio.run(main(parser.parse_args().execute))
