"""Explicitly synthetic editor copy scenarios; all network requests intercepted.

No live Sim mutations, native roles, model tasks or persistent credentials.
"""
import asyncio
import copy
import json
import mimetypes
import os
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, expect

from probes.sim.publication_capability_browser import CHROME, fixture

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'var/workflow-copy-20261022/browser-fixture'
ORIGIN = 'https://copy-fixture.test'
CASES = ('normal', 'lost-create', 'lost-save', 'failed-get', 'wrong-draft', 'same-source',
         'existing-new', 'refused', 'unsupported', 'dirty', 'late-logout', 'late-project',
         'late-get-logout', 'late-get-project')


def save(path, data):
    with os.fdopen(os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600), 'w') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)


async def scenario(browser, mode):
    context = await browser.new_context(viewport={'width': 1440, 'height': 1000})
    source = fixture()
    original = copy.deepcopy(source)
    source_name = '原生角色流程（离线模拟）'
    new_name = '副本 <img src=x onerror=alert(1)> 中文'
    copied = None
    posts, reads, errors = [], [], []
    operations = {}
    late = asyncio.Event()
    late_seen = asyncio.Event()
    failed_get = False

    def detail(workflow):
        if workflow == 'source':
            return dict(workflowId='source', name=source_name, description='原说明_离线',
                        editable=mode != 'unsupported', draftEmpty=False, document=source,
                        stateSha256='a'*64, isDeployed=True,
                        publishedBindings=[dict(bindingId='original-published-binding', revision=7)])
        return dict(workflowId='copy', name=new_name, description='原说明_离线',
                    editable=True, draftEmpty=copied is None, document=copied,
                    stateSha256='b'*64, isDeployed=False, publishedBindings=[])

    async def route(r):
        nonlocal copied, failed_get
        req = r.request
        parsed = urlsplit(req.url)
        path = parsed.path
        if parsed.netloc != 'copy-fixture.test':
            errors.append('unexpected external request')
            await r.abort()
            return
        if path.startswith('/extension/v1/authoring/'):
            if req.method == 'POST':
                body = req.post_data_json
                kind = 'save' if path.endswith('/save') else 'create'
                if not (path.endswith('/workflows') or path.endswith('/workflows/copy/save')):
                    errors.append('unexpected mutation')
                    await r.abort()
                    return
                posts.append(dict(path=path, body=copy.deepcopy(body)))
                if kind == 'save':
                    assert body['expectedStateSha256'] == 'b'*64
                    copied = copy.deepcopy(body['document'])
                receipt = dict(workflowId='copy', outcome='saved' if kind == 'save' else 'created',
                               stateSha256='c'*64)
                operation = dict(projectId='synthetic', operationId=body['operationId'], kind=kind,
                                 status='accepted', safeToResubmit=False, receipt=receipt)
                operations[body['operationId']] = copy.deepcopy(operation)
                if kind == 'create':
                    assert body['name'] == new_name and body['description'] == '原说明_离线'
                    if mode == 'refused':
                        await r.fulfill(status=503, headers={'X-Agent-Line-Admission': 'closed'},
                                        json={'detail': '维护期间未准入'})
                        return
                    if mode in ('late-logout', 'late-project'):
                        late_seen.set()
                        await late.wait()
                    if mode == 'lost-create':
                        await r.abort()
                        return
                    if mode == 'same-source':
                        operation['receipt']['workflowId'] = 'source'
                    if mode == 'existing-new':
                        copied = copy.deepcopy(source)
                        copied['nodes'][1]['name'] = '另一编辑者已保存的内容'
                elif mode == 'lost-save':
                    await r.abort()
                    return
                data = operation
            else:
                assert req.method == 'GET'
                reads.append(path)
                if path.endswith('/projects'):
                    data = dict(projects=[dict(id='synthetic', name='离线模拟项目'),
                                          dict(id='other', name='另一个模拟项目')], canPublish=True)
                elif path.endswith('/roles'):
                    data = dict(roles=[dict(roleBindingId='synthetic_role', roleAgentId='离线原生角色',
                                           memoryPolicyId='native-role', outputSchemaId='task-result')],
                                source='configured_existing_roles', nativeAvailabilityVerified=False)
                elif '/operations/' in path:
                    data = operations[path.rsplit('/', 1)[-1]]
                elif path.endswith('/workflows'):
                    data = dict(workflows=[dict(id='source', name=source_name, isDeployed=True)], nextCursor=None)
                else:
                    workflow = path.rsplit('/', 1)[-1]
                    if workflow == 'copy' and mode == 'failed-get' and not failed_get:
                        failed_get = True
                        await r.fulfill(status=503, json={'detail': '暂时无法读取副本草稿'})
                        return
                    data = detail(workflow)
                    if workflow == 'copy' and mode == 'wrong-draft' and not failed_get:
                        data['workflowId'] = 'source'
                        failed_get = True
                    if workflow == 'copy' and mode in ('late-get-logout', 'late-get-project'):
                        late_seen.set()
                        await late.wait()
            await r.fulfill(status=200, json=data)
            return
        file = (ROOT/'frontend'/path.removeprefix('/workbench/')).resolve()
        if req.method == 'GET' and path.startswith('/workbench/') and file.parent == ROOT/'frontend' and file.is_file():
            await r.fulfill(content_type=mimetypes.guess_type(file)[0] or 'application/octet-stream',
                            body=file.read_bytes(), headers={'Content-Security-Policy':
                            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'"})
        else:
            errors.append('unexpected request')
            await r.abort()

    await context.route('**/*', route)
    page = await context.new_page()
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('dialog', lambda dialog: dialog.accept())
    await page.goto(ORIGIN+'/workbench/editor.html')
    await page.locator('#token').fill('fixture-only-credential')
    await page.locator('#login').click()
    await page.locator('#project').select_option('synthetic')
    await page.locator('.workflow-choice').click()
    await expect(page.locator('#flow-heading')).to_have_text(source_name)
    await page.locator('#copy-details summary').click()
    if mode == 'unsupported':
        await expect(page.locator('#copy-submit')).to_be_disabled()
    elif mode == 'dirty':
        await page.locator('#node-list button').nth(1).click()
        await page.locator('#agent-instruction').fill('未应用的本地编辑')
        await expect(page.locator('#copy-submit')).to_be_disabled()
        await expect(page.locator('#copy-hint')).to_contain_text('先应用节点配置')
    else:
        await page.locator('#copy-name').fill(new_name)
        await page.locator('#copy-submit').click()
        if mode in ('late-logout', 'late-project', 'late-get-logout', 'late-get-project'):
            await asyncio.wait_for(late_seen.wait(), 10)
            if mode.endswith('logout'):
                await page.locator('#logout').click()
            else:
                # A project event can arrive from host code even when its select
                # is disabled while a request is in flight.
                await page.locator('#project').evaluate("e=>{e.value='other';e.dispatchEvent(new Event('change'));}")
                await expect(page.locator('#refresh')).to_be_enabled()
            late.set()
            await page.wait_for_timeout(120)
            await expect(page.locator('#flow-heading')).to_have_text('选择一个流程开始搭建')
            assert await page.locator('#copy-name').input_value() == ''
            assert len(posts) == 1
        elif mode == 'refused':
            await expect(page.locator('#notice')).to_contain_text('修改未提交')
            await expect(page.locator('#flow-heading')).to_have_text(source_name)
            await expect(page.locator('#copy-submit')).to_be_enabled()
        elif mode == 'same-source':
            await expect(page.locator('#notice')).to_contain_text('副本身份无法核验')
            await expect(page.locator('#copy-submit')).to_be_disabled()
            await expect(page.locator('#observe')).to_be_enabled()
            assert '/extension/v1/authoring/projects/synthetic/workflows/copy' not in reads
        elif mode == 'existing-new':
            await expect(page.locator('#notice')).to_contain_text('未覆盖')
            await expect(page.locator('#node-list')).to_contain_text('另一编辑者已保存的内容')
            await expect(page.locator('#save')).to_be_disabled()
            assert len(posts) == 1
        else:
            if mode in ('lost-create', 'failed-get', 'wrong-draft'):
                await expect(page.locator('#observe')).to_be_enabled()
                if mode == 'lost-create':
                    await expect(page.locator('#notice')).to_contain_text('结果尚未确认')
                    await expect(page.locator('#copy-submit')).to_be_disabled()
                    await page.locator('#copy-form').evaluate("e=>e.dispatchEvent(new Event('submit',{cancelable:true}))")
                    assert len(posts) == 1
                await page.locator('#observe').click()
            await expect(page.locator('#notice')).to_contain_text('尚未保存到 Sim')
            await expect(page.locator('#flow-heading')).to_have_text(new_name)
            await expect(page.locator('#node-list button')).to_have_count(3)
            assert await page.locator('#published-bindings a').count() == 0
            assert await page.locator('#flow-heading img').count() == 0
            assert len(posts) == 1 and copied is None
            await expect(page.locator('#publish')).to_be_disabled()
            # Querying a confirmed create does not regenerate or replace the
            # dirty local copy, nor issue its save automatically.
            await page.locator('#observe').click()
            assert len(posts) == 1
            await expect(page.locator('#save')).to_be_enabled()
            await page.locator('#save').click()
            if mode == 'lost-save':
                await expect(page.locator('#notice')).to_contain_text('结果尚未确认')
                await page.locator('#observe').click()
            await expect(page.locator('#notice')).to_have_text('草稿已保存并读回核验')
            await expect(page.locator('#save')).to_be_disabled()
            assert copied == original and len(posts) == 2
            assert posts[1]['path'].endswith('/workflows/copy/save')
            assert posts[0]['body']['operationId'] != posts[1]['body']['operationId']
            # A fresh GET from Sim is authoritative after a page reopen.
            await page.reload()
            await page.locator('#token').fill('fixture-only-credential')
            await page.locator('#login').click()
            await page.locator('#project').select_option('synthetic')
            await expect(page.locator('#refresh')).to_be_enabled()
            # Fixture list uses the source only; directly open copy through a
            # repeat operation is not possible after reload, as in production.
            assert await page.evaluate('localStorage.length+sessionStorage.length') == 0
            if mode == 'normal':
                await page.locator('.workflow-choice').click()
                await page.locator('#copy-details summary').click()
                await page.screenshot(path=str(OUT/'desktop.png'), full_page=True)
                await page.set_viewport_size({'width':390,'height':844})
                await page.screenshot(path=str(OUT/'mobile.png'), full_page=True)
                assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                assert (await page.locator('#copy-details summary').bounding_box())['height'] >= 44
            await page.locator('#logout').click()
            assert await page.locator('#copy-name').input_value() == ''
            assert await page.locator('#canvas').text_content() == ''
    assert source == original
    assert not errors, errors
    result = dict(mode=mode, passed=True, fixture=True, writes=len(posts), sourceUnchanged=True,
                  unexpectedRequests=0, pageErrors=0, liveWrites=0, modelTasks=0)
    await context.close()
    return result


async def run():
    OUT.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(OUT.parent, 0o700)
    os.chmod(OUT, 0o700)
    results = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=CHROME, args=['--no-sandbox'])
        try:
            for mode in CASES:
                results.append(await scenario(browser, mode))
                print(json.dumps(results[-1]), flush=True)
        finally:
            await browser.close()
            for screenshot in OUT.glob('*.png'):
                screenshot.chmod(0o600)
            save(OUT/'report.json', dict(fixture=True, results=results, expectedScenarios=len(CASES),
                                        passed=len(results)==len(CASES)))
    return len(results) == len(CASES)


if __name__ == '__main__':
    raise SystemExit(0 if asyncio.run(run()) else 1)
