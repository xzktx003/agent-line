"""Fully intercepted native permission UI fixtures; no upstream requests."""
import asyncio,json,mimetypes,re
from pathlib import Path
from playwright.async_api import async_playwright,expect
ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'.omx/state/native-approvals'
CHROME='/data01/home/zukang.xu/.cache/ms-playwright/chromium_headless_shell-1223/chrome-headless-shell-linux64/chrome-headless-shell'
async def main():
    OUT.mkdir(parents=True,exist_ok=True)
    identity=dict(upstreamTaskId='native',codexThreadId='thread',codexTurnId='turn')
    record=dict(projectId='项目甲',roleAgentId='existing-parent',workflowId='workflow',runId='run-original',nodeId='编写代码',nodeExecutionId='node-execution',attemptId='attempt-one',revision='2',requestId='request',purpose='execution_permission',taskId='task-a',identity=identity,roleSessionId='agent-line-task-a',targetAgentId='codex',providerItemId='item',method='item/commandExecution/requestApproval',createdAt=1000,expiresAt=4102444800,summary='运行当前项目测试；不会自动批准其他请求',severity='high',status='waiting',bound=True,decision=None,deliveryStatus='not_submitted')
    posts=[];errors=[];csp=[];mode='ok';empty=False;release=asyncio.Event();started=asyncio.Event()
    async with async_playwright() as p:
        browser=await p.chromium.launch(executable_path=CHROME,args=['--no-sandbox'])
        try:
            context=await browser.new_context(viewport={'width':1440,'height':1000})
            async def route(r):
                path=r.request.url.split('native.test')[-1].split('?')[0]
                if path.startswith('/extension/'):
                    if path=='/extension/v1/approvals':data={'approvals':[],'nextCursor':None}
                    elif path.endswith('/health'):data={'storage':'available','controlledExecution':'unknown','readyForExecution':False}
                    elif path.endswith('/workflows'):data={'workflows':[]}
                    elif path.endswith('/node-tasks'):data={'tasks':[],'nextCursor':None}
                    elif '/execution-approvals' not in path and '/node-tasks/' in path:data={'taskId':path.split('/')[-1],'projectId':'fixture','executionStatus':'accepted','resourcesReleased':False,'verificationStatus':'pending'}
                    elif '/execution-approvals' in path:
                        task=path.split('/')[4]
                        current={**record,'taskId':task,'roleSessionId':'agent-line-'+task}
                        if task=='forbidden':await r.fulfill(status=404,content_type='application/json',body='{}');return
                        if r.request.method=='POST':
                            body=r.request.post_data_json;assert set(body)=={'decisionId','decision','expectedIdentity'};posts.append(body)
                            if mode=='stale':await r.fulfill(status=409,content_type='application/json',body='{}');return
                            assert body['expectedIdentity']==identity
                            if mode=='abort':await r.abort();return
                            if mode=='late':started.set();await release.wait()
                            data={**current,'decision':{**{k:body[k] for k in ('decisionId','decision')},'decidedBy':'human'},'status':'decision_recorded','deliveryStatus':'accepted'}
                        else:data={'taskId':task,'identity':identity,'approvals':[] if empty else [current]}
                    else:raise AssertionError('unexpected API '+path)
                    await r.fulfill(status=200,content_type='application/json',body=json.dumps(data));return
                file=ROOT/'frontend'/path.removeprefix('/workbench/')
                if file.is_file():await r.fulfill(status=200,content_type=mimetypes.guess_type(file)[0] or 'application/octet-stream',body=file.read_bytes(),headers={'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self' data:"})
                else:await r.fulfill(status=404,body='')
            await context.route('**/*',route);page=await context.new_page()
            page.on('pageerror',lambda e:errors.append(str(e)));page.on('console',lambda m:csp.append(m.text) if 'Content Security Policy' in m.text else None)
            await page.goto('https://native.test/workbench/approvals.html#execution-task=task-a')
            assert await page.locator('#native-task').input_value()=='task-a'
            await expect(page.locator('#native-query')).to_be_disabled()
            await page.locator('#approval-token').fill('offline-human');await page.locator('#approval-login button').click()
            await expect(page.locator('#approval-connection')).to_have_text('审批身份已连接')
            async def query(task):
                await page.locator('#native-task').fill(task);await page.locator('#native-query').click()
                await expect(page.locator('#native-message')).not_to_contain_text('请查询当前任务')
            async def decide(choice):
                await page.locator('#native-'+choice).click();await expect(page.locator('#native-confirm')).to_be_visible();await expect(page.locator('#native-confirm-context')).to_contain_text('原运行：run-original');await expect(page.locator('#native-confirm-context')).to_contain_text('本次尝试：attempt-one');await page.locator('#native-confirm-submit').click()
            await query('task-a');await expect(page.locator('#native-approve')).to_be_enabled()
            await page.locator('#execution-permissions').scroll_into_view_if_needed();await page.screenshot(path=str(OUT/'desktop.png'),full_page=True)
            await page.set_viewport_size({'width':390,'height':844});await page.screenshot(path=str(OUT/'mobile.png'),full_page=True)
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            await page.evaluate("Object.defineProperty(crypto,'randomUUID',{value:undefined,configurable:true})")
            await decide('approve');await expect(page.locator('#native-message')).to_contain_text('不代表执行、测试或流程成功');assert len(posts)==1
            assert re.fullmatch('[0-9a-f]{32}',posts[0]['decisionId'])
            await query('task-a');await expect(page.locator('#native-approve')).to_be_disabled()
            await query('task-reject');await decide('reject');await expect(page.locator('#native-message')).to_contain_text('权限回应已接收');assert posts[-1]['decision']=='reject'
            await query('forbidden');await expect(page.locator('#native-message')).to_contain_text('未确认批准');await expect(page.locator('#native-approve')).to_be_disabled()
            for change in ({'bound':False,'status':'unbound'},{'expiresAt':1,'status':'expired'},{'status':'not_observed'},{'decision':{'decisionId':'old','decision':'approve','decidedBy':'human'},'status':'decision_recorded','deliveryStatus':'intent'}):
                old=dict(record);record.update(change);await query('task-state');await expect(page.locator('#native-approve')).to_be_disabled();record=old
            empty=True;await query('task-empty');await expect(page.locator('#native-message')).to_contain_text('不能据此推断已批准');empty=False
            mode='abort';await query('task-unknown');await decide('approve');await expect(page.locator('#native-message')).to_contain_text('只查询原记录');await query('task-unknown');await expect(page.locator('#native-approve')).to_be_disabled();assert len(posts)==3
            assert re.fullmatch('[0-9a-f]{32}',posts[2]['decisionId'])
            await expect(page.locator('#native-decision-state')).to_contain_text(posts[2]['decisionId'])
            mode='ok';await query('task-stale');await page.locator('#native-approve').click()
            identity['codexTurnId']='new-turn'
            await page.locator('#native-confirm-back').click();await query('task-stale');await expect(page.locator('#native-detail')).to_contain_text('new-turn');assert len(posts)==3
            mode='stale';await decide('approve');await expect(page.locator('#native-message')).to_contain_text('状态冲突');await expect(page.locator('#native-approve')).to_be_disabled();assert len(posts)==4
            mode='late';await query('task-late');await decide('reject');await started.wait();await query('task-new')
            release.set();await page.wait_for_timeout(100);await expect(page.locator('#native-detail')).to_contain_text('task-new');assert len(posts)==5
            release.clear();started.clear();await query('task-disconnect');await decide('approve');await started.wait()
            await page.locator('#approval-logout').click();release.set();await page.wait_for_timeout(100);assert len(posts)==6
            await expect(page.locator('#native-detail')).to_have_text('尚未选择执行权限请求');await expect(page.locator('#native-query')).to_be_disabled()
            assert await page.locator('#approval-token').input_value()=='' and await page.locator('#native-task').input_value()==''
            assert await page.evaluate('localStorage.length+sessionStorage.length')==0 and not errors and not csp
            await page.goto('https://native.test/workbench/index.html')
            await page.locator('#token').fill('offline-service');await page.locator('#login-form button').click()
            await expect(page.locator('#connection-badge')).to_have_text('服务已连接')
            for task in ('task-link-a','task-link-b'):
                await page.locator('#task-id').fill(task);await page.locator('#task-form button').click()
                link=page.locator('#task-result a',has_text='查看执行权限审批')
                await expect(link).to_have_attribute('href','./approvals.html#execution-task='+task)
                assert await link.count()==1 and 'offline-service' not in await link.get_attribute('href')
            await page.set_viewport_size({'width':1440,'height':1000});await page.locator('#task-result').scroll_into_view_if_needed();await page.screenshot(path=str(OUT/'task-link-desktop.png'),full_page=True)
            await page.set_viewport_size({'width':390,'height':844});await page.screenshot(path=str(OUT/'task-link-mobile.png'),full_page=True)
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            await page.locator('#logout').click();assert await page.locator('#task-result a').count()==0
            assert not errors and not csp and len(posts)==6
            report={'mode':'offline_intercepted_fixture','simulatedPosts':len(posts),'realWrites':0,'pageErrors':errors,'cspErrors':csp,'checks':['http_crypto_fallback','fallback_unknown_stable_id','task_link_current_and_no_secret','hash_prefill','shared_human_login','approve','reject','scope404','unbound_expired_not_observed','history_intent','empty_not_approved','unknown_no_repost','fresh_turn_query','stale_turn409','late_response_switch_task','late_response_disconnect','disconnect_clear','storage0','mobile_no_overflow']}
            (OUT/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report))
        finally:await browser.close()
if __name__=='__main__':asyncio.run(main())
