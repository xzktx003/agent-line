"""Offline intercepted verification cancellation UI. No real API or models."""
import asyncio
import json
import mimetypes
from pathlib import Path
from playwright.async_api import async_playwright, expect
ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'.omx/state/verification-cancel'
CHROME='/data01/home/zukang.xu/.cache/ms-playwright/chromium_headless_shell-1223/chrome-headless-shell-linux64/chrome-headless-shell'

async def main():
    OUT.mkdir(parents=True,exist_ok=True)
    task={'taskId':'task-test','projectId':'offline','roleBindingId':'existing-role','executionStatus':'succeeded',
          'resourcesReleased':True,'verificationStatus':'pending','memoryStatus':'not_requested','canCancelVerification':True,
          'verificationCancellationRequested':False}
    posts=[];errors=[];mode='accepted';release=asyncio.Event();started=asyncio.Event()
    async with async_playwright() as p:
        browser=await p.chromium.launch(executable_path=CHROME,args=['--no-sandbox'])
        try:
            context=await browser.new_context(viewport={'width':1440,'height':1000})
            async def route(r):
                path=r.request.url.split('verification.test')[-1].split('?')[0]
                if path.startswith('/extension/'):
                    if r.request.method=='POST':
                        assert path.endswith('/cancel');posts.append(path)
                        if mode=='unknown':await r.abort();return
                        if mode=='late':started.set();await release.wait()
                        data={**task,'canCancelVerification':False,'verificationCancellationRequested':True}
                    elif path.endswith('/health'):data={'storage':'available','controlledExecution':'unknown','readyForExecution':False}
                    elif path.endswith('/workflows'):data={'workflows':[]}
                    elif path.endswith('/node-tasks'):data={'tasks':[],'nextCursor':None}
                    else:data=task
                    await r.fulfill(status=200,content_type='application/json',body=json.dumps(data));return
                file=ROOT/'frontend'/path.removeprefix('/workbench/')
                if path.endswith('/'):file=file/'index.html'
                if file.is_file():await r.fulfill(status=200,content_type=mimetypes.guess_type(file)[0] or 'application/octet-stream',body=file.read_bytes(),headers={'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self' data:"})
                else:await r.fulfill(status=404,body='not found')
            await context.route('**/*',route)
            page=await context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
            await page.goto('https://verification.test/workbench/')
            async def login():
                await page.locator('#token').fill('offline-fixture-token');await page.locator('#login-form button').click()
                await expect(page.locator('#connection-badge')).to_have_text('服务已连接')
            async def query():
                await page.locator('#task-id').fill(task['taskId']);await page.locator('#task-form button').click()
                await expect(page.locator('#task-result')).to_contain_text(task['taskId'])
            async def confirm():
                await page.locator('#cancel-task').click();await expect(page.locator('#cancel-description')).to_contain_text('不会删除产物')
                await expect(page.locator('#cancel-description')).to_contain_text('不会撤销已完成的编码')
                await page.locator('#confirm-cancel').click()
            await login();await query()
            await expect(page.locator('#cancel-task')).to_have_text('停止验收')
            await expect(page.locator('#task-result')).to_contain_text('编码执行资源已退出，验收仍在进行')
            await confirm();await expect(page.locator('#notice')).to_contain_text('等待测试退出确认')
            await expect(page.locator('#task-result')).to_contain_text('验收停止待确认');assert len(posts)==1
            await page.locator('#task-result').scroll_into_view_if_needed();await page.screenshot(path=str(OUT/'desktop.png'),full_page=True)
            await page.set_viewport_size({'width':390,'height':844});await page.screenshot(path=str(OUT/'mobile.png'),full_page=True)
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            task.update(canCancelVerification=False,verificationCancellationRequested=True,verificationStatus='canceled');await query()
            await expect(page.locator('#task-result')).to_contain_text('已取消验收');assert not await page.locator('#cancel-task').is_visible()
            task.update(taskId='task-unknown',canCancelVerification=True,verificationCancellationRequested=False,verificationStatus='pending')
            mode='unknown';await query();await confirm();await expect(page.locator('#notice')).to_contain_text('未取得明确回执')
            await query();await expect(page.locator('#cancel-task')).to_be_disabled();assert len(posts)==2
            task.update(taskId='task-late');mode='late';await query();await confirm();await started.wait()
            await page.locator('#logout').click();release.set();await page.wait_for_timeout(100)
            await expect(page.locator('#task-result')).to_contain_text('尚未选择节点任务');assert not await page.locator('#cancel-task').is_visible()
            assert await page.locator('#token').input_value()==''
            assert await page.evaluate('localStorage.length+sessionStorage.length')==0
            assert not errors;assert len(posts)==3
            (OUT/'report.json').write_text(json.dumps({'scope':'offline intercepted fixtures only','checks':12,'fakePosts':len(posts),'nativeWrites':0,'pageErrors':errors,'desktop':'1440x1000','mobile':'390x844','storageEntries':0},ensure_ascii=False,indent=2))
            print(json.dumps({'checks':12,'fakePosts':3,'nativeWrites':0,'pageErrors':0}))
        finally:await browser.close()

if __name__=='__main__':asyncio.run(main())
