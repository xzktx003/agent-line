"""Native browser approval probe; never fabricates UI evidence with API writes."""
import asyncio, importlib.util, json, os, uuid
from pathlib import Path
from playwright.async_api import async_playwright
ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'var/gateway'
BASE='http://10.30.0.28:3194'
CHROME='/data01/home/zukang.xu/.cache/ms-playwright/chromium_headless_shell-1223/chrome-headless-shell-linux64/chrome-headless-shell'

def save(path,data):
    fd=os.open(path,os.O_CREAT|os.O_TRUNC|os.O_WRONLY,0o600)
    with os.fdopen(fd,'w') as f: json.dump(data,f,ensure_ascii=False,indent=2)

async def main():
    statepath=OUT/'approval-ui-state.json'
    state=json.loads(statepath.read_text()) if statepath.exists() else {}
    auth=json.loads((ROOT/'var/sim/probe-auth.json').read_text())
    async with async_playwright() as p:
        browser=await p.chromium.launch(executable_path=CHROME,args=['--no-sandbox'])
        context=await browser.new_context(viewport={'width':1440,'height':1000})
        credentials=json.loads((ROOT/'var/sim/probe-account.json').read_text())
        page=await context.new_page()
        await page.goto(BASE+'/workspace/'+auth['workspaceId'])
        await page.locator('input[name=email]').fill(credentials['email'])
        await page.locator('input[name=password]').fill(credentials['password'])
        await page.get_by_role('button',name='Sign in',exact=True).click()
        await page.get_by_text('New workflow',exact=True).wait_for(timeout=20000)
        if not state:
            spec=importlib.util.spec_from_file_location('g',ROOT/'probes/sim/workflow_probe.py');g=importlib.util.module_from_spec(spec);spec.loader.exec_module(g)
            suffix=uuid.uuid4().hex[:10]
            start=g.block('start-'+suffix,'start_trigger');start['name']='start'
            human=g.block('approval-'+suffix,'human_in_the_loop_v2',{'inputFormat':('input-format',[{'id':'approved','name':'approved','type':'boolean','value':False}]),'notification':('tool-input',[]),'builderData':('response-format',[])},300);human['name']='approval'
            gate=g.block('decision-'+suffix,'condition',{'conditions':('condition-input',json.dumps([{'id':'approved','title':'if','value':'<approval.submission.approved> === true'},{'id':'rejected','title':'else','value':''}]))},600);gate['name']='decision'
            yes=g.response('approved-'+suffix,{'decision':'approved'});yes['position']={'x':900,'y':0}
            no=g.response('rejected-'+suffix,{'decision':'rejected'});no['position']={'x':900,'y':250}
            graph={'blocks':{b['id']:b for b in [start,human,gate,yes,no]},'edges':[g.edge(start['id'],human['id']),g.edge(human['id'],gate['id']),g.edge(gate['id'],yes['id'],'condition-approved'),g.edge(gate['id'],no['id'],'condition-rejected')],'loops':{},'parallels':{},'variables':{},'metadata':{'name':'Approval Ui '+suffix}}
            state={'status':'import-intent','name':graph['metadata']['name'],'humanBlockId':human['id']};save(statepath,state)
            await page.locator('input[type=file][accept=".json,.zip"]').set_input_files({'name':state['name']+'.json','mimeType':'application/json','buffer':json.dumps(graph).encode()})
            await page.get_by_text(state['name'],exact=True).first.wait_for(timeout=30000)
            await page.wait_for_timeout(2500)
            state.update(status='imported',workflowId=page.url.split('/w/')[-1]);save(statepath,state)
        else:
            if state['status']=='import-intent': raise RuntimeError('Reconcile unknown import before retry')
            await page.get_by_text(state['name'],exact=True).first.click()
            await page.wait_for_timeout(1500)
        for branch,choice,expected in [('approve',True,'approved'),('reject',False,'rejected')]:
            if branch not in state:
                intentkey=branch+'RunIntent'
                if state.get(intentkey): raise RuntimeError('Unknown Run outcome; reconcile native event log')
                state[intentkey]=True;save(statepath,state)
                await page.goto(BASE+'/workspace/'+auth['workspaceId']+'/w/'+state['workflowId'])
                await page.get_by_role('button',name='Run',exact=True).wait_for(timeout=20000)
                async with page.expect_response(lambda r:'/api/workflows/' in r.url and '/execute' in r.url and r.request.method=='POST') as response_info:
                    await page.get_by_role('button',name='Run',exact=True).click()
                response=await response_info.value
                stream=await response.text()
                save(OUT/('approval-ui-'+branch+'-events.json'),{'status':response.status,'events':stream})
                events=[json.loads(line[6:]) for line in stream.splitlines() if line.startswith('data: {')]
                paused=next(x for x in events if x['type']=='execution:paused')
                resume=paused['data']['output']['data']['_resume']
                state[branch]={'workflowId':resume['workflowId'],'runId':resume['executionId'],'contextId':resume['contextId'],'blockId':next(x['blockId'] for x in paused['data']['finalBlockLogs'] if x['blockType']=='human_in_the_loop_v2')}
                save(statepath,state)
            record=state[branch]
            await page.goto(BASE+'/resume/'+state['workflowId']+'/'+record['runId'])
            if not record.get('submitted'):
                await page.get_by_role('button',name='Resume Execution',exact=True).wait_for(timeout=15000)
                if choice:
                    await page.get_by_text('False',exact=True).click()
                    await page.get_by_text('True',exact=True).click()
                record.update(submitted='intent',choice=choice);save(statepath,state)
                async with page.expect_response(lambda r:'/api/resume/' in r.url and r.request.method=='POST') as response_info:
                    await page.get_by_role('button',name='Resume Execution',exact=True).click()
                response=await response_info.value
                record.update(submitted='received',httpStatus=response.status,resumeResponse=await response.json());save(statepath,state)
            if record['submitted']!='received': raise RuntimeError('Unknown submission outcome; never resubmit')
            for attempt in range(60):
                response=await context.request.get(BASE+'/api/v2/workflows/'+state['workflowId']+'/runs/'+record['runId']+'?includeOutput=true',headers={'X-API-Key':auth['apiKey']})
                assert response.status==200
                result=(await response.json())['data']
                if result['status']=='completed': break
                await page.wait_for_timeout(500)
            record['observed']=result;save(statepath,state)
            assert result['status']=='completed'
            assert result['output']['data']=={'decision':expected}
            await page.reload()
            await page.wait_for_timeout(1500)
            record['nativePageAfter']=(await page.locator('body').inner_text())[:2000]
            save(statepath,state)
            await page.screenshot(path=str(OUT/('approval-ui-'+branch+'-completed.png')),full_page=True)
        report={'workflowId':state['workflowId'],'creation':'original UI file import','execution':'original browser Run button','approval':'original Resume Form and Resume Execution button','branches':{branch:{'runId':state[branch]['runId'],'contextId':state[branch]['contextId'],'blockId':state[branch]['blockId'],'resumedExecutionId':state[branch]['resumeResponse']['executionId'],'status':state[branch]['observed']['status'],'output':state[branch]['observed']['output']} for branch in ['approve','reject']}}
        save(OUT/'approval-ui-report.json',report)
        print(json.dumps(report))
        await browser.close()

asyncio.run(main())
