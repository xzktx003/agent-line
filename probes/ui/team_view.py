"""Fixture-only browser checks for the project team graph editor.

All authoring API traffic is intercepted. The probe never contacts Sim, native
agents, model services, or a live authoring server.
"""
import asyncio
import copy
import json
import mimetypes
import os
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, expect

from probes.qwenpaw_memory.reconcile_browser import CHROME

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'var/team-view-20261022/ui'
ORIGIN = 'https://team-fixture.test'
PROJECTS = {'projects': [{'id': 'fixture-project', 'name': '模拟项目'},
                         {'id': 'other-project', 'name': '另一个模拟项目'}], 'canPublish': False}
ROLES = [
    {'roleAgentId': 'existing-planner', 'bindingIds': ['planner-binding'], 'enabled': True,
     'roleRevision': 'revision-4'},
    {'roleAgentId': 'existing-codex', 'bindingIds': ['codex-binding'], 'enabled': True,
     'roleRevision': 'revision-2'},
    {'roleAgentId': 'existing-disabled', 'bindingIds': ['disabled-binding'], 'enabled': False,
     'roleRevision': None},
]
ORIGINAL = [{'sourceAgentId': 'existing-planner', 'targetAgentId': 'existing-codex'}]


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    with os.fdopen(os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600), 'w') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)


def team(project='fixture-project', revision=5, edges=None, roles=None, orphans=None):
    return {'projectId': project, 'revision': revision, 'edges': copy.deepcopy(ORIGINAL if edges is None else edges),
            'roles': copy.deepcopy(ROLES if roles is None else roles),
            'orphanAgentIds': list(orphans or []), 'activeTasks': [], 'tasksAvailable': True,
            'tasksTruncated': False}


