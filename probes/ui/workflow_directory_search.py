"""Real directory UI with intercepted metadata API; no service/model writes."""
import asyncio
import json
import mimetypes
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import async_playwright, expect
from probes.sim.publication_capability_browser import CHROME
from probes.qwenpaw_memory.native_roundtrip import save

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'var/workflow-directory-20261022'
ORIGIN = 'https://directory-fixture.test'


async def scenario(browser, width):
    context = await browser.new_context(viewport={'width': width, 'height': 1000})
    requests, errors = [], []
    wait_started, release = asyncio.Event(), asyncio.Event()

    async def route(r):
        request = r.request
        parsed = urlsplit(request.url)
        assert parsed.netloc == 'directory-fixture.test'
        path = parsed.path
        assert request.method == 'GET', 'Directory must never mutate'
        if path.startswith('/extension/v1/authoring/'):
            if path.endswith('/projects'):
                data = {'projects': [{'id': 'one', 'name': '项目一'}, {'id': 'two', 'name': '项目二'}],
                        'canPublish': True}
            elif path.endswith('/roles'):
                data = {'roles': [], 'source': 'configured_existing_roles', 'nativeAvailabilityVerified': False}
            elif path.endswith('/workflows'):
                query = parse_qs(parsed.query, keep_blank_values=True)
                term, cursor = query.get('search', [''])[0], query.get('after', [''])[0]
                requests.append({'project': path.split('/')[-2], 'search': term, 'after': cursor})
                if term == '等待返回':
                    wait_started.set()
                    await release.wait()
                    data = {'workflows': [{'id': 'late', 'name': '迟到的敏感结果'}], 'nextCursor': None}
                elif term == '发布' and cursor == '':
                    data = {'workflows': [], 'nextCursor': 'page-two', 'search': term, 'scannedCount': 20}
                elif term == '发布' and cursor == 'page-two':
                    data = {'workflows': [{'id': 'release', 'name': '发布检查', 'description': '核对本轮变更',
                        'isDeployed': True, 'deploymentStatus': 'deployed', 'currentVersion': 2,
                        'needsRedeployment': True, 'runCount': 3, 'lastRunAt': '2026-10-22T08:30:00Z'}],
                        'nextCursor': None, 'search': term, 'scannedCount': 1}
                else:
                    data = {'workflows': [{'id': 'draft', 'name': '待配置草稿', 'description': '保留原生记忆',
                        'isDeployed': False, 'deploymentStatus': 'draft', 'currentVersion': None, 'runCount': 0, 'lastRunAt': None},
                        {'id': 'legacy', 'name': '旧接口流程', 'isDeployed': False}], 'nextCursor': None}
            else:
                raise AssertionError('Unexpected API route')
            await r.fulfill(json=data)
            return
        file = (ROOT / 'frontend' / path.removeprefix('/workbench/')).resolve()
        assert path.startswith('/workbench/') and file.parent == ROOT / 'frontend' and file.is_file()
        await r.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file)[0] or 'application/octet-stream')

    await context.route('**/*', route)
    page = await context.new_page()
    page.on('pageerror', lambda error: errors.append(str(error)))
    await page.goto(ORIGIN + '/workbench/editor.html')
    await expect(page.locator('#workflow-search')).to_be_disabled()
    await page.locator('#token').fill('fixture-directory-credential')
    await page.locator('#login').click()
    await page.locator('#project').select_option('one')
    await expect(page.locator('.workflow-choice')).to_have_count(2)
    await expect(page.locator('.workflow-choice').first).to_contain_text('运行次数：0 · 最近运行：尚无运行')
    await expect(page.locator('.workflow-choice').last).to_contain_text('运行次数：尚无记录 · 最近运行：尚无记录')
    await expect(page.locator('.workflow-choice').last).to_contain_text('发布状态待核对')
    await expect(page.locator('.workflow-choice').first).to_contain_text('暂无当前发布版本')
    await page.locator('#workflow-search').fill('  发布  ')
    await page.locator('#workflow-search-submit').click()
    await expect(page.locator('#workflow-list')).to_contain_text('本页没有匹配项，可继续下一页查找')
    await expect(page.locator('#next')).to_be_enabled()
    await page.locator('#next').click()
    await expect(page.locator('.workflow-choice')).to_have_count(1)
    await expect(page.locator('.workflow-choice')).to_contain_text('当前发布：v2 · 草稿有未发布修改')
    await expect(page.locator('.workflow-choice')).to_contain_text('发布检查')
    await expect(page.locator('.workflow-choice')).to_contain_text('运行次数：3')
    await expect(page.locator('#page-label')).to_have_text('第 2 页')
    assert requests[-1]['search'] == '发布' and requests[-1]['after'] == 'page-two'
    assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
    await page.screenshot(path=OUT / f'search-{width}.png', full_page=True)
    (OUT / f'search-{width}.png').chmod(0o600)
    await page.locator('#previous').click()
    await expect(page.locator('#workflow-list')).to_contain_text('本页没有匹配项')
    await page.locator('#workflow-search-clear').click()
    await expect(page.locator('.workflow-choice')).to_have_count(2)
    assert requests[-1]['search'] == '' and requests[-1]['after'] == ''
    await page.locator('#workflow-search').fill('未提交关键词')
    await page.locator('#project').select_option('two')
    await expect(page.locator('#workflow-search')).to_have_value('')
    await expect(page.locator('.workflow-choice')).to_have_count(2)
    assert requests[-1]['project'] == 'two' and requests[-1]['after'] == ''
    await page.locator('#workflow-search').fill('旧服务搜索')
    await page.locator('#workflow-search-submit').click()
    await expect(page.locator('#notice')).to_contain_text('当前服务未确认此搜索条件')
    await expect(page.locator('.workflow-choice')).to_have_count(0)
    await expect(page.locator('#next')).to_be_disabled()
    await page.locator('#workflow-search').fill('等待返回')
    await page.locator('#workflow-search-submit').click()
    await asyncio.wait_for(wait_started.wait(), timeout=5)
    await page.locator('#logout').click()
    release.set()
    await expect(page.locator('#workflow-search')).to_have_value('')
    await expect(page.locator('#workflow-search')).to_be_disabled()
    await expect(page.locator('#workflow-list')).to_have_text('尚未加载流程。')
    await page.wait_for_timeout(100)
    assert '迟到的敏感结果' not in await page.locator('body').inner_text()
    assert await page.evaluate('localStorage.length === 0 && sessionStorage.length === 0')
    assert not errors, errors
    await context.close()
    return {'width': width, 'requests': requests, 'emptyMatchPagination': True,
            'metadataUnknownPreserved': True, 'clearAndProjectReset': True,
            'logoutLateResponseIgnored': True, 'bodyOverflow': False, 'writes': 0}


async def main():
    OUT.mkdir(mode=0o700, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=CHROME, headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage'])
        try:
            cases = [await scenario(browser, width) for width in (1440, 390)]
        finally:
            await browser.close()
    report = {'mode': 'intercepted_api_real_browser', 'cases': cases,
              'actualServiceWrites': 0, 'modelCalls': 0}
    save(OUT / 'browser-report.json', report)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    asyncio.run(main())
