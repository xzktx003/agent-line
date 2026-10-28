"""Synthetic publication receipts against real static UI; zero live API/model calls."""
import argparse
import asyncio
import copy
import json
import mimetypes
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / '.omx/state/publication-capability-ui'
CHROME = '/data01/home/zukang.xu/.cache/ms-playwright/chromium_headless_shell-1223/chrome-headless-shell-linux64/chrome-headless-shell'
CASES = {
    'execution_required': '任务执行与结果回传尚未就绪，已阻止发布。草稿已保留，请核对执行服务配置后再发布',
    'memory_commit_required': '此节点需要提交记忆，但尚未配置同一角色的记忆审核与回写，已阻止发布。草稿已保留，请核对配置',
    'verification_required': '自动验收策略尚未配置或不受支持，发布被阻止。请核对服务端验收配置',
}


def fixture():
    return dict(schemaVersion=1, variables={}, nodes=[
        dict(id='start', type='start', name='开始', position=dict(x=20, y=90), config={}),
        dict(id='agent', type='agent', name='原生角色任务', position=dict(x=280, y=90), config=dict(
            roleBindingId='synthetic_role', instruction='离线界面测试，不执行真实任务',
            inputFrom=dict(nodeId='start', field='input'), memoryPolicyId='native-role',
            outputSchemaId='task-result', requireMemoryCommit=True, allowFailureBranch=False)),
        dict(id='end', type='end', name='结束', position=dict(x=540, y=90), config=dict(resultFrom='agent')),
    ], edges=[dict(id='first', source='start', target='agent'), dict(id='last', source='agent', target='end')])


