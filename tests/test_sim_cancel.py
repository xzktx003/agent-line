import unittest

import httpx

from agent_line.adapters.sim import SimClient, SubmissionUncertain, UpstreamFailure


class SimCancelTests(unittest.TestCase):
    def receipt(self, **changes):
        data = dict(success=True, runId='run-1', redisAvailable=True,
                    durablyRecorded=True, locallyAborted=False, pausedCancelled=True,
                    reason='recorded')
        data.update(changes)
        return {'data': data}

    def call(self, payload=None, status=200, error=None, headers=None):
        self.requests = []

        def handler(request):
            self.requests.append(request)
            if error:
                raise error
            return httpx.Response(status, json=payload, headers=headers)

        with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as http:
            return SimClient('http://sim.test', 'private-key', client=http).cancel('wf-1', 'run-1')

    def test_official_contract_and_sanitized_ack(self):
        payload = self.receipt(secret='must-not-return')
        result = self.call(payload)
        self.assertEqual(result, dict(workflowId='wf-1', runId='run-1',
                                     transportStatus='accepted', cancelRequested=True,
                                     verifiedStopped=False, redisAvailable=True,
                                     durablyRecorded=True, locallyAborted=False,
                                     pausedCancelled=True, reason='recorded'))
        request, = self.requests
        self.assertEqual(request.method, 'POST')
        self.assertEqual(str(request.url), 'http://sim.test/api/v2/workflows/wf-1/runs/run-1/cancel')
        self.assertEqual(request.headers['x-api-key'], 'private-key')
        self.assertEqual(request.content, b'')

    def test_terminal_noop_is_accepted_not_cancelled(self):
        result = self.call(self.receipt(success=False, reason='already_completed',
                                      durablyRecorded=False, pausedCancelled=False))
        self.assertFalse(result['cancelRequested'])
        self.assertFalse(result['verifiedStopped'])
        self.assertEqual(result['transportStatus'], 'accepted')

    def test_malformed_receipts_are_uncertain(self):
        examples = [None, {}, {'error': 'private-key'}, self.receipt(runId='other'),
                    self.receipt(workflowId='other'), self.receipt(error='private-key'),
                    self.receipt(reason=3), self.receipt(reason='private-key')]
        for key in ('success', 'redisAvailable', 'durablyRecorded', 'locallyAborted', 'pausedCancelled'):
            missing = self.receipt()
            del missing['data'][key]
            examples.extend([missing, self.receipt(**{key: 1})])
        for payload in examples:
            with self.subTest(payload=payload), self.assertRaises(SubmissionUncertain) as caught:
                self.call(payload)
            self.assertEqual(caught.exception.run_id, 'run-1')
            self.assertNotIn('private-key', str(caught.exception))
            self.assertEqual(len(self.requests), 1)

    def test_http_rejection(self):
        for status in (400, 401, 403, 404, 409, 422, 429):
            with self.subTest(status=status), self.assertRaises(UpstreamFailure):
                self.call({'error': 'private-key'}, status)
            self.assertEqual(len(self.requests), 1)

    def test_network_redirect_server_error_not_retried(self):
        for status in (302, 500, 503):
            with self.assertRaises(SubmissionUncertain):
                self.call({}, status, headers={'Location': 'http://other.test'})
            self.assertEqual(len(self.requests), 1)
        for error in (httpx.ReadTimeout('private-key'), httpx.ConnectError('private-key')):
            with self.assertRaises(SubmissionUncertain):
                self.call(error=error)
            self.assertEqual(len(self.requests), 1)

    def test_conflicting_header_and_top_level_error(self):
        with self.assertRaises(SubmissionUncertain):
            self.call(self.receipt(), headers={'X-Run-Id': 'other'})
        with self.assertRaises(SubmissionUncertain):
            self.call(dict(self.receipt(), error='private-key'))

    def test_optional_reason_missing_is_none(self):
        payload = self.receipt()
        del payload['data']['reason']
        self.assertIsNone(self.call(payload)['reason'])
