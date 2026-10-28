"""Activation identity is input provenance, never permission to execute loops."""
import unittest

import httpx
from agent_line.adapters.sim import SimClient
from agent_line.adapters.sim_wait import SimWaitClient, WaitInputUnavailable


class SimWaitActivationTests(unittest.TestCase):
    def payload(self, iteration=0):
        context = 'wait_loop'+str(iteration)
        return {'data': {'workflowId': 'workflow', 'runId': 'run', 'status': 'paused',
            'paused': {'blockedOnBlockId': 'wait', 'contextId': context, 'pauseKind': 'human',
                       'pausePointCount': iteration+1, 'resumedCount': iteration},
            'blockOutputs': {'wait': {'taskInput': {'iteration': iteration, 'text': '中文'},
                'response': {'resume': {'contextId': context}}}}}}

    def reader(self, payload=None, handler=None):
        calls = []
        def request(r):
            calls.append(r)
            return handler(r) if handler else httpx.Response(200, json=payload)
        transport = httpx.Client(transport=httpx.MockTransport(request), follow_redirects=True)
        self.addCleanup(transport.close)
        return SimWaitClient(SimClient('http://sim.test', 'PRIVATE_TOKEN', client=transport)), calls

    def read(self, payload, context='wait_loop0'):
        reader, calls = self.reader(payload)
        result = reader.read_input('workflow', 'run', 'wait', context_id=context)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].method, 'GET')
        self.assertEqual(calls[0].url.path, '/api/v2/workflows/workflow/runs/run')
        self.assertEqual(dict(calls[0].url.params), {'selectedOutputs': 'wait'})
        return result

    def rejects(self, payload, context='wait_loop0'):
        reader, calls = self.reader(payload)
        with self.assertRaises(WaitInputUnavailable) as caught:
            reader.read_input('workflow', 'run', 'wait', context_id=context)
        self.assertEqual(len(calls), 1)
        self.assertNotIn('PRIVATE_TOKEN', str(caught.exception))

    def test_three_iterations_read_exact_activation_input_and_context(self):
        for iteration in range(3):
            with self.subTest(iteration=iteration):
                result = self.read(self.payload(iteration), 'wait_loop'+str(iteration))
                self.assertEqual(result['input'], {'iteration': iteration, 'text': '中文'})
                self.assertEqual(result['run']['contextId'], 'wait_loop'+str(iteration))
                self.assertEqual(result['run']['blockId'], 'wait')
                self.assertEqual(result['run']['pausePointCount'], iteration+1)
                self.assertEqual(result['run']['resumedCount'], iteration)

    def test_old_output_context_cannot_supply_new_activation_input(self):
        payload = self.payload(1)
        payload['data']['blockOutputs']['wait'] = self.payload(0)['data']['blockOutputs']['wait']
        self.rejects(payload, 'wait_loop1')

    def test_pause_advanced_after_selection_cannot_supply_new_round(self):
        self.rejects(self.payload(2), 'wait_loop1')
        payload = self.payload(1)
        payload['data']['blockOutputs']['wait']['response']['resume']['contextId'] = 'wait_loop2'
        payload['data']['blockOutputs']['wait']['taskInput'] = {'iteration': 2}
        self.rejects(payload, 'wait_loop1')

    def test_run_block_and_pause_identity_must_all_match(self):
        for field, value in [('workflowId', 'other'), ('runId', 'other'), ('status', 'completed')]:
            payload = self.payload(); payload['data'][field] = value
            with self.subTest(field=field): self.rejects(payload)
        for field, value in [('blockedOnBlockId', 'other'), ('contextId', 'other'), ('pauseKind', 'timer')]:
            payload = self.payload(); payload['data']['paused'][field] = value
            with self.subTest(field=field): self.rejects(payload)

    def test_only_one_outstanding_pause_and_strict_integer_counts(self):
        for points, resumed in [(0, 0), (2, 0), (1, 1), (1, -1), (True, 0), (1, False),
                                ('1', 0), (1, 0.0), (3, 3)]:
            payload = self.payload(); payload['data']['paused'].update(pausePointCount=points, resumedCount=resumed)
            with self.subTest(points=points, resumed=resumed): self.rejects(payload)

    def test_missing_or_wrong_response_context_rejected(self):
        for block in [{'taskInput': {}}, {'taskInput': {}, 'response': None},
                      {'taskInput': {}, 'response': {}},
                      {'taskInput': {}, 'response': {'resume': {}}},
                      {'taskInput': {}, 'response': {'resume': 'PRIVATE_TOKEN'}},
                      {'response': {'resume': {'contextId': 'wait_loop0'}}}]:
            payload = self.payload(); payload['data']['blockOutputs']['wait'] = block
            with self.subTest(block=block): self.rejects(payload)

    def test_default_reader_still_refuses_loop_context(self):
        payload = self.payload()
        payload['data']['blockOutputs'] = {'wait.taskInput': {'iteration': 0}}
        reader, calls = self.reader(payload)
        with self.assertRaises(WaitInputUnavailable): reader.read_input('workflow', 'run', 'wait')
        self.assertEqual(dict(calls[0].url.params), {'selectedOutputs': 'wait.taskInput'})

    def test_explicit_base_context_preserves_nonloop_projection(self):
        payload = self.payload(); payload['data']['paused']['contextId'] = 'wait'
        payload['data']['blockOutputs'] = {'wait.taskInput': {'iteration': 0}}
        reader, calls = self.reader(payload)
        result = reader.read_input('workflow', 'run', 'wait', context_id='wait')
        self.assertEqual(result['run']['contextId'], 'wait')
        self.assertEqual(dict(calls[0].url.params), {'selectedOutputs': 'wait.taskInput'})

    def test_malicious_identifiers_do_not_send_network_request(self):
        for args in [('workflow', '../run', 'wait', 'wait_loop0'),
                     ('workflow', 'run', 'wait', '../wait_loop0'),
                     ('workflow', 'run', 'wait', 'wait?token=PRIVATE_TOKEN'),
                     ('workflow', 'run', 'wait', ''), ('workflow', 'run', 'wait', 1),
                     ('workflow', 'run', 'wait', 'x'*161)]:
            reader, calls = self.reader(self.payload())
            with self.subTest(args=args), self.assertRaises(WaitInputUnavailable):
                reader.read_input(*args[:3], context_id=args[3])
            self.assertEqual(calls, [])

    def test_json_duplicates_nonobject_and_secret_projection_refused(self):
        bad = ['{"x":1,"x":2}', '{"x":{"y":1,"y":2}}', '{"x":NaN}',
               '[]', 'null', 'true', 'malformed PRIVATE_TOKEN', [], None, 4,
               {'key': '{{PRIVATE_TOKEN}}'}, {'nested': ['[REDACTED_SECRET]']},
               {'__simLargeValueRef': 'PRIVATE_TOKEN'}]
        for value in bad:
            payload = self.payload(); payload['data']['blockOutputs']['wait']['taskInput'] = value
            with self.subTest(value=value): self.rejects(payload)
        payload = self.payload(); payload['data']['blockOutputs']['wait']['taskInput'] = '{"nested":{"ok":true}}'
        result = self.read(payload)
        self.assertEqual(result['input'], {'nested': {'ok': True}})
        self.assertEqual(result['encoding'], 'json-object-string')

    def test_input_size_and_projection_metadata_rejected(self):
        for value in [{'text': 'x'*65537}, '{"text":"'+'x'*65537+'"}']:
            payload = self.payload(); payload['data']['blockOutputs']['wait']['taskInput'] = value
            self.rejects(payload)
        for flag in ('executionDataTruncated', 'truncated', 'redacted', 'hidden'):
            payload = self.payload(); payload['data'][flag] = True
            self.rejects(payload)
        payload = self.payload(); payload['data']['blockOutputs']['unrelated'] = {'PRIVATE_TOKEN': 1}
        self.rejects(payload)

    def test_error_envelope_http_redirect_and_transport_are_bounded(self):
        for status in (302, 401, 403, 404, 500):
            reader, calls = self.reader(handler=lambda r: httpx.Response(status,
                json={'error': 'PRIVATE_TOKEN'}, headers={'Location': 'http://evil.invalid'}))
            with self.assertRaises(WaitInputUnavailable) as caught:
                reader.read_input('workflow', 'run', 'wait', context_id='wait_loop0')
            self.assertNotIn('PRIVATE_TOKEN', str(caught.exception)); self.assertEqual(len(calls), 1)
        for envelope in [{'error': 'PRIVATE_TOKEN', **self.payload()}, {'data': []}, ['PRIVATE_TOKEN']]:
            self.rejects(envelope)
        def timeout(request): raise httpx.ReadTimeout('PRIVATE_TOKEN', request=request)
        reader, calls = self.reader(handler=timeout)
        with self.assertRaises(WaitInputUnavailable) as caught:
            reader.read_input('workflow', 'run', 'wait', context_id='wait_loop0')
        self.assertEqual(str(caught.exception), 'upstream_transport_failed')
        self.assertEqual(len(calls), 1)
