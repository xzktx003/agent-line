import copy
import unittest
from unittest.mock import Mock

from probes.qwenpaw_memory.native_roundtrip import submit_once, verify_chain


class NativeRoundtripProofTests(unittest.TestCase):
    def setUp(self):
        self.identity = {'upstreamTaskId': 'native-1'}
        def event(kind, name, call, **fields):
            return {'type': kind, 'content': [{'data': {'name': name, 'call_id': call, **fields}}]}
        self.messages = [event('plugin_call', 'memory_search', 'm1'),
            event('plugin_call_output', 'memory_search', 'm1', state='success', output='agent-line-task-A.md MARKER'),
            event('plugin_call', 'agent_line_delegate', 'd1', arguments={'instruction': 'write MARKER'}),
            event('plugin_call_output', 'agent_line_delegate', 'd1', state='success',
                  output={'taskId': 'B', 'identity': self.identity})]
        self.parent = {'status': 'finished', 'result': {'status': 'completed', 'output': self.messages}}

    def verify(self):
        return verify_chain(self.parent, 'MARKER', 'agent-line-task-A.md', 'B', self.identity)

    def test_real_linked_source_and_identity(self):
        self.assertEqual(self.verify()['memoryCallId'], 'm1')

    def test_narrative_cannot_replace_tools(self):
        self.parent['result']['output'] = [{'type': 'message', 'content': [{'text': 'memory_search MARKER success'}]}]
        with self.assertRaises(ValueError): self.verify()

    def test_wrong_source_or_call_or_missing_marker_rejected(self):
        original = copy.deepcopy(self.messages)
        for fields in ({'output': 'other.md MARKER'}, {'call_id': 'other'}, {'output': 'agent-line-task-A.md'}, {'state': 'error'}):
            self.messages[:] = copy.deepcopy(original)
            self.messages[1]['content'][0]['data'].update(fields)
            with self.assertRaises(ValueError): self.verify()

    def test_delegate_without_recalled_marker_rejected(self):
        self.messages[2]['content'][0]['data']['arguments'] = {'instruction': 'write invented'}
        with self.assertRaises(ValueError): self.verify()

    def test_wrong_delegate_identity_or_call_rejected(self):
        self.messages[3]['content'][0]['data']['output'] = {'taskId': 'B', 'identity': {'upstreamTaskId': 'other'}}
        with self.assertRaises(ValueError): self.verify()
        self.messages[3]['content'][0]['data']['call_id'] = 'other'
        with self.assertRaises(ValueError): self.verify()

    def test_reordered_tools_rejected(self):
        self.messages[1], self.messages[2] = self.messages[2], self.messages[1]
        with self.assertRaises(ValueError): self.verify()

    def test_rerun_does_not_resubmit_even_after_unknown(self):
        dispatcher = Mock(); dispatcher.submit.side_effect = TimeoutError
        state = {'A': {'taskId': 'same-task'}}; persisted = []
        def persist(): persisted.append(copy.deepcopy(state))
        with self.assertRaises(TimeoutError): submit_once(dispatcher, state, 'A', persist)
        submit_once(dispatcher, state, 'A', persist)
        dispatcher.submit.assert_called_once_with('same-task')
        self.assertTrue(persisted[0]['A']['submissionIntent'])

    def test_persistence_failure_prevents_post(self):
        dispatcher = Mock(); persist = Mock(side_effect=OSError)
        with self.assertRaises(OSError): submit_once(dispatcher, {'A': {'taskId': 'B'}}, 'A', persist)
        dispatcher.submit.assert_not_called()


class TimeoutIdentityTests(unittest.TestCase):
    def test_existing_attempt_cannot_change_timeout(self):
        from probes.qwenpaw_memory.native_roundtrip import attempt_timeout
        self.assertEqual(attempt_timeout({'parentTimeoutSeconds': 900}, 900), 900)
        with self.assertRaises(ValueError): attempt_timeout({'parentTimeoutSeconds': 900}, 300)

    def test_legacy_attempt_retains_300_and_never_silently_expands(self):
        from probes.qwenpaw_memory.native_roundtrip import attempt_timeout
        self.assertEqual(attempt_timeout({}, 300), 300)
        with self.assertRaises(ValueError): attempt_timeout({}, 900)
