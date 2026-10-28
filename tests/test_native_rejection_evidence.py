"""Native reject evidence is tool output plus exact original approval bindings."""
import copy
import json
import shlex
import unittest

from probes.qwenpaw.permission_evidence import rejected_native_evidence


class NativeRejectionEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.nonce = 'a1' * 16
        self.identity = {'upstreamTaskId': 'native-task',
                         'codexThreadId': 'thread', 'codexTurnId': 'turn'}
        command = 'python3 -c "print(\'' + self.nonce + '\')" '
        script = ('text(await tools.exec_command({cmd:' + json.dumps(command)
                  + ',sandbox_permissions:"require_escalated",justification:"Approve this probe?",'
                    'max_output_tokens:1000}));\n')
        self.rows = [
            {'type': 'session_meta', 'payload': {'id': 'thread', 'cwd': '/isolated/work'}},
            {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'turn'}},
            {'type': 'turn_context', 'payload': {'turn_id': 'turn', 'cwd': '/isolated/work',
                'root_turn_id': 'turn', 'approval_policy': 'on-request',
                'sandbox_policy': {'type': 'workspace-write'}}},
            {'type': 'response_item', 'payload': {
                'type': 'custom_tool_call', 'id': 'ctc-1', 'status': 'completed',
                'call_id': 'call-1', 'name': 'exec', 'input': script,
                'internal_chat_message_metadata_passthrough': {'turn_id': 'turn'}}},
            {'type': 'response_item', 'payload': {
                'type': 'custom_tool_call_output', 'id': 'ctco-1', 'call_id': 'call-1',
                'output': [
                    {'type': 'input_text', 'text': 'Script failed\nWall time 1.6 seconds\nOutput:\n'},
                    {'type': 'input_text', 'text': 'Script error:\nexec_command failed: CreateProcess { message: "Rejected(\\"rejected by user\\")" }'}],
                'internal_chat_message_metadata_passthrough': {'turn_id': 'turn'}}},
            {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 'turn',
                                            'root_turn_id': 'turn'}}]
        self.approval = {
            'purpose': 'execution_permission', 'identity': dict(self.identity),
            'taskId': 'task', 'roleSessionId': 'agent-line-task',
            'targetAgentId': 'codex', 'requestId': 'approval-uuid',
            'providerItemId': 'exec-item-uuid', 'method': 'item/commandExecution/requestApproval',
            'bound': True, 'status': 'waiting', 'decision': None,
            'deliveryStatus': 'not_submitted',
            'summary': '/usr/bin/bash -lc ' + shlex.quote(command)}
        self.decision = {**copy.deepcopy(self.approval), 'status': 'decision_recorded',
            'deliveryStatus': 'accepted', 'decision': {'decisionId': 'reject-once',
                                                    'decision': 'reject', 'decidedBy': 'human'}}
        self.launcher = {
            'owner': 'b' * 32, 'containerId': 'c' * 64, 'workspace': '/isolated/work',
            'threads': ['thread'], 'identities': [
                {'codexThreadId': 'thread', 'codexTurnId': 'turn', 'nativeTerminal': True}],
            'approvalBindings': [{'requestId': 0, 'method': 'item/commandExecution/requestApproval',
                                  'threadId': 'thread', 'turnId': 'turn',
                                  'itemId': 'exec-item-uuid', 'resolved': True}]}

    def evidence(self):
        return rejected_native_evidence(self.rows, self.identity, self.approval,
                                        self.decision, self.launcher, self.nonce)

    def test_exact_native_failure_without_command_item_uses_explicit_unique_correlation(self):
        before = copy.deepcopy((self.rows, self.identity, self.approval, self.decision, self.launcher))
        result = self.evidence()
        self.assertEqual(result['status'], 'verified_native_rejection')
        self.assertEqual(result['launcherRequestId'], 0)
        self.assertEqual(result['requestId'], 'approval-uuid')
        self.assertEqual(result['providerItemId'], 'exec-item-uuid')
        self.assertEqual(result['callId'], 'call-1')
        self.assertEqual(result['correlation'], 'unique_single_tool_and_approval_same_turn')
        self.assertIs(result['directItemToCallIdMapping'], False)
        self.assertEqual(result['commandExecution'], 'rejected_before_create_process')
        self.assertEqual(before, (self.rows, self.identity, self.approval, self.decision, self.launcher))

    def test_duplicate_call_output_approval_or_turn_is_ambiguous(self):
        original = copy.deepcopy(self.rows)
        for index in (0, 1, 2, 3, 4, 5):
            with self.subTest(index=index):
                self.rows = copy.deepcopy(original)
                self.rows.insert(index, copy.deepcopy(self.rows[index]))
                with self.assertRaises(ValueError):
                    self.evidence()
        self.rows = original
        self.launcher['approvalBindings'] *= 2
        with self.assertRaises(ValueError):
            self.evidence()

    def test_wrong_thread_turn_workspace_and_call_ids_are_rejected(self):
        changes = [(0, 'id', 'other-thread'), (0, 'cwd', '/other'),
                   (1, 'turn_id', 'other-turn'), (2, 'turn_id', 'other-turn'),
                   (2, 'root_turn_id', 'other-turn'), (4, 'call_id', 'other-call'),
                   (5, 'turn_id', 'other-turn'), (5, 'root_turn_id', 'other-turn')]
        for index, key, value in changes:
            with self.subTest(index=index, key=key):
                old = self.rows[index]['payload'].get(key)
                self.rows[index]['payload'][key] = value
                with self.assertRaises(ValueError):
                    self.evidence()
                self.rows[index]['payload'][key] = old
        for index in (3, 4):
            self.rows[index]['payload']['internal_chat_message_metadata_passthrough']['turn_id'] = 'other'
            with self.assertRaises(ValueError):
                self.evidence()
            self.rows[index]['payload']['internal_chat_message_metadata_passthrough']['turn_id'] = 'turn'

    def test_replay_of_another_turn_or_out_of_order_output_is_rejected(self):
        self.rows.insert(3, {'type': 'event_msg', 'payload': {
            'type': 'task_started', 'turn_id': 'replayed'}})
        with self.assertRaises(ValueError):
            self.evidence()
        self.rows.pop(3)
        self.rows[3], self.rows[4] = self.rows[4], self.rows[3]
        with self.assertRaises(ValueError):
            self.evidence()

    def test_extra_executions_or_script_statements_are_not_a_single_probe(self):
        original = self.rows[3]['payload']['input']
        for script in (original + original, '/*fake*/' + original,
                       original.replace('text(await', 'text("fake");text(await'),
                       original.replace('require_escalated', 'use_default'),
                       original.replace('tools.exec_command', 'tools.other'),
                       original.replace('python3 -c', 'echo ignored; python3 -c')):
            with self.subTest(script_kind=script[:25]):
                self.rows[3]['payload']['input'] = script
                with self.assertRaises(ValueError):
                    self.evidence()
        self.rows[3]['payload']['input'] = original

    def test_wrong_nonce_or_approval_command_cannot_match(self):
        self.nonce = 'd1' * 16
        with self.assertRaises(ValueError):
            self.evidence()
        self.nonce = 'a1' * 16
        for summary in (None, 'echo ' + self.nonce,
                        self.approval['summary'] + ' unexpected-argument'):
            self.approval['summary'] = summary
            self.decision['summary'] = summary
            with self.assertRaises(ValueError):
                self.evidence()

    def test_model_rejection_text_is_not_native_tool_output(self):
        text = json.dumps(self.rows[4]['payload']['output'])
        self.rows[4]['payload'] = {'type': 'message', 'role': 'assistant',
                                  'content': [{'type': 'output_text', 'text': text}]}
        self.rows[5]['payload']['last_agent_message'] = 'The approval was rejected.'
        with self.assertRaises(ValueError):
            self.evidence()

    def test_success_stdout_nonce_or_unrelated_failure_is_not_native_rejection(self):
        chunks = self.rows[4]['payload']['output']
        for first, second in (
            ('Script succeeded\nWall time 1 seconds\nOutput:\n', chunks[1]['text']),
            (chunks[0]['text'] + self.nonce + '\n', chunks[1]['text']),
            (chunks[0]['text'], 'Script error:\nexec_command failed: network timeout'),
            (chunks[0]['text'], chunks[1]['text'] + '\n' + self.nonce)):
            old = copy.deepcopy(chunks)
            chunks[0]['text'], chunks[1]['text'] = first, second
            with self.assertRaises(ValueError):
                self.evidence()
            chunks[:] = old

    def test_native_command_item_or_extra_tool_event_cannot_hide_success(self):
        for row in (
            {'type': 'event_msg', 'payload': {'type': 'item_completed', 'turn_id': 'turn',
                'item': {'type': 'CommandExecution', 'status': 'completed', 'exit_code': 0,
                         'stdout': self.nonce}}},
            {'type': 'event_msg', 'payload': {'type': 'exec_command_end', 'turn_id': 'turn',
                                            'exit_code': 0, 'stdout': self.nonce}},
            {'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'exec_command'}}):
            self.rows.insert(5, row)
            with self.assertRaises(ValueError):
                self.evidence()
            self.rows.pop(5)

    def test_mismatched_unaccepted_or_approved_human_decision_is_rejected(self):
        mutations = [('requestId', 'different'), ('providerItemId', 'other-item'),
                     ('identity', {**self.identity, 'codexTurnId': 'other'}),
                     ('deliveryStatus', 'unknown'), ('bound', False),
                     ('decision', {'decisionId': 'approve', 'decision': 'approve', 'decidedBy': 'human'})]
        for key, value in mutations:
            old = self.decision[key]
            self.decision[key] = value
            with self.assertRaises(ValueError):
                self.evidence()
            self.decision[key] = old

    def test_unresolved_wrong_item_and_boolean_rpc_or_terminal_are_rejected(self):
        binding = self.launcher['approvalBindings'][0]
        for key, value in (('resolved', False), ('itemId', 'other-item'),
                           ('turnId', 'other-turn'), ('requestId', False)):
            old = binding[key]
            binding[key] = value
            with self.assertRaises(ValueError):
                self.evidence()
            binding[key] = old
        self.launcher['identities'][0]['nativeTerminal'] = 1
        with self.assertRaises(ValueError):
            self.evidence()


if __name__ == '__main__':
    unittest.main()
