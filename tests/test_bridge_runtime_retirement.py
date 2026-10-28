"""A completed task must retire its client without stopping later sessions."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from fastapi import HTTPException

from tests import test_bridge_interruption as fixtures


class RuntimeRetirementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = fixtures.InterruptionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.temp.cleanup)
        self.bridge = self.fixture.bridge
        self.bridge.retire_runtime = fixtures.module.ExecutionBridge.retire_runtime.__get__(self.bridge)
        self.workspace = SimpleNamespace(agent_id='fixed', workspace_dir=self.bridge.workspace,
            task_tracker=SimpleNamespace(has_active_tasks=AsyncMock(return_value=False)),
            harness_runtime=SimpleNamespace(stop=AsyncMock()))
        self.manager = Mock()
        self.manager.get_loaded_agent.return_value = self.workspace
        self.bridge.application = SimpleNamespace(state=SimpleNamespace(multi_agent_manager=self.manager))
        self.record = self.bridge.ledger.get('task')[1]
        self.record['terminalConfirmed'] = True
        self.receipt = self.fixture.proof['exitReceipt']
        self.native = AsyncMock(return_value={'status': 'finished'})
        self.patcher = patch('qwenpaw.app.routers.console.get_console_chat_task', self.native)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    async def retire(self):
        return await self.bridge.retire_runtime('task', self.record, self.receipt)

    async def test_idle_exact_target_stops_and_persists_original_identity(self):
        self.assertTrue(await self.retire())
        self.workspace.harness_runtime.stop.assert_awaited_once_with()
        self.native.assert_awaited_once_with('native')
        fact = self.bridge.ledger.get('task')[1]['runtimeRetirement']
        self.assertEqual(fact['identity'], self.fixture.identity)
        self.assertEqual(fact['containerId'], self.receipt['containerId'])
        self.assertEqual(fact['taskId'], 'task')
        self.assertEqual(fact['state'], 'stopped')

    async def test_observing_old_completion_does_not_touch_new_active_runtime(self):
        self.assertTrue(await self.retire())
        self.manager.get_loaded_agent.reset_mock()
        self.native.reset_mock()
        self.workspace.task_tracker.has_active_tasks.return_value = True
        self.bridge.ledger.claim('new', {'targetAgentId': 'fixed', 'workspace': str(self.bridge.workspace)})
        self.assertTrue(await self.retire())
        self.manager.get_loaded_agent.assert_not_called()
        self.native.assert_not_awaited()
        self.workspace.harness_runtime.stop.assert_awaited_once()

    async def test_missing_application_or_manager_preserves_lease(self):
        for app in (None, SimpleNamespace(state=SimpleNamespace())):
            self.bridge.application = app
            self.assertFalse(await self.retire())
        self.workspace.harness_runtime.stop.assert_not_awaited()

    async def test_unloaded_target_is_not_started(self):
        self.manager.get_loaded_agent.return_value = None
        self.assertTrue(await self.retire())
        self.assertEqual(self.bridge.ledger.get('task')[1]['runtimeRetirement']['state'], 'unloaded')
        self.manager.get_agent.assert_not_called()
        self.native.assert_not_awaited()
        self.workspace.harness_runtime.stop.assert_not_awaited()

    async def test_active_tracker_or_unfinished_native_task_does_not_stop(self):
        self.workspace.task_tracker.has_active_tasks.return_value = True
        self.assertFalse(await self.retire())
        self.native.assert_not_awaited()
        self.workspace.task_tracker.has_active_tasks.return_value = False
        for value in ('running', 'pending', None):
            self.native.return_value = {'status': value}
            self.assertFalse(await self.retire())
        self.workspace.harness_runtime.stop.assert_not_awaited()

    async def test_other_unreleased_target_receipt_prevents_stop(self):
        self.bridge.ledger.claim('other', {'targetAgentId': 'fixed', 'workspace': str(self.bridge.workspace)})
        self.assertFalse(await self.retire())
        self.workspace.harness_runtime.stop.assert_not_awaited()
        self.bridge.ledger.patch('other', resourcesReleased=True)
        self.assertTrue(await self.retire())

    async def test_missing_background_handle_only_accepts_verified_native_terminal(self):
        self.native.side_effect = HTTPException(404)
        self.record['terminalConfirmed'] = False
        self.assertFalse(await self.retire())
        self.workspace.harness_runtime.stop.assert_not_awaited()
        self.record['terminalConfirmed'] = True
        self.native.side_effect = HTTPException(503)
        self.assertFalse(await self.retire())
        self.native.side_effect = HTTPException(404)
        self.assertTrue(await self.retire())

    async def test_reopened_ledger_reuses_retirement_without_touching_runtime(self):
        self.assertTrue(await self.retire())
        self.bridge.ledger = fixtures.module.ReceiptLedger(self.bridge.ledger.path)
        self.bridge.application = None
        self.assertTrue(await self.retire())
        self.workspace.harness_runtime.stop.assert_awaited_once()

    async def test_another_targets_record_does_not_block_this_idle_target(self):
        self.bridge.ledger.claim('other', {'targetAgentId': 'other', 'workspace': str(self.bridge.workspace)})
        self.assertTrue(await self.retire())

    async def test_changed_target_id_or_directory_never_stops(self):
        self.workspace.agent_id = 'another'
        self.assertFalse(await self.retire())
        self.workspace.agent_id = 'fixed'
        self.workspace.workspace_dir = '/different-workspace'
        self.assertFalse(await self.retire())
        self.workspace.harness_runtime.stop.assert_not_awaited()

    async def test_failed_stop_or_persistence_is_not_a_retirement_fact(self):
        self.workspace.harness_runtime.stop.side_effect = RuntimeError('unknown stop')
        self.assertFalse(await self.retire())
        self.assertNotIn('runtimeRetirement', self.bridge.ledger.get('task')[1])
        self.workspace.harness_runtime.stop.side_effect = None
        with patch.object(self.bridge.ledger, 'patch', side_effect=OSError('write failed')):
            self.assertFalse(await self.retire())
        self.assertNotIn('runtimeRetirement', self.bridge.ledger.get('task')[1])

    async def test_new_active_task_or_workspace_swap_during_stop_is_not_released(self):
        async def new_task():
            self.workspace.task_tracker.has_active_tasks.return_value = True
        self.workspace.harness_runtime.stop.side_effect = new_task
        self.assertFalse(await self.retire())
        self.workspace.task_tracker.has_active_tasks.return_value = False
        async def replaced():
            self.manager.get_loaded_agent.return_value = Mock()
        self.workspace.harness_runtime.stop.side_effect = replaced
        self.assertFalse(await self.retire())
        self.assertNotIn('runtimeRetirement', self.bridge.ledger.get('task')[1])

    async def test_retirement_cannot_be_reused_for_changed_original_identity(self):
        self.assertTrue(await self.retire())
        self.record['identity'] = {**self.fixture.identity, 'codexTurnId': 'other-turn'}
        self.assertFalse(await self.retire())
        self.workspace.harness_runtime.stop.assert_awaited_once()

    async def test_malformed_or_boolean_schema_fact_does_not_repeat_stop(self):
        self.assertTrue(await self.retire())
        good = self.bridge.ledger.get('task')[1]['runtimeRetirement']
        for fact in (True, [], {}, {**good, 'schemaVersion': True},
                     {**good, 'containerId': 'other'}, {**good, 'extra': True}):
            self.bridge.ledger.patch('task', runtimeRetirement=fact)
            self.assertFalse(await self.retire())
        self.workspace.harness_runtime.stop.assert_awaited_once()

    async def test_no_stop_before_positive_exit_or_native_terminal(self):
        self.bridge.container_exit_receipt = AsyncMock(return_value=None)
        result = await self.bridge.release_resources('task', self.record)
        self.assertFalse(result['resourcesReleased'])
        self.workspace.harness_runtime.stop.assert_not_awaited()
        self.bridge.container_exit_receipt.return_value = self.receipt
        self.record['terminalConfirmed'] = False
        result = await self.bridge.release_resources('task', self.record)
        self.assertFalse(result['resourcesReleased'])
        self.workspace.harness_runtime.stop.assert_not_awaited()

    async def test_bridge_submit_waits_for_retirement_lock(self):
        self.bridge._runtime_lock = asyncio.Lock()
        self.bridge._submit = AsyncMock(return_value=({'status': 'accepted'}, True))
        async with self.bridge._runtime_lock:
            pending = asyncio.create_task(self.bridge.submit(SimpleNamespace(taskId='new'), Mock()))
            await asyncio.sleep(0)
            self.bridge._submit.assert_not_awaited()
        self.assertEqual(await pending, {'status': 'accepted'})

    async def test_duplicate_submit_observes_after_releasing_runtime_lock(self):
        self.bridge._submit = AsyncMock(return_value=({'status': 'accepted'}, False))
        async def observe(task):
            async with self.bridge._runtime_lock:
                return {'taskId': task, 'status': 'succeeded'}
        self.bridge.observe = observe
        result = await asyncio.wait_for(self.bridge.submit(SimpleNamespace(taskId='task'), Mock()), 1)
        self.assertEqual(result['status'], 'succeeded')
