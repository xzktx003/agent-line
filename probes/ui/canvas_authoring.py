"""Real browser pointer/keyboard canvas checks; all service calls intercepted."""
import asyncio
import copy
import json
import mimetypes
from pathlib import Path
from urllib.parse import urlsplit
from playwright.async_api import async_playwright, expect
from agent_line.sim_authoring import compile_document
from probes.sim.publication_capability_browser import CHROME
from probes.qwenpaw_memory.native_roundtrip import save

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'var/canvas-authoring-20261022'


async def scenario(browser, width):
    context = await browser.new_context(viewport={'width': width, 'height': 1000})
    saved = {'schemaVersion': 1, 'nodes': [
        {'id': 'start', 'type': 'start', 'name': '开始', 'position': {'x': 50, 'y': 110}, 'config': {}},
        {'id': 'end', 'type': 'end', 'name': '结束', 'position': {'x': 410, 'y': 110}, 'config': {'output': {}}}],
        'edges': [{'id': 'edge', 'source': 'start', 'target': 'end'}], 'variables': {}}
    writes, errors = [], []

    async def route(r):
        nonlocal saved
        request = r.request
        parsed = urlsplit(request.url)
        assert parsed.netloc == 'canvas-fixture.test'
        path = parsed.path
        if path.startswith('/extension/v1/authoring/'):
            if request.method == 'POST':
                assert path.endswith('/flow/save')
                body = request.post_data_json
                compile_document(body['document'], 'flow')
                writes.append(copy.deepcopy(body))
                saved = copy.deepcopy(body['document'])
                data = {'projectId': 'fixture', 'operationId': body['operationId'], 'kind': 'save',
                        'status': 'accepted', 'receipt': {'workflowId': 'flow', 'outcome': 'saved',
                                                         'stateSha256': 'b'*64}}
            elif path.endswith('/projects'):
                data = {'projects': [{'id': 'fixture', 'name': '画布验证项目'}], 'canPublish': True}
            elif path.endswith('/roles'):
                data = {'roles': [], 'source': 'configured_existing_roles', 'nativeAvailabilityVerified': False}
            elif path.endswith('/workflows'):
                data = {'workflows': [{'id': 'flow', 'name': '画布操作验证', 'deploymentStatus': 'draft'}],
                        'nextCursor': None}
            else:
                assert request.method == 'GET' and path.endswith('/workflows/flow')
                data = {'workflowId': 'flow', 'name': '画布操作验证', 'stateSha256': 'a'*64,
                        'editable': True, 'document': copy.deepcopy(saved), 'draftEmpty': False}
            await r.fulfill(json=data)
            return
        file = (ROOT / 'frontend' / path.removeprefix('/workbench/')).resolve()
        assert path.startswith('/workbench/') and file.parent == ROOT / 'frontend'
        await r.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file)[0] or 'application/octet-stream')

    await context.route('**/*', route)
    page = await context.new_page()
    page.on('pageerror', lambda error: errors.append(str(error)))
    await page.goto('https://canvas-fixture.test/workbench/editor.html')
    await page.locator('#token').fill('fixture-only')
    await page.locator('#login').click()
    await page.locator('#project').select_option('fixture')
    await page.locator('.workflow-choice').click()
    await expect(page.locator('#save')).to_be_disabled()
    # Remove the original edge, then rebuild by native browser pointer gestures.
    await page.locator('.edge-row button').click()
    await page.locator('#node-type').select_option('human')
    await page.locator('#drag-node').drag_to(page.locator('#canvas'), target_position={'x': 120, 'y': 80})
    await expect(page.locator('[data-node-id]')).to_have_count(3)
    await page.locator('#node-type').select_option('condition')
    await page.locator('#add-node').click()
    await expect(page.locator('[data-node-id]')).to_have_count(4)
    await page.locator('#canvas-layout').click()
    await expect(page.locator('#notice')).to_contain_text('已自动整理节点位置')
    # Selection is by actual node identity, never screen order.
    ids = await page.locator('[data-node-id]').evaluate_all('es=>es.map(e=>({id:e.dataset.nodeId,label:e.getAttribute("aria-label")}))')
    human = next(item['id'] for item in ids if item['label'].startswith('人工确认'))
    condition = next(item['id'] for item in ids if item['label'].startswith('条件分支'))

    async def connect(source, target, branch=None):
        await page.locator('#edge-source').select_option(source)
        if branch is not None:
            await page.locator('#edge-branch').select_option(branch)
        output = page.locator(f'[data-node-id="{source}"] .flow-output')
        end = page.locator(f'[data-node-id="{target}"] rect')
        await output.scroll_into_view_if_needed()
        await end.scroll_into_view_if_needed()
        first, last = await output.bounding_box(), await end.bounding_box()
        assert first and last
        await page.mouse.move(first['x']+first['width']/2,first['y']+first['height']/2)
        await page.mouse.down()
        await page.mouse.move(last['x']+last['width']/2,last['y']+last['height']/2,steps=8)
        await page.mouse.up()

    await connect('start', human)
    await expect(page.locator('.edge-row')).to_have_count(1)
    await connect('start', human)  # Same outlet must not silently duplicate.
    await expect(page.locator('.edge-row')).to_have_count(1)
    await expect(page.locator('#notice')).to_contain_text('此出口已有连线')
    await connect(human, condition)
    rules = await page.locator('#edge-source').select_option(condition)
    rule = await page.locator('#edge-branch option').first.get_attribute('value')
    await connect(condition, 'end', rule)
    await connect(condition, 'end', 'else')
    await expect(page.locator('.edge-row')).to_have_count(4)
    # Layout may move nodes but must leave every configured branch untouched.
    await page.locator('#canvas-layout').click()
    await page.locator('#save').click()
    await expect(page.locator('#save')).to_be_disabled()
    assert len(writes) == 1 and len(saved['nodes']) == 4 and len(saved['edges']) == 4
    branch_edges = [edge for edge in saved['edges'] if edge['source'] == condition]
    assert {edge['branch'] for edge in branch_edges} == {rule, 'else'}
    before = copy.deepcopy(saved)
    await page.locator('#reload').click()
    await expect(page.locator('[data-node-id]')).to_have_count(4)
    assert len(writes) == 1 and saved == before
    await expect(page.locator('#save')).to_be_disabled()
    await page.locator('#canvas').scroll_into_view_if_needed()
    assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    OUT.mkdir(mode=0o700, exist_ok=True)
    screenshot = OUT / f'canvas-{width}.png'
    await page.screenshot(path=str(screenshot), full_page=True)
    screenshot.chmod(0o600)
    await page.locator('#logout').click()
    await expect(page.locator('[data-node-id]')).to_have_count(0)
    assert not errors, errors
    result = {'width': width, 'savedMutations': len(writes), 'realServiceMutations': 0,
              'nativePaletteDrop': True, 'pointerConnections': 4, 'duplicateOutletRejected': True,
              'conditionRuleAndElsePreserved': True, 'layoutOnlyChangesPositions': True,
              'saveCompiledAndReloaded': True, 'logoutClearsCanvas': True}
    save(OUT / f'document-{width}.json', saved)
    await context.close()
    return result


async def main():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=CHROME, headless=True,
                                                   args=['--no-sandbox'])
        try:
            results = [await scenario(browser, width) for width in (1440, 390)]
            save(OUT / 'browser-report.json', {'mode': 'intercepted_api_real_pointer_browser',
                                               'cases': results, 'modelCalls': 0})
            print(json.dumps(results, ensure_ascii=False))
        finally:
            await browser.close()


if __name__ == '__main__':
    asyncio.run(main())
