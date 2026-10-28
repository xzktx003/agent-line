"""Read existing main tasks in a real LAN browser; no fixture routes or writes."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3

import httpx
from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'var/task-state-context-20261022/browser-native'
CHROME = '/data01/home/zukang.xu/.cache/ms-playwright/chromium_headless_shell-1223/chrome-headless-shell-linux64/chrome-headless-shell'
TASK = 'task-f4a4628ba0874b98b15a5e2647b63431'
ORIGIN = 'http://10.30.0.28:3193'


def private_json(path, value):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
    path.chmod(0o600)


def snapshot(config):
    with sqlite3.connect(Path(config['database']).as_uri() + '?mode=ro', uri=True) as db:
        db.execute('BEGIN')
        tables = {name: db.execute('SELECT * FROM ' + name + ' ORDER BY rowid').fetchall()
                  for name in ('tasks', 'events', 'workspace_leases', 'admission_guard')}
    files = {ROOT / 'var/adapter/service.json', ROOT / 'var/executor/config.json',
             ROOT / 'var/qwenpaw/config.json', ROOT / 'var/qwenpaw/agent-line-process.json'}
    files |= set((ROOT / 'var/qwenpaw/workspaces').glob('*/agent.json'))
    files |= {Path(value) for key, value in config.items() if key.endswith('File')}
    digest = lambda data: hashlib.sha256(data).hexdigest()
    return {'tables': {name: digest(json.dumps(rows, ensure_ascii=False).encode())
                       for name, rows in tables.items()},
            'files': {str(path.relative_to(ROOT)): digest(path.read_bytes()) for path in sorted(files)},
            'tasks': len(tables['tasks']), 'leases': len(tables['workspace_leases'])}


async def main(baseline=False):
    OUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    OUT.chmod(0o700)
    config = json.loads((ROOT / 'var/adapter/service.json').read_text())
    token = next(key for key, scopes in config['tokens'].items() if 'native-memory-roundtrip' in scopes)
    before = snapshot(config)
    headers = {'Authorization': 'Bearer ' + token}
    with httpx.Client(base_url=ORIGIN, headers=headers, follow_redirects=False, timeout=30) as client:
        response = client.get('/extension/v1/health')
        assert response.status_code == 200
        health = response.json()
        assert health['controlledExecution'] == 'confirmed' and health['readyForExecution'] is True
        response = client.get('/extension/v1/node-tasks/' + TASK)
        assert response.status_code == 200
        task = response.json()
        assert task['executionStatus'] == 'succeeded' and task['verificationStatus'] == 'passed'
        assert task['memoryStatus'] == 'committed' and task['resourcesReleased'] is True
    writes, errors, checks = [], [], []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=CHROME, args=['--no-sandbox'])
        try:
            context = await browser.new_context(viewport={'width': 1440, 'height': 1000})
            page = await context.new_page()
            page.on('pageerror', lambda error: errors.append(type(error).__name__))
            page.on('request', lambda req: writes.append(req.method)
                    if '/extension/v1/' in req.url and req.method not in ('GET', 'HEAD') else None)
            await page.goto(ORIGIN + '/workbench/')
            await page.locator('#token').fill(token)
            await page.locator('#login-form button').click()
            await expect(page.locator('#connection-badge')).to_have_text('服务已连接')
            await page.locator('#task-id').fill(TASK)
            await page.locator('#task-form button').click()
            await expect(page.locator('#task-result')).to_contain_text('验证通过')
            await expect(page.locator('#task-result')).to_contain_text('已写入')
            checks.append('real_existing_task_has_independent_execution_verification_memory_states')
            if not baseline:
                trace = page.locator('#task-result .task-trace')
                await trace.locator('summary').click()
                for key in ('id', 'revision', 'runId', 'nodeId', 'nodeExecutionId', 'attemptId'):
                    await expect(trace).to_contain_text(task['workflow'][key])
                for key in ('upstreamTaskId', 'codexThreadId', 'codexTurnId'):
                    await expect(trace).to_contain_text(task['executionIdentity'][key])
                checks.append('workflow_activation_attempt_and_native_codex_ids_match_actual_http_record')
            for width, height, name in ((1440, 1000, 'desktop'), (390, 844, 'mobile')):
                await page.set_viewport_size({'width': width, 'height': height})
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                screenshot = OUT / (('baseline-' if baseline else '') + name + '.png')
                await page.locator('#task-result').screenshot(path=str(screenshot))
                screenshot.chmod(0o600)
                checks.append(name + '_no_horizontal_overflow')
            await page.locator('#logout').click()
            await expect(page.locator('#task-result')).to_contain_text('尚未选择节点任务')
            assert TASK not in await page.locator('#task-result').inner_text()
            assert await page.locator('#token').input_value() == ''
            assert await page.evaluate('localStorage.length + sessionStorage.length') == 0
            assert not writes and not errors
            checks += ['logout_clears_task_and_credentials', 'zero_api_mutations_and_browser_errors']
        finally:
            await browser.close()
    assert snapshot(config) == before, 'Task/history/configuration changed during read-only observation'
    checks.append('all_task_rows_events_leases_guards_and_configuration_unchanged')
    report = {'mode': 'real_browser_existing_main_task_no_mocks', 'baseline': baseline,
              'taskId': TASK, 'checks': checks, 'apiMutations': len(writes), 'before': before,
              'limitations': ['same_host_LAN_address_not_another_device',
                              'failure_states_are_separate_fixture_evidence',
                              'no_new_model_task_or_memory_write']}
    path = OUT / ('baseline-report.json' if baseline else 'report.json')
    private_json(path, report)
    print(json.dumps({'checks': len(checks), 'apiMutations': 0, 'report': str(path.relative_to(ROOT))}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', action='store_true')
    asyncio.run(main(parser.parse_args().baseline))
