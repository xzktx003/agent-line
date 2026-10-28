"""Archive UI with intercepted API; all real service/model mutations are zero."""
import asyncio
import json
import mimetypes
from pathlib import Path
from urllib.parse import urlsplit
from playwright.async_api import async_playwright, expect
from probes.sim.publication_capability_browser import CHROME
from probes.qwenpaw_memory.native_roundtrip import save
from tests.test_authoring_api import document

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'var/workflow-archive-ui-20261022'


async def scenario(browser, width, mode):
    context = await browser.new_context(viewport={'width': width, 'height': 1000})
    archived = False
    operations, posts, errors = {}, [], []
    audit_wait, release = asyncio.Event(), asyncio.Event()
    delay_audit = False
    reconciled = False

    async def route(r):
        nonlocal archived, reconciled
        request, parsed = r.request, urlsplit(r.request.url)
        assert parsed.netloc == 'archive-fixture.test'
        path = parsed.path
        prefix = '/extension/v1/authoring/'
        if path.startswith(prefix):
            if request.method == 'POST' and path.endswith('/reconcile'):
                reconciled = True
                key = path.split('/')[-2]
                data = operations[key] | {'status': 'accepted', 'receipt': {'workflowId': 'flow', 'outcome': 'archived'}}
                operations[key] = data
            elif request.method == 'POST':
                assert path.endswith('/flow/archive')
                body = request.post_data_json
                assert body['expectedStateSha256'] == 'a'*64
                posts.append(body)
                archived = True
                data = {'projectId': 'fixture', 'operationId': body['operationId'], 'kind': 'archive',
                        'status': 'accepted', 'receipt': {'workflowId': 'flow', 'outcome': 'archived'}}
                if mode == 'native-uncertain':
                    data = data | {'status': 'uncertain', 'receipt': None}
                operations[body['operationId']] = data
                if mode == 'lost-receipt':
                    await r.abort()
                    return
            elif path.endswith('/projects'):
                data = {'projects': [{'id': 'fixture', 'name': '归档界面验证项目'}], 'canPublish': True}
            elif path.endswith('/roles'):
                data = {'roles': [], 'source': 'configured_existing_roles', 'nativeAvailabilityVerified': False}
            elif path.endswith('/workflows'):
                data = {'workflows': [] if archived else [{'id': 'flow', 'name': '待归档流程'}], 'nextCursor': None}
            elif path.endswith('/workflows/flow'):
                data = {'workflowId': 'flow', 'name': '待归档流程', 'stateSha256': 'a'*64,
                        'document': document(), 'editable': True, 'draftEmpty': False}
            elif '/operations/' in path:
                data = operations[path.rsplit('/', 1)[-1]]
            elif path.endswith('/archives'):
                data = {'archives': [{'operationId': next(iter(operations)), 'workflowId': 'flow',
                                     'name': '待归档流程', 'status': 'uncertain' if mode=='native-uncertain' and not reconciled else 'accepted'}] if archived else [], 'nextCursor': None}
            elif '/archives/' in path:
                if delay_audit:
                    audit_wait.set()
                    await release.wait()
                data = {'projectId': 'fixture', 'operationId': path.rsplit('/', 1)[-1], 'status': 'uncertain' if mode=='native-uncertain' and not reconciled else 'accepted',
                        'readOnly': True, 'snapshot': {'workflowId': 'flow', 'detail': {'name': '归档前保留的流程'},
                        'draft': {'visible': 'fixture-only'}, 'versions': [{'id': 'version-1', 'version': 1}],
                        'runs': [{'runId': 'run-1', 'status': 'completed'}]}}
            else:
                raise AssertionError(path)
            await r.fulfill(json=data)
            return
        file = (ROOT / 'frontend' / path.removeprefix('/workbench/')).resolve()
        assert path.startswith('/workbench/') and file.parent == ROOT / 'frontend'
        await r.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file)[0] or 'application/octet-stream')

    await context.route('**/*', route)
    page = await context.new_page()
    page.on('pageerror', lambda error: errors.append(str(error)))
    await page.goto('https://archive-fixture.test/workbench/editor.html')
    await page.locator('#token').fill('fixture-only')
    await page.locator('#login').click()
    await page.locator('#project').select_option('fixture')
    await page.locator('.workflow-choice').click()
    await expect(page.locator('#archive-workflow')).to_be_enabled()
    page.once('dialog', lambda dialog: dialog.accept())
    await page.locator('#archive-workflow').click()
    if mode == 'lost-receipt':
        await expect(page.locator('#notice')).to_contain_text('提交结果尚未确认')
        await expect(page.locator('#archive-workflow')).to_be_disabled()
        await page.locator('#observe').click()
    if mode == 'native-uncertain':
        await page.locator('#refresh-archives').click()
        await page.locator('.archive-choice').click()
        await expect(page.locator('#archive-reconcile')).to_be_visible()
        await page.locator('#archive-reconcile').click()
    await expect(page.locator('.workflow-choice')).to_have_count(0)
    await expect(page.locator('.archive-choice')).to_have_count(1)
    await page.locator('.archive-choice').click()
    await expect(page.locator('#archive-audit-summary')).to_contain_text('1个版本、1次运行')
    await expect(page.locator('#archive-run-list')).to_contain_text('已完成')
    await expect(page.locator('#save')).to_be_disabled()
    async with page.expect_download() as info:
        await page.locator('#archive-download').click()
    download = await info.value
    assert download.suggested_filename == 'workflow-archive-audit.json'
    assert len(posts) == 1
    assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    OUT.mkdir(exist_ok=True, mode=0o700)
    screenshot = OUT / f'{mode}-{width}.png'
    await page.screenshot(path=str(screenshot), full_page=True)
    screenshot.chmod(0o600)
    delay_audit = True
    await page.locator('.archive-choice').click()
    await asyncio.wait_for(audit_wait.wait(), 5)
    await page.locator('#logout').click()
    release.set()
    await page.wait_for_timeout(100)
    await expect(page.locator('#archive-audit')).to_be_hidden()
    await expect(page.locator('#archive-audit-heading')).to_have_text('归档前审计')
    assert await page.locator('#archive-version-list').inner_text() == ''
    assert await page.locator('#token').input_value() == ''
    assert not errors, errors
    result = {'width': width, 'mode': mode, 'fixtureArchivePosts': len(posts), 'auditReadOnly': True,
              'lostReceiptOriginalQueryOnly': True, 'downloadRequested': True,
              'logoutLateAuditIgnored': True, 'actualServiceWrites': 0, 'modelCalls': 0}
    await context.close()
    return result


async def main():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=CHROME, headless=True, args=['--no-sandbox'])
        try:
            results = [await scenario(browser, width, mode) for width, mode in
                       ((1440, 'confirmed'), (390, 'confirmed'), (1440, 'lost-receipt'), (390, 'native-uncertain'))]
            save(OUT / 'report.json', {'mode': 'intercepted_api_real_browser', 'cases': results})
            print(json.dumps(results, ensure_ascii=False))
        finally:
            await browser.close()


if __name__ == '__main__':
    asyncio.run(main())
