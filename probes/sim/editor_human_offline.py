"""Offline fixed-fixture UI checks; all HTTP is intercepted, no server or Sim."""
import asyncio
import json
import mimetypes
from pathlib import Path
from playwright.async_api import async_playwright, expect
ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'.omx/state/editor-human-conditions'
CHROME='/data01/home/zukang.xu/.cache/ms-playwright/chromium_headless_shell-1223/chrome-headless-shell-linux64/chrome-headless-shell'

def fixture():
    def node(i,t,c,x,y):return dict(id=i,type=t,name=i,config=c,position=dict(x=x,y=y))
    return dict(schemaVersion=1,variables={},nodes=[node('开始','start',{},20,90),node('确认','human',{'requiredBoolean':'approved'},220,90),node('分支','condition',{'rules':[{'id':'yes','field':'input.ok','operator':'eq','value':True}]},420,90),node('通过','end',{'output':{'approved':True}},640,20),node('拒绝','end',{'output':{'approved':False}},640,200)],edges=[dict(id='a',source='开始',target='确认'),dict(id='b',source='确认',target='分支'),dict(id='c',source='分支',target='通过',branch='yes'),dict(id='d',source='分支',target='拒绝',branch='else')])

async def main():
    doc=fixture(); mapping={'开始':'start','确认':'approval','分支':'condition','通过':'yes_end','拒绝':'no_end'}
    for node in doc['nodes']:node['id']=mapping[node['id']]
    for edge in doc['edges']:edge['source']=mapping[edge['source']];edge['target']=mapping[edge['target']]
    writes=[];errors=[];csp=[]
    async with async_playwright() as p:
        browser=await p.chromium.launch(executable_path=CHROME,args=['--no-sandbox'])
        try:
            context=await browser.new_context(viewport={'width':1440,'height':1000})
            async def route(r):
                nonlocal doc
                path=r.request.url.split('editor.test')[-1].split('?')[0]
                if path.startswith('/extension/'):
                    if r.request.method=='POST':
                        body=r.request.post_data_json;writes.append(body)
                        assert path.endswith('/save');doc=body['document']
                        data={'projectId':'offline','operationId':body['operationId'],'kind':'save','status':'accepted','safeToResubmit':False,'receipt':{'workflowId':'fixture','outcome':'saved','stateSha256':'a'*64}}
                    elif path.endswith('/projects'):data={'projects':[{'id':'offline','name':'离线固定测试项目'}],'canPublish':False,'nodeTypes':[]}
                    elif path.endswith('/roles'):data={'roles':[],'source':'configured_existing_roles','nativeAvailabilityVerified':False}
                    elif path.endswith('/workflows'):data={'workflows':[{'id':'fixture','name':'人工确认分支离线测试','isDeployed':False}],'nextCursor':None}
                    else:data={'workflowId':'fixture','name':'人工确认分支离线测试','document':doc,'editable':True,'draftEmpty':False,'stateSha256':'a'*64,'isDeployed':False}
                    await r.fulfill(status=200,content_type='application/json',body=json.dumps(data));return
                file=ROOT/'frontend'/path.removeprefix('/workbench/')
                if file.is_file():await r.fulfill(status=200,content_type=mimetypes.guess_type(file)[0] or 'application/octet-stream',body=file.read_bytes(),headers={'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'"})
                else:await r.fulfill(status=404,body='not found')
            await context.route('**/*',route)
            page=await context.new_page();page.on('pageerror',lambda e:errors.append(str(e)));page.on('dialog',lambda d:d.accept());page.on('console',lambda m:csp.append(m.text) if 'Content Security Policy' in m.text else None)
            await page.goto('https://editor.test/workbench/editor.html');await page.locator('#token').fill('offline-fixture-token');await page.locator('#login').click();await expect(page.locator('#connection')).to_have_text('编辑服务已连接')
            await page.locator('#project').select_option('offline');await page.locator('.workflow-choice').click()
            await page.locator('#node-list').get_by_role('button',name='分支',exact=True).click()
            row=page.locator('#rule-list');await row.locator('[data-field=source]').select_option(label='确认 · 人工确认结果')
            await expect(row.locator('[data-field=field]')).to_have_value('result.approval.approved');assert await row.locator('[data-field=field]').get_attribute('readonly') is not None
            assert await row.locator('[data-field=type]').is_disabled()
            assert await row.locator('[data-field=operator] option[value=gt]').evaluate('(el)=>el.disabled')
            await row.locator('[data-field=value]').fill('false')
            await page.locator('#node-form button[type=submit]').click()
            await page.locator('#save').click();await expect(page.locator('#notice')).to_contain_text('保存')
            rule=doc['nodes'][2]['config']['rules'][0];assert rule['source']=='approval' and rule['field']=='result.approval.approved' and rule['value'] is False
            await page.screenshot(path=str(OUT/'desktop.png'),full_page=True)
            await page.set_viewport_size({'width':390,'height':844});await page.screenshot(path=str(OUT/'mobile.png'),full_page=True)
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            await page.locator('#node-list').get_by_role('button',name='通过',exact=True).click();await page.locator('#end-mode').select_option('result');assert await page.locator('#end-result-source option[value="approval"]').count()==0
            await page.locator('#node-list').get_by_role('button',name='确认',exact=True).click();await page.locator('#delete-node').click()
            await page.locator('#node-list').get_by_role('button',name='分支',exact=True).click();await expect(row.locator('[data-field=source]')).to_have_value('approval');assert '失效' in await row.locator('[data-field=source] option:checked').inner_text()
            await page.locator('#node-form button[type=submit]').click();await expect(page.locator('#notice')).to_contain_text('前序 Agent 或人工确认')
            assert len(writes)==1
            await page.locator('#logout').click();await expect(page.locator('#connection')).to_have_text('尚未连接');assert await page.locator('#rule-list').inner_text()==''
            assert await page.evaluate('localStorage.length+sessionStorage.length')==0 and not errors and not csp
            (OUT/'offline-report.json').write_text(json.dumps({'passed':True,'mode':'offline_intercepted_fixture','realRequests':0,'simulatedSaveCount':len(writes),'pageErrors':errors,'cspErrors':csp,'checks':['human_boolean_rule','readonly_field','eq_ne_only','saved_source','end_agent_only','stale_source_retained','stale_apply_blocked','mobile_no_overflow','logout_storage']},ensure_ascii=False,indent=2))
            print('Offline human condition browser checks passed; zero real HTTP.')
        finally:await browser.close()
if __name__=='__main__':asyncio.run(main())
