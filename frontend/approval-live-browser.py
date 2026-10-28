"""Submit only pre-bound isolated test approvals using the actual Chinese UI."""
import asyncio,json,os
from pathlib import Path
from playwright.async_api import async_playwright
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'var/gateway'
BASE='http://10.30.0.28:3193'
CHROME='/data01/home/zukang.xu/.cache/ms-playwright/chromium_headless_shell-1223/chrome-headless-shell-linux64/chrome-headless-shell'
def save(path,data):
    fd=os.open(path,os.O_CREAT|os.O_TRUNC|os.O_WRONLY,0o600)
    with os.fdopen(fd,'w') as f:json.dump(data,f,ensure_ascii=False,indent=2)
async def main():
    setup=json.loads((ROOT/'var/approvals-live/ui-state.json').read_text())
    assert setup['setup']=='trusted_test_setup_no_decision'
    token=json.loads((ROOT/'var/adapter/approvals.json').read_text())['credentials'][0]['token']
    sim=json.loads((ROOT/'var/sim/probe-auth.json').read_text())
    statefile=OUT/'approval-live-browser-state.json'
    states=json.loads(statefile.read_text()) if statefile.exists() else {}
    async with async_playwright() as p:
        browser=await p.chromium.launch(executable_path=CHROME,args=['--no-sandbox'])
        context=await browser.new_context(viewport={'width':1440,'height':1100},timezone_id='Asia/Shanghai')
        page=await context.new_page()
        await page.goto(BASE+'/workbench/approvals.html')
        await page.locator('#approval-token').fill(token)
        await page.locator('#approval-login button').click()
        await page.wait_for_function("()=>document.querySelector('#approval-connection').textContent==='审批身份已连接'")
        assert await page.locator('#approval-token').input_value()==''
        for branch in ['approve','reject']:
            run=setup['runs'][branch];id=run['approvalId']
            await page.get_by_role('button').filter(has=page.get_by_text(id,exact=True)).click()
            await page.wait_for_function("()=>!document.querySelector('#approval-query').disabled")
            assert run['runId'] in await page.locator('#approval-detail').inner_text()
            if id not in states:
                assert await page.locator('#approval-'+branch).is_enabled()
                await page.locator('#approval-'+branch).click()
                states[id]={'status':'intent','branch':branch,'runId':run['runId']};save(statefile,states)
                async with page.expect_response(lambda r:r.url.endswith('/approvals/'+id+'/decision') and r.request.method=='POST') as response_info:
                    await page.locator('#approval-confirm-submit').click()
                response=await response_info.value
                assert response.status==200
                result=await response.json()
                states[id].update(status='received',response=result);save(statefile,states)
            else:
                assert states[id]['status']=='received','Unknown prior decision outcome; never repeat'
            for _ in range(50):
                await page.locator('#approval-query').click()
                await page.wait_for_function("()=>!document.querySelector('#approval-query').disabled")
                if '执行已结束' in await page.locator('#approval-detail').inner_text():break
                await page.wait_for_timeout(300)
            assert '执行已结束' in await page.locator('#approval-detail').inner_text()
            assert await page.locator('#approval-approve').is_disabled() and await page.locator('#approval-reject').is_disabled()
            response=await context.request.get('http://127.0.0.1:3194/api/v2/workflows/'+run['workflowId']+'/runs/'+run['runId']+'?includeOutput=true',headers={'X-API-Key':sim['apiKey']})
            result=(await response.json())['data']
            assert result['status']=='completed'
            assert result['output']['data']['decision']==('approved' if branch=='approve' else 'rejected')
            states[id]['nativeObserved']=result;save(statefile,states)
            await page.screenshot(path=str(OUT/('approval-live-'+branch+'.png')),full_page=True)
        assert token not in await page.locator('body').inner_text()
        assert await page.evaluate('()=>localStorage.length+sessionStorage.length')==0
        await page.locator('#approval-logout').click()
        await browser.close()
    report={'passed':True,'surface':'actual Chinese approval UI; final GET verification only','approvals':[{ 'approvalId':id,'decision':v['branch'],'decisionId':v['response']['decision']['decisionId'],'runId':v['runId'],'deliveryStatusAtReceipt':v['response'].get('deliveryStatus'),'nativeStatus':v['nativeObserved']['status'],'output':v['nativeObserved']['output']} for id,v in states.items()]}
    save(OUT/'approval-live-browser-report.json',report);print(json.dumps(report,ensure_ascii=False))
asyncio.run(main())