async def check_case(browser, outcome, *, capture_reference=False):
    doc = fixture()
    writes, reads, errors, csp = [], [], [], []
    operations = {}
    instruction = '原生记忆保持不变_离线测试草稿_关闭再打开仍需保留'
    context = await browser.new_context(viewport={'width': 1440, 'height': 1000})

    async def route(r):
        nonlocal doc
        req = r.request
        parsed = urlsplit(req.url)
        path = parsed.path
        if parsed.netloc != 'publication-fixture.test':
            errors.append('Unexpected external request')
            await r.abort()
            return
        if path.startswith('/extension/v1/authoring/'):
            if req.method == 'POST' and path.endswith(('/save', '/publish')):
                body = req.post_data_json
                kind = 'save' if path.endswith('/save') else 'publish'
                writes.append(dict(path=path, body=copy.deepcopy(body)))
                if kind == 'save':
                    doc = copy.deepcopy(body['document'])
                receipt = dict(workflowId='synthetic_workflow', outcome='saved' if kind == 'save' else outcome,
                               stateSha256='a'*64)
                data = dict(projectId='synthetic', operationId=body['operationId'], kind=kind,
                            status='accepted', safeToResubmit=False, receipt=receipt)
                operations[body['operationId']] = copy.deepcopy(data)
            elif req.method == 'GET':
                reads.append(path)
                if '/operations/' in path:
                    data = operations[path.rsplit('/', 1)[-1]]
                elif path.endswith('/projects'):
                    data = dict(projects=[dict(id='synthetic', name='离线模拟项目（未连接服务）')], canPublish=True)
                elif path.endswith('/roles'):
                    data = dict(roles=[dict(roleBindingId='synthetic_role', roleAgentId='离线模拟角色',
                                           memoryPolicyId='native-role', outputSchemaId='task-result')],
                                source='configured_existing_roles', nativeAvailabilityVerified=False)
                elif path.endswith('/workflows'):
                    data = dict(workflows=[dict(id='synthetic_workflow', name='发布阻止提示离线验证', isDeployed=False)],
                                nextCursor=None)
                else:
                    data = dict(workflowId='synthetic_workflow', name='发布阻止提示离线验证', document=copy.deepcopy(doc),
                                editable=True, draftEmpty=False, stateSha256='a'*64, isDeployed=False)
            else:
                errors.append('Unexpected synthetic API mutation: ' + req.method + ' ' + path)
                await r.abort()
                return
            await r.fulfill(status=200, content_type='application/json', body=json.dumps(data))
            return
        file = (ROOT / 'frontend' / path.removeprefix('/workbench/')).resolve()
        if req.method == 'GET' and path.startswith('/workbench/') and file.parent == ROOT / 'frontend' and file.is_file():
            await r.fulfill(status=200, content_type=mimetypes.guess_type(file)[0] or 'application/octet-stream',
                            body=file.read_bytes(), headers={'Content-Security-Policy':
                            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'"})
        else:
            errors.append('Unexpected synthetic static request')
            await r.abort()

    await context.route('**/*', route)

    async def open_page():
        page = await context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('console', lambda message: csp.append(message.text) if 'Content Security Policy' in message.text else None)
        page.on('dialog', lambda dialog: dialog.accept())
        await page.goto('https://publication-fixture.test/workbench/editor.html')
        await page.locator('#token').fill('synthetic-credential-never-sent-to-live-service')
        await page.locator('#login').click()
        await expect(page.locator('#connection')).to_have_text('编辑服务已连接')
        await page.locator('#project').select_option('synthetic')
        await page.locator('.workflow-choice').click()
        return page

    async def assert_draft(page):
        await page.locator('#node-list').get_by_role('button', name='原生角色任务', exact=True).click()
        await expect(page.locator('#agent-instruction')).to_have_value(instruction)
        await expect(page.locator('#agent-require-memory')).to_be_checked()
        assert await page.locator('#published-bindings').is_hidden()
        assert await page.locator('.run-binding-link').count() == 0

    try:
        page = await open_page()
        await page.locator('#node-list').get_by_role('button', name='原生角色任务', exact=True).click()
        await page.locator('#agent-instruction').fill(instruction)
        await page.locator('#node-form button[type=submit]').click()
        await page.locator('#save').click()
        await expect(page.locator('#operation-status')).to_contain_text('草稿已保存')
        await expect(page.locator('#save')).to_be_disabled()
        await page.locator('#publish').click()
        await expect(page.locator('#operation-status')).to_have_text(CASES[outcome])
        await expect(page.locator('#notice')).to_have_text(CASES[outcome])
        operation_id = writes[-1]['body']['operationId']
        assert operations[operation_id]['receipt']['outcome'] != 'published'
        assert len(writes) == 2
        await assert_draft(page)
        await page.locator('#observe').click()
        await expect(page.locator('#operation-status')).to_have_text(CASES[outcome])
        assert reads[-1].endswith('/operations/' + operation_id)
        # Reopening the same draft retains the original receipt and only GETs it.
        await page.locator('.workflow-choice').click()
        await assert_draft(page)
        await page.locator('#observe').click()
        await expect(page.locator('#operation-status')).to_have_text(CASES[outcome])
        assert len(writes) == 2
        if capture_reference or outcome == 'memory_commit_required':
            prefix = 'reference-' if capture_reference else ''
            await page.locator('#operation-panel').scroll_into_view_if_needed()
            await page.screenshot(path=str(OUT / (prefix + 'desktop.png')), full_page=True)
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            await page.locator('#operation-panel').screenshot(path=str(OUT / (prefix + 'desktop-receipt.png')))
            await page.set_viewport_size({'width': 390, 'height': 844})
            await page.screenshot(path=str(OUT / (prefix + 'mobile.png')), full_page=True)
            await page.locator('#operation-panel').screenshot(path=str(OUT / (prefix + 'mobile-receipt.png')))
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        # A truly new page does not auto-recover in-memory operation IDs. It can
        # reopen the server draft, which remains unpublished and unchanged.
        await page.close()
        page = await open_page()
        await assert_draft(page)
        assert await page.locator('#operation-panel').is_hidden()
        assert len(writes) == 2
        await page.locator('#logout').click()
        assert await page.locator('#agent-instruction').input_value() == ''
        assert await page.evaluate('localStorage.length + sessionStorage.length') == 0
        assert not errors and not csp, (errors, csp)
        return dict(outcome=outcome, passed=True, syntheticPostCount=len(writes), pageErrors=errors, cspErrors=csp,
                    checks=['specific_chinese_reason', 'saved_draft_retained', 'memory_policy_retained',
                            'not_published_no_run_link', 'same_operation_query_retains_reason',
                            'same_page_draft_reopen_retains_receipt', 'new_page_reopens_saved_draft',
                            'no_automatic_resubmission', 'logout_clears', 'credentials_not_persisted'])
    finally:
        await context.close()


async def main(*, capture_reference=False):
    OUT.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=CHROME, args=['--no-sandbox'])
        try:
            outcomes = ['verification_required'] if capture_reference else CASES
            reports = [await check_case(browser, outcome, capture_reference=capture_reference) for outcome in outcomes]
        finally:
            await browser.close()
    report = dict(mode='synthetic_intercepted_receipts_real_static_ui', passed=True, realApiCalls=0, realModelCalls=0,
                  cases=reports, desktopWidth=1440, mobileWidth=390,
                  limitation='Closing the page discards operation IDs as documented; saved drafts can be reopened.')
    target = 'reference-report.json' if capture_reference else 'report.json'
    (OUT / target).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print('Synthetic publication UI checks passed; zero live API or model calls.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture-reference', action='store_true')
    asyncio.run(main(capture_reference=parser.parse_args().capture_reference))
