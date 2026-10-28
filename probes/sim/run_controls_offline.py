"""Offline intercepted control API; no server, real stop or model request."""
import asyncio
import json
import mimetypes
import re
from pathlib import Path
from playwright.async_api import async_playwright, expect
ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'.omx/state/run-controls'
CHROME='/data01/home/zukang.xu/.cache/ms-playwright/chromium_headless_shell-1223/chrome-headless-shell-linux64/chrome-headless-shell'

async def main():
    OUT.mkdir(parents=True,exist_ok=True)
    control={'supported':True,'state':'active','version':0,'operationId':None,'taskCounts':{'total':4,'queued':1,'active':1,'uncertain':1,'resourcesUnreleased':2,'verificationPending':1,'verificationCancelRequested':1,'verificationCanceled':1}}
    posts=[];errors=[];csp=[];mode='ok';supported=True
    async with async_playwright() as p:
        browser=await p.chromium.launch(executable_path=CHROME,args=['--no-sandbox'])
        try:
            context=await browser.new_context(viewport={'width':1440,'height':1000})
            async def route(r):
                nonlocal control
                path=r.request.url.split('controls.test')[-1].split('?')[0]
                if path.startswith('/extension/'):
                    if r.request.method=='POST':
                        assert path.endswith('/control');body=r.request.post_data_json;posts.append(body)
                        if mode=='abort':await r.abort();return
                        if mode=='conflict':await r.fulfill(status=409,content_type='application/json',body='{"detail":"version changed"}');return
                        assert body['expectedVersion']==control['version']
                        control={**control,'version':control['version']+1,'operationId':body['operationId'],'state':{'pause':'paused','continue':'active','stop':'stop_requested'}[body['action']]}
                        if body['action']=='pause':
                            assert body.get('activeExecution') in ('continue','interrupt')
                            control['activeExecution']=body['activeExecution']
                        else:
                            assert 'activeExecution' not in body
                            control.pop('activeExecution', None)
                        data={'control':control,'operation':{key:control[key] for key in ('state','version','operationId')}}
                    elif path.endswith('/health'):data={'storage':'available','controlledExecution':'unknown','readyForExecution':False}
                    elif path.endswith('/workflows'):data={'workflows':[{'bindingId':'binding','projectId':'offline'}]}
                    elif path.endswith('/node-tasks'):data={'tasks':[],'nextCursor':None}
                    else:data={'submission':{'status':'accepted'},'execution':{'runId':'run','status':'run_canceled' if control['state']=='stop_requested' else 'running'},**({'control':control} if supported else {})}
                    await r.fulfill(status=200,content_type='application/json',body=json.dumps(data));return
                file=ROOT/'frontend'/path.removeprefix('/workbench/')
                if path.endswith('/'):file=file/'index.html'
                if file.is_file():await r.fulfill(status=200,content_type=mimetypes.guess_type(file)[0] or 'application/octet-stream',body=file.read_bytes(),headers={'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self' data:"})
                else:await r.fulfill(status=404,body='not found')
            await context.route('**/*',route)
            page=await context.new_page();page.on('pageerror',lambda e:errors.append(str(e)));page.on('console',lambda m:csp.append(m.text) if 'Content Security Policy' in m.text else None)
            await page.goto('https://controls.test/workbench/runs.html');await page.locator('#token').fill('offline-only-token');await page.locator('#login-form button').click();await expect(page.locator('#connection-badge')).to_have_text('操作已授权')
            await page.evaluate("Object.defineProperty(crypto,'randomUUID',{value:undefined,configurable:true})")
            assert not await page.locator('#run-controls').is_visible()
            await page.locator('#query-binding').fill('binding');await page.locator('#run-id').fill('run')
            async def query():
                await page.locator('#run-form > button[type="submit"]').click();await expect(page.locator('#run-form > button[type="submit"]')).to_be_enabled()
            await query();await expect(page.locator('#pause-run')).to_be_enabled()
            await page.locator('#pause-run').click();await expect(page.locator('#pause-run-dialog')).to_be_visible()
            await expect(page.locator('#pause-run-dialog')).to_contain_text('未确认前不能视为已停止')
            assert len(posts)==0
            await page.locator('#dismiss-pause-run').click();await expect(page.locator('#pause-run-dialog')).to_be_hidden();assert len(posts)==0
            await page.locator('#pause-run').click();await page.locator('#pause-run-continue').click();await expect(page.locator('#continue-run')).to_be_enabled()
            await expect(page.locator('#run-control-hint')).to_contain_text('活动任务继续执行')
            assert posts[-1]['activeExecution']=='continue'
            saved={**control,'taskCounts':dict(control['taskCounts'])}
            control={**control,'activeExecution':'unrecorded'}
            await query();await expect(page.locator('#run-control-hint')).to_contain_text('没有记录')
            await expect(page.locator('#run-control-hint')).not_to_contain_text('已停止')
            await expect(page.locator('#run-control-state')).to_contain_text('后继任务准入已暂停')
            control=saved;await query();await expect(page.locator('#run-control-hint')).to_contain_text('活动任务继续执行')
            await expect(page.locator('#run-control-counts')).to_contain_text('编码资源待确认 2')
            await expect(page.locator('#run-control-counts')).to_contain_text('测试验收待确认 1 · 验收停止待确认 1 · 已取消验收 1')
            original_counts=dict(control['taskCounts'])
            control['taskCounts']={k:v for k,v in original_counts.items() if not k.startswith('verification')}
            await query();await expect(page.locator('#run-control-counts')).to_contain_text('测试验收状态未提供')
            await expect(page.locator('#continue-run')).to_be_enabled()
            for bad in ({'verificationPending':1}, {'verificationPending':True,'verificationCancelRequested':1,'verificationCanceled':1},
                        {'verificationPending':-1,'verificationCancelRequested':1,'verificationCanceled':1},
                        {'verificationPending':'1','verificationCancelRequested':1,'verificationCanceled':1}):
                control['taskCounts']={k:v for k,v in original_counts.items() if not k.startswith('verification')};control['taskCounts'].update(bad)
                await query();await expect(page.locator('#run-control-counts')).to_contain_text('测试验收状态待核对')
                await expect(page.locator('#continue-run')).to_be_enabled()
                assert '测试验收待确认 0' not in await page.locator('#run-control-counts').inner_text()
            control['taskCounts']=original_counts;await query()
            await page.locator('#run-controls').scroll_into_view_if_needed()
            await page.screenshot(path=str(OUT/'desktop.png'),full_page=True)
            await page.set_viewport_size({'width':390,'height':844});await page.screenshot(path=str(OUT/'mobile.png'),full_page=True)
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            mode='conflict';await page.locator('#continue-run').click();await expect(page.locator('#notice')).to_contain_text('版本已变化');assert not await page.locator('#run-controls').is_visible();assert len(posts)==2
            control={**control,'state':'active','version':2,'operationId':'other-operation'}
            await query();mode='abort';await page.locator('#pause-run').click();await page.locator('#pause-run-continue').click();await expect(page.locator('#run-control-hint')).to_contain_text('结果未知');assert len(posts)==3
            unknown=posts[-1]
            await query();await expect(page.locator('#pause-run')).to_be_disabled();assert len(posts)==3
            control={**control,'version':3,'operationId':'newer-other'}
            await query();await expect(page.locator('#run-control-hint')).to_contain_text(unknown['operationId']);await expect(page.locator('#pause-run')).to_be_disabled();assert len(posts)==3
            control={**control,'version':4,'operationId':unknown['operationId'],'state':'paused','activeExecution':unknown['activeExecution']}
            await query();await expect(page.locator('#continue-run')).to_be_enabled();mode='ok'
            await page.locator('#continue-run').click();await expect(page.locator('#pause-run')).to_be_enabled()
            await page.locator('#stop-run').click();await expect(page.locator('#stop-run-dialog')).to_be_visible();await expect(page.locator('#stop-run-dialog')).to_contain_text('不删除角色或记忆');assert len(posts)==4
            await page.locator('#dismiss-stop-run').click();assert len(posts)==4
            await page.locator('#stop-run').click();await page.locator('#confirm-stop-run').click();await expect(page.locator('#run-control-state')).to_contain_text('已申请停止')
            await query();await expect(page.locator('#run-result')).to_contain_text('原生运行已取消');await expect(page.locator('#run-control-counts')).to_contain_text('编码资源待确认 2');await expect(page.locator('#run-control-hint')).to_contain_text('不能证明 Codex 已退出');assert len(posts)==5
            supported=False;await query();assert not await page.locator('#run-controls').is_visible()
            await page.locator('#logout').click();assert await page.locator('#token').input_value()=='';assert await page.locator('#run-control-counts').inner_text()=='';assert not await page.locator('#run-controls').is_visible()
            assert await page.evaluate('localStorage.length+sessionStorage.length')==0 and not errors and not csp
            assert len({body['operationId'] for body in posts})==len(posts)
            assert all(re.fullmatch('control-[0-9a-f]{32}',body['operationId']) for body in posts)
            (OUT/'offline-report.json').write_text(json.dumps({'passed':True,'mode':'offline_fixed_fixture','realHttpRequests':0,'simulatedPosts':len(posts),'pageErrors':errors,'cspErrors':csp,'checks':['http_crypto_fallback_unique_control_ids','verification_counts','legacy_counts_unavailable','partial_and_invalid_counts_unknown','hidden_until_get','pause_choice_before_submit','pause_continue','unrecorded_pause_not_stopped','conflict_requires_get','unknown_no_resubmit','different_operation_newer_version_stays_unknown','exact_get_reconciles','stop_confirmation','native_cancel_not_resource_exit','unsupported_hidden','mobile_no_overflow','logout_storage']},ensure_ascii=False,indent=2))
            print('Offline run control checks passed; zero real control requests.')
        finally:await browser.close()
if __name__=='__main__':asyncio.run(main())
