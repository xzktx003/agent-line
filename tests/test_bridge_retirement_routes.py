"""Authenticated restart observation through the real plugin HTTP router."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from qwenpaw.app import auth


PLUGIN = Path(__file__).resolve().parents[1] / 'plugins/agent-line'
PACKAGE = 'bridge_retirement_route_fixture'
package = ModuleType(PACKAGE)
package.__path__ = [str(PLUGIN)]
sys.modules[PACKAGE] = package
spec = importlib.util.spec_from_file_location(
    PACKAGE + '.execution_bridge', PLUGIN / 'execution_bridge.py')
bridge_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bridge_module
spec.loader.exec_module(bridge_module)


class BridgeRetirementRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.codex_home = self.root / 'codex-home'
        self.codex_home.mkdir()
        token_path = self.root / 'service-token'
        self.service_token = 'retirement-route-service-' + 's' * 48
        token_path.write_text(self.service_token)
        token_path.chmod(0o600)
        for name in ('control.py', 'launcher.py'):
            (self.root / name).write_text('# Test fixture: never executed.\n')
            (self.root / name).chmod(0o600)
        database = self.root / 'receipts.sqlite'
        bridge_module.ReceiptLedger.initialize(database)
        self.bridge = bridge_module.ExecutionBridge({
            'receipt_db': str(database), 'service_token_file': str(token_path),
            'workspace': str(self.workspace), 'codex_home': str(self.codex_home),
            'target_agent_id': 'fixed-codex', 'managed_role_ids': ['parent'],
            'container_control': str(self.root / 'control.py'),
            'container_launcher': str(self.root / 'launcher.py')})
        self.identity = {'upstreamTaskId': 'original-native',
                         'codexThreadId': 'original-thread',
                         'codexTurnId': 'original-turn'}
        self.bridge.ledger.claim('task', {
            'targetAgentId': 'fixed-codex', 'workspace': str(self.workspace),
            'roleSessionId': 'agent-line-task'})
        # A restarted host has the durable receipt but no in-memory application
        # binding or native background-task handle. Reopen the actual database.
        self.bridge.ledger.patch('task', status='accepted',
                                 identity={'upstreamTaskId': 'original-native'})
        self.bridge.ledger = bridge_module.ReceiptLedger(database)
        self.assertIsNone(self.bridge.application)
        (self.workspace / 'harnesses').mkdir()
        (self.workspace / 'harnesses/codex_sessions.json').write_text(
            json.dumps({'agent-line-task': 'original-thread'}))
        (self.codex_home / 'sessions').mkdir()
        self.rollout = self.codex_home / 'sessions/rollout-original-thread.jsonl'
        self.entries = [
            {'type': 'session_meta', 'payload': {
                'id': 'original-thread', 'cwd': str(self.workspace)}},
            {'type': 'event_msg', 'payload': {
                'type': 'task_started', 'turn_id': 'original-turn'}},
            {'type': 'event_msg', 'payload': {
                'type': 'task_complete', 'turn_id': 'original-turn',
                'last_agent_message': 'Original native task completed.'}}]
        self.rollout.write_text(''.join(json.dumps(x) + '\n' for x in self.entries))
        self.proof = {
            'schemaVersion': 1, 'resourcesReleased': True,
            'codexThreadId': 'original-thread', 'codexTurnId': 'original-turn',
            'owner': 'a' * 32, 'containerId': 'b' * 64,
            'exitReceipt': {
                'schemaVersion': 1, 'owner': 'a' * 32, 'containerId': 'b' * 64,
                'workspace': str(self.workspace), 'running': False, 'pid': 0,
                'status': 'exited', 'finishedAt': '2026-10-22T01:00:00Z',
                'source': 'docker-inspect-before-remove',
                'identities': [{'codexThreadId': 'original-thread',
                                'codexTurnId': 'original-turn',
                                'nativeTerminal': True}]},
            'launcherExitProof': {
                'schemaVersion': 1, 'source': 'procfs-launcher-exit',
                'owner': 'a' * 32, 'containerId': 'b' * 64,
                'pid': 12345, 'startTicks': '789', 'uid': os.getuid(),
                'state': 'absent', 'observedAt': 1790000000.5}}
        self.stop = AsyncMock()
        self.tracker = SimpleNamespace(has_active_tasks=AsyncMock(return_value=False))
        self.loaded = SimpleNamespace(
            agent_id='fixed-codex', workspace_dir=self.workspace,
            task_tracker=self.tracker, harness_runtime=SimpleNamespace(stop=self.stop))
        self.manager = Mock()
        self.manager.get_loaded_agent.return_value = self.loaded
        self.app = FastAPI()
        self.app.state.multi_agent_manager = self.manager
        self.app.add_middleware(auth.AuthMiddleware)
        self.app.include_router(self.bridge.router(), prefix='/api/agent-line')

        def start_patch(patcher):
            value = patcher.start()
            self.addCleanup(patcher.stop)
            return value

        # Real host HMAC signing/verification and middleware, with in-memory
        # auth storage/config only. No user's auth file or keys are accessed.
        start_patch(patch.object(auth, '_load_auth_data', return_value={
            'jwt_secret': 'isolated-retirement-test-key', 'revoked_tokens_meta': {}}))
        start_patch(patch.object(auth, 'is_auth_enabled', return_value=True))
        start_patch(patch.object(auth, 'has_registered_users', return_value=True))
        start_patch(patch.object(auth, '_get_config_cached', return_value=(
            SimpleNamespace(security=SimpleNamespace(allow_no_auth_hosts=[])), [])))
        self.host_token = auth.create_token('isolated-route-owner')
        self.headers = {'Authorization': 'Bearer ' + self.host_token,
                        'X-Agent-Line-Service-Token': self.service_token}
        start_patch(patch('qwenpaw.config.utils.load_agent_config', return_value=
            SimpleNamespace(backend='codex', workspace_dir=str(self.workspace),
                            backend_settings={'binary': str(self.root / 'launcher.py')})))
        self.native = start_patch(patch(
            'qwenpaw.app.routers.console.get_console_chat_task',
            AsyncMock(side_effect=HTTPException(404, 'Old native handle unavailable'))))

        async def control(*args, **kwargs):
            self.assertEqual(args[1], str(self.root / 'control.py'))
            self.assertIn(args[2], ('observe', 'stop'))
            self.assertEqual(args[3:], ('--thread', 'original-thread',
                '--turn', 'original-turn', '--workspace', str(self.workspace)))
            return SimpleNamespace(returncode=0, communicate=AsyncMock(
                return_value=(json.dumps(self.proof).encode(), b'')))

        self.control = start_patch(patch.object(
            bridge_module.asyncio, 'create_subprocess_exec', AsyncMock(side_effect=control)))
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.url = '/api/agent-line/executions/task'

    def test_restart_get_requires_both_credentials_then_retires_original_task_once(self):
        cases = [({}, 401),
                 ({'X-Agent-Line-Service-Token': self.service_token}, 401),
                 ({'Authorization': 'Bearer ' + self.host_token}, 403),
                 ({**self.headers, 'Authorization': 'Bearer forged'}, 401),
                 ({**self.headers, 'X-Agent-Line-Service-Token': 'wrong'}, 403)]
        for headers, status in cases:
            with self.subTest(status=status, header_names=sorted(headers)):
                self.assertEqual(self.client.get(self.url, headers=headers).status_code, status)
        self.assertIsNone(self.bridge.application)
        self.control.assert_not_awaited()
        self.stop.assert_not_awaited()

        response = self.client.get(self.url, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertIs(self.bridge.application, self.app)
        self.assertEqual(result['identity'], self.identity)
        self.assertEqual(result['status'], 'succeeded')
        self.assertTrue(result['terminalConfirmed'])
        self.assertTrue(result['resourcesReleased'])
        self.assertEqual(result['resourceExitReceipt'], self.proof['exitReceipt'])
        self.assertEqual(result['runtimeRetirement']['state'], 'stopped')
        self.assertEqual(result['runtimeRetirement']['identity'], self.identity)
        self.assertEqual(self.bridge.ledger.get('task')[1], result)
        self.stop.assert_awaited_once_with()
        self.assertEqual(self.native.await_count, 2)

        # Viewing an old task after the next session starts must use the durable
        # fact, rather than touching that session's target runtime or tracker.
        self.bridge.ledger.claim('new-task', {
            'targetAgentId': 'fixed-codex', 'workspace': str(self.workspace)})
        self.tracker.has_active_tasks.return_value = True
        self.manager.get_loaded_agent.reset_mock()
        self.tracker.has_active_tasks.reset_mock()
        self.native.reset_mock()
        again = self.client.get(self.url, headers=self.headers)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json(), result)
        self.stop.assert_awaited_once_with()
        self.manager.get_loaded_agent.assert_not_called()
        self.tracker.has_active_tasks.assert_not_awaited()
        self.native.assert_not_awaited()

    def test_native_terminal_and_container_exit_without_launcher_proof_keep_unreleased(self):
        self.proof = copy.deepcopy(self.proof)
        self.proof.pop('launcherExitProof')
        result = self.client.get(self.url, headers=self.headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['status'], 'succeeded')
        self.assertTrue(result.json()['terminalConfirmed'])
        self.assertFalse(result.json()['resourcesReleased'])
        self.assertNotIn('runtimeRetirement', result.json())
        self.stop.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
