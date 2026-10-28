"""Intercepted real-browser static-parallel editing checks; zero live API/model calls."""
import asyncio
import copy
import json
import mimetypes
import sys
from pathlib import Path

from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agent_line.sim_authoring import compile_document
OUT = ROOT / '.omx/state/editor-static-parallel'
CHROME = '/data01/home/zukang.xu/.cache/ms-playwright/chromium_headless_shell-1223/chrome-headless-shell-linux64/chrome-headless-shell'
ROLE = 'existing_role'


def fixture():
    def node(i, kind, config, x, y):
        return dict(id=i, type=kind, name=i, config=config, position=dict(x=x, y=y))
    def agent(source):
        return dict(roleBindingId=ROLE, instruction='离线固定任务，不调用模型',
                    inputFrom=dict(nodeId=source, field='input' if source == 'start' else 'output'),
                    memoryPolicyId='native-role', outputSchemaId='task-result',
                    requireMemoryCommit=False, allowFailureBranch=True)
    def edge(a, b, branch=None):
        return dict(id=a+'_'+b, source=a, target=b, **({'branch': branch} if branch else {}))
    return dict(schemaVersion=1, variables={}, nodes=[
        node('start', 'start', {}, 10, 130),
        node('fork', 'parallel', dict(branches=['branch_a', 'branch_b'], join='join', maxConcurrency=2), 200, 130),
        node('branch_a', 'agent', agent('start'), 400, 20),
        node('branch_b', 'agent', agent('start'), 400, 230),
        node('join', 'join', dict(parallelId='fork', policy='all_success', requiredBranches=[]), 610, 130),
        node('after', 'agent', agent('join'), 810, 130),
        node('condition', 'condition', dict(rules=[dict(id='yes', source='join', field='result.output.allSucceeded', operator='eq', value=True)]), 1010, 130),
        node('end_yes', 'end', dict(resultFrom='join'), 1210, 30),
        node('end_no', 'end', dict(output={}), 1210, 240),
    ], edges=[edge('start', 'fork'), edge('fork', 'branch_a'), edge('fork', 'branch_b'),
              edge('branch_a', 'join'), edge('branch_b', 'join'), edge('join', 'after'),
              edge('after', 'condition'), edge('condition', 'end_yes', 'yes'), edge('condition', 'end_no', 'else')])


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    doc = fixture()
    writes, reads, errors, csp, checks, compiled_saves = [], [], [], [], [], []
    uncertain = False
    late = False
    held, release = asyncio.Event(), asyncio.Event()
    operation = None
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=CHROME, args=['--no-sandbox'])
        try:
            context = await browser.new_context(viewport={'width': 1440, 'height': 1000})
            async def route(r):
                nonlocal doc, operation
                path = r.request.url.split('editor.test')[-1].split('?')[0]
                if path.startswith('/extension/'):
                    if r.request.method == 'POST':
                        body = r.request.post_data_json
                        writes.append({'path': path, 'body': copy.deepcopy(body)})
                        kind = 'save' if path.endswith('/save') else 'publish'
                        assert kind == 'save' or path.endswith('/publish')
                        if kind == 'save':
                            compiled = compile_document(body['document'], 'offline-workflow')
                            assert isinstance(compiled, dict)
                            compiled_saves.append(next(n['config']['policy'] for n in body['document']['nodes'] if n['type'] == 'join'))
                            doc = copy.deepcopy(body['document'])
                        receipt = dict(workflowId='fixture', outcome='saved' if kind == 'save' else 'published', stateSha256='a'*64)
                        if kind == 'publish': receipt.update(bindingId='offline_binding', revision=1)
                        operation = dict(projectId='offline', operationId=body['operationId'], kind=kind, status='accepted', receipt=receipt)
                        if uncertain:
                            await r.fulfill(status=503, content_type='application/json', body='{}')
                            return
                        data = operation
                    else:
                        reads.append(path)
                        if '/operations/' in path: data = operation
                        elif path.endswith('/projects'): data = dict(projects=[dict(id='offline', name='离线固定测试项目')], canPublish=True)
                        elif path.endswith('/roles'): data = dict(roles=[dict(roleBindingId=ROLE, roleAgentId='既有角色', memoryPolicyId='native-role', outputSchemaId='task-result')], source='configured_existing_roles')
                        elif path.endswith('/workflows'): data = dict(workflows=[dict(id='fixture', name='并行分支与汇合离线验证')], nextCursor=None)
                        else:
                            data = dict(workflowId='fixture', document=copy.deepcopy(doc), editable=True, draftEmpty=False, stateSha256='a'*64, name='并行分支与汇合离线验证')
                            if late:
                                held.set()
                                await release.wait()
                    await r.fulfill(status=200, content_type='application/json', body=json.dumps(data))
                    return
                file = ROOT / 'frontend' / path.removeprefix('/workbench/')
                if file.is_file():
                    await r.fulfill(status=200, content_type=mimetypes.guess_type(file)[0] or 'application/octet-stream', body=file.read_bytes(), headers={'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'"})
                else: await r.fulfill(status=404, body='not found')
            await context.route('**/*', route)
            page = await context.new_page()
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.on('console', lambda m: csp.append(m.text) if 'Content Security Policy' in m.text else None)
            page.on('dialog', lambda d: d.accept())
            await page.goto('https://editor.test/workbench/editor.html')
            await page.locator('#token').fill('offline-only-secret')
            await page.locator('#login').click()
            await expect(page.locator('#connection')).to_have_text('编辑服务已连接')
            await page.locator('#project').select_option('offline')
            await page.locator('.workflow-choice').click()
            async def select(name):
                await page.locator('#node-list').get_by_role('button', name=name, exact=True).click()
            async def apply():
                await page.locator('#node-form button[type=submit]').click()
            async def reload():
                await page.locator('#reload').click()
                await expect(page.locator('#save')).to_be_disabled()
                await expect(page.locator('#draft-status')).to_contain_text('当前草稿已读取')
            async def save():
                before = len(writes)
                await page.locator('#save').click()
                await expect(page.locator('#save')).to_be_disabled()
                assert len(writes) == before+1
            await select('fork')
            assert await page.locator('#parallel-branches').evaluate('(e)=>[...e.selectedOptions].map(o=>o.value)') == ['branch_a', 'branch_b']
            await expect(page.locator('#parallel-join')).to_have_value('join')
            await page.locator('#parallel-cap').fill('3')
            await expect(page.locator('#save')).to_be_disabled()
            await expect(page.locator('#draft-status')).to_contain_text('表单尚未应用')
            await apply(); await save(); await reload(); await select('fork')
            await expect(page.locator('#parallel-cap')).to_have_value('3')
            assert doc['nodes'][1]['config'] == dict(branches=['branch_a', 'branch_b'], join='join', maxConcurrency=3)
            checks += ['fork_edit_payload_readback', 'form_dirty_save_blocked']
            await page.locator('#parallel-branches').select_option(['branch_a']); await apply()
            await expect(page.locator('#notice')).to_contain_text('2–20')
            await page.locator('#parallel-branches').select_option(['branch_a', 'branch_b']); await apply()
            for target in ['branch_a', 'branch_b']:
                await page.locator('#edge-list .edge-row').filter(has_text='fork → '+target).get_by_role('button').click()
            await page.locator('#save').click(); await expect(page.locator('#notice')).to_contain_text('准确连接')
            assert len(writes) == 1
            for target in ['branch_a', 'branch_b']:
                await page.locator('#edge-source').select_option('fork')
                await page.locator('#edge-target').select_option(target)
                await page.locator('#connect').click()
            assert await page.locator('#edge-list .edge-row').filter(has_text='fork →').count() == 2
            checks += ['branch_count_invalid_apply_blocked', 'exact_edges_required', 'fork_multiple_default_edges_editable']
            await page.locator('#parallel-cap').fill('21'); await apply()
            assert await page.locator('#parallel-cap').evaluate('(e)=>!e.validity.valid')
            assert len(writes) == 1
            await page.locator('#parallel-cap').fill('3'); await apply()
            checks.append('invalid_cap_native_constraint_no_write')
            for policy in ['all_success', 'allow_partial', 'required_success']:
                await select('join'); await page.locator('#join-policy').select_option(policy)
                if policy == 'required_success':
                    await page.locator('#join-required').select_option([]); await apply()
                    await expect(page.locator('#notice')).to_contain_text('至少选择一个')
                    await page.locator('#join-required').select_option(['branch_a'])
                await apply(); await save(); await reload(); await select('join')
                await expect(page.locator('#join-policy')).to_have_value(policy)
                cfg = next(n for n in doc['nodes'] if n['id'] == 'join')['config']
                assert cfg == dict(parallelId='fork', policy=policy, requiredBranches=['branch_a'] if policy == 'required_success' else [])
                checks.append(policy+'_payload_readback')
            await page.locator('#publish').click(); await expect(page.locator('#operation-status')).to_contain_text('已发布')
            checks.append('explicit_publish_fixed_digest')
            await select('after'); await expect(page.locator('#agent-source')).to_have_value('join')
            assert await page.locator('#agent-source option[value="branch_a"]').count() == 0
            await apply()
            await select('condition'); await expect(page.locator('[data-field=source]')).to_have_value('join')
            assert await page.locator('[data-field=source] option[value="branch_a"]').count() == 0
            await apply(); await select('end_yes'); await expect(page.locator('#end-result-source')).to_have_value('join')
            assert await page.locator('#end-result-source option[value="branch_b"]').count() == 0
            await apply(); await save()
            checks += ['join_agent_input', 'join_condition_source', 'join_end_source', 'branch_bypass_not_offered']
            await select('fork')
            await page.locator('.editor-panel').screenshot(path=str(OUT/'desktop-fork.png'))
            await select('join'); await page.locator('.editor-panel').screenshot(path=str(OUT/'desktop-join.png'))
            await page.set_viewport_size({'width': 390, 'height': 844})
            await page.locator('.editor-panel').screenshot(path=str(OUT/'mobile-join.png'))
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            await select('fork'); await page.locator('.editor-panel').screenshot(path=str(OUT/'mobile-fork.png'))
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            checks.append('desktop_mobile_no_document_overflow')
            # A deleted branch stays visibly selected as invalid, never silently removed.
            before = len(writes)
            await select('branch_a'); await page.locator('#delete-node').click(); await select('fork')
            assert '失效的原节点' in await page.locator('#parallel-branches option[value="branch_a"]').inner_text()
            await apply(); await expect(page.locator('#notice')).to_contain_text('有效的不同 Agent')
            assert len(writes) == before
            checks.append('deleted_branch_preserved_apply_blocked')
            await reload()
            await select('join'); await page.locator('#delete-node').click(); await select('after')
            await expect(page.locator('#agent-source')).to_have_value('join')
            assert '失效' in await page.locator('#agent-source option:checked').inner_text()
            await apply(); await expect(page.locator('#notice')).to_contain_text('请选择开始输入')
            checks.append('deleted_join_reference_preserved_blocked')
            await reload()
            # An out-of-pair requirement is retained for correction, never submitted.
            doc['nodes'][4]['config']['requiredBranches'] = ['missing_branch']
            await reload(); await select('join')
            assert '不在配对分支中' in await page.locator('#join-required option[value="missing_branch"]').inner_text()
            await apply(); await expect(page.locator('#notice')).to_contain_text('配对分支')
            assert len(writes) == before
            doc = fixture(); await reload()
            checks.append('required_subset_invalid_preserved')
            # Existing loop graphs are preserved; new parallel cannot be mixed into them.
            await page.locator('#node-type').select_option('loop'); await page.locator('#add-node').click()
            await expect(page.locator('#notice')).to_contain_text('暂不能混用')
            assert await page.locator('#node-list button').count() == 9
            checks.append('loop_combination_explicitly_blocked')
            # Both new types remain available in the existing single-node workflow.
            for kind, original in [('parallel', 'fork'), ('join', 'join')]:
                await page.locator('#node-type').select_option(kind); await page.locator('#add-node').click()
                await expect(page.locator('#notice')).to_contain_text('只支持一对')
                await select(original); await page.locator('#delete-node').click()
                await page.locator('#add-node').click()
                await expect(page.locator('#'+kind+'-config')).to_be_visible()
                await reload()
            checks += ['existing_add_node_entry', 'single_parallel_pair_bound']
            # Unknown save outcome blocks further mutations; original operation GET only.
            uncertain = True
            await select('fork'); await page.locator('#parallel-cap').fill('4'); await apply()
            before = len(writes); await page.locator('#save').click()
            await expect(page.locator('#notice')).to_contain_text('只查询原操作')
            await expect(page.locator('#save')).to_be_disabled(); await expect(page.locator('#publish')).to_be_disabled()
            assert len(writes) == before+1
            await page.locator('#observe').click(); await expect(page.locator('#operation-status')).to_contain_text('已保存')
            assert len(writes) == before+1 and any('/operations/' in path for path in reads)
            uncertain = False; checks.append('unknown_save_original_get_only')
            # Unrecognized payload is read-only even if a broken server marks editable.
            doc['nodes'][1]['config']['futurePolicy'] = {'keep': True}
            await page.locator('#reload').click(); await expect(page.locator('#draft-status')).to_contain_text('只读保护')
            await expect(page.locator('#add-node')).to_be_disabled()
            assert doc['nodes'][1]['config']['futurePolicy'] == {'keep': True}
            checks.append('unknown_parallel_config_readonly_no_clobber')
            doc = fixture()
            late = True
            await page.locator('#reload').click(no_wait_after=True)
            await asyncio.wait_for(held.wait(), 10)
            # Logout is intentionally available while a read is in flight.
            await page.locator('#logout').click(); release.set()
            await expect(page.locator('#connection')).to_have_text('尚未连接')
            await page.wait_for_timeout(100)
            assert await page.locator('#node-list button').count() == 0
            for ident in ['parallel-branches', 'parallel-join', 'join-parallel', 'join-required']:
                assert await page.locator('#'+ident+' option').count() == 0
            assert await page.locator('#token').input_value() == ''
            assert await page.evaluate('localStorage.length+sessionStorage.length') == 0
            assert set(compiled_saves) == {'all_success', 'allow_partial', 'required_success'}
            checks.append('every_save_real_compile_document_all_three_policies')
            checks += ['late_read_after_logout_ignored', 'logout_sensitive_fields_cleared', 'no_persistent_credentials']
            assert not errors and not csp
            report = dict(passed=True, mode='offline_intercepted_real_browser', realRequests=0, modelCalls=0,
                          simulatedWrites=len(writes), compilerVerifiedSaves=len(compiled_saves), compilerVerifiedPolicies=sorted(set(compiled_saves)), pageErrors=errors, cspErrors=csp, checks=checks)
            (OUT/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(json.dumps(report, ensure_ascii=False))
        finally:
            await browser.close()


if __name__ == '__main__':
    asyncio.run(main())
