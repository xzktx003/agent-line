"""Real browser evidence; credentials stay in memory and are never printed."""
import asyncio
import json
import uuid
import importlib.util
from pathlib import Path
from playwright.async_api import async_playwright

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'var/gateway'
BASE='http://10.30.0.28:3194'
CHROME='/data01/home/zukang.xu/.cache/ms-playwright/chromium_headless_shell-1223/chrome-headless-shell-linux64/chrome-headless-shell'

async def main():
    OUT.mkdir(exist_ok=True)
    report={}
    async with async_playwright() as p:
        browser=await p.chromium.launch(executable_path=CHROME, args=['--no-sandbox'])
        context=await browser.new_context(viewport={'width':1440,'height':1000})
        qwen=json.loads((ROOT/'var/qwenpaw-secrets/local-login.json').read_text())
        login=await context.request.post(BASE+'/qwenpaw/api/auth/login',data=qwen)
        report['qwenLoginStatus']=login.status
        auth=await login.json()
        token=auth.get('access_token') or auth.get('token')
        if not token:
            raise RuntimeError('Qwen login did not return token')
        page=await context.new_page()
        async def accept_discard(dialog):
            await dialog.accept()
        page.on('dialog', accept_discard)
        await page.goto(BASE+'/qwenpaw/')
        await page.evaluate('(token)=>localStorage.setItem("qwenpaw_auth_token",token)', token)
        await page.reload()
        await page.wait_for_timeout(4000)
        report['qwenURL']=page.url
        report['qwenTitle']=await page.title()
        await page.screenshot(path=str(OUT/'qwen-prefixed.png'),full_page=True)
        await page.get_by_text('Console account', exact=True).click()
        await page.wait_for_timeout(1000)
        await page.get_by_text('Settings', exact=True).click()
        await page.wait_for_timeout(1500)
        await page.get_by_text('工作流工作台', exact=True).first.click()
        await page.wait_for_timeout(1000)
        await page.get_by_role('button',name='打开原版画布').click()
        await page.wait_for_timeout(2500)
        frame=page.frame_locator('iframe[title="Sim 原版工作流编辑器"]')
        report['pluginMenuURL']=page.url
        report['iframeLoginVisible']=await frame.locator('input[name="email"]').is_visible()
        sim=json.loads((ROOT/'var/sim/probe-account.json').read_text())
        await frame.locator('input[name="email"]').fill(sim['email'])
        await frame.locator('input[name="password"]').fill(sim['password'])
        await frame.get_by_role('button',name='Sign in',exact=True).click()
        await frame.get_by_text('New workflow',exact=True).wait_for(timeout=20000)
        await page.wait_for_timeout(2500)
        auth=json.loads((ROOT/'var/sim/probe-auth.json').read_text())
        liveframe=next(f for f in page.frames if '/workspace/' in f.url)
        assert liveframe.url.startswith(BASE+'/workspace/'+auth['workspaceId']+'/'), 'Not the authorized isolated workspace'
        statefile=OUT/'canvas-state.json'
        if statefile.exists():
            existing=json.loads(statefile.read_text())
            if existing['status']=='import-intent':
                raise RuntimeError('Unknown import outcome; reconcile before creating again')
            wid=existing['workflowId'];report.update(workflowId=wid,name=existing['name'])
            await frame.get_by_text(existing['name'],exact=True).first.click()
            await page.wait_for_timeout(2000)
        else:
            spec=importlib.util.spec_from_file_location('sim_graph',ROOT/'probes/sim/workflow_probe.py')
            module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
            graph=module.graph()
            suffix=uuid.uuid4().hex[:10]
            ids={key:key+'-'+suffix for key in graph['blocks']}
            blocks={}
            for key,block in graph['blocks'].items():
                block['id']=ids[key]
                blocks[ids[key]]=block
            graph['blocks']=blocks
            for edge in graph['edges']:
                edge['id']+='-'+suffix
                edge['source']=ids[edge['source']];edge['target']=ids[edge['target']]
            graph['metadata']={'name':'Canvas Browser '+suffix}
            statefile.write_text(json.dumps({'status':'import-intent','name':graph['metadata']['name']}))
            await frame.locator('input[type="file"][accept=".json,.zip"]').set_input_files({'name':graph['metadata']['name']+'.json','mimeType':'application/json','buffer':json.dumps(graph).encode()})
            await frame.get_by_text(graph['metadata']['name'],exact=True).first.wait_for(timeout=30000)
            await page.wait_for_timeout(3000)
            realframe=next(f for f in page.frames if '/workspace/' in f.url)
            wid=realframe.url.split('/w/')[-1].split('?')[0]
            report['workflowId']=wid
            report['name']=graph['metadata']['name']
            statefile.write_text(json.dumps({'status':'imported','workflowId':wid,'name':report['name']}))
        await frame.locator('.react-flow__node').filter(has_text='gate').first.dblclick()
        await page.wait_for_timeout(1000)
        expression='<start.input> === 7'
        await frame.locator('textarea').fill(expression)
        await page.get_by_role('heading',name='工作流工作台',exact=True).click()
        auth=json.loads((ROOT/'var/sim/probe-auth.json').read_text())
        async def saved():
            r=await context.request.get(BASE+'/api/v2/workflows/'+wid+'/state',headers={'X-API-Key':auth['apiKey']})
            assert r.status==200
            return (await r.json())['data']
        for attempt in range(30):
            current=await saved()
            condition=next(b for b in current['blocks'].values() if b['type']=='condition')
            if expression in condition['subBlocks']['conditions']['value']:
                break
            await page.wait_for_timeout(500)
        else:
            raise RuntimeError('Canvas edit did not persist to official API')
        report['editedExpression']=expression
        report['persistedBlockCount']=len(current['blocks'])
        report['persistedEdgeCount']=len(current['edges'])
        await page.get_by_role('button',name='关闭画布',exact=True).click()
        await page.get_by_role('button',name='打开原版画布',exact=True).click()
        await frame.get_by_text(report['name'],exact=True).first.wait_for(timeout=20000)
        await frame.get_by_text(report['name'],exact=True).first.click()
        await frame.locator('.react-flow__node').filter(has_text='gate').first.dblclick()
        assert (await frame.locator('textarea').input_value()).strip()==expression
        report['reopenedParameterMatches']=True
        reopened=await saved()
        assert reopened['blocks']==current['blocks'] and reopened['edges']==current['edges']
        report['reopenedGraphMatches']=True
        (OUT/'canvas-persisted-state.json').write_text(json.dumps(reopened,ensure_ascii=False,indent=2))
        await page.screenshot(path=str(OUT/'canvas-persisted.png'),full_page=True)
        deploy=frame.get_by_role('button',name='Deploy',exact=True)
        if await deploy.count():
            await deploy.click()
        else:
            await frame.get_by_role('button',name='Live',exact=True).first.click()
        await page.wait_for_timeout(1000)
        report['deployedViaOriginalUI']=await frame.get_by_text('Live Workflow',exact=True).is_visible()
        intentfile=OUT/'canvas-executions.json'
        records=json.loads(intentfile.read_text()) if intentfile.exists() else {}
        for branch,value in [('yes',7),('no',8)]:
            if branch not in records:
                record={'workflowId':wid,'runId':str(uuid.uuid4()),'state':'intent','input':{'input':value}}
                records[branch]=record
                intentfile.write_text(json.dumps(records,indent=2))
                r=await context.request.post(BASE+'/api/v2/workflows/'+wid+'/execute',data={'input':record['input']},headers={'X-API-Key':auth['apiKey'],'X-Run-Id':record['runId']})
                record['httpStatus']=r.status
                if r.status>=400:
                    intentfile.write_text(json.dumps(records,indent=2))
                    raise RuntimeError('One-shot official execution failed; reconcile intent without resubmission')
                result=(await r.json())['data']
                assert result['runId']==record['runId']
                record['state']='submitted'
                intentfile.write_text(json.dumps(records,indent=2))
            record=records[branch]
            assert record['workflowId']==wid
            for attempt in range(40):
                r=await context.request.get(BASE+'/api/v2/workflows/'+wid+'/runs/'+record['runId']+'?includeOutput=true',headers={'X-API-Key':auth['apiKey']})
                assert r.status==200
                result=(await r.json())['data']
                if result['status'] not in ['running','queued']:
                    break
                await page.wait_for_timeout(250)
            record['observed']=result
            intentfile.write_text(json.dumps(records,indent=2))
            assert result['status']=='completed', result['status']
            assert result['output']['data']=={'branch':branch}, 'Unexpected branch output'
        report['branches']={branch:{'runId':v['runId'],'status':v['observed']['status'],'output':v['observed']['output']} for branch,v in records.items()}
        report['executionSurface']='Official v2 API of graph created, edited and deployed through iframe UI'
        await page.screenshot(path=str(OUT/'canvas-deployed.png'),full_page=True)
        await browser.close()
    (OUT/'canvas-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report,ensure_ascii=False))

asyncio.run(main())
