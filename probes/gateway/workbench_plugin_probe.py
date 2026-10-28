"""Read-only live QwenPaw workbench embedding; fixture checks are labeled separately."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'var/gateway/workbench-plugin'
BASE = 'http://10.30.0.28:3194'
CHROME = '/data01/home/zukang.xu/.cache/ms-playwright/chromium_headless_shell-1223/chrome-headless-shell-linux64/chrome-headless-shell'


def save(name, value):
    path = OUT / name
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def private_json(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('private_regular_configuration_required')
    return json.loads(path.read_text())


def check_health(health, *, expect_ready=False):
    if expect_ready:
        if health.get('readyForExecution') is not True or health.get('controlledExecution') != 'confirmed':
            raise RuntimeError('main_execution_not_confirmed_ready')
    elif health.get('readyForExecution') is not False:
        raise RuntimeError('main_execution_must_remain_disabled')


async def plugin_menu(page):
    await page.get_by_text('Console account', exact=True).click()
    await page.get_by_text('Settings', exact=True).click()
    await page.get_by_text('工作流工作台', exact=True).first.click()
    await page.get_by_role('button', name='打开中文工作台', exact=True).wait_for()


async def overflow(frame):
    return await frame.locator('body').evaluate('el=>({width:innerWidth,scroll:document.documentElement.scrollWidth,overflow:document.documentElement.scrollWidth>innerWidth+1})')


async def main(*, expect_ready=False, live_only=False, expected_task_ids=()):
    global OUT
    if expect_ready:
        OUT = ROOT / 'var/gateway/workbench-plugin-ready'
        if len(expected_task_ids) != 2 or len(set(expected_task_ids)) != 2:
            raise ValueError('two_original_task_identities_required')
    OUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    report = {'live': {}, 'fixtures': {}, 'extensionMutationAttempts': [], 'otherMutationAttempts': [], 'authSessionPosts': 0,
              'expectedReady': expect_ready, 'liveOnly': live_only}
    qwen = private_json(ROOT / 'var/qwenpaw-secrets/local-login.json')
    service_file = ROOT / 'var/adapter/service.json'
    config = private_json(service_file)
    service_token = next(iter(config['tokens']))
    authoring = None
    if expect_ready:
        authoring_file = Path(config['authoringFile'])
        authoring = private_json(authoring_file if authoring_file.is_absolute() else service_file.parent / authoring_file)
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=CHROME, args=['--no-sandbox'])
        context = await browser.new_context(viewport={'width': 1440, 'height': 1000})
        async def forbid_mutation(route):
            if route.request.method != 'GET':
                path = urlsplit(route.request.url).path
                key = 'extensionMutationAttempts' if path.startswith('/extension/v1/') else 'otherMutationAttempts'
                report[key].append({'method': route.request.method, 'path': path})
                await route.abort()
            else:
                await route.continue_()
        await context.route('**/*' if live_only else '**/extension/v1/**', forbid_mutation)
        page = await context.new_page()
        try:
            login = await context.request.post(BASE + '/qwenpaw/api/auth/login', data=qwen)
            report['authSessionPosts'] += 1
            if login.status != 200:
                raise RuntimeError('official_qwen_login_failed')
            auth = await login.json(); token = auth.get('token') or auth.get('access_token')
            await page.goto(BASE + '/qwenpaw/')
            await page.evaluate('(token)=>localStorage.setItem("qwenpaw_auth_token",token)', token)
            await page.reload()
            await page.wait_for_timeout(2000)
            await plugin_menu(page)
            plugin_url = page.url
            await page.get_by_role('button', name='打开中文工作台', exact=True).click()
            frame = page.frame_locator('iframe[title="Agent Line 中文工作台"]')
            await frame.get_by_role('heading', name='把工作交给流程', exact=True).wait_for()
            report['live']['menuOpensChineseWorkbench'] = True
            await frame.locator('#token').fill(service_token)
            async with page.expect_response(lambda r: '/extension/v1/health' in r.url) as awaited:
                await frame.get_by_role('button', name='连接服务', exact=True).click()
            health_response = await awaited.value
            health = await health_response.json()
            report['live']['health'] = health
            check_health(health, expect_ready=expect_ready)
            if health_response.status != 200:
                raise RuntimeError('authenticated_health_failed')
            unauthenticated = await context.request.get(BASE+'/extension/v1/health')
            report['live']['anonymousHealthStatus'] = unauthenticated.status
            if unauthenticated.status != 401:
                raise RuntimeError('anonymous_health_must_require_authentication')
            await frame.locator('#task-list button').first.wait_for()
            # Query actual directory IDs from the same authenticated read API; never invent fixtures.
            response = await context.request.get(BASE+'/extension/v1/node-tasks', headers={'Authorization':'Bearer '+service_token})
            if response.status != 200:
                raise RuntimeError('main_task_list_failed')
            tasks = [{'taskId': task_id} for task_id in expected_task_ids] if expected_task_ids else (await response.json())['tasks'][:2]
            report['live']['queriedTasks'] = []
            for task in tasks:
                tid = task.get('taskId') or task.get('id')
                await frame.locator('#task-id').fill(tid)
                async with page.expect_response(lambda r: r.url.endswith('/extension/v1/node-tasks/'+tid)) as task_response:
                    await frame.get_by_role('button', name='查询任务', exact=True).click()
                actual = await task_response.value
                result = await actual.json()
                if actual.status != 200 or result.get('taskId') != tid:
                    raise RuntimeError('original_task_not_readable')
                report['live']['queriedTasks'].append({'taskId':tid,'httpStatus':actual.status,'executionStatus':result.get('executionStatus'),'verificationStatus':result.get('verificationStatus')})
            if len(report['live']['queriedTasks']) != 2:
                raise RuntimeError('two_existing_tasks_required')
            report['live']['desktopOverflow'] = await overflow(frame)
            await page.screenshot(path=str(OUT/'desktop-task-proof.png'), full_page=True)
            await frame.locator('body').evaluate('()=>scrollTo(0,0)')
            await page.screenshot(path=str(OUT/'desktop-workbench.png'), full_page=True)
            await frame.get_by_role('link', name='流程搭建', exact=True).click()
            await frame.get_by_role('heading', name='流程搭建', exact=True).wait_for()
            report['live']['editorNavigation'] = True
            if authoring:
                credential = authoring['credentials'][0]
                await frame.locator('#token').fill(credential['token'])
                await frame.locator('#login').click()
                await expect(frame.locator('#connection')).to_have_text('编辑服务已连接')
                await frame.locator('#project').select_option(credential['projectScopes'][0])
                await expect(frame.locator('#refresh')).to_be_enabled()
                report['live']['authenticatedEditorReadable'] = True
            await page.screenshot(path=str(OUT/'desktop-editor.png'), full_page=True)
            await frame.get_by_role('link', name='运行工作台', exact=True).click()
            await frame.get_by_role('heading', name='把工作交给流程', exact=True).wait_for()
            report['live']['navigationClearsCredential'] = await frame.locator('#token').input_value() == '' and await frame.locator('#login-form').is_visible()
            await page.set_viewport_size({'width':390,'height':844})
            await page.wait_for_function('document.querySelector("iframe").clientWidth > 250')
            await page.wait_for_timeout(350)
            report['live']['mobileOverflow'] = await overflow(frame)
            report['live']['hostMobileOverflow'] = await page.evaluate('({width:innerWidth,scroll:document.documentElement.scrollWidth,overflow:document.documentElement.scrollWidth>innerWidth+1})')
            await page.screenshot(path=str(OUT/'mobile-workbench.png'), full_page=True)
            await frame.get_by_role('link', name='流程搭建', exact=True).click()
            await frame.get_by_role('heading', name='流程搭建', exact=True).wait_for()
            report['live']['mobileEditorOverflow'] = await overflow(frame)
            await page.screenshot(path=str(OUT/'mobile-editor.png'), full_page=True)
            if any(report['live'][key]['overflow'] for key in ('desktopOverflow','mobileOverflow','hostMobileOverflow','mobileEditorOverflow')):
                raise RuntimeError('workbench_horizontal_overflow')
            if authoring:
                # Reload must clear a genuinely connected page, not an already
                # empty credential field left by navigation.
                await frame.locator('#token').fill(credential['token'])
                await frame.locator('#login').click()
                await expect(frame.locator('#connection')).to_have_text('编辑服务已连接')
                report['live']['reloadHadAuthenticatedEditor'] = True
            await page.set_viewport_size({'width':1440,'height':1000})
            async with page.expect_event('dialog') as dialog_event:
                click = asyncio.create_task(page.get_by_role('button',name='重新加载工作台',exact=True).click())
            await (await dialog_event.value).accept();await click
            await frame.get_by_role('heading',name='把工作交给流程',exact=True).wait_for()
            report['live']['reloadClearsPageCredential'] = await frame.locator('#token').input_value()==''
            async with page.expect_event('dialog') as dialog_event:
                click = asyncio.create_task(page.get_by_role('button',name='关闭工作台',exact=True).click())
            await (await dialog_event.value).accept();await click
            report['live']['confirmedCloseRemovesIframe'] = await page.locator('iframe').count()==0
            await page.get_by_role('button',name='打开中文工作台',exact=True).click()
            await frame.get_by_role('heading',name='把工作交给流程',exact=True).wait_for()
            if not live_only:
                # Dismissing mode switch must leave the current iframe intact.
                before = await page.locator('iframe').get_attribute('src')
                async with page.expect_event('dialog') as dialog_event:
                    click = asyncio.create_task(page.get_by_role('button',name='打开原版画布',exact=True).click())
                dialog = await dialog_event.value
                await dialog.dismiss();await click
                report['live']['dismissSwitchPreservesIframe'] = await page.locator('iframe').get_attribute('src') == before
                async with page.expect_event('dialog') as dialog_event:
                    click = asyncio.create_task(page.get_by_role('button',name='打开原版画布',exact=True).click())
                await (await dialog_event.value).accept();await click
                simframe=page.frame_locator('iframe[title="Sim 原版工作流编辑器"]')
                await simframe.locator('input[name="email"]').wait_for(timeout=20000)
                report['live']['simSeparateLoginRequired'] = True
                async with page.expect_event('dialog') as dialog_event:
                    click=asyncio.create_task(page.get_by_role('button',name='打开中文工作台',exact=True).click())
                await (await dialog_event.value).accept();await click
            await frame.locator('#token').fill(service_token)
            await frame.get_by_role('button',name='连接服务',exact=True).click()
            await frame.locator('#connected-actions').wait_for(state='visible')
            await page.get_by_text('Console account',exact=True).click()
            await page.get_by_text('Logout',exact=True).click()
            await page.wait_for_timeout(700)
            report['live']['qwenLogoutRemovesIframe'] = await page.locator('iframe').count()==0
            login=await context.request.post(BASE+'/qwenpaw/api/auth/login',data=qwen)
            report['authSessionPosts'] += 1
            auth=await login.json();token=auth.get('token') or auth.get('access_token')
            await page.evaluate('(token)=>localStorage.setItem("qwenpaw_auth_token",token)',token)
            await page.goto(plugin_url)
            await page.get_by_role('button',name='打开中文工作台',exact=True).click()
            await frame.locator('#login-form').wait_for(state='visible')
            report['live']['reloginHasNoTemporaryCredential'] = await frame.locator('#token').input_value()==''
            if not live_only:
                # Fixture routes deliberately test only public configuration handling.
                async def failed_config(route):await route.fulfill(status=500,body='unavailable')
                await page.route('**/agent-line-gateway/config',failed_config)
                await page.reload()
                retry=page.get_by_role('button',name=re.compile('重试'))
                await retry.wait_for()
                await page.unroute('**/agent-line-gateway/config',failed_config)
                await retry.click()
                await page.get_by_role('button',name='打开中文工作台',exact=True).wait_for(state='visible')
                await expect(page.get_by_role('button',name='打开中文工作台',exact=True)).to_be_enabled()
                report['fixtures']['config500RetryRecovers'] = await page.get_by_role('button',name='打开中文工作台',exact=True).is_enabled()
                async def malicious_config(route):await route.fulfill(status=200,json={'simEditorPath':'/workspace','workbenchPath':'https://example.invalid/workbench/'})
                await page.route('**/agent-line-gateway/config',malicious_config)
                await page.reload()
                await page.get_by_role('button',name=re.compile('重试')).wait_for()
                report['fixtures']['crossOriginConfigRejected'] = await page.locator('iframe').count()==0 and await page.get_by_role('button',name='打开中文工作台',exact=True).is_disabled()
                await page.unroute('**/agent-line-gateway/config',malicious_config)
            if report['extensionMutationAttempts'] or report['otherMutationAttempts']:
                raise RuntimeError('unexpected_business_mutation_attempt')
            for key in ('navigationClearsCredential','reloadClearsPageCredential','confirmedCloseRemovesIframe',
                        'qwenLogoutRemovesIframe','reloginHasNoTemporaryCredential'):
                if report['live'].get(key) is not True:
                    raise RuntimeError('credential_or_lifecycle_check_failed')
            report['scope'] = 'pure_live_get_queries_and_existing_qwen_auth_session_only' if live_only else 'live_queries_plus_explicit_config_fixtures'
            report['status']='completed_checks'
        except Exception as exc:
            report['status']='incomplete';report['failureType']=type(exc).__name__
            await page.screenshot(path=str(OUT/'failure.png'),full_page=True)
            raise
        finally:
            save('report.json',report)
            await browser.close()
    print(json.dumps({'status':report['status'],'extensionMutationAttempts':len(report['extensionMutationAttempts']),'queriedTasks':len(report['live'].get('queriedTasks',[]))}))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expect-ready',action='store_true',help='Require confirmed/ready main; default retains guarded expectation')
    parser.add_argument('--live-only',action='store_true',help='Skip Sim mode/config fixtures; allow only browser GET requests')
    parser.add_argument('--expected-task-id',action='append',default=[],help='Existing task identity; specify the two original IDs in ready mode')
    args=parser.parse_args()
    try:
        asyncio.run(main(expect_ready=args.expect_ready,live_only=args.live_only,expected_task_ids=args.expected_task_id))
    except Exception as error:
        raise SystemExit('Workbench browser verification failed: '+type(error).__name__) from None
