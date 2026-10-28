"""Show the existing real test-failure task in the current task center.

Does not rerun Codex, change role models, or relax sandbox policy. The source
ledger is copied read-only into this probe's directory before the UI starts.
"""
import asyncio
import hashlib
import json
import os
import secrets
import socket
import sqlite3
import threading
from pathlib import Path

import uvicorn
from fastapi import Header, HTTPException
from playwright.async_api import async_playwright, expect

from agent_line.service import ServiceConfig, create_app
from agent_line.store import TaskStore
from agent_line.workbench import mount_workbench


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'var/pinned-test-failure-known-template/tasks.sqlite'
OUT = ROOT / 'var/ac63-task-center-20261027'
CHROME = ('/data01/home/zukang.xu/.cache/ms-playwright/chromium_headless_shell-1223/'
          'chrome-headless-shell-linux64/chrome-headless-shell')
TASK = 'task-4f118a0762814ecebb444a0438c256c9'
PROJECT = 'native-memory-roundtrip'
NODE = 'al_1517d9bf43716b68c05ae8e393ce46a7--agent'
ATTEMPT = '1'
RUN = 'da3379e0-c3da-5765-92bd-080ef0f0c5d6'


def private_json(path, value):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    path.chmod(0o600)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def task_projection(path):
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as db:
        row = db.execute(
            'SELECT execution_status, verification_status, memory_status, resources_released, version, payload, evidence '
            'FROM tasks WHERE id=?', (TASK,)).fetchone()
        events = [kind for kind, in db.execute(
            'SELECT kind FROM events WHERE task_id=? ORDER BY seq', (TASK,))]
    return {'row': row[:5], 'payloadSha256': hashlib.sha256(row[5].encode()).hexdigest(),
            'evidenceSha256': hashlib.sha256(row[6].encode()).hexdigest(), 'events': events}


def copy_ledger():
    OUT.mkdir(mode=0o700, exist_ok=True)
    OUT.chmod(0o700)
    destination = OUT / 'tasks.sqlite'
    for suffix in ('', '-wal', '-shm'):
        extra = Path(str(destination) + suffix)
        if extra.exists():
            extra.unlink()
    source = sqlite3.connect(f'file:{SOURCE}?mode=ro', uri=True)
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    destination.chmod(0o600)
    return destination


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class Server:
    def __init__(self, store, token):
        workspace = ROOT / 'var/executor/workspace'
        app = create_app(store, ServiceConfig(
            tokens={token: {PROJECT}},
            projects={PROJECT: {'workspace': str(workspace), 'name': '工作流接入验证'}},
            bindings={},
            controlled_execution='unknown'))
        mount_workbench(app, ROOT / 'frontend')

        def require_probe_token(authorization: str | None) -> None:
            scheme, _, value = (authorization or '').partition(' ')
            supplied = value.encode('utf-8') if scheme.lower() == 'bearer' else b''
            if not secrets.compare_digest(supplied, token.encode('utf-8')):
                raise HTTPException(401, '需要有效的服务凭证', headers={'WWW-Authenticate': 'Bearer'})

        # The copied ledger has no workflow, work-item, or trigger configuration.
        # Answer those directory reads with the same empty contracts as an
        # authorized service that has nothing configured, so the runs page can
        # open the task without treating a missing directory as a page error.
        @app.get('/extension/v1/workflows')
        def empty_workflows(authorization: str | None = Header(default=None)):
            require_probe_token(authorization)
            return {'workflows': []}

        @app.get('/extension/v1/work-items/projects')
        def empty_work_item_projects(authorization: str | None = Header(default=None)):
            require_probe_token(authorization)
            return {'source': 'configured_projects', 'projects': []}

        @app.get('/extension/v1/triggers')
        def empty_triggers(authorization: str | None = Header(default=None)):
            require_probe_token(authorization)
            return {'source': 'configured_trigger_directory', 'configured': True,
                    'triggers': [], 'nextCursor': None}
        self.port = free_port()
        self.config = uvicorn.Config(app, host='127.0.0.1', port=self.port, log_level='error',
                                     access_log=False)
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, name='ac63-task-center', daemon=True)

    def start(self):
        self.thread.start()
        while not self.server.started:
            if not self.thread.is_alive():
                raise RuntimeError('task center server exited before listening')
            self.thread.join(0.05)

    def stop(self):
        self.server.should_exit = True
        self.thread.join(10)


