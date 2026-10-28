"""Real editor authoring and a deliberately lost save receipt in an isolated project.

Run with the dedicated authoring HTTP probe configuration served on loopback 3196.
An existing state never authorizes repeating its browser mutations.
"""
import asyncio
import json
from pathlib import Path
import sys
import uuid

from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from probes.sim.authoring_probe import write

BASE = 'http://127.0.0.1:3196'
OUT = ROOT / 'var/sim-authoring-http/browser'
CHROME = '/data01/home/zukang.xu/.cache/ms-playwright/chromium_headless_shell-1223/chrome-headless-shell-linux64/chrome-headless-shell'


async def main():
    OUT.mkdir(mode=0o700, exist_ok=True)
    state_path = OUT / 'state.json'
    if state_path.exists():
        raise RuntimeError('Existing browser state: inspect it; do not repeat mutations')
    credential = json.loads((OUT.parent / 'authoring.json').read_text())['credentials'][0]['token']
    project = 'authoring-http-probe'
    workflow_name = '中文搭建验收_' + uuid.uuid4().hex[:8]
    state = {'intent': 'one_real_browser_workflow', 'workflowName': workflow_name, 'receipts': []}
    write(state_path, state)
    errors = []; posts = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=CHROME, args=['--no-sandbox'])
        try:
            context = await browser.new_context(viewport={'width': 1440, 'height': 1000})
            page = await context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('dialog', lambda dialog: dialog.accept())
            page.on('request', lambda request: posts.append(request.url) if request.method == 'POST' else None)
            async def connect(target):
                await target.goto(BASE + '/workbench/editor.html')
                await target.locator('#token').fill(credential)
                await target.locator('#login').click()
                await expect(target.locator('#connection')).to_have_text('编辑服务已连接')
                await target.locator('#project').select_option(project)
                await expect(target.locator('#refresh')).to_be_enabled()
            await connect(page)
            await page.locator('summary').click()
            await page.locator('#workflow-name').fill(workflow_name)
            async with page.expect_response(lambda response: response.request.method == 'POST' and response.url.endswith('/workflows')) as creation:
                await page.locator('#create').click()
            created = await (await creation.value).json()
            assert created['status'] == 'accepted' and created['receipt']['outcome'] == 'created'
            workflow = created['receipt']['workflowId']; state['workflowId'] = workflow
            state['receipts'].append(created); write(state_path, state)
            await expect(page.locator('#flow-heading')).to_have_text(workflow_name)
            await expect(page.locator('#save')).to_be_enabled()
            await page.locator('#node-type').select_option('condition'); await page.locator('#add-node').click()
            await page.locator('#node-type').select_option('end'); await page.locator('#add-node').click()
            for name, x, y in [('条件分支', 300, 110), ('结束', 620, 40), ('结束2', 620, 240)]:
                await page.locator('#node-list').get_by_role('button', name=name, exact=True).click()
                await page.locator('#node-x').fill(str(x)); await page.locator('#node-y').fill(str(y))
                if name.startswith('结束'):
                    await page.locator('#end-output').fill(json.dumps({'结果': name}, ensure_ascii=False))
                await page.locator('#node-form').get_by_role('button', name='应用节点配置').click()
            await page.locator('#edge-list').get_by_role('button', name='删除连线').click()
            for source, target, branch in [('开始', '条件分支', None), ('条件分支', '结束', 'rule'), ('条件分支', '结束2', 'else')]:
                await page.locator('#edge-source').select_option(label=source)
                await page.locator('#edge-target').select_option(label=target)
                if branch == 'else': await page.locator('#edge-branch').select_option('else')
                elif branch == 'rule': await page.locator('#edge-branch').select_option(index=0)
                await page.locator('#connect').click()
            async with page.expect_response(lambda response: response.request.method == 'POST' and response.url.endswith('/save')) as saving:
                await page.locator('#save').click()
            saved = await (await saving.value).json()
            state['receipts'].append(saved); write(state_path, state)
            assert saved['status'] == 'accepted' and saved['receipt']['outcome'] == 'saved'
            await expect(page.locator('#publish')).to_be_enabled()
            async with page.expect_response(lambda response: response.request.method == 'POST' and response.url.endswith('/publish')) as publishing:
                await page.locator('#publish').click()
            published = await (await publishing.value).json()
            state['receipts'].append(published); write(state_path, state)
            assert published['status'] == 'accepted' and published['receipt']['outcome'] == 'published'
            await expect(page.locator('#notice')).to_contain_text('已发布并核验版本')
            await page.locator('.editor-panel').screenshot(path=str(OUT / 'desktop-editor.png'))
            await page.screenshot(path=str(OUT / 'desktop-page.png'), full_page=True)
            await page.goto(BASE + '/workbench/')
            await page.screenshot(path=str(OUT / 'reference-workbench.png'), full_page=True)
            await connect(page)
            await page.locator('.workflow-choice').filter(has_text=workflow_name).click()
            await expect(page.locator('#publish')).to_be_enabled()
            await expect(page.locator('#node-list button')).to_have_count(4)
            # Lose the HTTP receipt after the actual accepted save, then query its original identity.
            await page.locator('#node-list').get_by_role('button', name='结束2', exact=True).click()
            await page.locator('#node-x').fill('650')
            await page.locator('#node-form').get_by_role('button', name='应用节点配置').click()
            lost_receipts = []
            async def lose_receipt(route):
                response = await route.fetch()
                data = await response.json(); lost_receipts.append(data)
                state['lostReceipt'] = data; write(state_path, state)
                await route.abort('connectionfailed')
            await page.route('**/workflows/' + workflow + '/save', lose_receipt, times=1)
            before = len(posts); await page.locator('#save').click()
            await expect(page.locator('#notice')).to_contain_text('提交结果尚未确认')
            await expect(page.locator('#save')).to_be_disabled()
            assert len(lost_receipts) == 1 and lost_receipts[0]['receipt']['outcome'] == 'saved'
            await page.locator('#observe').click()
            await expect(page.locator('#operation-status')).to_contain_text('草稿已保存并读回核验')
            assert len(posts) == before + 1
            await expect(page.locator('#publish')).to_be_enabled()
            await expect(page.locator('#save')).to_be_disabled()
            assert await page.evaluate('localStorage.length + sessionStorage.length') == 0
            await page.locator('#logout').click()
            assert await page.locator('#token').input_value() == ''
            assert await page.locator('#canvas').locator('g').count() == 0
            mobile = await browser.new_context(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
            mobile_page = await mobile.new_page(); mobile_page.on('pageerror', lambda error: errors.append(str(error)))
            await connect(mobile_page)
            await mobile_page.locator('.workflow-choice').filter(has_text=workflow_name).click()
            await expect(mobile_page.locator('#node-list button')).to_have_count(4)
            assert await mobile_page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            await mobile_page.screenshot(path=str(OUT / 'mobile-page.png'), full_page=True)
            await mobile_page.locator('.editor-panel').screenshot(path=str(OUT / 'mobile-editor.png'))
            assert not errors
            report = {'status': 'passed_live_browser', 'workflowId': workflow,
                      'checks': ['real_create_four_nodes_connections_save_publish', 'reload_preserves_document',
                                 'lost_real_save_receipt_queries_without_repost', 'no_browser_credential_storage',
                                 'logout_clears_sensitive_content', 'mobile_no_document_overflow', 'no_page_errors'],
                      'posts': len(posts), 'lostSavePosts': 1, 'pageErrors': errors,
                      'scope': 'isolated loopback authoring HTTP deployment; no QwenPaw roles or memory changed'}
            write(OUT / 'report.json', report)
            print(json.dumps({'status': report['status'], 'checks': len(report['checks'])}))
        finally:
            await browser.close()


if __name__ == '__main__':
    asyncio.run(main())
