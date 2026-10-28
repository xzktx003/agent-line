"""Loaded native runtime retirement after a proven nonterminal executor exit."""
import copy
import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException

from tests import test_bridge_runtime_retirement as fixtures


class InterruptedRuntimeRetirementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.RuntimeRetirementTests.setUp(self)
        self.record['terminalConfirmed'] = False
        self.record['nativeStatus'] = 'unknown'
        self.bridge.ledger.patch('task', terminalConfirmed=False, nativeStatus='unknown')
        self.receipt = copy.deepcopy(self.receipt)
        self.native.side_effect = HTTPException(404)

    async def retire(self):
        return await self.bridge.retire_runtime('task', self.record, self.receipt, interrupted=True)

    async def test_loaded_target_and_lost_handle_retire_after_verified_interruption(self):
        result, control = await self.fixture.observe()
        self.assertTrue(result['executorInterrupted'])
        self.assertTrue(result['resourcesReleased'])
        self.assertFalse(result['terminalConfirmed'])
        self.assertEqual(result['nativeStatus'], 'unknown')
        self.assertEqual(result['identity'], self.fixture.identity)
        self.assertEqual(result['runtimeRetirement']['state'], 'stopped')
        self.assertEqual(result['runtimeRetirement']['identity'], self.fixture.identity)
        self.assertEqual(control.call_args.args[2], 'observe')
        self.workspace.harness_runtime.stop.assert_awaited_once_with()
        repeated, _ = await self.fixture.observe()
        self.assertEqual(repeated, result)
        self.workspace.harness_runtime.stop.assert_awaited_once()

    async def test_interruption_mode_requires_complete_original_identity_and_unknown_terminal(self):
        original = copy.deepcopy(self.record)
        for changes in ({'terminalConfirmed': True}, {'terminalConfirmed': None},
                        {'nativeStatus': 'task_complete'}, {'nativeStatus': None},
                        {'identity': {'upstreamTaskId': 'native'}},
                        {'identity': {**self.fixture.identity, 'codexTurnId': 'other'}}):
            with self.subTest(changes=changes):
                self.record = {**original, **changes}
                self.assertFalse(await self.retire())
        self.workspace.harness_runtime.stop.assert_not_awaited()

    async def test_only_one_exact_false_hint_permits_interrupted_retirement(self):
        identity = self.fixture.identity
        variants = [[], [None], [{'codexThreadId': 'thread', 'codexTurnId': 'turn'}]]
        for value in (True, None, 0, 'false'):
            variants.append([{'codexThreadId': identity['codexThreadId'],
                              'codexTurnId': identity['codexTurnId'], 'nativeTerminal': value}])
        variants += [self.receipt['identities'] * 2,
                     [{'codexThreadId': 'other', 'codexTurnId': 'turn', 'nativeTerminal': False}]]
        for hints in variants:
            with self.subTest(hints=hints):
                self.receipt['identities'] = hints
                self.assertFalse(await self.retire())
        self.workspace.harness_runtime.stop.assert_not_awaited()

    async def test_changed_saved_identity_or_terminal_fact_cannot_use_stale_record(self):
        for changes in ({'identity': {**self.fixture.identity, 'upstreamTaskId': 'another-native'}},
                        {'terminalConfirmed': True}, {'nativeStatus': 'task_complete'}):
            with self.subTest(changes=changes):
                self.bridge.ledger.patch('task', **changes)
                self.assertFalse(await self.retire())
                self.bridge.ledger.patch('task', identity=self.fixture.identity,
                                         terminalConfirmed=False, nativeStatus='unknown')
        self.workspace.harness_runtime.stop.assert_not_awaited()

    async def test_interruption_mode_requires_owned_positive_exit_receipt(self):
        original = copy.deepcopy(self.receipt)
        for field, value in (('schemaVersion', True), ('owner', 'invalid'), ('containerId', 'invalid'),
                             ('workspace', '/another'), ('source', 'missing'), ('running', True),
                             ('pid', 1), ('pid', False), ('status', 'running'), ('finishedAt', '')):
            with self.subTest(field=field):
                self.receipt = {**original, field: value}
                self.assertFalse(await self.retire())
        self.workspace.harness_runtime.stop.assert_not_awaited()

    async def test_active_target_and_other_unreleased_receipt_remain_blocked(self):
        self.workspace.task_tracker.has_active_tasks.return_value = True
        self.assertFalse(await self.retire())
        self.native.assert_not_awaited()
        self.workspace.task_tracker.has_active_tasks.return_value = False
        self.bridge.ledger.claim('other', {'targetAgentId': 'fixed', 'workspace': str(self.bridge.workspace)})
        self.assertFalse(await self.retire())
        self.workspace.harness_runtime.stop.assert_not_awaited()
        self.bridge.ledger.patch('other', resourcesReleased=True)
        self.assertTrue(await self.retire())

    async def test_only_missing_handle_can_use_restart_exception(self):
        self.native.side_effect = HTTPException(503)
        self.assertFalse(await self.retire())
        self.native.side_effect = None
        for status in ('running', 'pending', None):
            self.native.return_value = {'status': status}
            self.assertFalse(await self.retire())
        self.workspace.harness_runtime.stop.assert_not_awaited()

    async def test_stop_or_retirement_persistence_failure_stays_unconfirmed(self):
        self.workspace.harness_runtime.stop.side_effect = RuntimeError('stop unavailable')
        self.assertFalse(await self.retire())
        self.assertNotIn('runtimeRetirement', self.bridge.ledger.get('task')[1])
        self.workspace.harness_runtime.stop.side_effect = None
        with patch.object(self.bridge.ledger, 'patch', side_effect=OSError('ledger unavailable')):
            self.assertFalse(await self.retire())
        self.assertNotIn('runtimeRetirement', self.bridge.ledger.get('task')[1])

    async def test_runtime_replacement_or_new_active_task_after_stop_stays_unconfirmed(self):
        async def replace():
            self.manager.get_loaded_agent.return_value = Mock()
        self.workspace.harness_runtime.stop.side_effect = replace
        self.assertFalse(await self.retire())
        self.manager.get_loaded_agent.return_value = self.workspace
        async def active():
            self.workspace.task_tracker.has_active_tasks.return_value = True
        self.workspace.harness_runtime.stop.side_effect = active
        self.assertFalse(await self.retire())
        self.assertNotIn('runtimeRetirement', self.bridge.ledger.get('task')[1])

    async def test_unloaded_runtime_still_requires_valid_interruption_hint(self):
        self.manager.get_loaded_agent.return_value = None
        self.receipt['identities'][0].pop('nativeTerminal')
        self.assertFalse(await self.retire())
        self.assertNotIn('runtimeRetirement', self.bridge.ledger.get('task')[1])
        self.workspace.harness_runtime.stop.assert_not_awaited()

    async def test_legacy_missing_handle_does_not_gain_interruption_permission(self):
        self.assertFalse(await self.bridge.retire_runtime('task', self.record, self.receipt))
        self.workspace.harness_runtime.stop.assert_not_awaited()
        self.record['terminalConfirmed'] = True
        self.assertTrue(await self.bridge.retire_runtime('task', self.record, self.receipt))


if __name__ == '__main__':
    unittest.main()
