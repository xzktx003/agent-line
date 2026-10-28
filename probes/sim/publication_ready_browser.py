"""Live GET-only UI proof for an already published, completed candidate flow.

--config points to private JSON containing baseUrl, serviceFile, projectId,
workflowId, bindingId, revision, runId, taskId, roleBindingId, and roleAgentId.
It never starts a service, creates a task, or substitutes business responses.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import stat
import time
from urllib.parse import quote, urlsplit

CHROME = '/data01/home/zukang.xu/.cache/ms-playwright/chromium_headless_shell-1223/chrome-headless-shell-linux64/chrome-headless-shell'
FIELDS = {'baseUrl', 'serviceFile', 'projectId', 'workflowId', 'bindingId', 'revision',
          'runId', 'taskId', 'roleBindingId', 'roleAgentId'}


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def private_json(path):
    path = Path(path)
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077,
            'private_regular_configuration_required')
    return json.loads(path.read_text())


def resolve(base, value):
    path = Path(value)
    return path if path.is_absolute() else base / path


def load_configuration(path):
    path = Path(path).absolute()
    config = private_json(path)
    require(isinstance(config, dict) and set(config) == FIELDS, 'configuration_fields_invalid')
    url = urlsplit(config['baseUrl'])
    require(url.scheme == 'http' and url.hostname in {'127.0.0.1', 'localhost', '::1'}
            and url.port is not None and not url.username and not url.password
            and url.path in {'', '/'} and not url.query and not url.fragment, 'loopback_origin_required')
    require(type(config['revision']) is int and config['revision'] > 0, 'revision_invalid')
    for field in FIELDS - {'baseUrl', 'serviceFile', 'revision'}:
        require(isinstance(config[field], str) and config[field] and len(config[field]) <= 256,
                'identity_invalid')
    service_file = resolve(path.parent, config['serviceFile'])
    service = private_json(service_file)
    authoring = private_json(resolve(service_file.parent, service['authoringFile']))
    project = config['projectId']
    credentials = [entry['token'] for entry in authoring['credentials']
                   if project in entry['projectScopes'] and 'edit_workflows' in entry['capabilities']
                   and entry['expiresAt'] > time.time()]
    tokens = [token for token, scopes in service['tokens'].items() if project in scopes]
    require(len(credentials) == 1 and len(tokens) == 1, 'unique_project_credentials_required')
    binding = service['bindings'][config['roleBindingId']]
    require(binding['projectId'] == project and binding['roleAgentId'] == config['roleAgentId'],
            'configured_role_identity_changed')
    return config, credentials[0], tokens[0], path.parent / 'browser'


def check_document(config, detail):
    document = detail['document']
    require(detail.get('workflowId') == config['workflowId'] and detail.get('isDeployed') is True,
            'published_workflow_identity_changed')
    require(any(binding.get('bindingId') == config['bindingId'] and binding.get('revision') == config['revision']
                for binding in detail.get('publishedBindings', [])), 'published_revision_missing')
    agents = [node for node in document['nodes'] if node['type'] == 'agent']
    require(len(agents) == 1 and agents[0]['config']['roleBindingId'] == config['roleBindingId']
            and agents[0]['config']['memoryPolicyId'] == 'native-role', 'native_role_policy_changed')
    digest = hashlib.sha256(json.dumps(document, sort_keys=True, ensure_ascii=False,
                                       separators=(',', ':')).encode()).hexdigest()
    return agents[0], digest


def check_results(config, run, task):
    require(task.get('taskId') == config['taskId'] and task.get('projectId') == config['projectId']
            and task.get('roleBindingId') == config['roleBindingId'], 'task_identity_changed')
    require(task.get('executionStatus') == 'succeeded' and task.get('verificationStatus') == 'passed'
            and task.get('resourcesReleased') is True, 'task_not_verified_and_released')
    execution = run.get('resumption', {}).get('execution') or run.get('execution', {})
    require(execution.get('status') in {'completed', 'succeeded'}, 'flow_not_completed')
    return dict(taskId=task['taskId'], executionStatus=task['executionStatus'],
                verificationStatus=task['verificationStatus'], memoryStatus=task.get('memoryStatus'),
                resourcesReleased=True, runStatus=execution['status'])


def write(path, data):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)


async def main(config_path):
    from playwright.async_api import async_playwright, expect
    config, editor_token, service_token, out = load_configuration(config_path)
    out.mkdir(mode=0o700, exist_ok=True)
    info = out.lstat()
    require(stat.S_ISDIR(info.st_mode) and not out.is_symlink() and info.st_uid == os.getuid()
            and not info.st_mode & 0o077, 'private_output_directory_required')
    base = config['baseUrl'].rstrip('/')
    origin = urlsplit(base).netloc
    authoring_base = '/extension/v1/authoring/projects/' + quote(config['projectId'], safe='')
    detail_path = authoring_base + '/workflows/' + quote(config['workflowId'], safe='')
    run_path = '/extension/v1/workflows/' + quote(config['bindingId'], safe='') + '/runs/' + quote(config['runId'], safe='')
    task_path = '/extension/v1/node-tasks/' + quote(config['taskId'], safe='')
    blocked, errors, csp, gets, snapshots = [], [], [], [], []

    async def read_only(route):
        req = route.request
        url = urlsplit(req.url)
        if req.method != 'GET' or url.scheme != 'http' or url.netloc != origin:
            blocked.append(dict(method=req.method, path=url.path))
            await route.abort()
        else:
            gets.append(url.path)
            await route.continue_()

    async def response_for(page, path, action):
        async with page.expect_response(lambda response: urlsplit(response.url).path == path
                                        and response.request.method == 'GET') as event:
            await action()
        response = await event.value
        require(response.status == 200, 'live_get_failed')
        return await response.json()

    async def attach(context):
        await context.route('**/*', read_only)
        page = await context.new_page()
        page.on('pageerror', lambda error: errors.append(type(error).__name__))
        page.on('console', lambda message: csp.append('csp_violation') if 'Content Security Policy' in message.text else None)
        return page

    async def editor(page, prefix):
        await page.goto(base + '/workbench/editor.html')
        await page.locator('#token').fill(editor_token)
        await page.locator('#login').click()
        await expect(page.locator('#connection')).to_have_text('编辑服务已连接')
        listing = await response_for(page, authoring_base + '/workflows',
                                    lambda: page.locator('#project').select_option(config['projectId']))
        while not any(row['id'] == config['workflowId'] for row in listing['workflows']):
            require(listing.get('nextCursor') is not None, 'workflow_missing_from_directory')
            listing = await response_for(page, authoring_base + '/workflows', lambda: page.locator('#next').click())
        row = next(row for row in listing['workflows'] if row['id'] == config['workflowId'])
        detail = await response_for(page, detail_path,
                                    lambda: page.locator('.workflow-choice').filter(has_text=row['name']).click())
        agent, digest = check_document(config, detail)
        await page.locator('#node-list').get_by_role('button', name=agent['name'], exact=True).click()
        await expect(page.locator('#agent-role')).to_have_value(config['roleBindingId'])
        await expect(page.locator('#agent-instruction')).to_have_value(agent['config']['instruction'])
        await expect(page.locator('.native-memory-note')).to_contain_text('原生记忆')
        await expect(page.locator('.run-binding-link').filter(has_text='版本 ' + str(config['revision']) + ' ·')).to_have_count(1)
        require(not await page.locator('#token').input_value(), 'editor_credential_field_not_cleared')
        await screenshot(page, prefix + '-editor')
        return dict(documentSha256=digest, stateSha256=detail['stateSha256'], revision=config['revision'])

    async def screenshot(page, stem):
        for width, height, size in ((1440, 1000, 'desktop'), (390, 844, 'mobile')):
            await page.set_viewport_size(dict(width=width, height=height))
            require(await page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'horizontal_overflow')
            await page.screenshot(path=str(out / (stem + '-' + size + '.png')), full_page=True)
            if stem.endswith('-results'):
                # Keep long genuine Sim output intact while also capturing the
                # status summaries at readable resolution for a bounded review.
                for target in ('run', 'task'):
                    await page.locator('#' + target + '-result > dl').screenshot(
                        path=str(out / (stem + '-' + size + '-' + target + '-summary.png')))
        require(await page.evaluate('localStorage.length + sessionStorage.length') == 0, 'persistent_browser_storage_found')

    async def results(page, prefix):
        await page.goto(base + '/workbench/')
        await page.locator('#token').fill(service_token)
        await page.locator('#login-form button').click()
        await expect(page.locator('#connection-badge')).to_have_text('服务已连接')
        await page.locator('#query-binding').fill(config['bindingId'])
        await page.locator('#run-id').fill(config['runId'])
        run = await response_for(page, run_path, lambda: page.locator('#run-form button[type=submit]').click())
        await page.locator('#task-id').fill(config['taskId'])
        task = await response_for(page, task_path, lambda: page.locator('#task-form button[type=submit]').click())
        summary = check_results(config, run, task)
        await expect(page.locator('#run-result')).to_contain_text(config['runId'])
        await expect(page.locator('#task-result')).to_contain_text(config['taskId'])
        await expect(page.locator('#task-result')).to_contain_text('验证通过')
        await expect(page.locator('#task-result')).to_contain_text('执行资源已退出')
        await screenshot(page, prefix + '-results')
        return summary

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=CHROME, args=['--no-sandbox'])
        try:
            # Separate contexts have no shared credentials, cookies or storage.
            for index in range(2):
                context = await browser.new_context(viewport=dict(width=1440, height=1000))
                try:
                    page = await attach(context)
                    draft = await editor(page, 'open' + str(index + 1))
                    await page.close()
                    page = await attach(context)
                    result = await results(page, 'open' + str(index + 1))
                    snapshots.append(dict(draft=draft, results=result))
                finally:
                    await context.close()
            require(snapshots[0] == snapshots[1], 'reopened_results_changed')
            require(not blocked and not errors and not csp, 'browser_read_only_or_error_check_failed')
            write(out / 'report.json', dict(mode='live_get_only_existing_published_flow', passed=True,
                workflowId=config['workflowId'], bindingId=config['bindingId'], runId=config['runId'],
                taskId=config['taskId'], revision=config['revision'], roleBindingId=config['roleBindingId'],
                roleAgentId=config['roleAgentId'], snapshots=snapshots, blockedRequests=blocked,
                pageErrors=errors, cspErrors=csp, getPaths=gets, businessMutations=0, syntheticResponses=0,
                checks=['live_published_draft_and_revision', 'native_role_memory_policy_preserved',
                        'live_completed_run', 'live_independently_verified_released_task',
                        'close_context_and_reopen_same_results', 'desktop_mobile_no_overflow',
                        'no_persistent_credentials', 'get_only_no_new_execution']))
            for path in out.glob('*.png'):
                path.chmod(0o600)
            print('Live read-only publication UI verification passed; no business mutations.')
        except Exception:
            write(out / 'failure.json', dict(passed=False, blockedRequests=blocked, pageErrors=errors,
                                            cspErrors=csp, getPaths=gets, snapshots=snapshots))
            raise
        finally:
            await browser.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    try:
        asyncio.run(main(parser.parse_args().config))
    except Exception as error:
        # Do not echo Playwright call arguments or credential-bearing config.
        raise SystemExit('Read-only browser verification failed: ' + type(error).__name__) from None