async def main():
    source_before = digest(SOURCE)
    projection_before = task_projection(SOURCE)
    ledger = copy_ledger()
    token = secrets.token_urlsafe(32)
    store = TaskStore(ledger)
    server = Server(store, token)
    server.start()
    origin = f'http://127.0.0.1:{server.port}'
    writes, errors, checks = [], [], []
    try:
        async with async_playwright() as pw:
            libraries = '/data01/home/zukang.xu/.cache/research-hub/chromium-libs/root/usr/lib/x86_64-linux-gnu'
            env = os.environ.copy()
            env['LD_LIBRARY_PATH'] = libraries + (':' + env['LD_LIBRARY_PATH'] if env.get('LD_LIBRARY_PATH') else '')
            browser = await pw.chromium.launch(executable_path=CHROME, args=['--no-sandbox'], env=env)
            try:
                context = await browser.new_context(viewport={'width': 1440, 'height': 1000})
                page = await context.new_page()
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('console', lambda msg: errors.append('console:' + msg.type + ':' + msg.text) if msg.type in ('error','warning') else None)
                page.on('request', lambda req: writes.append(req.method)
                        if '/extension/v1/' in req.url and req.method not in ('GET', 'HEAD') else None)
                await page.goto(origin + '/workbench/')
                overview = page.locator('#overview-tasks')
                await expect(overview).to_contain_text(TASK)
                await expect(overview).to_contain_text('执行：执行成功')
                await expect(overview).to_contain_text('验证：验证失败')
                await expect(overview).to_contain_text('记忆：未请求回写')
                await expect(overview).to_contain_text('需要关注：独立验收未通过，执行成功与验收结论分别保留')
                await expect(overview).to_contain_text('节点：' + NODE)
                await overview.locator('summary').click()
                await expect(overview).to_contain_text('尝试：' + ATTEMPT)
                await expect(overview).to_contain_text('运行：' + RUN)
                overview_text = await overview.inner_text()
                checks.append('overview_separates_execution_verification_and_memory')
                if '整体成功' in overview_text or '验证通过' in overview_text:
                    raise AssertionError('overview presented an overall or verification success')
                checks.append('overview_does_not_claim_overall_success')
                await page.screenshot(path=OUT / 'overview.png', full_page=True)
                await page.goto(origin + '/workbench/runs.html#task=' + TASK)
                await page.locator('#token').fill(token)
                await page.locator('#login-form button').click()
                try:
                    await expect(page.locator('#connection-badge')).to_have_text('操作已授权', timeout=15000)
                except Exception:
                    notice = await page.locator('#notice').inner_text()
                    badge = await page.locator('#connection-badge').inner_text()
                    raise AssertionError(f'login badge={badge!r} notice={notice!r} diagnostics={errors!r}') from None
                await expect(page.locator('#task-id')).to_have_value(TASK)
                await page.locator('#task-form button').click()
                detail = page.locator('#task-result')
                for text in ('执行成功', '验证失败', '未请求回写',
                             '独立验收未通过，执行成功与验收结论分别保留',
                             '节点标识', NODE, '本次尝试', ATTEMPT,
                             '原生执行任务', 'task-33c10e76f61b',
                             '产物与独立验收记录', '命令以非零状态退出',
                             '独立测试', 'addition-unittests', '退出码'):
                    await expect(detail).to_contain_text(text)
                # The artifact check passed; the task verification did not.
                await expect(detail).to_contain_text('符合声明的字节与大小策略')
                detail_text = await detail.inner_text()
                if '整体成功' in detail_text or '验证通过' in detail_text:
                    raise AssertionError('detail presented an overall or verification success')
                checks.append('detail_traces_node_attempt_and_real_test_evidence')
                await page.locator('#task-result .task-trace summary', has_text='任务事件时间线').click()
                await page.locator('#task-result button', has_text='读取任务事件').click()
                await expect(detail).to_contain_text('已记录独立验证失败')
                await expect(detail).to_contain_text('已记录执行成功终态')
                checks.append('events_keep_execution_success_and_verification_failure_separate')
                if writes:
                    raise AssertionError('task center issued mutations: ' + ','.join(writes))
                checks.append('browser_issued_no_mutations')
                if errors:
                    raise AssertionError('page errors: ' + ','.join(errors))
                checks.append('browser_has_no_page_errors')
                await page.screenshot(path=OUT / 'detail.png', full_page=True)
            finally:
                await browser.close()
    finally:
        server.stop()
    source_after = digest(SOURCE)
    projection_after = task_projection(SOURCE)
    copy_after = task_projection(ledger)
    if source_before != source_after or projection_before != projection_after or projection_before != copy_after:
        raise AssertionError('real failure ledger changed during read-only observation')
    checks.append('source_and_copy_ledgers_unchanged')
    report = {
        'status': 'passed',
        'decision': 'observe_existing_real_test_failure',
        'notChanged': ['sandbox', 'apparmor', 'user_namespace', 'reme', 'role_model_gpt-6.1-sol', 'main_task_database'],
        'taskId': TASK,
        'executionStatus': 'succeeded',
        'verificationStatus': 'failed',
        'memoryStatus': 'not_requested',
        'nodeId': NODE,
        'attemptId': ATTEMPT,
        'runId': RUN,
        'checks': checks,
        'mutations': writes,
        'pageErrors': errors,
        'sourceSha256': source_after,
    }
    private_json(OUT / 'report.json', report)
    print(json.dumps({'status': 'passed', 'checks': len(checks), 'port': server.port}, ensure_ascii=False))


if __name__ == '__main__':
    asyncio.run(main())
