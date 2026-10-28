"""An owned executor exit proves interruption, never a fabricated native terminal."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from starlette.requests import Request

spec = importlib.util.spec_from_file_location(
    'interruption_bridge', Path(__file__).resolve().parents[1] / 'plugins/agent-line/execution_bridge.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class InterruptionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.bridge = module.ExecutionBridge.__new__(module.ExecutionBridge)
        b = self.bridge
        b.workspace = root / 'work'; b.workspace.mkdir()
        b.codex_home = root / 'home'; b.codex_home.mkdir()
        b.container_control = root / 'control.py'
        b.target = 'fixed'; b.target_config = Mock()
        b.retire_runtime = AsyncMock(return_value=True)
        database = root / 'receipts.sqlite'
        module.ReceiptLedger.initialize(database)
        b.ledger = module.ReceiptLedger(database)
        b.ledger.claim('task', {'targetAgentId': 'fixed', 'workspace': str(b.workspace),
                               'roleSessionId': 'agent-line-task'})
        self.identity = {'upstreamTaskId': 'native', 'codexThreadId': 'thread', 'codexTurnId': 'turn'}
        b.ledger.patch('task', status='accepted', identity=self.identity)
        (b.workspace / 'harnesses').mkdir()
        (b.workspace / 'harnesses/codex_sessions.json').write_text(json.dumps({'agent-line-task': 'thread'}))
        (b.codex_home / 'sessions').mkdir()
        self.rollout = b.codex_home / 'sessions/rollout-thread.jsonl'
        self.entries = [
            {'type': 'session_meta', 'payload': {'id': 'thread', 'cwd': str(b.workspace)}},
            {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'turn'}},
        ]
        self.write_rollout()
        self.proof = {'schemaVersion': 1, 'resourcesReleased': True,
                      'codexThreadId': 'thread', 'codexTurnId': 'turn',
                      'owner': 'a' * 32, 'containerId': 'b' * 64,
                      'exitReceipt': {'schemaVersion': 1, 'owner': 'a' * 32,
                          'containerId': 'b' * 64, 'workspace': str(b.workspace),
                          'identities': [{'codexThreadId': 'thread', 'codexTurnId': 'turn', 'nativeTerminal': False}],
                          'running': False, 'pid': 0, 'status': 'exited',
                          'finishedAt': '2026-10-21T12:15:00Z', 'source': 'docker-inspect-before-remove'}}
        self.proof['launcherExitProof'] = {
            'schemaVersion': 1, 'source': 'procfs-launcher-exit',
            'owner': 'a' * 32, 'containerId': 'b' * 64,
            'pid': 123, 'startTicks': '456', 'uid': os.getuid(),
            'state': 'absent', 'observedAt': 1790000000.25,
        }

    def write_rollout(self):
        self.rollout.write_text('\n'.join(json.dumps(x) for x in self.entries) + '\n')

    async def observe(self, proof=None, code=0):
        process = Mock(returncode=code)
        process.communicate = AsyncMock(return_value=(json.dumps(self.proof if proof is None else proof).encode(), b''))
        with patch.object(module.asyncio, 'create_subprocess_exec', AsyncMock(return_value=process)) as create, patch(
            'qwenpaw.app.routers.console.get_console_chat_task', AsyncMock(side_effect=HTTPException(404))):
            result = await self.bridge.observe('task')
        return result, create

    async def test_exit_without_native_terminal_is_independent_and_idempotent(self):
        first, create = await self.observe()
        self.assertEqual(create.call_args.args[2], 'observe')
        self.assertEqual(first['status'], 'accepted')
        self.assertFalse(first['terminalConfirmed'])
        self.assertTrue(first['executorInterrupted'])
        self.assertTrue(first['resourcesReleased'])
        self.assertEqual(first['resourceExitReceipt'], self.proof['exitReceipt'])
        self.assertEqual(first['nativeStatus'], 'unknown')
        self.assertEqual(first['reason'], 'executor_exit_without_native_terminal')
        self.assertEqual(first['identity'], self.identity)
        second, _ = await self.observe()
        self.assertEqual(first, second)

    async def test_missing_live_malformed_or_mismatching_proof_is_not_interruption(self):
        variants = [({}, 0), (self.proof, 1), ([], 0)]
        for field, value in [('workspace', '/other'), ('owner', 'f' * 32), ('containerId', 'f' * 64),
                             ('running', True), ('pid', 1), ('pid', False), ('status', 'running'),
                             ('source', 'not-found'), ('finishedAt', ''), ('identities', []),
                             ('identities', [None]), ('identities', self.proof['exitReceipt']['identities'] * 2),
                             ('identities', self.proof['exitReceipt']['identities'] + [None]), ('schemaVersion', True)]:
            candidate = copy.deepcopy(self.proof); candidate['exitReceipt'][field] = value
            variants.append((candidate, 0))
        for field, value in [('owner', 'short'), ('containerId', 'short'), ('codexTurnId', 'other'),
                             ('schemaVersion', True)]:
            candidate = copy.deepcopy(self.proof); candidate[field] = value
            if field in ('owner', 'containerId'): candidate['exitReceipt'][field] = value
            variants.append((candidate, 0))
        for proof, code in variants:
            with self.subTest(proof=proof, code=code):
                result, _ = await self.observe(proof, code)
                self.assertFalse(result.get('executorInterrupted', False))
                self.assertFalse(result['resourcesReleased'])
                self.assertIsNone(result.get('resourceExitReceipt'))
                self.assertFalse(result['terminalConfirmed'])

    async def test_terminal_written_during_control_observe_wins_over_interruption(self):
        actions = []
        async def start(*args, **kwargs):
            actions.append(args[2])
            process = Mock(returncode=0)
            async def communicate():
                if args[2] == 'observe':
                    self.entries.append({'type': 'event_msg', 'payload': {
                        'type': 'task_complete', 'turn_id': 'turn', 'last_agent_message': 'done'}})
                    self.write_rollout()
                return json.dumps(self.proof).encode(), b''
            process.communicate = communicate
            return process
        with patch.object(module.asyncio, 'create_subprocess_exec', side_effect=start), patch(
                'qwenpaw.app.routers.console.get_console_chat_task', AsyncMock(side_effect=HTTPException(404))):
            result = await self.bridge.observe('task')
        self.assertEqual(actions, ['observe', 'stop'])
        self.assertEqual(result['status'], 'succeeded')
        self.assertTrue(result['terminalConfirmed'])
        self.assertFalse(result['executorInterrupted'])
        self.assertTrue(result['resourcesReleased'])
        self.assertEqual(result['output']['text'], 'done')

    async def test_post_exit_recheck_rejects_changed_metadata_and_turn(self):
        for change in ('metadata', 'turn'):
            with self.subTest(change=change):
                self.entries[0]['payload']['cwd'] = str(self.bridge.workspace)
                self.entries[1]['payload']['turn_id'] = 'turn'
                self.write_rollout()
                async def communicate():
                    if change == 'metadata': self.entries[0]['payload']['cwd'] = '/other'
                    else: self.entries[1]['payload']['turn_id'] = 'other-turn'
                    self.write_rollout()
                    return json.dumps(self.proof).encode(), b''
                process = Mock(returncode=0); process.communicate = communicate
                with patch.object(module.asyncio, 'create_subprocess_exec', AsyncMock(return_value=process)), patch(
                        'qwenpaw.app.routers.console.get_console_chat_task', AsyncMock(side_effect=HTTPException(404))):
                    result = await self.bridge.observe('task')
                self.assertFalse(result.get('executorInterrupted', False))
                self.assertFalse(result['resourcesReleased'])
                self.assertFalse(result['terminalConfirmed'])

    async def test_receipt_native_terminal_hint_alone_never_means_interruption(self):
        proof = copy.deepcopy(self.proof)
        proof['exitReceipt']['identities'][0]['nativeTerminal'] = True
        result, create = await self.observe(proof)
        self.assertEqual(create.call_args.args[2], 'observe')
        self.assertFalse(result.get('executorInterrupted', False))
        self.assertFalse(result['terminalConfirmed'])
        self.assertFalse(result['resourcesReleased'])

    async def test_stale_interruption_does_not_survive_missing_proof_or_binding_change(self):
        await self.observe()
        result, _ = await self.observe({}, 1)
        self.assertFalse(result['executorInterrupted'])
        self.assertFalse(result['resourcesReleased'])
        await self.observe()
        self.bridge.target = 'another-role'
        result, create = await self.observe()
        create.assert_not_called()
        self.assertFalse(result['executorInterrupted'])
        self.assertFalse(result['resourcesReleased'])
        self.assertIsNone(result['resourceExitReceipt'])

    async def test_bound_rollout_mismatch_does_not_use_control(self):
        self.entries[0]['payload']['cwd'] = '/untrusted'; self.write_rollout()
        result, create = await self.observe()
        create.assert_not_called()
        self.assertFalse(result['resourcesReleased'])

    async def test_native_terminal_uses_existing_stop_path(self):
        self.entries.append({'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 'turn'}})
        self.write_rollout()
        result, create = await self.observe()
        self.assertEqual(create.call_args.args[2], 'stop')
        self.assertTrue(result['terminalConfirmed'])
        self.assertTrue(result['resourcesReleased'])
        self.assertFalse(result.get('executorInterrupted', False))
        self.assertEqual(result['status'], 'succeeded')

    async def test_cancel_preserves_independent_exit_proof_without_stop_ack(self):
        expected, _ = await self.observe()
        with patch.object(self.bridge, 'observe', AsyncMock(return_value=expected)), patch(
                'qwenpaw.app.routers.console.post_console_chat_stop', AsyncMock()) as stop:
            result = await self.bridge.cancel('task', Request({'type': 'http', 'headers': [], 'state': {}}))
        stop.assert_not_called()
        self.assertTrue(result['executorInterrupted'])
        self.assertTrue(result['cancellationRequested'])
        self.assertEqual(result['resourceExitReceipt'], self.proof['exitReceipt'])
        self.assertFalse(result['terminalConfirmed'])