async def run_case(browser, mode):
    context = await browser.new_context(viewport={'width': 1440, 'height': 1000})
    page = await context.new_page()
    errors, posts, gets, unexpected = [], [], [], []
    initial = team()
    if mode == 'canonical-ids':
        initial = team(edges=[{'sourceAgentId': 'agent_A', 'targetAgentId': 'agent_B'}], roles=[
            {'roleAgentId': 'agent_A', 'bindingIds': ['binding_A'], 'enabled': True, 'roleRevision': None},
            {'roleAgentId': 'agent_B', 'bindingIds': ['binding_B'], 'enabled': True, 'roleRevision': 'r2'},
            {'roleAgentId': 'Agent_C', 'bindingIds': ['binding_C'], 'enabled': True, 'roleRevision': 'r3'}])
    if mode == 'active-task':
        initial['activeTasks'] = [{'taskId': 'fixture-task-A', 'roleAgentId': 'existing-planner',
                                   'workflowRunId': 'fixture-run-A', 'executionStatus': 'running',
                                   'verificationStatus': 'pending', 'memoryStatus': 'not_requested',
                                   'resourcesReleased': False,
                                   'executionIdentity': {'agentId': 'default', 'workflowRunId': 'fixture-run-A',
                                                         'privatePayload': 'never-render-this'}}]
    if mode == 'xss-id':
        initial = team(edges=[], roles=[{'roleAgentId': '<img src=x onerror=alert(1)>',
                                        'bindingIds': ['unsafe-binding'], 'enabled': True,
                                        'roleRevision': 'xss-fixture'}, *copy.deepcopy(ROLES[1:])])
    state = {'team': initial, 'project': 'fixture-project', 'operation': None, 'teamReads': 0,
             'mode': mode, 'late': asyncio.Event(), 'release': asyncio.Event()}
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('dialog', lambda dialog: dialog.accept())

    async def route(r):
        req = r.request
        parsed = urlsplit(req.url)
        path = parsed.path
        if parsed.netloc != 'team-fixture.test':
            unexpected.append({'origin': parsed.netloc, 'path': path})
            await r.abort()
            return
        if path.startswith('/extension/v1/authoring/'):
            if req.method == 'POST':
                body = req.post_data_json
                posts.append({'path': path, 'body': copy.deepcopy(body)})
                assert path == '/extension/v1/authoring/projects/fixture-project/team'
                assert set(body) == {'operationId', 'expectedRevision', 'edges'}
                if mode == 'conflict':
                    await r.fulfill(status=409, json={'detail': 'stale revision'})
                    return
                receipt = {'projectId': 'fixture-project', 'operationId': body['operationId'],
                           'revision': body['expectedRevision'] + 1, 'edges': copy.deepcopy(body['edges'])}
                state['operation'] = copy.deepcopy(receipt)
                state['team']['revision'] = receipt['revision']
                state['team']['edges'] = copy.deepcopy(receipt['edges'])
                if mode == 'late-save':
                    state['late'].set()
                    await state['release'].wait()
                if mode == 'late-logout':
                    state['late'].set()
                    await state['release'].wait()
                    await r.fulfill(status=401, json={'detail': 'expired'})
                    return
                if mode == 'lost-response' or mode.startswith('bad-query'):
                    await r.abort('connectionreset')
                    return
                if mode == 'bad-receipt-project':
                    receipt['projectId'] = 'other-project'
                elif mode == 'bad-receipt-edges':
                    receipt['edges'] = []
                elif mode == 'bad-receipt-version':
                    receipt['revision'] = body['expectedRevision']
                await r.fulfill(status=200, json=receipt)
                return
            if req.method != 'GET':
                unexpected.append({'method': req.method, 'path': path})
                await r.abort()
                return
            gets.append(path)
            if path.endswith('/projects'):
                await r.fulfill(json=PROJECTS)
                return
            if path.endswith('/team/operations/' + str((state['operation'] or {}).get('operationId', 'no-op'))):
                receipt = copy.deepcopy(state['operation'])
                if mode == 'bad-query-project': receipt['projectId'] = 'other-project'
                if mode == 'bad-query-edges': receipt['edges'] = []
                if mode == 'bad-query-version': receipt['revision'] = -1
                await r.fulfill(json=receipt or {})
                return
            if path.endswith('/team'):
                state['teamReads'] += 1
                result = copy.deepcopy(state['team'])
                if mode == 'wrong-team-project': result['projectId'] = 'other-project'
                elif mode == 'bad-team-edges': result['edges'] = [{'sourceAgentId': 'x', 'targetAgentId': 'x'}]
                elif mode == 'bad-team-version': result['revision'] = '5'
                elif mode == 'role-config-change': result['roles'] = result['roles'][:2]
                if mode == 'late-read' and state['teamReads'] == 1:
                    state['late'].set()
                    await state['release'].wait()
                await r.fulfill(json=result)
                return
            unexpected.append({'method': req.method, 'path': path})
            await r.abort()
            return
        if req.method == 'GET' and path.startswith('/workbench/'):
            relative = path.removeprefix('/workbench/')
            file = (ROOT / 'frontend' / relative).resolve()
            if file.parent == ROOT / 'frontend' and file.is_file():
                await r.fulfill(content_type=mimetypes.guess_type(file)[0] or 'application/octet-stream',
                                body=file.read_bytes(), headers={'Content-Security-Policy':
                                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'"})
                return
        unexpected.append({'method': req.method, 'path': path})
        await r.abort()

    await context.route('**/*', route)
    await page.goto(ORIGIN + '/workbench/team.html')
    await page.locator('#team-token').fill('fixture-only-credential')
    await page.locator('#team-connect').click()
    await expect(page.locator('#team-actions')).to_be_visible(timeout=5000)
    await page.locator('#team-project').select_option('fixture-project')
    if mode == 'late-read':
        await asyncio.wait_for(state['late'].wait(), 10)
        await page.locator('#team-project').evaluate("e=>{e.value='other-project';e.dispatchEvent(new Event('change'));}")
        state['release'].set()
        await expect(page.locator('#team-content')).to_be_hidden()
        assert not posts
    elif mode in ('wrong-team-project', 'bad-team-edges', 'bad-team-version'):
        await expect(page.locator('#team-content')).to_be_hidden()
        assert not posts
    else:
        try:
            await expect(page.locator('#team-content')).to_be_visible()
        except Exception as error:
            raise AssertionError({'mode': mode, 'notice': await page.locator('#team-notice').inner_text(),
                                  'pageErrors': errors, 'team': state['team']}) from error
        await expect(page.locator('#team-graph')).to_contain_text('')
        if mode not in ('xss-id', 'canonical-ids'):
            assert await page.locator('#team-equivalent').get_by_text('existing-planner', exact=True).count() == 1
        if mode == 'canonical-ids':
            assert await page.locator('#team-equivalent').get_by_text('Agent_C', exact=True).count() == 1
        # Native-looking names are rendered as text, including in SVG.
        if mode == 'xss-id':
            pass
        if mode == 'disabled-role':
            await expect(page.locator('#team-equivalent')).to_contain_text('existing-disabled')
            await expect(page.locator('#team-equivalent')).to_contain_text('已停用')
        if mode == 'role-config-change':
            await expect(page.locator('#team-equivalent')).not_to_contain_text('existing-disabled')
        if mode == 'active-task':
            await expect(page.locator('#role-tasks')).to_contain_text('fixture-task-A')
            await expect(page.locator('#role-tasks')).to_contain_text('未请求回写')
            assert 'never-render-this' not in await page.locator('#role-tasks').inner_text()
        if mode == 'orphan':
            state['team'] = team(edges=[*ORIGINAL, {'sourceAgentId': 'retired-agent',
                                                    'targetAgentId': 'existing-codex'}],
                                 orphans=['retired-agent'])
            await page.locator('#team-refresh').click()
            await expect(page.locator('#team-equivalent')).to_contain_text('retired-agent')
            assert await page.locator('#team-equivalent img').count() == 0
            remove = page.locator('#team-equivalent button').filter(has_text='移除失效关系')
            await remove.click()
            await page.locator('#team-save').click()
            await expect(page.locator('#team-notice')).to_contain_text('已保存')
            assert len(posts) == 1
        elif mode in ('normal', 'conflict', 'lost-response', 'bad-receipt-project',
                      'bad-receipt-edges', 'bad-receipt-version', 'bad-query-project',
                      'bad-query-edges', 'bad-query-version', 'late-save', 'late-read', 'xss-id',
                      'disabled-role', 'role-config-change', 'canonical-ids', 'late-logout'):
            source = ('<img src=x onerror=alert(1)>' if mode == 'xss-id' else
                      'agent_B' if mode == 'canonical-ids' else 'existing-codex')
            if mode == 'xss-id': target = 'existing-codex'
            elif mode == 'canonical-ids': target = 'agent_A'
            elif mode == 'disabled-role': target = 'existing-disabled'
            else: target = 'existing-planner'
            await page.locator('#edge-source').select_option(source)
            await page.locator('#edge-target').select_option(target)
            await page.locator('#edge-add').click()
            if mode == 'disabled-role':
                assert await page.locator('#edge-staging').get_by_text('existing-disabled').count() == 1
            if mode == 'xss-id':
                assert await page.locator('#team-graph img').count() == 0
                assert '<img src=x onerror=alert(1)>' in (await page.locator('#team-graph').text_content())
            if mode in ('normal', 'canonical-ids'):
                assert await page.locator('#edge-staging button').first.evaluate(
                    '(e) => e.getBoundingClientRect().height >= 44')
                assert await page.locator('#team-graph .team-node text').first.evaluate(
                    '(e) => Number.parseFloat(getComputedStyle(e).fontSize) >= 13')
            if mode in ('late-save', 'late-logout'):
                await page.locator('#team-save').click()
                await asyncio.wait_for(state['late'].wait(), 10)
                if mode == 'late-logout':
                    state['release'].set()
                    await expect(page.locator('#team-login')).to_be_visible()
                    await expect(page.locator('#team-content')).to_be_hidden()
                    assert await page.locator('#team-token').input_value() == ''
                else:
                    await page.locator('#team-project').evaluate("e=>{e.value='other-project';e.dispatchEvent(new Event('change'));}")
                    state['release'].set()
                    await expect(page.locator('#team-title')).to_contain_text('fixture-project')
                    assert len(posts) == 1
            else:
                await page.locator('#team-save').click()
                if mode == 'disabled-role':
                    await expect(page.locator('#team-notice')).to_contain_text('保存')
                    assert len(posts) == 1
                    assert any(edge['targetAgentId'] == 'existing-disabled'
                               for edge in posts[0]['body']['edges'])
                if mode == 'conflict':
                    await expect(page.locator('#team-notice')).to_contain_text('版本已变化')
                    await expect(page.locator('#edge-staging')).to_contain_text('existing-planner')
                    assert await page.locator('#team-save').is_disabled()
                elif mode.startswith('bad-receipt'):
                    await expect(page.locator('#team-notice')).to_contain_text('结果待核对')
                    assert await page.locator('#team-save').is_disabled()
                    assert await page.locator('#team-observe').is_visible()
                elif mode == 'lost-response':
                    await expect(page.locator('#team-observe')).to_be_visible()
                    before = len(posts)
                    await page.locator('#team-observe').click()
                    assert len(posts) == before
                    assert any('/team/operations/' in path for path in gets)
                elif mode.startswith('bad-query'):
                    await expect(page.locator('#team-observe')).to_be_visible()
                    await page.locator('#team-observe').click()
                    assert len(posts) == 1
                if mode in ('normal', 'canonical-ids'):
                    assert len(posts) == 1
                    body = posts[0]['body']
                    if mode == 'normal':
                        assert body['expectedRevision'] == 5
                        assert body['edges'] == [
                            {'sourceAgentId': 'existing-codex', 'targetAgentId': 'existing-planner'},
                            {'sourceAgentId': 'existing-planner', 'targetAgentId': 'existing-codex'}]
                    else:
                        assert body['expectedRevision'] == 5
                        assert body['edges'] == [{'sourceAgentId': 'agent_A', 'targetAgentId': 'agent_B'},
                                                 {'sourceAgentId': 'agent_B', 'targetAgentId': 'agent_A'}]
                    assert set(body) == {'operationId', 'expectedRevision', 'edges'}
                    assert not await page.evaluate('localStorage.length + sessionStorage.length')
                    for width, height, suffix in ((1440, 1000, 'desktop'), (390, 844, 'mobile')):
                        await page.set_viewport_size({'width': width, 'height': height})
                        assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                        assert await page.locator('#team-graph .team-node text').first.evaluate(
                            '(e) => Number.parseFloat(getComputedStyle(e).fontSize) >= 13')
                        assert await page.locator('#team-equivalent').evaluate(
                            '(e) => e.scrollWidth <= e.clientWidth')
                        await page.screenshot(path=str(OUT / f'{suffix}.png'), full_page=True)
                    assert await page.locator('#team-save').evaluate('(e) => e.getBoundingClientRect().height >= 44')
                    assert await page.locator('#edge-add').evaluate('(e) => e.getBoundingClientRect().height >= 44')
                    await page.locator('#team-logout').click()
                    assert await page.locator('#team-token').input_value() == ''
                    assert not await page.evaluate('localStorage.length + sessionStorage.length')
    result = {'mode': mode, 'passed': True, 'apiPosts': len(posts), 'apiGets': len(gets),
              'unexpectedRequests': unexpected, 'pageErrors': errors}
    assert not unexpected, unexpected
    assert not errors, errors
    await context.close()
    return result


async def main():
    OUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(OUT, 0o700)
    modes = ['normal', 'conflict', 'lost-response', 'bad-receipt-project', 'bad-receipt-edges',
             'bad-receipt-version', 'bad-query-project', 'bad-query-edges', 'bad-query-version',
             'wrong-team-project', 'bad-team-edges', 'bad-team-version', 'disabled-role',
             'role-config-change', 'orphan', 'late-save', 'late-read', 'late-logout', 'xss-id',
             'canonical-ids', 'active-task']
    if os.getenv('TEAM_VIEW_CASES'):
        modes = os.environ['TEAM_VIEW_CASES'].split(',')
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=CHROME, args=['--no-sandbox'])
        try:
            results = []
            for mode in modes:
                try:
                    results.append(await run_case(browser, mode))
                except Exception as error:
                    raise RuntimeError('fixture case failed: ' + mode) from error
        finally:
            await browser.close()
    report = {'passed': True, 'fixtureOnly': True, 'nativeAgentCalls': 0, 'modelCalls': 0,
              'simCalls': 0, 'project': 'synthetic fixture only', 'cases': results}
    save(OUT / 'report.json', report)
    for file in OUT.glob('*.png'):
        file.chmod(0o600)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    asyncio.run(main())
