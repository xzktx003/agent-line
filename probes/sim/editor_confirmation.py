"""One bounded visual confirmation and real maintenance rejection, no Sim writes."""
import asyncio
import json
from pathlib import Path
import sys

from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agent_line.sim_journal import SimJournal
from agent_line.store import TaskStore
from probes.sim.authoring_probe import write
from probes.sim.editor_browser import BASE, CHROME, OUT


async def main():
    state = json.loads((OUT / 'state.json').read_text())
    token = json.loads((OUT.parent / 'authoring.json').read_text())['credentials'][0]['token']
    tasks = TaskStore(OUT.parent / 'tasks.sqlite')
    journal = SimJournal(OUT.parent / 'sim.sqlite')
    with tasks.connect() as db:
        assert db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0
    with journal.connect() as db:
        original_count = db.execute('SELECT count(*) FROM operations').fetchone()[0]
    errors = []; posts = []
    owner = None
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=CHROME, args=['--no-sandbox'])
        try:
            for label, width, height in [('desktop', 1440, 1000), ('mobile', 390, 844)]:
                context = await browser.new_context(viewport={'width': width, 'height': height})
                page = await context.new_page()
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('dialog', lambda dialog: dialog.accept())
                page.on('request', lambda request: posts.append(request.url) if request.method == 'POST' else None)
                await page.goto(BASE + '/workbench/editor.html')
                await page.locator('#token').fill(token); await page.locator('#login').click()
                await expect(page.locator('#connection')).to_have_text('编辑服务已连接')
                await page.locator('#project').select_option('authoring-http-probe')
                await expect(page.locator('#refresh')).to_be_enabled()
                await page.locator('.workflow-choice').filter(has_text=state['workflowName']).click()
                await expect(page.locator('#node-list button')).to_have_count(4)
                await page.locator('#node-list').get_by_role('button', name='结束2', exact=True).focus()
                await page.keyboard.press('Enter')
                await expect(page.locator('#node-form')).to_be_visible()
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                await page.locator('.editor-panel').screenshot(path=str(OUT / (label + '-editor-confirmed.png')))
                await page.screenshot(path=str(OUT / (label + '-page-confirmed.png')), full_page=True)
                if label == 'desktop':
                    # The real server proves non-admission before any new journal intent.
                    owner = tasks.enter_maintenance()
                    await page.locator('#node-x').fill('660')
                    await page.locator('#node-form').get_by_role('button', name='应用节点配置').click()
                    async with page.expect_response(lambda response: response.request.method == 'POST' and response.url.endswith('/save')) as response:
                        await page.locator('#save').click()
                    rejected = await response.value
                    assert rejected.status == 503 and rejected.headers.get('x-agent-line-admission') == 'closed'
                    await expect(page.locator('#notice')).to_contain_text('修改未提交')
                    await expect(page.locator('#save')).to_be_enabled()
                    await expect(page.locator('#observe')).to_be_disabled()
                    with journal.connect() as db:
                        assert db.execute('SELECT count(*) FROM operations').fetchone()[0] == original_count
                    tasks.leave_maintenance(owner); owner = None
                    await page.locator('#logout').click()
                    assert await page.locator('#canvas g').count() == 0
                assert await page.evaluate('localStorage.length + sessionStorage.length') == 0
                # Existing workbench navigation must also fit after adding the editor link.
                await page.goto(BASE + '/workbench/')
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                await context.close()
            assert not errors
            with journal.connect() as db:
                assert db.execute('SELECT count(*) FROM operations').fetchone()[0] == original_count
            write(OUT / 'confirmation.json', {'status': 'passed', 'browserErrors': errors,
                                            'desktopMobileNoDocumentOverflow': True,
                                            'keyboardNodeSelection': True,
                                            'maintenanceReal503RejectedBeforeIntent': True,
                                            'browserAllowsExplicitCorrectionAfterRejection': True,
                                            'newJournalIntents': 0, 'nativeMutationRequests': 0,
                                            'browserPosts': len(posts)})
            print(json.dumps({'status': 'passed', 'newJournalIntents': 0, 'nativeMutationRequests': 0}))
        finally:
            if owner is not None:
                tasks.leave_maintenance(owner)
            await browser.close()


if __name__ == '__main__':
    asyncio.run(main())
