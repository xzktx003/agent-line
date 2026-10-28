"""Browser UI contract tests with mock approval API; no real decisions submitted."""
import asyncio,json
from pathlib import Path
from playwright.async_api import async_playwright
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'var/gateway'
BASE='http://10.30.0.28:3193/workbench/approvals.html'
CHROME='/data01/home/zukang.xu/.cache/ms-playwright/chromium_headless_shell-1223/chrome-headless-shell-linux64/chrome-headless-shell'
TOKEN='mock-human-approval-credential'
async def main():
    checks=[];posts=[];headers=[];mode='approve';cursor_requests=[]
    def record(id='approval-1'):
        return {'approvalId':id,'projectId':'项目甲','workflowId':'workflow-native','runId':'run-original','blockId':'节点：代码审阅','contextId':'pause-context','revision':'v1','activationId':'activation-1','attemptId':'attempt-1','expiresAt':'2026-10-22T12:00:00Z','purpose':'human_approval','status':'waiting','decision':None,'boundPauseObserved':True,'deliveryStatus':'not_submitted','runStatus':'paused'}
    current=record()
    async with async_playwright() as p:
        browser=await p.chromium.launch(executable_path=CHROME,args=['--no-sandbox'])
        page=await browser.new_page(viewport={'width':1440,'height':1100})
        async def api(route):
            nonlocal current
            req=route.request;headers.append(req.headers.get('authorization'));assert TOKEN not in req.url
            if req.method=='POST':
                body=req.post_data_json;posts.append(body)
                assert set(body)=={'decisionId','decision'}
                if mode=='unknown':await route.abort('failed');return
                current.update(status='decision_recorded',decision=body,deliveryStatus='uncertain' if mode=='delivery-unknown' else 'accepted',runStatus='running')
                await route.fulfill(json=current);return
            if req.url.split('?')[0].endswith('/approvals'):
                cursor_requests.append(req.url)
                await route.fulfill(json={'approvals':[current],'nextCursor':None if 'after=' in req.url else 'cursor-2'});return
            await route.fulfill(json=current)
        await page.route('**/extension/v1/approvals**',api)
        await page.goto(BASE)
        assert await page.locator('#approval-approve').is_disabled()
        async def login():
            await page.locator('#approval-token').fill(TOKEN)
            await page.locator('#approval-login button').click()
            await page.wait_for_function("()=>document.querySelector('#approval-connection').textContent==='审批身份已连接'")
        async def select():
            await page.locator('#approval-list button').first.click()
            await page.wait_for_function("()=>!document.querySelector('#approval-query').disabled")
        await login();await select()
        assert await page.locator('#approval-token').input_value()==''
        assert await page.evaluate('()=>localStorage.length+sessionStorage.length')==0
        assert set(headers)=={'Bearer '+TOKEN}
        checks.append('独立人工凭证仅内存，未写URL/浏览器存储')
        await page.locator('#approval-next').click()
        await page.wait_for_function("()=>document.querySelector('#approval-page').textContent==='第 2 页'")
        assert any('after=cursor-2' in u for u in cursor_requests)
        checks.append('授权列表与游标分页')
        await page.locator('#approval-approve').click()
        await page.locator('#approval-confirm-submit').click()
        await page.wait_for_function("()=>document.querySelector('#approval-decision-state').textContent.includes('提交已处理')")
        assert posts[0]['decision']=='approve'
        assert await page.locator('#approval-reject').is_disabled()
        assert '恢复请求已接收' in await page.locator('#approval-detail').inner_text()
        checks.append('批准不可重复，接收回执不冒充完成')
        # Select another fixture; this remains a mock, not a real approval task.
        current=record('approval-2');mode='reject'
        await page.locator('#approval-refresh').click();await select()
        await page.locator('#approval-reject').click();await page.locator('#approval-confirm-submit').click()
        await page.wait_for_function("()=>document.querySelector('#approval-decision-state').textContent.includes('提交已处理')")
        assert posts[-1]['decision']=='reject'
        checks.append('拒绝显式确认及正确请求体')
        current=record('approval-3');mode='unknown'
        await page.locator('#approval-refresh').click();await select()
        await page.locator('#approval-approve').click();await page.locator('#approval-confirm-submit').click()
        await page.wait_for_function("()=>document.querySelector('#approval-decision-state').textContent.includes('提交结果未知')")
        fixed_id=posts[-1]['decisionId'];count=len(posts)
        await page.locator('#approval-query').click()
        await page.wait_for_function("()=>!document.querySelector('#approval-query').disabled")
        assert fixed_id in await page.locator('#approval-decision-state').inner_text()
        assert await page.locator('#approval-approve').is_disabled()
        await page.locator('#approval-logout').click();await login();await select()
        assert fixed_id in await page.locator('#approval-decision-state').inner_text()
        assert len(posts)==count and await page.locator('#approval-reject').is_disabled()
        checks.append('未知结果保留决定标识，查询/重连不重发')
        await page.screenshot(path=str(OUT/'approval-desktop-unknown.png'),full_page=True)
        current=record('approval-delivery-unknown');mode='delivery-unknown'
        await page.locator('#approval-refresh').click();await select()
        await page.locator('#approval-reject').click();await page.locator('#approval-confirm-submit').click()
        await page.wait_for_function("()=>document.querySelector('#approval-decision-state').textContent.includes('提交结果未知')")
        await page.locator('#approval-query').click()
        await page.wait_for_function("()=>!document.querySelector('#approval-query').disabled")
        assert '提交结果未知' in await page.locator('#approval-decision-state').inner_text()
        assert await page.locator('#approval-approve').is_disabled()
        checks.append('服务已记录决定但投递uncertain仍不可重发')
        current=record('approval-xss');current['projectId']='<img src=x onerror="window.__xss=true"> '+TOKEN
        current['boundPauseObserved']=False
        await page.locator('#approval-refresh').click();await select()
        assert await page.locator('#approval-approve').is_disabled()
        assert await page.locator('#approval-detail img,#approval-list img').count()==0
        assert await page.evaluate('()=>window.__xss===undefined')
        assert TOKEN not in await page.locator('body').inner_text()
        checks.append('未确认暂停点禁止决定；XSS文本安全且凭证脱敏')
        current=record('approval-expired');current['status']='expired'
        await page.locator('#approval-refresh').click();await select()
        assert await page.locator('#approval-reject').is_disabled()
        checks.append('过期记录只读')
        current=record('approval-mobile')
        await page.locator('#approval-refresh').click();await select()
        await page.set_viewport_size({'width':390,'height':844})
        assert await page.evaluate('()=>document.documentElement.scrollWidth<=innerWidth')
        await page.screenshot(path=str(OUT/'approval-mobile.png'),full_page=True)
        checks.append('移动端无横向溢出')
        await browser.close()
    report={'evidence':'real browser with mocked API; no real approvals changed','passed':True,'checks':checks,'postCount':len(posts)}
    (OUT/'approval-browser-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False))
asyncio.run(main())
