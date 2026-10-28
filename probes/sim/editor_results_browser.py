"""Isolated real authoring UI; no model task, existing project/role only.

--prepare creates private standalone runtime config once. Default browser run
records every outbound POST intent before sending and refuses an existing state.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
OUT = ROOT / 'var/editor-result-browser'
BASE = 'http://127.0.0.1:3196'
PROJECT = 'native-memory-roundtrip'
ROLE = 'native-loop-parent'
NATIVE_ROLE = 'agent-line-native-loop-parent'
CHROME = '/data01/home/zukang.xu/.cache/ms-playwright/chromium_headless_shell-1223/chrome-headless-shell-linux64/chrome-headless-shell'


def write(path, value):
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush(); os.fsync(stream.fileno())
    temporary.replace(path)


def prepare():
    from agent_line.store import TaskStore
    from agent_line.sim_journal import SimJournal
    OUT.mkdir(mode=0o700, exist_ok=False)
    auth = json.loads((ROOT/'var/sim/probe-auth.json').read_text())
    host = json.loads((ROOT/'var/qwenpaw/config.json').read_text())
    workspace = host['plugins']['agent-line']['execution_bridge']['workspace']
    workspace = str(Path(workspace).resolve(strict=True))
    TaskStore.initialize(OUT/'tasks.sqlite'); SimJournal.initialize(OUT/'sim.sqlite')
    write(OUT/'authoring.json', {'schemaVersion':1,'credentials':[{'token':secrets.token_urlsafe(40),'subject':'editor-result-browser','projectScopes':[PROJECT],'capabilities':['edit_workflows','publish_workflows'],'expiresAt':time.time()+86400}], 'workspaces':{PROJECT:auth['workspaceId']}})
    write(OUT/'service.json', {'schemaVersion':1,'database':'tasks.sqlite','tokens':{secrets.token_urlsafe(40):[PROJECT]},'projects':{PROJECT:{'name':'原生记忆工作流验收','workspace':workspace,'enabled':True}},'bindings':{ROLE:{'projectId':PROJECT,'roleAgentId':NATIVE_ROLE,'enabled':True}},'authoringFile':'authoring.json','sim':{'authFile':str(ROOT/'var/sim/probe-auth.json'),'journal':'sim.sqlite','installationId':'editor-result-browser','bindings':{}}})
    print('Prepared isolated authoring configuration; no execution worker configured.')


async def main():
    from playwright.async_api import async_playwright, expect
    state_path=OUT/'state.json'
    if state_path.exists():raise RuntimeError('Existing state: observe only; no repeated browser mutations')
    token=json.loads((OUT/'authoring.json').read_text())['credentials'][0]['token']
    state={'intent':'single_result_reference_workflow','name':'结果引用验收_'+uuid.uuid4().hex[:8],'operations':[]}
    write(state_path,state)
    errors=[];csp=[];run_posts=[]
    async with async_playwright() as playwright:
        browser=await playwright.chromium.launch(executable_path=CHROME,args=['--no-sandbox'])
        try:
            context=await browser.new_context(viewport={'width':1440,'height':1000})
            async def intent(route):
                request=route.request
                if request.method=='POST':
                    if '/runs' in request.url:run_posts.append(request.url)
                    body=request.post_data_json
                    state['operations'].append({'path':request.url.removeprefix(BASE),'operationId':body.get('operationId'),'status':'intent'})
                    write(state_path,state)
                await route.continue_()
            await context.route('**/extension/v1/**',intent)
            async def connect(page):
                if not getattr(page, '_probe_listeners', False):
                    page._probe_listeners = True
                    page.on('pageerror',lambda error:errors.append(str(error)))
                    page.on('dialog',lambda dialog:dialog.accept())
                    page.on('console',lambda message:csp.append(message.text) if 'Content Security Policy' in message.text else None)
                await page.goto(BASE+'/workbench/editor.html');await page.locator('#token').fill(token);await page.locator('#login').click()
                await expect(page.locator('#connection')).to_have_text('编辑服务已连接')
                await page.locator('#project').select_option(PROJECT);await expect(page.locator('#refresh')).to_be_enabled()
            page=await context.new_page();await connect(page)
            await page.locator('summary').click();await page.locator('#workflow-name').fill(state['name'])
            async with page.expect_response(lambda r:r.request.method=='POST' and r.url.endswith('/workflows')) as event:await page.locator('#create').click()
            created=await (await event.value).json();assert created['receipt']['outcome']=='created'
            wid=created['receipt']['workflowId'];state['workflowId']=wid;state['create']=created;write(state_path,state)
            await expect(page.locator('#flow-heading')).to_have_text(state['name'])
            for kind in ['agent','condition','end']:
                await page.locator('#node-type').select_option(kind);await page.locator('#add-node').click()
            async def select(name):await page.locator('#node-list').get_by_role('button',name=name,exact=True).click()
            async def apply():await page.locator('#node-form').get_by_role('button',name='应用节点配置').click()
            async def edge(source,target,branch=None):
                await page.locator('#edge-source').select_option(label=source);await page.locator('#edge-target').select_option(label=target)
                if branch is not None:await page.locator('#edge-branch').select_option('else' if branch=='else' else {'index':0})
                await page.locator('#connect').click()
            await page.locator('#edge-list').get_by_role('button',name='删除连线').click()
            await edge('开始','Agent任务');await edge('Agent任务','条件分支')
            await page.locator('#edge-source').select_option(label='条件分支');await page.locator('#edge-target').select_option(label='结束');await page.locator('#edge-branch').select_option(index=0);await page.locator('#connect').click()
            await edge('条件分支','结束2','else')
            for name,x,y in [('开始',20,140),('Agent任务',230,140),('条件分支',450,140),('结束',690,40),('结束2',690,250)]:
                await select(name);await page.locator('#node-x').fill(str(x));await page.locator('#node-y').fill(str(y))
                if name=='Agent任务':
                    await page.locator('#agent-role').select_option(ROLE);await page.locator('#agent-instruction').fill('编辑结果引用验收，不运行模型。')
                if name=='条件分支':
                    await page.locator('#rule-list [data-field=source]').select_option(label='Agent任务 · Agent 结果')
                    await page.locator('#rule-list [data-field=field]').fill('result.verificationStatus')
                    await page.locator('#rule-list [data-field=type]').select_option('string')
                    await page.locator('#rule-list [data-field=value]').fill('passed')
                if name=='结束':
                    await page.locator('#end-mode').select_option('result');await page.locator('#end-result-source').select_option(label='Agent任务 · Agent 结果')
                if name=='结束2':await page.locator('#end-output').fill('{"result":"not_passed"}')
                await apply()
            async with page.expect_response(lambda r:r.request.method=='POST' and r.url.endswith('/save')) as event:await page.locator('#save').click()
            saved=await (await event.value).json();state['save']=saved;write(state_path,state);assert saved['receipt']['outcome']=='saved'
            async with page.expect_response(lambda r:r.request.method=='POST' and r.url.endswith('/publish')) as event:await page.locator('#publish').click()
            published=await (await event.value).json();state['publish']=published;write(state_path,state);assert published['receipt']['outcome']=='published'
            binding=published['receipt']['bindingId']
            await expect(page.locator('#published-bindings a')).to_have_count(1)
            link=await page.locator('#published-bindings a').get_attribute('href');assert link=='./?binding='+binding and token not in link
            await page.locator('#reload').click();await expect(page.locator('#publish')).to_be_enabled()
            await expect(page.locator('#published-bindings a')).to_have_count(1)
            response=await context.request.get(BASE+f'/extension/v1/authoring/projects/{PROJECT}/workflows/{wid}',headers={'Authorization':'Bearer '+token})
            detail=await response.json();assert response.status==200
            nodes=detail['document']['nodes'];agent=next(n for n in nodes if n['type']=='agent');condition=next(n for n in nodes if n['type']=='condition')
            assert condition['config']['rules'][0]['source']==agent['id'] and condition['config']['rules'][0]['field']=='result.verificationStatus'
            end=next(n for n in nodes if n['name']=='结束');assert end['config']=={'resultFrom':agent['id']}
            assert next(n for n in nodes if n['name']=='结束2')['config']=={'output':{'result':'not_passed'}}
            assert detail['publishedBindings'][0]['bindingId']==binding
            state['readback']=detail;write(state_path,state)
            await select('条件分支');assert await page.locator('#rule-list [data-field=source]').input_value()==agent['id']
            assert await page.locator('#rule-list [data-field=field]').input_value()=='result.verificationStatus'
            await page.screenshot(path=str(OUT/'desktop-page.png'),full_page=True);await page.locator('.editor-panel').screenshot(path=str(OUT/'desktop-editor.png'))
            await page.locator('#published-bindings a').click()
            await expect(page.locator('#binding')).to_have_value(binding);await expect(page.locator('#query-binding')).to_have_value(binding)
            assert await page.locator('#token').input_value()=='' and not run_posts
            assert await page.evaluate('localStorage.length+sessionStorage.length')==0
            # Invalid old source is retained; locally bypassing Agent cannot be saved.
            await connect(page);await page.locator('.workflow-choice').filter(has_text=state['name']).click();await expect(page.locator('#node-list button')).to_have_count(5)
            await page.locator('#edge-list .edge-row').filter(has_text='开始 → Agent任务').get_by_role('button').click();await edge('开始','条件分支');await select('条件分支')
            await expect(page.locator('#rule-list [data-field=source]')).to_contain_text('原来源失效')
            assert await page.locator('#rule-list [data-field=source]').input_value()==agent['id']
            await apply();await expect(page.locator('#notice')).to_contain_text('必须是所有路径必经')
            assert len(state['operations'])==3
            await page.locator('#logout').click();assert await page.locator('#published-bindings a').count()==0
            assert await page.locator('#rule-list').text_content()=='' and await page.locator('#token').input_value()==''
            mobile=await browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
            mp=await mobile.new_page();await connect(mp);await mp.locator('.workflow-choice').filter(has_text=state['name']).click();await expect(mp.locator('#node-list button')).to_have_count(5)
            await mp.locator('#node-list').get_by_role('button',name='结束',exact=True).click()
            await expect(mp.locator('#end-mode')).to_have_value('result');await expect(mp.locator('#end-result-source')).to_have_value(agent['id'])
            assert await mp.evaluate('document.documentElement.scrollWidth<=innerWidth')
            await mp.screenshot(path=str(OUT/'mobile-page.png'),full_page=True);await mp.locator('.editor-panel').screenshot(path=str(OUT/'mobile-editor.png'))
            assert await mp.evaluate('localStorage.length+sessionStorage.length')==0
            assert not errors and not csp and not run_posts
            report={'status':'passed_live_browser','workflowId':wid,'bindingId':binding,'revision':published['receipt']['revision'],'postCount':len(state['operations']),'runPostCount':len(run_posts),'checks':['real_create_save_publish','source_and_resultFrom_roundtrip','literal_end_preserved','publishedBindings_reload','binding_deeplink_prefill_without_run','invalid_source_retained_and_rejected_local_only','desktop_mobile_screenshots','mobile_no_overflow','storage_empty_logout_clears','no_page_or_csp_errors'],'pageErrors':errors,'cspErrors':csp,'scope':'isolated authoring only; no models/runs/native roles/memory changes'}
            write(OUT/'report.json',report);print(json.dumps({'status':report['status'],'workflowId':wid,'checks':len(report['checks'])}))
        finally:await browser.close()

async def observe_only():
    """Existing identity verification; every POST is blocked, never retried."""
    from playwright.async_api import async_playwright, expect
    state=json.loads((OUT/'state.json').read_text())
    token=json.loads((OUT/'authoring.json').read_text())['credentials'][0]['token']
    blocked=[];errors=[];dialogs=[]
    async with async_playwright() as p:
        browser=await p.chromium.launch(executable_path=CHROME,args=['--no-sandbox'])
        try:
            context=await browser.new_context(viewport={'width':1440,'height':1000})
            async def readonly(route):
                if route.request.method=='POST':blocked.append(route.request.url);await route.abort()
                else:await route.continue_()
            await context.route('**/extension/v1/**',readonly)
            page=await context.new_page()
            async def dialog_once(dialog):dialogs.append(dialog.type);await dialog.accept()
            page.on('dialog',dialog_once);page.on('pageerror',lambda error:errors.append(str(error)))
            await page.goto(BASE+'/workbench/editor.html');await page.locator('#token').fill(token);await page.locator('#login').click()
            await expect(page.locator('#connection')).to_have_text('编辑服务已连接')
            await page.locator('#project').select_option(PROJECT);await expect(page.locator('#refresh')).to_be_enabled()
            await page.locator('.workflow-choice').filter(has_text=state['name']).click();await expect(page.locator('#node-list button')).to_have_count(5)
            await page.locator('#node-list').get_by_role('button',name='条件分支',exact=True).click()
            assert await page.locator('#rule-list [data-field=field]').input_value()=='result.verificationStatus'
            await page.locator('#node-list').get_by_role('button',name='结束',exact=True).click()
            assert await page.locator('#end-mode').input_value()=='result'
            assert await page.locator('#end-result-source').input_value()
            await page.locator('#node-x').fill('701')
            await page.locator('#logout').click()
            await expect(page.locator('#connection')).to_have_text('尚未连接')
            assert dialogs==['confirm'] and not blocked and not errors
            report=json.loads((OUT/'report.json').read_text())
            report['probeWarning']='Initial probe attached a duplicate dialog listener on reconnect; corrected in probe only. No business request was repeated.'
            report['readOnlyFollowup']={'passed':True,'blockedPosts':len(blocked),'dialogsHandledOnce':len(dialogs),'pageErrors':errors}
            write(OUT/'report.json',report)
            print(json.dumps({'status':'passed_read_only_followup','posts':0,'dialogs':len(dialogs)}))
        finally:await browser.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--prepare',action='store_true');parser.add_argument('--observe',action='store_true');args=parser.parse_args()
    if args.prepare:prepare()
    elif args.observe:asyncio.run(observe_only())
    else:asyncio.run(main())
