"""Bounded template UI check: fixture API, real static UI and graph compiler.

No real service mutations, native roles, model requests or publication.
"""
import asyncio
import copy
import json
import mimetypes
import os
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, expect
from agent_line.sim_authoring import compile_document
from probes.sim.publication_capability_browser import CHROME
from probes.qwenpaw_memory.native_roundtrip import save

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'var/workflow-templates-20261022'
ORIGIN = 'https://templates-fixture.test'


async def scenario(browser, mode, width=1440):
    context = await browser.new_context(viewport={'width': width, 'height': 1000})
    posts, errors, operations = [], [], {}
    saved = None
    roles = [dict(roleBindingId=slot, roleAgentId='fixture-' + slot,
                  memoryPolicyId='native-role', outputSchemaId='task-result')
             for slot in (('developer',) if mode == 'repair' else ('planner', 'developer', 'reviewer'))]

    async def route(r):
        nonlocal saved
        request = r.request
        parsed = urlsplit(request.url)
        assert parsed.netloc == 'templates-fixture.test'
        path = parsed.path
        if path.startswith('/extension/v1/authoring/'):
            if request.method == 'POST':
                body = request.post_data_json
                kind = 'save' if path.endswith('/save') else 'create'
                assert path in ('/extension/v1/authoring/projects/fixture/workflows',
                                '/extension/v1/authoring/projects/fixture/workflows/new-draft/save')
                posts.append({'kind': kind, 'body': copy.deepcopy(body)})
                if kind == 'save':
                    assert body['expectedStateSha256'] == 'a' * 64
                    compile_document(body['document'], 'new-draft')  # Real backend contract, not a fake green save.
                    saved = copy.deepcopy(body['document'])
                operation = dict(projectId='fixture', operationId=body['operationId'], kind=kind,
                    status='accepted', receipt={'workflowId': 'new-draft',
                        'outcome': 'saved' if kind == 'save' else 'created', 'stateSha256': 'b' * 64})
                operations[body['operationId']] = operation
                if mode.startswith('lost-create') and kind == 'create':
                    await r.abort(); return
                data = operation
            else:
                assert request.method == 'GET'
                if path.endswith('/projects'):
                    data = {'projects': [{'id': 'fixture', 'name': '离线模板项目'}], 'canPublish': True}
                elif path.endswith('/roles'):
                    data = {'source': 'configured_existing_roles', 'roles': roles, 'nativeAvailabilityVerified': False}
                elif path.endswith('/workflows'):
                    data = {'workflows': [{'id':'source','name':'原有已保存草稿','isDeployed':False}]
                            if mode == 'lost-create-dirty' else [], 'nextCursor': None}
                elif '/operations/' in path:
                    data = operations[path.rsplit('/', 1)[-1]]
                elif path.endswith('/workflows/source'):
                    data = {'workflowId':'source','name':'原有已保存草稿','editable':True,'draftEmpty':False,
                        'stateSha256':'c'*64,'publishedBindings':[],
                        'document':{'schemaVersion':1,'variables':{},'nodes':[
                            {'id':'start','name':'开始','type':'start','position':{'x':50,'y':70},'config':{}},
                            {'id':'end','name':'结束','type':'end','position':{'x':280,'y':70},'config':{'output':{}}}],
                            'edges':[{'id':'edge','source':'start','target':'end'}]}}
                else:
                    assert path.endswith('/workflows/new-draft')
                    data = {'workflowId': 'new-draft', 'name': '项目交付流程', 'description': '',
                            'editable': True, 'draftEmpty': saved is None,
                            'document': saved, 'stateSha256': ('b' if saved else 'a') * 64,
                            'publishedBindings': [], 'isDeployed': False}
            await r.fulfill(json=data)
            return
        file = (ROOT / 'frontend' / path.removeprefix('/workbench/')).resolve()
        if request.method == 'GET' and path.startswith('/workbench/') and file.parent == ROOT / 'frontend' and file.is_file():
            await r.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file)[0] or 'application/octet-stream')
            return
        raise AssertionError('Unexpected fixture request')

    await context.route('**/*', route)
    page = await context.new_page()
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('dialog', lambda dialog: dialog.accept())
    await page.goto(ORIGIN + '/workbench/editor.html')
    await page.locator('#token').fill('synthetic-editor-credential')
    await page.locator('#login').click()
    await page.locator('#project').select_option('fixture')
    await expect(page.locator('#roles-status')).to_contain_text('已读取')
    if mode == 'lost-create-dirty':
        await page.locator('.workflow-choice').click()
        await page.locator('#node-list button').first.click()
        await page.locator('#node-name').fill('尚未应用的旧编辑')
        await expect(page.locator('#draft-status')).to_contain_text('尚未应用')
    await page.locator('#template-details summary').click()
    if mode == 'repair':
        await expect(page.locator('#template-submit')).to_be_disabled()
        await page.locator('#template-choice').select_option('bounded-repair-v1')
    await page.locator('#template-name').fill('项目交付流程')
    if mode == 'duplicate':
        await page.locator('#template-role-planner').select_option('developer')
        await page.locator('#template-submit').click()
        await expect(page.locator('#notice')).to_contain_text('需要不同的正式角色')
        assert not posts
    else:
        await page.locator('#template-submit').click()
        if mode.startswith('lost-create'):
            await expect(page.locator('#notice')).to_contain_text('尚未确认')
            await expect(page.locator('#template-submit')).to_be_disabled()
            await page.locator('#observe').click()
        await expect(page.locator('#notice')).to_contain_text('模板“')
        await expect(page.locator('#draft-status')).to_contain_text('未保存')
        assert len(posts) == 1 and posts[0]['kind'] == 'create'
        if mode == 'team':
            await page.screenshot(path=OUT / ('template-' + str(width) + '.png'), full_page=True)
            (OUT / ('template-' + str(width) + '.png')).chmod(0o600)
        await page.locator('#save').click()
        await expect(page.locator('#notice')).to_contain_text('草稿已保存')
        assert len(posts) == 2 and saved is not None
        await page.locator('#reload').click()
        await expect(page.locator('#draft-status')).to_contain_text('当前草稿已读取')
        assert len(posts) == 2
        expected = 1 if mode == 'repair' else 3
        assert len([node for node in saved['nodes'] if node['type'] == 'agent']) == expected
    assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    await page.locator('#logout').click()
    assert await page.locator('#template-name').input_value() == ''
    assert await page.locator('#template-roles select').count() == 0
    assert await page.evaluate('localStorage.length===0 && sessionStorage.length===0')
    assert not errors, errors
    await context.close()
    return {'mode': mode, 'width': width, 'fixturePosts': len(posts), 'published': False,
            'savedThroughRealCompiler': saved is not None, 'bodyOverflow': False, 'logoutCleared': True}


async def main():
    OUT.mkdir(mode=0o700, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=CHROME, headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage'])
        try:
            cases = [await scenario(browser, mode, width) for mode, width in
                     [('team', 1440), ('team', 390), ('repair', 1440), ('lost-create', 1440),
                      ('lost-create-dirty', 1440), ('duplicate', 1440)]]
        finally:
            await browser.close()
    report = {'mode': 'synthetic_api_real_ui_compiler', 'cases': cases,
              'actualServiceWrites': 0, 'modelCalls': 0, 'publicationCalls': 0}
    save(OUT / 'browser-report.json', report)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    asyncio.run(main())
