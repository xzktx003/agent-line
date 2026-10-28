"""Native pause identities stay distinct without exposing execution snapshots."""
import copy
import unittest

import httpx

from agent_line.adapters.sim import SimClient
from agent_line.adapters.sim_wait import SimWaitClient, WaitInputUnavailable


class SimPausePointsTests(unittest.TestCase):
    def payload(self):
        return {'workflowId': 'wf', 'executionId': 'root', 'id': 'pause-record',
                'status': 'paused', 'totalPauseCount': 2, 'resumedCount': 0,
                'executionSnapshot': {'credential': 'must-not-project'},
                'queue': [{'private': 'must-not-project'}],
                'pausePoints': [dict(contextId=block, blockId=block, pauseKind='human',
                                     resumeStatus='paused', snapshotReady=True,
                                     response={'private': 'must-not-project'})
                                for block in ('first', 'second')]}

    def client(self, handler):
        http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
        self.addCleanup(http.close)
        return SimWaitClient(SimClient('http://sim.test', 'private-key', client=http))

    def test_both_original_contexts_are_read_and_private_fields_discarded(self):
        calls = []
        def handler(request):
            calls.append(request)
            self.assertEqual(request.method, 'GET')
            self.assertEqual(request.url.path, '/api/resume/wf/root')
            self.assertEqual(request.headers['x-api-key'], 'private-key')
            return httpx.Response(200, json=self.payload())
        result = self.client(handler).read_pauses('wf', 'root')
        self.assertEqual(len(calls), 1)
        self.assertEqual([p['contextId'] for p in result['pausePoints']], ['first', 'second'])
        self.assertEqual(set(result), {'workflowId', 'runId', 'totalPauseCount', 'resumedCount', 'pausePoints'})
        for point in result['pausePoints']:
            self.assertEqual(set(point), {'contextId', 'blockId', 'resumeStatus', 'snapshotReady', 'pauseKind'})
        self.assertNotIn('must-not-project', str(result))

    def test_partial_resume_keeps_both_identities_and_exact_status(self):
        for status in ('resumed', 'failed', 'queued', 'resuming'):
            payload = self.payload()
            payload['pausePoints'][1]['resumeStatus'] = status
            payload['pausePoints'][0]['snapshotReady'] = False
            payload['resumedCount'] = int(status == 'resumed')
            result = self.client(lambda r: httpx.Response(200, json=payload)).read_pauses('wf', 'root')
            self.assertEqual(result['pausePoints'][1]['resumeStatus'], status)
            self.assertIs(result['pausePoints'][0]['snapshotReady'], False)

    def test_wrong_identity_counts_or_duplicate_context_refuse_entire_list(self):
        changes = [lambda d: d.update(workflowId='other'), lambda d: d.update(executionId='other'),
                   lambda d: d.update(totalPauseCount=True), lambda d: d.update(totalPauseCount=3),
                   lambda d: d.update(resumedCount=True), lambda d: d.update(resumedCount=1),
                   lambda d: d.update(pausePoints=[]),
                   lambda d: d['pausePoints'].__setitem__(1, copy.deepcopy(d['pausePoints'][0]))]
        for change in changes:
            payload = self.payload(); change(payload)
            with self.subTest(payload=payload), self.assertRaises(WaitInputUnavailable):
                self.client(lambda r: httpx.Response(200, json=payload)).read_pauses('wf', 'root')

    def test_unproven_activations_and_malformed_flags_refuse(self):
        changes = [('contextId', 'first_loop0'), ('contextId', 'first₍0₎'),
                   ('contextId', '../first'), ('blockId', 'second'), ('pauseKind', 'time'),
                   ('snapshotReady', 1), ('resumeStatus', 'unknown'),
                   ('parallelScope', {}), ('loopScope', {'iteration': 0})]
        for key, value in changes:
            payload = self.payload(); payload['pausePoints'][0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(WaitInputUnavailable):
                self.client(lambda r: httpx.Response(200, json=payload)).read_pauses('wf', 'root')

    def test_bad_identifier_does_not_request(self):
        with self.assertRaises(WaitInputUnavailable):
            self.client(lambda r: self.fail('unexpected request')).read_pauses('wf', '../root')

    def test_unavailable_has_no_redirect_fallback_retry_or_body_disclosure(self):
        for status in (302, 401, 403, 404, 500):
            calls = []
            def handler(request):
                calls.append(request)
                return httpx.Response(status, json={'private': 'private-key'},
                                      headers={'location': 'http://evil.test'})
            with self.assertRaises(WaitInputUnavailable) as context:
                self.client(handler).read_pauses('wf', 'root')
            self.assertEqual(len(calls), 1)
            self.assertNotIn('private-key', str(context.exception))

    def test_transport_and_oversized_details_are_bounded(self):
        def timeout(request):
            raise httpx.ReadTimeout('private-key')
        with self.assertRaises(WaitInputUnavailable) as context:
            self.client(timeout).read_pauses('wf', 'root')
        self.assertEqual(str(context.exception), 'upstream_transport_failed')
        with self.assertRaises(WaitInputUnavailable):
            self.client(lambda r: httpx.Response(200, content=b' ' * 1048577)).read_pauses('wf', 'root')

    def test_excessive_pause_count_cannot_authorize_a_partial_subset(self):
        payload = self.payload()
        payload['pausePoints'] = [dict(contextId=f'wait{i}', blockId=f'wait{i}', pauseKind='human',
                                       resumeStatus='paused', snapshotReady=True) for i in range(201)]
        payload['totalPauseCount'] = 201
        with self.assertRaises(WaitInputUnavailable):
            self.client(lambda r: httpx.Response(200, json=payload)).read_pauses('wf', 'root')
