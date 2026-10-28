"""Archive audits persist before DELETE; unknown outcomes cannot replay."""
import json
import unittest
from unittest.mock import Mock
from tests import test_authoring_api as fixture_module
from agent_line.authoring_api import state_hash, namespace
from agent_line.sim_authoring import AuthoringError
from agent_line.workflow_archive import record
from agent_line.workflows import WorkflowGateway, StartRequest
from fastapi import HTTPException


class ArchiveAPITests(unittest.TestCase):
    def setUp(self):
        self.fx = fixture_module.AuthoringAPITests(methodName='runTest')
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.wid = self.fx.owned(fixture_module.document())
        self.body = {'operationId': 'archive-once', 'expectedStateSha256': state_hash(self.fx.remote.states[self.wid])}
        self.fx.remote.versions = Mock(return_value={'data': [], 'nextCursor': None})
        self.fx.remote.runs = Mock(return_value={'data': [], 'nextCursor': None})
        self.fx.remote.get_run_audit = Mock()
        self.archives = []

        def archive(wid):
            # The snapshot must be durably committed before the remote mutation.
            found = record(self.fx.journal, 'one', wid)
            self.assertEqual(found['status'], 'intent')
            self.assertEqual(found['payload']['snapshot']['draft'], self.fx.remote.states[wid])
            self.archives.append(wid)
            self.fx.remote.read_error = AuthoringError('upstream_rejected')
            return {'id': wid, 'deleted': True, 'archived': True}
        self.fx.remote.archive = Mock(side_effect=archive)

    def archive(self, **changes):
        return self.fx.request('POST', f'/projects/one/workflows/{self.wid}/archive',
                               body=self.body | changes)

    def audit(self, project='one', token='edit'):
        return self.fx.request('GET', f'/projects/{project}/archives/archive-once', token=token)

    def test_archive_snapshot_precedes_delete_and_remains_readable_offline(self):
        response = self.archive()
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()['receipt']['outcome'], 'archived')
        audit = self.audit()
        self.assertEqual(audit.status_code, 200, audit.text)
        self.assertTrue(audit.json()['readOnly'])
        self.assertEqual(audit.json()['snapshot']['draft'], self.fx.remote.states[self.wid])
        self.assertEqual(audit.json()['historyScope'], 'api_visible_at_capture')
        self.assertEqual(self.fx.request('GET', '/projects/one/workflows').json()['workflows'], [])
        directory = self.fx.request('GET', '/projects/one/archives').json()
        self.assertEqual(len(directory['archives']), 1)
        calls = list(self.fx.remote.calls)
        self.assertEqual(self.archive().json(), response.json())
        self.assertEqual(self.fx.remote.calls, calls)
        self.assertEqual(self.archives, [self.wid])

    def test_unknown_cannot_delete_again_edit_publish_or_start(self):
        self.fx.remote.archive.side_effect = TimeoutError('private remote secret')
        response = self.archive()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['status'], 'uncertain')
        self.assertNotIn('private', response.text)
        self.assertEqual(self.archive().json(), response.json())
        self.assertEqual(self.archive(operationId='new-archive').status_code, 422)
        self.fx.remote.archive.assert_called_once()
        save = self.fx.save(self.wid)
        self.assertEqual(save.json()['receipt']['outcome'], 'not_submitted')
        publish = self.fx.publish(self.wid)
        self.assertEqual(publish.json()['receipt']['outcome'], 'not_submitted')
        native = Mock()
        gateway = WorkflowGateway(native, self.fx.journal,
            {'binding': {'projectId': 'one', 'workflowId': self.wid}}, installation_id='installation')
        with self.assertRaises(HTTPException) as caught:
            gateway.start('binding', StartRequest(idempotencyKey='new', input={}), {'one'})
        self.assertEqual(caught.exception.status_code, 409)
        native.execute.assert_not_called()
        self.assertEqual(self.audit().status_code, 200)
        original = self.fx.request('GET', '/projects/one/operations/archive-once?kind=archive')
        self.assertEqual(original.json()['status'], 'uncertain')

    def test_scope_ownership_and_changed_request_rejected(self):
        self.assertEqual(self.fx.request('POST', f'/projects/two/workflows/{self.wid}/archive',
                                        token='other', body=self.body).status_code, 404)
        self.assertEqual(self.fx.request('POST', '/projects/one/workflows/unowned/archive', body=self.body).status_code, 404)
        self.assertEqual(self.archive(expectedStateSha256='0'*64).status_code, 422)
        self.fx.remote.archive.assert_not_called()
        self.assertEqual(self.archive().status_code, 202)
        self.assertEqual(self.archive(expectedStateSha256='0'*64).status_code, 409)
        self.assertEqual(self.audit('two', 'other').status_code, 404)
        self.assertEqual(self.audit('one', 'other').status_code, 404)
        self.assertEqual(self.fx.request('GET', '/projects/one/archives', token=None).status_code, 401)

    def test_terminal_runs_and_all_version_definitions_are_retained(self):
        self.fx.remote.versions.return_value = {'data': [{'id': 'deployment-1', 'version': 1, 'isActive': True}], 'nextCursor': None}
        self.fx.remote.runs.return_value = {'data': [{'runId': 'run-one', 'status': 'completed'}], 'nextCursor': None}
        self.fx.remote.get_run_audit.return_value = {'workflowId': self.wid, 'runId': 'run-one',
                                                    'status': 'completed', 'output': {'evidence': 'kept'}}
        response = self.archive()
        self.assertEqual(response.status_code, 202, response.text)
        snapshot = self.audit().json()['snapshot']
        self.assertEqual(snapshot['versions'][0]['state'], self.fx.remote.states[self.wid])
        self.assertEqual(snapshot['runs'][0]['output'], {'evidence': 'kept'})

    def test_nonterminal_or_ambiguous_runs_and_bad_pagination_never_archive(self):
        for status in ('pending', 'queued', 'running', 'paused', 'redacting', 'unknown'):
            with self.subTest(status=status):
                self.fx.remote.runs.return_value = {'data': [{'runId': 'active', 'status': status}], 'nextCursor': None}
                self.assertEqual(self.archive().status_code, 422)
        self.fx.remote.runs.return_value = {'data': [], 'nextCursor': 'same'}
        self.assertEqual(self.archive().status_code, 422)
        self.fx.remote.archive.assert_not_called()
        self.assertIsNone(record(self.fx.journal, 'one', self.wid))

    def test_local_unknown_submission_blocks_archive_and_audit_tampering_rejects(self):
        with self.fx.journal.connect() as db:
            db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?)',
                       (self.wid, 'lost-run', '', '{}', 'uncertain', None, 1))
        self.assertEqual(self.archive().status_code, 422)
        self.fx.remote.archive.assert_not_called()
        with self.fx.journal.connect() as db:
            db.execute("DELETE FROM operations WHERE workflow=? AND context=''", (self.wid,))
        self.assertEqual(self.archive().status_code, 202)
        with self.fx.journal.connect() as db:
            db.execute("UPDATE operations SET input=json_set(input,'$.snapshot.detail.name','tampered') "
                       "WHERE workflow=? AND context='author-archive'", (namespace('one'),))
        self.assertEqual(self.audit().status_code, 422)

    def test_capacity_limits_and_failed_reads_do_not_create_delete_intent(self):
        from unittest.mock import patch
        with patch('agent_line.workflow_archive.MAX_BYTES', 100):
            self.assertEqual(self.archive().status_code, 422)
        self.fx.remote.versions.return_value = {'data': [{'version': 1}]*1001, 'nextCursor': None}
        self.assertEqual(self.archive().status_code, 422)
        self.fx.remote.versions.side_effect = AuthoringError('upstream_transport_failed')
        self.assertEqual(self.archive().status_code, 422)
        self.fx.remote.archive.assert_not_called()
        self.assertIsNone(record(self.fx.journal, 'one', self.wid))

    def test_reconcile_only_reads_native_archive_scope_and_never_repeats_delete(self):
        self.fx.remote.archive.side_effect = TimeoutError()
        self.assertEqual(self.archive().json()['status'], 'uncertain')
        self.fx.remote.list_archived = Mock(return_value={'data': [], 'nextCursor': None})
        path = '/projects/one/archives/archive-once/reconcile'
        self.assertEqual(self.fx.request('POST', path).json()['status'], 'uncertain')
        self.fx.remote.list_archived.return_value = {'data': [{'id': self.wid}], 'nextCursor': None}
        response = self.fx.request('POST', path)
        self.assertEqual(response.json()['status'], 'accepted')
        self.assertEqual(response.json()['receipt']['reconciledFrom'], 'native_archived_scope')
        self.fx.remote.archive.assert_called_once()
        reads = self.fx.remote.list_archived.call_count
        self.assertEqual(self.fx.request('POST', path).json(), response.json())
        self.assertEqual(self.fx.remote.list_archived.call_count, reads)
        self.assertEqual(self.audit().status_code, 200)

    def test_archive_serializes_new_static_start_without_touching_existing_history(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        entered, release, start_reached = threading.Event(), threading.Event(), threading.Event()
        original_archive = self.fx.remote.archive.side_effect
        def delayed_archive(wid):
            entered.set()
            if not release.wait(5):
                raise AssertionError('test release timed out')
            return original_archive(wid)
        self.fx.remote.archive.side_effect = delayed_archive
        native = Mock()
        gateway = WorkflowGateway(native, self.fx.journal,
            {'binding': {'projectId': 'one', 'workflowId': self.wid}}, installation_id='installation')
        lookup = gateway.binding_for
        def signaled_lookup(*args):
            binding = lookup(*args)
            start_reached.set()
            return binding
        gateway.binding_for = signaled_lookup
        with ThreadPoolExecutor(max_workers=2) as pool:
            archive_future = pool.submit(self.archive)
            self.assertTrue(entered.wait(5))
            start_future = pool.submit(gateway.start, 'binding', StartRequest(idempotencyKey='concurrent', input={}), {'one'})
            try:
                self.assertTrue(start_reached.wait(5))
                native.execute.assert_not_called()
            finally:
                release.set()
            self.assertEqual(archive_future.result(5).status_code, 202)
            with self.assertRaises(HTTPException) as caught:
                start_future.result(5)
            self.assertEqual(caught.exception.status_code, 409)
        native.execute.assert_not_called()

    def test_aggregate_record_limit_and_version_identity_mismatch_fail_closed(self):
        from unittest.mock import patch
        self.fx.remote.versions.return_value = {'data': [{'id': 'other-deployment', 'version': 1, 'isActive': True}], 'nextCursor': None}
        self.assertEqual(self.archive().status_code, 422)
        self.fx.remote.versions.return_value = {'data': [{'id': 'deployment-1', 'version': 1, 'isActive': True}], 'nextCursor': None}
        self.fx.remote.runs.return_value = {'data': [{'runId': 'one-run', 'status': 'completed'}], 'nextCursor': None}
        with patch('agent_line.workflow_archive.MAX_ITEMS', 1):
            self.assertEqual(self.archive().status_code, 422)
        self.fx.remote.archive.assert_not_called()
        self.assertIsNone(record(self.fx.journal, 'one', self.wid))
