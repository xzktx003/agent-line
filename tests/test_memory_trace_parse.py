import unittest

from agent_line.memory_trace import parse_memory_trace


def _block(kind, name, call_id, **fields):
    return {'type': kind, 'content': [{'data': {'name': name, 'call_id': call_id, **fields}}]}


def _snapshot(**extra):
    value = {
        'status': 'finished',
        'result': {'output': [
            _block('plugin_call', 'memory_search', 'search-1', arguments={'query': 'constraint'}),
            _block('plugin_call_output', 'memory_search', 'search-1', state='success', output='found'),
            _block('plugin_call', 'agent_line_delegate', 'deleg-1', arguments={'instruction': 'read only'}),
            _block('plugin_call_output', 'agent_line_delegate', 'deleg-1', state='success', output='done'),
        ]},
    }
    value.update(extra)
    return value


class MemoryTraceParentIdentityTests(unittest.TestCase):
    def test_omitted_console_task_id_stays_observed(self):
        trace = parse_memory_trace('task-1', 'role-1', 'parent-1', _snapshot(), {}, read_at=10)
        self.assertEqual(trace['state'], 'observed')
        self.assertEqual(trace['parentTaskId'], 'parent-1')
        self.assertEqual(trace['searches'][0]['resultState'], 'success')
        self.assertEqual(trace['delegations'][0]['callId'], 'deleg-1')
        self.assertEqual(trace['delegations'][0]['resultState'], 'success')

    def test_matching_task_id_stays_observed(self):
        trace = parse_memory_trace(
            'task-1', 'role-1', 'parent-1', _snapshot(task_id='parent-1'), {}, read_at=10)
        self.assertEqual(trace['state'], 'observed')

    def test_different_task_id_stays_unknown(self):
        trace = parse_memory_trace(
            'task-1', 'role-1', 'parent-1', _snapshot(task_id='other-parent'), {}, read_at=10)
        self.assertEqual(trace['state'], 'unknown')
        self.assertIsNone(trace['searches'])
        self.assertIsNone(trace['delegations'])


if __name__ == '__main__':
    unittest.main()
