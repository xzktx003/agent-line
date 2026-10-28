"""Native soft archive never degrades into hard deletion or retry."""
import json
from pathlib import Path
import tempfile
import unittest
import httpx
from agent_line.adapters.sim import SimClient
from agent_line.sim_authoring import SimAuthoringClient, AuthoringError, AuthoringUncertain
from agent_line.sim_journal import SimJournal


class ArchiveTransportTests(unittest.TestCase):
    def client(self, status=200, payload=None, workspace='workspace', transport_error=False):
        self.calls = []
        if payload is None:
            payload = {'data': {'id': 'wf', 'deleted': True, 'archived': True}}

        def handler(request):
            self.calls.append(request)
            if request.method == 'DELETE':
                if transport_error:
                    raise httpx.ReadTimeout('private', request=request)
                return httpx.Response(status, json=payload)
            if request.url.path.endswith('/wf'):
                return httpx.Response(200, json={'data': {'id': 'wf', 'name': '自有流程',
                                                         'workspaceId': workspace}})
            return httpx.Response(status, json=payload)
        http = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(http.close)
        return SimAuthoringClient(SimClient('https://sim.test', 'private', client=http), 'workspace')

    def test_exact_soft_archive_response_and_workspace_precheck(self):
        result = self.client().archive('wf')
        self.assertTrue(result['archived'])
        self.assertEqual([(r.method, r.url.path) for r in self.calls],
                         [('GET', '/api/v2/workflows/wf'), ('DELETE', '/api/v2/workflows/wf')])
        self.assertEqual(self.calls[-1].content, b'')
        self.assertNotIn('permanent', str(self.calls[-1].url))
        with self.assertRaises(AuthoringError):
            self.client(workspace='other').archive('wf')
        self.assertEqual([r.method for r in self.calls], ['GET'])

    def test_no_hard_delete_or_wrong_identity_response_accepted(self):
        for patch in ({'id': 'other'}, {'archived': False}, {'deleted': 1}):
            with self.subTest(patch=patch):
                payload = {'data': {'id': 'wf', 'deleted': True, 'archived': True} | patch}
                with self.assertRaises(AuthoringUncertain):
                    self.client(payload=payload).archive('wf')
                self.assertEqual(sum(r.method == 'DELETE' for r in self.calls), 1)

    def test_transient_failure_and_redirect_never_repeat_delete(self):
        for status in (302, 500, 503):
            with self.subTest(status=status), self.assertRaises(AuthoringUncertain):
                self.client(status=status).archive('wf')
            self.assertEqual(sum(r.method == 'DELETE' for r in self.calls), 1)
        with self.assertRaises(AuthoringUncertain):
            self.client(transport_error=True).archive('wf')
        self.assertEqual(sum(r.method == 'DELETE' for r in self.calls), 1)

    def test_archived_listing_is_scoped_and_cursor_bounded(self):
        api = self.client(payload={'data': [{'id': 'wf', 'name': '归档', 'workspaceId': 'workspace'}],
                                   'nextCursor': 'native-cursor'})
        self.assertEqual(api.list_archived('previous', 1)['nextCursor'], 'native-cursor')
        request = self.calls[-1]
        self.assertEqual(request.url.params['workspaceId'], 'workspace')
        self.assertEqual(request.url.params['scope'], 'archived')
        self.assertEqual(request.url.params['cursor'], 'previous')
        self.assertEqual(request.method, 'GET')
        with self.assertRaises(AuthoringError):
            api.list_archived(limit=101)

    def test_uncertain_archive_survives_reopen_without_second_remote_call(self):
        api = self.client(transport_error=True)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'sim.sqlite'
            journal = SimJournal.initialize(path)
            payload = {'workflowId': 'wf', 'projectId': 'one'}
            first = journal.mutate('project-one', 'archive-one', 'archive', payload, lambda: api.archive('wf'))
            self.assertEqual(first['status'], 'uncertain')
            reopened = SimJournal(path)
            again = reopened.mutate('project-one', 'archive-one', 'archive', payload,
                                     lambda: self.fail('must not repeat archive'))
            self.assertEqual(first, again)
            self.assertEqual(sum(r.method == 'DELETE' for r in self.calls), 1)
            with reopened.connect() as db:
                row = db.execute('SELECT input FROM operations').fetchone()
            self.assertEqual(json.loads(row['input']), payload)
