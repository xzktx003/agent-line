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
OUT = ROOT / 'var/canvas-interactions-20261022'


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
                assert body['document']['edges'] == saved['edges']
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
    await page.locator('#canvas-in').click()
    await expect(page.locator('#canvas-scale')).to_have_text('125%')
    await expect(page.locator('#save')).to_be_disabled()
    await page.locator('#canvas-out').click()
    await expect(page.locator('#canvas-scale')).to_have_text('100%')

    async def begin_drag(dx, dy):
        node = page.locator('[data-node-id="start"]')
        await node.scroll_into_view_if_needed()
        box = await node.bounding_box()
        assert box
        x, y = box['x']+box['width']/2, box['y']+box['height']/2
        await page.mouse.move(x, y)
        await page.mouse.down()
        await page.mouse.move(x+dx, y+dy, steps=6)

    await begin_drag(50, 30)
    # A cancellation from another pointer must not cancel this drag.
    await page.locator('#canvas').dispatch_event('pointercancel', {'pointerId': 999, 'isPrimary': False})
    await expect(page.locator('#canvas')).to_have_class('dragging')
    # Keyboard-triggered logout during drag must ask before discarding.
    dialogs = []
    async def reject_discard(dialog):
        dialogs.append(dialog.message)
        await dialog.dismiss()
    page.once('dialog', reject_discard)
    await page.locator('#logout').evaluate('button=>button.click()')
    assert len(dialogs) == 1 and '未保存' in dialogs[0]
    await expect(page.locator('[data-node-id]')).to_have_count(2)
    await page.mouse.up()
    await expect(page.locator('#save')).to_be_enabled()
    moved = {'x': float(await page.locator('#node-x').input_value()),
             'y': float(await page.locator('#node-y').input_value())}
    assert moved['x'] > 50 and moved['y'] > 110
    assert writes == []
    await page.locator('#save').click()
    await expect(page.locator('#save')).to_be_disabled()
    assert len(writes) == 1 and saved['nodes'][0]['position'] == moved
    await page.locator('#canvas-fit').click()
    await begin_drag(30, 15)
    await page.keyboard.press('Escape')
    await page.mouse.up()
    await expect(page.locator('#save')).to_be_disabled()
    assert float(await page.locator('#node-x').input_value()) == moved['x']
    assert float(await page.locator('#node-y').input_value()) == moved['y']

    await page.locator('#node-name').fill('尚未应用的名称')
    await begin_drag(30, 10)
    await page.mouse.up()
    await expect(page.locator('#notice')).to_contain_text('请先应用或放弃节点配置')
    assert await page.locator('#node-name').input_value() == '尚未应用的名称'
    assert float(await page.locator('#node-x').input_value()) == moved['x']
    page.once('dialog', lambda dialog: dialog.accept())
    await page.locator('#reload').click()
    await expect(page.locator('#canvas-scale')).to_have_text('100%')
    await page.locator('#node-list button').first.click()
    await page.locator('#canvas-focus').click()
    centered = await page.locator('#canvas').evaluate('e => {const b=e.viewBox.baseVal; return {x:b.x+b.width/2,y:b.y+b.height/2}}')
    assert abs(centered['x']-(moved['x']+85)) < 0.1
    assert abs(centered['y']-(moved['y']+32)) < 0.1
    await page.locator('#canvas-fit').click()
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
              'dragPositionSavedThroughCompiler': True, 'zoomDoesNotDirty': True,
              'escapeRestoresSavedPosition': True, 'unappliedFormPreserved': True,
              'focusCentersSelectedNode': True, 'logoutClearsCanvas': True}
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
