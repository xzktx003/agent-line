"""Real finite-loop authoring browser probe: three mutations, never execute."""
import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from probes.sim import editor_results_browser as prior
OUT=ROOT/'var/editor-loop-browser'
BASE='http://127.0.0.1:3196'
PROJECT=prior.PROJECT
prior.OUT=OUT
write=prior.write


def prepare():
    prior.prepare()
    config=json.loads((OUT/'service.json').read_text())
    config['sim']['installationId']='editor-loop-browser'
    write(OUT/'service.json',config)


async def main():
    from playwright.async_api import async_playwright, expect
    state_path=OUT/'state.json'
    if state_path.exists():raise RuntimeError('Existing state: use --observe; never recreate or resend')
    token=json.loads((OUT/'authoring.json').read_text())['credentials'][0]['token']
    state={'name':'有限循环验收_'+uuid.uuid4().hex[:8],'operations':[]}
    write(state_path,state)
    errors=[];csp=[];run_posts=[]
    async with async_playwright() as p:
        browser=await p.chromium.launch(executable_path=prior.CHROME,args=['--no-sandbox'])
        try:
            context=await browser.new_context(viewport={'width':1440,'height':1000})
            async def track(route):
                req=route.request
                if req.method=='POST':
                    if '/runs' in req.url:run_posts.append(req.url);await route.abort();return
                    body=req.post_data_json
                    state['operations'].append({'path':req.url.removeprefix(BASE),'operationId':body.get('operationId'),'status':'intent'})
                    write(state_path,state)
                    if len(state['operations'])>3:await route.abort();raise RuntimeError('Unexpected mutation')
                await route.continue_()
            await context.route('**/extension/v1/**',track)
            page=await context.new_page()
            page.on('pageerror',lambda e:errors.append(str(e)))
            page.on('console',lambda m:csp.append(m.text) if 'Content Security Policy' in m.text else None)
            page.on('dialog',lambda d:d.accept())
            await page.goto(BASE+'/workbench/editor.html');await page.locator('#token').fill(token);await page.locator('#login').click()
            await expect(page.locator('#connection')).to_have_text('编辑服务已连接')
            await page.locator('#project').select_option(PROJECT)
            await expect(page.locator('#refresh')).to_be_enabled()
            await page.locator('summary').click();await page.locator('#workflow-name').fill(state['name'])
            async with page.expect_response(lambda r:r.request.method=='POST' and r.url.endswith('/workflows')) as e:await page.locator('#create').click()
            state['create']=await (await e.value).json();wid=state['create']['receipt']['workflowId'];state['workflowId']=wid;write(state_path,state)
            await expect(page.locator('#node-list button')).to_have_count(2)
            for kind in ('agent','loop'):
                await page.locator('#node-type').select_option(kind);await page.locator('#add-node').click()
            async def select(name):await page.locator('#node-list').get_by_role('button',name=name,exact=True).click()
            async def apply():await page.locator('#node-form').get_by_role('button',name='应用节点配置').click()
            await select('Agent任务');assert not await page.locator('#agent-allow-failure').is_checked()
            await page.locator('#agent-role').select_option(prior.ROLE);await page.locator('#agent-instruction').fill('仅编辑器有限循环保存验收，不运行模型。')
            await page.locator('#agent-allow-failure').check();await apply()
            await select('有限循环');await page.locator('#loop-body').select_option(label='Agent任务');await page.locator('#loop-max').fill('3');await apply()
            await page.locator('#edge-list').get_by_role('button',name='删除连线').click()
            async def edge(source,target,branch=None):
                await page.locator('#edge-source').select_option(label=source);await page.locator('#edge-target').select_option(label=target)
                if branch:await page.locator('#edge-branch').select_option(branch)
                await page.locator('#connect').click()
            await edge('开始','有限循环');await edge('有限循环','Agent任务','body');await edge('有限循环','结束','done')
            for name,x,y in [('开始',20,140),('有限循环',240,140),('Agent任务',460,270),('结束',690,90)]:
                await select(name);await page.locator('#node-x').fill(str(x));await page.locator('#node-y').fill(str(y))
                if name=='结束':await page.locator('#end-mode').select_option('result');await page.locator('#end-result-source').select_option(label='Agent任务 · Agent 结果')
                await apply()
            async with page.expect_response(lambda r:r.request.method=='POST' and r.url.endswith('/save')) as e:await page.locator('#save').click()
            state['save']=await (await e.value).json();write(state_path,state);assert state['save']['receipt']['outcome']=='saved',state['save']
            async with page.expect_response(lambda r:r.request.method=='POST' and r.url.endswith('/publish')) as e:await page.locator('#publish').click()
            state['publish']=await (await e.value).json();write(state_path,state);assert state['publish']['receipt']['outcome']=='published'
            response=await context.request.get(BASE+f'/extension/v1/authoring/projects/{PROJECT}/workflows/{wid}',headers={'Authorization':'Bearer '+token})
            assert response.status==200;detail=await response.json();state['readback']=detail;write(state_path,state)
            doc=detail['document'];loop=next(n for n in doc['nodes'] if n['type']=='loop');agent=next(n for n in doc['nodes'] if n['type']=='agent');end=next(n for n in doc['nodes'] if n['type']=='end')
            assert loop['config']=={'body':agent['id'],'maxIterations':3}
            assert end['config']=={'resultFrom':agent['id']} and agent['config']['allowFailureBranch'] is True
            assert {(e['branch'],e['target']) for e in doc['edges'] if e['source']==loop['id']}=={('body',agent['id']),('done',end['id'])}
            assert state['publish']['receipt']['externalTasks']['loopPolicies']
            await page.locator('#reload').click();await expect(page.locator('#node-list button')).to_have_count(4)
            await select('有限循环');await expect(page.locator('#loop-body')).to_have_value(agent['id']);await expect(page.locator('#loop-max')).to_have_value('3')
            await page.screenshot(path=str(OUT/'desktop-page.png'),full_page=True);await page.locator('.editor-panel').screenshot(path=str(OUT/'desktop-editor.png'))
            await page.set_viewport_size({'width':390,'height':844});await page.screenshot(path=str(OUT/'mobile-page.png'),full_page=True);await page.locator('.editor-panel').screenshot(path=str(OUT/'mobile-editor.png'))
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            # Local deletion leaves a dangling body ID visible and blocks save.
            await select('Agent任务');await page.locator('#delete-node').click();await select('有限循环')
            await expect(page.locator('#loop-body')).to_have_value(agent['id'])
            assert '失效' in await page.locator('#loop-body option:checked').inner_text()
            await page.locator('#save').click();await expect(page.locator('#notice')).to_contain_text('失效')
            assert len(state['operations'])==3
            await page.locator('#logout').click();await expect(page.locator('#connection')).to_have_text('尚未连接')
            assert await page.locator('#loop-body option').count()==0 and await page.locator('#token').input_value()==''
            assert await page.evaluate('localStorage.length+sessionStorage.length')==0
            assert not errors and not csp and not run_posts
            binding=state['publish']['receipt']['bindingId'];await page.goto(BASE+'/workbench/?binding='+binding)
            await expect(page.locator('#binding')).to_have_value(binding);await expect(page.locator('#query-binding')).to_have_value(binding)
            assert await page.locator('#token').input_value()==''
            write(OUT/'report.json',{'passed':True,'workflowId':wid,'writes':3,'runPosts':0,'pageErrors':errors,'cspErrors':csp,'checks':['explicit_failure_policy','loop_readback','body_done_edges','end_body_result','published_loop_policy','invalid_body_preserved_save_blocked','desktop_mobile_no_overflow','storage_empty_logout','binding_prefill_only']})
            print('Finite loop browser passed: three authoring POSTs, zero runs.')
        finally:await browser.close()


async def observe():
    import httpx
    state=json.loads((OUT/'state.json').read_text())
    token=json.loads((OUT/'authoring.json').read_text())['credentials'][0]['token']
    with httpx.Client() as client:
        response=client.get(BASE+f"/extension/v1/authoring/projects/{PROJECT}/workflows/{state['workflowId']}",headers={'Authorization':'Bearer '+token})
        response.raise_for_status();data=response.json()
        assert data['document']==state['readback']['document']
    print('Existing workflow readback unchanged; zero writes.')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--prepare',action='store_true');parser.add_argument('--observe',action='store_true');args=parser.parse_args()
    if args.prepare:prepare()
    elif args.observe or (OUT/'state.json').exists():asyncio.run(observe())
    else:asyncio.run(main())
