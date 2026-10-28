"""Real QwenPaw loader smoke test in a fresh process and isolated working dir."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

class PluginContractTest(unittest.TestCase):
    def test_real_loader_authenticated_routes_and_unload(self):
        code = r'''
import asyncio
import json
import sys
from pathlib import Path
from fastapi import FastAPI
from fastapi.testclient import TestClient
from qwenpaw.plugins.loader import PluginLoader
from qwenpaw.plugins.architecture import PluginManifest
from qwenpaw.app.auth import create_token, verify_token

async def check():
    directory = Path(sys.argv[1])
    manifest = PluginManifest(**json.loads((directory / 'plugin.json').read_text()))
    app = FastAPI()
    # Exercise the host identity contract with real QwenPaw signed-token verification.
    # Full host middleware/config/browser validation is a separate E2E gate.
    @app.middleware('http')
    async def identity(request, call_next):
        token = request.headers.get('authorization', '').removeprefix('Bearer ')
        if token:
            request.state.user = verify_token(token)
        return await call_next(request)
    loader = PluginLoader([directory.parent])
    loader.registry.set_plugin_http_app(app)
    record = await loader.load_plugin(manifest, directory, config={'managed_role_ids': ['planner'], 'codex_target_agent_id': 'fixed-codex'})
    assert record.enabled
    import inspect
    from types import SimpleNamespace
    from qwenpaw.app.agent_context import set_current_agent_id, set_current_session_id
    from agentscope.message import ToolResultState
    hooks = [h for h in loader.registry.get_startup_hooks() if h.plugin_id == 'agent-line' and 'register_tool' in h.hook_name]
    assert len(hooks) == 2
    for hook in hooks:
        hook.callback()
    from qwenpaw.agents.tools import agent_line_delegate
    assert list(inspect.signature(agent_line_delegate).parameters) == ['instruction']
    assert not agent_line_delegate._tool_descriptor.enabled_by_default
    set_current_agent_id('planner')
    set_current_session_id('registered-role-session')
    result = await agent_line_delegate('Implement within assigned task')
    assert result.state == ToolResultState.ERROR
    assert '尚未配置' in result.content[0].text
    set_current_agent_id('unmanaged')
    assert (await agent_line_delegate('test')).state == ToolResultState.DENIED
    registration = next(r for r in loader.registry.get_middleware_factories() if r.plugin_id == 'agent-line')
    middleware = registration.factory(SimpleNamespace(agent_id='planner'), None)
    invoked = []
    async def downstream():
        invoked.append(True)
        yield 'allowed'
    for tool_name in ('execute_shell_command', 'write_file', 'submit_to_agent', 'chat_with_agent', 'spawn_subagent', 'unknown_tool'):
        try:
            async for item in middleware.on_acting(None, {'tool_call': SimpleNamespace(name=tool_name)}, downstream):
                pass
        except PermissionError:
            pass
        else:
            raise AssertionError('Restricted role invoked ' + tool_name)
    assert not invoked
    items = [item async for item in middleware.on_acting(None, {'tool_call': SimpleNamespace(name='memory_search')}, downstream)]
    assert items == ['allowed']
    assert registration.factory(SimpleNamespace(agent_id='unmanaged'), None) is None
    # Receipt persistence/crash windows exercise the real ledger and submission
    # bridge; native invocation is a deterministic test double only in this test.
    import importlib
    import os
    from fastapi import HTTPException, Request
    module = importlib.import_module(record.instance.__class__.__module__ + '.execution_bridge')
    workspace = Path(os.environ['QWENPAW_WORKING_DIR'])
    db_path = workspace / 'receipts.sqlite3'
    module.ReceiptLedger.initialize(db_path)
    token_path = workspace / 'service-token'
    token_path.write_text('x' * 48)
    token_path.chmod(0o600)
    os.environ['CODEX_HOME'] = str(workspace)
    bridge = module.ExecutionBridge({'receipt_db':str(db_path),'service_token_file':str(token_path),'workspace':str(workspace),'codex_home':str(workspace),'target_agent_id':'fixed-codex'})
    bridge.target_config = lambda: None
    bridge.roles = frozenset({'planner'})
    bridge.current_revision = lambda role: 'qwenpaw-role-v1:' + 'a'*64
    for task in ('task1', 'task2'):
        bridge.ledger.bind(task, 'planner', 'agent-line-'+task, 'qwenpaw-role-v1:' + 'a'*64)
    from qwenpaw.app.routers import console
    native_original = console.post_console_chat_task
    native_calls = []
    async def native_submit(payload, request):
        native_calls.append(payload)
        assert request.state.agent_id == 'fixed-codex'
        return {'task_id':'native-test-receipt'}
    console.post_console_chat_task = native_submit
    request = Request({'type':'http','method':'POST','path':'/','headers':[],'state':{},'app':app})
    body = module.ExecutionRequest(taskId='task1',roleSessionId='agent-line-task1',instruction='bounded task')
    first = await bridge.submit(body, request)
    assert first['identity'] == {'upstreamTaskId':'native-test-receipt'}
    duplicate = await bridge.submit(body, request)
    assert len(native_calls) == 1
    assert not duplicate['terminalConfirmed']
    try:
        await bridge.submit(body.model_copy(update={'instruction':'different'}), request)
    except HTTPException as exc:
        assert exc.status_code == 409
    else: raise AssertionError('Changed task payload accepted')
    async def lost_receipt(payload, request):
        native_calls.append(payload)
        raise RuntimeError('Native response lost after possible side effect')
    console.post_console_chat_task = lost_receipt
    uncertain = module.ExecutionRequest(taskId='task2',roleSessionId='agent-line-task2',instruction='bounded task')
    assert (await bridge.submit(uncertain, request))['status'] == 'unknown'
    await bridge.submit(uncertain, request)
    assert len(native_calls) == 2
    reopened = module.ReceiptLedger(db_path)
    assert reopened.get('task2')[1]['status'] == 'unknown'
    assert reopened.bind('parent-task', 'planner', 'agent-line-parent-task', 'qwenpaw-role-v1:' + 'a'*64)
    assert not reopened.bind('parent-task', 'planner', 'agent-line-parent-task', 'qwenpaw-role-v1:' + 'a'*64)
    assert reopened.bound_task('planner', 'agent-line-parent-task') == 'parent-task'
    try: reopened.bind('parent-task', 'different-role', 'agent-line-parent-task', 'qwenpaw-role-v1:' + 'a'*64)
    except HTTPException as exc: assert exc.status_code == 409
    else: raise AssertionError('Binding changed role')
    try: reopened.bound_task('planner', 'unbound-session')
    except HTTPException as exc: assert exc.status_code == 403
    else: raise AssertionError('Unknown parent admitted')
    (workspace / 'harnesses').mkdir()
    (workspace / 'harnesses/codex_sessions.json').write_text(json.dumps({'agent-line-task2':'native-thread'}))
    (workspace / 'sessions').mkdir()
    rollout = workspace / 'sessions/rollout-native-thread.jsonl'
    entries = [
        {'type':'session_meta','payload':{'id':'native-thread','cwd':str(workspace)}},
        {'type':'event_msg','payload':{'type':'task_started','turn_id':'native-turn'}},
    ]
    rollout.write_text(''.join(json.dumps(entry)+'\n' for entry in entries))
    running = await bridge.observe('task2')
    assert running['status'] == 'accepted' and not running['terminalConfirmed']
    assert running['identity']['codexTurnId'] == 'native-turn'
    with rollout.open('a') as stream:
        stream.write(json.dumps({'type':'event_msg','payload':{'type':'task_complete','turn_id':'native-turn','last_agent_message':'done'}})+'\n')
    finished = await bridge.observe('task2')
    assert finished['terminalConfirmed'] and finished['status'] == 'succeeded'
    assert not finished['resourcesReleased']  # Missing upstream receipt is not resource proof.
    assert finished['taskId'] == 'task2'
    console.post_console_chat_task = native_original
    try: bridge.authorize('wrong')
    except HTTPException as exc: assert exc.status_code == 403
    else: raise AssertionError('Wrong service credential accepted')
    bridge.authorize('x' * 48)
    client = TestClient(app)
    for endpoint in ('health', 'capabilities'):
        assert client.get('/api/agent-line/' + endpoint).status_code == 401
        assert client.get('/api/agent-line/' + endpoint, headers={'Authorization': 'Bearer forged'}).status_code == 401
    headers = {'Authorization': 'Bearer ' + create_token('isolated-test-user')}
    response = client.get('/api/agent-line/health', headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()['scope'] == 'plugin-registration-only'
    response = client.get('/api/agent-line/capabilities', headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()['capabilities']['codex_execution'] == 'unknown'
    assert response.json()['capabilities']['memory_roundtrip'] == 'unknown'
    await loader.unload_plugin(manifest.id)
    assert client.get('/api/agent-line/health', headers=headers).status_code == 404
    incompatible = PluginManifest(**{**manifest.model_dump(), 'qwenpaw_version': {'min': '999.0.0'}})
    rejected = await loader.load_plugin(incompatible, directory, config={})
    assert not rejected.enabled
    assert client.get('/api/agent-line/health', headers=headers).status_code == 404
asyncio.run(check())
'''
        with tempfile.TemporaryDirectory(prefix='agent-line-plugin-') as directory:
            env = dict(os.environ, QWENPAW_WORKING_DIR=directory, QWENPAW_SECRET_DIR=str(Path(directory) / 'secrets'))
            env.pop('QWENPAW_RUNTIME_ID', None)
            process = subprocess.run([str(ROOT / '.venv/bin/python'), '-c', code, str(ROOT / 'plugins/agent-line')], env=env, capture_output=True, text=True, timeout=90)
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)

if __name__ == '__main__':
    unittest.main()
