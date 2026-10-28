"""Native same-thread retry stays on the first turn and does not open another execution."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    'native_retry_bridge', Path(__file__).resolve().parents[1] / 'plugins/agent-line/execution_bridge.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def usage(turn):
    counters = {
        'input_tokens': 3, 'cached_input_tokens': 1, 'cache_write_input_tokens': 0,
        'output_tokens': 2, 'reasoning_output_tokens': 0, 'total_tokens': 5,
    }
    return {'type': 'token_usage_record', 'payload': {
        'thread_id': 'thread', 'turn_id': turn, 'response_id': 'resp-' + turn,
        'session_id': 'session-1', 'root_turn_id': turn,
        'usage': dict(counters), 'turn_token_usage': dict(counters)}}


class NativeRetryObservationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.bridge = module.ExecutionBridge.__new__(module.ExecutionBridge)
        bridge = self.bridge
        bridge.workspace = root / 'work'
        bridge.workspace.mkdir()
        bridge.codex_home = root / 'home'
        bridge.codex_home.mkdir()
        bridge.target = 'fixed'
        database = root / 'receipts.sqlite'
        module.ReceiptLedger.initialize(database)
        bridge.ledger = module.ReceiptLedger(database)
        bridge.ledger.claim('task', {
            'targetAgentId': 'fixed', 'workspace': str(bridge.workspace),
            'roleSessionId': 'agent-line-task'})
        bridge.ledger.patch('task', status='accepted', identity={'codexThreadId': 'thread'})
        (bridge.workspace / 'harnesses').mkdir()
        (bridge.workspace / 'harnesses/codex_sessions.json').write_text(
            json.dumps({'agent-line-task': 'thread'}))
        (bridge.codex_home / 'sessions').mkdir()
        self.rollout = bridge.codex_home / 'sessions/rollout-thread.jsonl'
        self.entries = [
            {'type': 'session_meta', 'payload': {'id': 'thread', 'cwd': str(bridge.workspace)}},
        ]

    def write(self, entries):
        self.rollout.write_text(''.join(json.dumps(entry) + '\n' for entry in entries))

    def started(self, turn):
        return {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': turn}}

    def completed(self, turn, text):
        return {'type': 'event_msg', 'payload': {
            'type': 'task_complete', 'turn_id': turn, 'last_agent_message': text}}

    def tool(self, turn, item_id):
        return {'type': 'event_msg', 'payload': {
            'type': 'item_completed', 'turn_id': turn,
            'item': {'id': item_id, 'type': 'commandExecution', 'status': 'completed', 'exit_code': 0}}}

    def receipt_count(self):
        with self.bridge.ledger.connection() as db:
            return db.execute('SELECT COUNT(*), GROUP_CONCAT(task_id) FROM receipts').fetchone()

    async def test_second_turn_keeps_first_identity_and_output(self):
        self.write(self.entries + [
            self.started('turn-1'), self.tool('turn-1', 'tool-1'), usage('turn-1'),
            self.completed('turn-1', 'original'),
            self.started('turn-2'), self.tool('turn-2', 'tool-2'),
            self.completed('turn-2', 'replacement'),
        ])
        result = await self.bridge.observe('task')
        self.assertEqual(result['identity']['codexThreadId'], 'thread')
        self.assertEqual(result['identity']['codexTurnId'], 'turn-1')
        self.assertEqual(result['status'], 'succeeded')
        self.assertTrue(result['terminalConfirmed'])
        self.assertEqual(result['output']['text'], 'original')
        self.assertEqual(result['output']['toolEvidence'], [
            {'id': 'tool-1', 'type': 'commandExecution', 'status': 'completed', 'exit_code': 0}])
        self.assertIsNone(result['codexUsage'])
        self.assertTrue(result['codexUsageFrozen'])
        self.assertEqual(self.receipt_count(), (1, 'task'))
        again = await self.bridge.observe('task')
        self.assertEqual(again['output']['text'], 'original')
        self.assertEqual(again['identity']['codexTurnId'], 'turn-1')
        self.assertEqual(self.receipt_count(), (1, 'task'))

    async def test_later_completion_does_not_finish_an_open_first_turn(self):
        self.write(self.entries + [
            self.started('turn-1'), self.started('turn-2'),
            self.completed('turn-2', 'replacement'),
        ])
        result = await self.bridge.observe('task')
        self.assertEqual(result['identity']['codexTurnId'], 'turn-1')
        self.assertEqual(result['status'], 'accepted')
        self.assertFalse(result['terminalConfirmed'])
        self.assertNotEqual(result['output']['text'], 'replacement')
        self.assertEqual(self.receipt_count(), (1, 'task'))

    async def test_missing_first_turn_bound_conflict_and_multiturn_usage_fail_closed(self):
        single = self.entries + [self.started('turn-1'), usage('turn-1'), self.completed('turn-1', 'original')]
        identity = {'codexThreadId': 'thread', 'codexTurnId': 'turn-1'}
        self.assertEqual(module.codex_usage_snapshot(single, identity, single[0]['payload'])['samplesObserved'], 1)
        self.assertIsNone(module.codex_usage_snapshot(
            single + [self.started('turn-2')], identity, single[0]['payload']))

        self.write(self.entries + [self.completed('later', 'no-start')])
        missing = await self.bridge.observe('task')
        self.assertEqual(missing['status'], 'unknown')
        self.assertNotIn('codexTurnId', missing['identity'])
        self.assertFalse(missing['terminalConfirmed'])

        self.bridge.ledger.patch('task', status='accepted', identity={'codexThreadId': 'thread'})
        self.write(self.entries + [
            {'type': 'event_msg', 'payload': {'type': 'task_started'}},
            self.started('later'), self.completed('later', 'not-first'),
        ])
        blank = await self.bridge.observe('task')
        self.assertEqual(blank['status'], 'unknown')
        self.assertNotIn('codexTurnId', blank['identity'])

        self.bridge.ledger.patch('task', status='accepted', identity={
            'codexThreadId': 'thread', 'codexTurnId': 'bound-turn'})
        self.write(self.entries + [self.started('turn-1'), self.completed('turn-1', 'original')])
        conflict = await self.bridge.observe('task')
        self.assertEqual(conflict['status'], 'unknown')
        self.assertEqual(conflict['identity'], {'codexThreadId': 'thread', 'codexTurnId': 'bound-turn'})
        self.assertFalse(conflict['terminalConfirmed'])
        self.assertIsNone(conflict['output'])
        self.assertEqual(self.receipt_count(), (1, 'task'))


if __name__ == '__main__':
    unittest.main()
