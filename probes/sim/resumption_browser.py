"""Read-only resumption UI verification; never submits a workflow or model task.

Config: private JSON {baseUrl, serviceFile, bindingId, runId}. Screenshots are
real GET results; synthetic response variants are explicitly labelled in report.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'var/resumption-browser'
CHROME = '/data01/home/zukang.xu/.cache/ms-playwright/chromium_headless_shell-1223/chrome-headless-shell-linux64/chrome-headless-shell'


def private_json(path):
    path = Path(path)
    info = path.lstat()
    if path.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('Private configuration permissions required')
    return json.loads(path.read_text())


def write(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)


async def main(config_path):
    from playwright.async_api import async_playwright, expect
    config = private_json(config_path)
    service = private_json(config['serviceFile'])
    token = next(iter(service['tokens']))
    base = config['baseUrl'].rstrip('/')
    target = base + '/extension/v1/workflows/' + config['bindingId'] + '/runs/' + config['runId']
    OUT.mkdir(mode=0o700, exist_ok=True)
    errors, csp, blocked = [], [], []
    report = {'mode':'read_only_existing_run', 'bindingId':config['bindingId'], 'runId':config['runId'], 'checks':{}}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=CHROME, args=['--no-sandbox'])
        try:
            context = await browser.new_context(viewport={'width':1440,'height':1000})
            async def readonly(route):
                if route.request.method not in ('GET','HEAD'):
                    blocked.append({'method':route.request.method,'path':route.request.url.split('?')[0]})
                    await route.abort()
                else:
                    await route.continue_()
            await context.route('**/*', readonly)
            page = await context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('console', lambda message: csp.append(message.text) if 'Content Security Policy' in message.text else None)
            await page.goto(base + '/workbench/')
            await page.locator('#token').fill(token)
            await page.locator('#login-form button').click()
            await expect(page.locator('#connection-badge')).to_have_text('服务已连接')
            await page.locator('#query-binding').fill(config['bindingId'])
            await page.locator('#run-id').fill(config['runId'])
            async def query():
                async with page.expect_response(lambda response: response.url == target) as event:
                    await page.locator('#run-form button').click()
                response = await event.value
                assert response.status == 200
                await expect(page.locator('#run-form button')).to_be_enabled()
                return await response.json()
            data = await query()
            assert data['resumption']['execution']['status'] == 'failed'
            assert data['execution']['status'] == 'paused'
            report['response'] = data
            await expect(page.locator('#run-result')).to_contain_text('原运行状态')
            await expect(page.locator('#run-result')).to_contain_text('恢复执行状态')
            await expect(page.locator('#run-result')).to_contain_text(data['resumption']['runId'])
            await expect(page.locator('#notice')).to_contain_text('恢复执行失败')
            report['checks']['real_parent_paused_child_failed'] = True
            await page.screenshot(path=str(OUT/'desktop-page.png'), full_page=True)
            await page.locator('#run-result').screenshot(path=str(OUT/'desktop-result.png'))
            await page.set_viewport_size({'width':390,'height':844})
            await page.screenshot(path=str(OUT/'mobile-page.png'), full_page=True)
            await page.locator('#run-result').screenshot(path=str(OUT/'mobile-result.png'))
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            report['checks']['mobile_no_horizontal_overflow'] = True
            # Explicit display fault injection: not additional upstream evidence.
            for status in ('uncertain','intent','unknown'):
                variant = json.loads(json.dumps(data))
                variant['resumeSubmission'] = {'contextId':'probe_newer_context','status':status,'runId':None,'safeToResubmit':False}
                async def fake(route):
                    await route.fulfill(status=200, content_type='application/json', body=json.dumps(variant))
                await page.route(target, fake)
                await query()
                await expect(page.locator('#notice')).to_contain_text('最新恢复提交结果未知')
                await expect(page.locator('#run-result')).to_contain_text('此前已接收的恢复状态')
                await expect(page.locator('#run-id')).to_have_value(config['runId'])
                await page.unroute(target, fake)
                report['checks']['synthetic_'+status+'_takes_precedence'] = True
            legacy = {key:value for key,value in data.items() if key not in ('resumeSubmission','resumption')}
            async def old_response(route):
                await route.fulfill(status=200, content_type='application/json', body=json.dumps(legacy))
            await page.route(target, old_response)
            await query()
            assert '恢复执行状态' not in await page.locator('#run-result').inner_text()
            report['checks']['synthetic_legacy_compatible'] = True
            await page.unroute(target, old_response)
            assert await page.evaluate('localStorage.length + sessionStorage.length') == 0
            assert token not in page.url
            await page.locator('#logout').click()
            await expect(page.locator('#run-result')).to_contain_text('还没有查询结果')
            assert data['resumption']['runId'] not in await page.locator('#run-result').inner_text()
            assert await page.locator('#token').input_value() == ''
            report['checks']['storage_empty_logout_cleared'] = True
            assert not blocked and not errors and not csp
            report.update({'nonReadRequests':blocked, 'pageErrors':errors, 'cspErrors':csp, 'passed':True})
            write(OUT/'report.json', report)
            print('Read-only browser checks passed; no write requests or model execution.')
        finally:
            await browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, default=OUT/'browser.json')
    asyncio.run(main(parser.parse_args().config))
