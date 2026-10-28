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
OUT = ROOT / 'var/agent-editor-browser'
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
    write(OUT/'authoring.json', {'schemaVersion':1,'credentials':[{'token':secrets.token_urlsafe(40),'subject':'agent-editor-browser','projectScopes':[PROJECT],'capabilities':['edit_workflows','publish_workflows'],'expiresAt':time.time()+86400}], 'workspaces':{PROJECT:auth['workspaceId']}})
    write(OUT/'service.json', {'schemaVersion':1,'database':'tasks.sqlite','tokens':{secrets.token_urlsafe(40):[PROJECT]},'projects':{PROJECT:{'name':'原生记忆工作流验收','workspace':workspace,'enabled':True}},'bindings':{ROLE:{'projectId':PROJECT,'roleAgentId':NATIVE_ROLE,'enabled':True}},'authoringFile':'authoring.json','sim':{'authFile':str(ROOT/'var/sim/probe-auth.json'),'journal':'sim.sqlite','installationId':'agent-editor-browser','bindings':{}}})
    print('Prepared isolated authoring configuration; no execution worker configured.')


async def main():
    from playwright.async_api import async_playwright, expect
    path=OUT/'state.json'
    if path.exists():raise RuntimeError('Existing browser state forbids repeating mutations; observe existing workflow instead')
    token=json.loads((OUT/'authoring.json').read_text())['credentials'][0]['token']
    name='Agent搭建验收_'+uuid.uuid4().hex[:8]
    state={'intent':'one_workflow_browser_validation','name':name,'projectId':PROJECT,'operations':[]}
    write(path,state)
    errors=[];csp=[]
    async with async_playwright() as p:
        browser=await p.chromium.launch(executable_path=CHROME,args=['--no-sandbox'])
        try:
            context=await browser.new_context(viewport={'width':1440,'height':1000})
            async def protect_post(route):
                request=route.request
                if request.method=='POST':
                    body=request.post_data_json
                    state['operations'].append({'path':request.url.removeprefix(BASE),'operationId':body.get('operationId'),'status':'intent'})
                    write(path,state)
                await route.continue_()
            await context.route('**/extension/v1/authoring/**',protect_post)
            async def attach(page):
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.on('dialog',lambda dialog:dialog.accept())
                page.on('console',lambda message:csp.append(message.text) if 'Content Security Policy' in message.text else None)
                await page.goto(BASE+'/workbench/editor.html')
                await page.locator('#token').fill(token);await page.locator('#login').click()
                await expect(page.locator('#connection')).to_have_text('编辑服务已连接')
                await page.locator('#project').select_option(PROJECT)
                await expect(page.locator('#roles-status')).to_contain_text('个配置中的既有角色绑定')
                await expect(page.locator('#refresh')).to_be_enabled()
            page=await context.new_page();await attach(page)
            await page.locator('summary').click();await page.locator('#workflow-name').fill(name)
            async with page.expect_response(lambda r:r.request.method=='POST' and r.url.endswith('/workflows')) as event:await page.locator('#create').click()
            created=await (await event.value).json();assert created['receipt']['outcome']=='created'
            workflow=created['receipt']['workflowId'];state['workflowId']=workflow;state['create']=created;write(path,state)
            await expect(page.locator('#flow-heading')).to_have_text(name)
            await page.locator('#node-type').select_option('agent');await page.locator('#add-node').click()
            await page.locator('#agent-role').select_option(ROLE)
            await page.locator('#agent-instruction').fill('仅用于流程编辑发布验收；本流程本轮不执行。')
            await page.locator('#agent-source').select_option(label='开始 · 开始输入')
            await page.locator('#node-x').fill('310');await page.locator('#node-y').fill('110')
            await page.locator('#node-form').get_by_role('button',name='应用节点配置').click()
            await page.locator('#node-list').get_by_role('button',name='结束',exact=True).click()
            await page.locator('#node-x').fill('600');await page.locator('#node-form').get_by_role('button',name='应用节点配置').click()
            await page.locator('#edge-list').get_by_role('button',name='删除连线').click()
            async def connect(source,target):
                await page.locator('#edge-source').select_option(label=source);await page.locator('#edge-target').select_option(label=target);await page.locator('#connect').click()
            await connect('开始','Agent任务');await connect('Agent任务','结束')
            async with page.expect_response(lambda r:r.request.method=='POST' and r.url.endswith('/save')) as event:await page.locator('#save').click()
            saved=await (await event.value).json();state['save']=saved;write(path,state);assert saved['receipt']['outcome']=='saved'
            await expect(page.locator('#publish')).to_be_enabled()
            async with page.expect_response(lambda r:r.request.method=='POST' and r.url.endswith('/publish')) as event:await page.locator('#publish').click()
            published=await (await event.value).json();state['publish']=published;write(path,state);assert published['receipt']['outcome']=='published'
            response=await context.request.get(BASE+f'/extension/v1/authoring/projects/{PROJECT}/workflows/{workflow}',headers={'Authorization':'Bearer '+token})
            detail=await response.json();assert response.status==200
            agent=next(n for n in detail['document']['nodes'] if n['type']=='agent')
            assert agent['config']['roleBindingId']==ROLE and agent['config']['memoryPolicyId']=='native-role'
            assert agent['config']['inputFrom']['field']=='input'
            external=published['receipt']['externalTasks']
            state['verifiedDocument']=detail;write(path,state)
            assert NATIVE_ROLE in json.dumps(external) and ROLE in json.dumps(external)
            await page.locator('#node-list').get_by_role('button',name='Agent任务',exact=True).click()
            await page.screenshot(path=str(OUT/'desktop-page.png'),full_page=True)
            await page.locator('.editor-panel').screenshot(path=str(OUT/'desktop-editor.png'))
            # Second Agent is a local unsaved form check only; no second workflow mutation.
            await page.locator('#node-type').select_option('agent');await page.locator('#add-node').click()
            await page.locator('#edge-list .edge-row').filter(has_text='Agent任务 → 结束').get_by_role('button').click()
            await connect('Agent任务','Agent任务2');await connect('Agent任务2','结束')
            await page.locator('#agent-role').select_option(ROLE);await page.locator('#agent-instruction').fill('本地输入来源选择检查，不保存。')
            await page.locator('#agent-source').select_option(label='Agent任务 · Agent 输出')
            await page.locator('#node-form').get_by_role('button',name='应用节点配置').click()
            await expect(page.locator('#save')).to_be_enabled()
            # GET-only failure preserves binding; no server configuration is changed.
            async def empty_roles(route):await route.fulfill(status=200,json={'roles':[],'source':'configured_existing_roles','nativeAvailabilityVerified':False})
            await page.route('**/projects/'+PROJECT+'/roles',empty_roles,times=1)
            await page.locator('#refresh-roles').click();await expect(page.locator('#roles-status')).to_contain_text('没有可用')
            assert await page.locator('#agent-role').input_value()==ROLE
            await expect(page.locator('#agent-role')).to_contain_text('失效或未核验')
            await expect(page.locator('#save')).to_be_disabled()
            async def failed_roles(route):await route.fulfill(status=503,json={'detail':'角色读取暂不可用'})
            await page.route('**/projects/'+PROJECT+'/roles',failed_roles,times=1)
            await page.locator('#refresh-roles').click();await expect(page.locator('#roles-status')).to_contain_text('读取失败')
            assert await page.locator('#agent-role').input_value()==ROLE
            await expect(page.locator('#save')).to_be_disabled()
            assert await page.evaluate('localStorage.length+sessionStorage.length')==0
            await page.locator('#logout').click()
            assert await page.locator('#agent-instruction').input_value()=='' and await page.locator('#token').input_value()==''
            assert await page.locator('#agent-role option').count()==0
            mobile=await browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
            mp=await mobile.new_page();await attach(mp)
            await mp.locator('.workflow-choice').filter(has_text=name).click()
            await expect(mp.locator('#node-list button')).to_have_count(3)
            await mp.locator('#node-list').get_by_role('button',name='Agent任务',exact=True).click()
            assert await mp.locator('#agent-role').input_value()==ROLE
            assert await mp.evaluate('document.documentElement.scrollWidth<=innerWidth')
            await mp.screenshot(path=str(OUT/'mobile-page.png'),full_page=True)
            await mp.locator('.editor-panel').screenshot(path=str(OUT/'mobile-editor.png'))
            assert await mp.evaluate('localStorage.length+sessionStorage.length')==0
            assert not errors and not csp
            report={'status':'passed_live_browser','workflowId':workflow,'projectId':PROJECT,'roleBindingId':ROLE,'roleAgentId':NATIVE_ROLE,'revision':published['receipt']['revision'],'checks':['real_create_save_publish_existing_role','published_external_policy_mapping','second_agent_output_picker_local_only','empty_and_failed_roles_preserve_binding_and_block_save','desktop_and_mobile_screenshots','mobile_no_document_overflow','no_credential_storage','logout_clears_fields','no_page_errors_or_csp_violations'],'postIntents':len(state['operations']),'pageErrors':errors,'cspErrors':csp,'scope':'authoring only; no executionFile/taskFlowFile; no model runs or native role/memory changes'}
            write(OUT/'report.json',report);print(json.dumps({'status':report['status'],'workflowId':workflow,'checks':len(report['checks'])}))
        finally:await browser.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--prepare',action='store_true');args=parser.parse_args()
    if args.prepare:prepare()
    else:asyncio.run(main())
