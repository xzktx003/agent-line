"""Expose accepted resume children without rewriting their paused parent."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import httpx
from fastapi.testclient import TestClient
from agent_line.adapters.sim import SimClient, SubmissionUncertain, UpstreamFailure
from agent_line.service import ServiceConfig, create_app
from agent_line.sim_journal import SimJournal
from agent_line.store import TaskStore
from agent_line.workflows import WorkflowGateway


class WorkflowResumptionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.tasks = TaskStore.initialize(self.root / 'tasks.sqlite')
        self.journal = SimJournal.initialize(self.root / 'journal.sqlite')
        self.requests = []
        self.child_status = 'failed'
        self.child_http = 200
        self.child_override = None
        def handler(request):
            self.requests.append((request.method, request.url.path))
            if request.method == 'POST':
                if request.url.path.endswith('/execute'):
                    return httpx.Response(202, json={'data': {'runId': 'parent', 'workflowId': 'workflow'}})
                return httpx.Response(202, headers={'X-Run-Id': 'child'}, json={
                    'data': {'runId': 'child', 'workflowId': 'workflow',
                             'contextId': json.loads(request.content)['contextId']}})
            child = request.url.path.endswith('/child')
            if child and self.child_override is not None:
                return httpx.Response(self.child_http, json=self.child_override)
            return httpx.Response(self.child_http if child else 200, json={'data': {
                'workflowId': 'workflow', 'runId': 'child' if child else 'parent',
                'status': self.child_status if child else 'paused'}})
        transport = httpx.Client(transport=httpx.MockTransport(handler)); self.addCleanup(transport.close)
        self.sim = SimClient('http://sim.invalid', 'private-key', client=transport)
        self.journal.execute(self.sim, 'workflow', 'parent', {})
        self.open_app()
        self.requests.clear()

    def open_app(self):
        self.gateway = WorkflowGateway(self.sim, self.journal,
            {'flow': {'projectId': 'one', 'workflowId': 'workflow'}}, installation_id='installation')
        app = create_app(self.tasks, ServiceConfig(tokens={'one-token': {'one'}, 'two-token': {'two'}},
            projects={'one': {'workspace': str(self.root)}, 'two': {'workspace': str(self.root)}}, bindings={}),
            workflows=self.gateway)
        self.http = TestClient(app); self.addCleanup(self.http.close)

    def read(self, run='parent', token='one-token'):
        return self.http.get('/extension/v1/workflows/flow/runs/'+run,
                             headers={'Authorization': 'Bearer '+token})

    def accepted(self, context='context-one'):
        self.journal.resume(self.sim, 'workflow', 'parent', context, {'approved': True})
        self.requests.clear()

    def test_paused_parent_and_failed_child_are_distinct_and_get_is_read_only(self):
        self.accepted()
        response = self.read(); self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result['execution']['status'], 'paused')
        self.assertEqual(result['resumption']['execution']['status'], 'failed')
        self.assertEqual(result['resumption']['runId'], 'child')
        self.assertEqual(result['resumption']['contextId'], 'context-one')
        self.assertEqual(result['resumption']['submissionStatus'], 'accepted')
        self.assertFalse(result['resumeSubmission']['safeToResubmit'])
        self.assertEqual(self.read().json(), result)
        self.assertTrue(all(method == 'GET' for method, _ in self.requests))

    def test_success_child_does_not_relabel_parent_completed(self):
        self.accepted(); self.child_status = 'completed'
        result = self.read().json()
        self.assertEqual(result['execution']['status'], 'paused')
        self.assertEqual(result['resumption']['execution']['status'], 'completed')

    def test_unknown_child_is_not_replayed(self):
        self.accepted(); self.child_http = 404
        result = self.read().json()
        self.assertEqual(result['resumption']['execution']['status'], 'unknown')
        self.assertEqual(result['resumption']['runId'], 'child')
        self.assertEqual(self.read().json(), result)
        self.assertTrue(all(method == 'GET' for method, _ in self.requests))

    def test_cross_project_and_unknown_parent_cannot_observe_child(self):
        self.accepted()
        self.assertEqual(self.read(token='two-token').status_code, 404)
        self.assertEqual(self.read(run='unknown-root').status_code, 404)
        self.assertEqual(self.requests, [])

    def test_new_uncertain_or_rejected_intent_preserves_last_accepted_child(self):
        self.accepted()
        for index, error, status in [(2, SubmissionUncertain('parent', 'context-two'), 'uncertain'),
                                     (3, UpstreamFailure(422, 'resume'), 'rejected')]:
            remote = Mock(); remote.resume.side_effect = error
            context = 'context-'+str(index)
            self.journal.resume(remote, 'workflow', 'parent', context, {})
            result = self.read().json()
            self.assertEqual(result['resumeSubmission']['contextId'], context)
            self.assertEqual(result['resumeSubmission']['status'], status)
            self.assertEqual(result['resumption']['contextId'], 'context-one')
            self.assertEqual(result['resumption']['runId'], 'child')
            self.assertEqual(remote.resume.call_count, 1)
        self.assertTrue(all(method == 'GET' for method, _ in self.requests))

    def test_crashed_intent_preserves_accepted_child_without_retry(self):
        self.accepted()
        remote = Mock(); remote.resume.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.journal.resume(remote, 'workflow', 'parent', 'context-two', {})
        result = self.read().json()
        self.assertEqual(result['resumeSubmission']['status'], 'intent')
        self.assertEqual(result['resumption']['runId'], 'child')
        self.assertEqual(remote.resume.call_count, 1)

    def test_reopen_and_maintenance_allow_read_only_child_observation(self):
        self.accepted()
        self.journal = SimJournal(self.journal.path); self.open_app()
        self.tasks.enter_maintenance()
        result = self.read()
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['resumption']['runId'], 'child')
        self.assertTrue(all(method == 'GET' for method, _ in self.requests))

    def test_no_resume_remains_compatible_and_metadata_is_excluded(self):
        before = self.read().json()
        self.assertNotIn('resumption', before)
        self.assertNotIn('resumeSubmission', before)
        self.journal.reserve_publication('workflow', 'parent', {'bindingId': 'publication'})
        for kind in ('create', 'save', 'publish'):
            self.journal.mutate('workflow', 'parent', kind, {}, lambda: {'resumedRunId': 'malicious'})
        self.assertIsNone(self.journal.resume_summary('workflow', 'parent'))
        self.assertEqual(self.read().json(), before)
        self.assertNotIn(('GET', '/api/v2/workflows/workflow/runs/malicious'), self.requests)

    def test_legacy_accepted_receipt_without_child_is_unknown(self):
        remote = Mock(); remote.resume.return_value = {'transportStatus': 'accepted'}
        self.journal.resume(remote, 'workflow', 'parent', 'legacy-context', {})
        summary = self.journal.resume_summary('workflow', 'parent')
        self.assertIsNone(summary['latestAccepted']['runId'])
        result = self.read().json()
        self.assertEqual(result['resumption']['execution']['status'], 'unknown')
        self.assertIsNone(result['resumption']['runId'])
        self.assertEqual(self.requests, [('GET', '/api/v2/workflows/workflow/runs/parent')])

    def test_receipt_identity_mismatch_never_fetches_untrusted_child(self):
        for changes in ({'workflowId': 'another'}, {'runId': 'another-parent'}, {'contextId': 'another-context'}):
            with self.subTest(changes=changes):
                remote = Mock(); remote.resume.return_value = dict(workflowId='workflow', runId='parent',
                    contextId='bad-context', resumedRunId='secret-child', transportStatus='accepted') | changes
                # Reuse one receipt slot after removing only this test's fixture.
                with self.journal.connect() as db:
                    db.execute("DELETE FROM operations WHERE context='bad-context'")
                self.journal.resume(remote, 'workflow', 'parent', 'bad-context', {})
                result = self.read().json()
                self.assertEqual(result['resumption']['execution']['status'], 'unknown')
                self.assertNotIn('secret-child', json.dumps(result))
        self.assertTrue(all(path.endswith('/parent') for _, path in self.requests))

    def test_observe_rejects_wrong_identity_and_invalid_envelopes_without_secret(self):
        self.accepted()
        for payload in [
            {'data': {'workflowId': 'other-project', 'runId': 'child', 'status': 'completed', 'output': 'SECRET'}},
            {'data': {'workflowId': 'workflow', 'runId': 'wrong-child', 'status': 'completed', 'output': 'SECRET'}},
            {'success': False, 'data': {'runId': 'child', 'status': 'completed', 'output': 'SECRET'}},
            {'error': 'SECRET', 'data': {'runId': 'child', 'status': 'completed'}},
            {'data': ['SECRET']},
        ]:
            with self.subTest(payload=payload):
                self.child_override = payload
                result = self.read()
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(result.json()['resumption']['execution']['status'], 'unknown')
                self.assertNotIn('SECRET', result.text)
        self.assertTrue(all(method == 'GET' for method, _ in self.requests))

    def test_child_forbidden_is_unknown_without_hiding_readable_parent(self):
        self.accepted(); self.child_http = 403
        response = self.read()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['execution']['status'], 'paused')
        self.assertEqual(response.json()['resumption']['execution']['status'], 'unknown')
        self.assertEqual(response.json()['resumption']['runId'], 'child')
        self.assertTrue(all(method == 'GET' for method, _ in self.requests))

    def test_unknown_attempt_without_accepted_child_has_no_invented_resumption(self):
        remote = Mock(); remote.resume.side_effect = SubmissionUncertain('parent', 'context')
        self.journal.resume(remote, 'workflow', 'parent', 'context', {})
        summary = self.journal.resume_summary('workflow', 'parent')
        self.assertIsNone(summary['latestAccepted'])
        self.assertEqual(summary['latestAttempt'], {
            'contextId': 'context', 'status': 'uncertain', 'runId': None, 'safeToResubmit': False})
        response = self.read()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn('resumption', response.json())
        self.assertEqual(response.json()['resumeSubmission'], summary['latestAttempt'])
        self.assertEqual(self.requests, [('GET', '/api/v2/workflows/workflow/runs/parent')])
        remote.resume.assert_called_once()

    def test_metadata_does_not_mask_an_existing_accepted_resume(self):
        self.accepted()
        before = self.journal.resume_summary('workflow', 'parent')
        self.journal.reserve_publication('workflow', 'parent', {'resumedRunId': 'metadata-child'})
        for kind in ('create', 'save', 'publish'):
            self.journal.mutate('workflow', 'parent', kind, {}, lambda: {'resumedRunId': 'metadata-child'})
        self.assertEqual(self.journal.resume_summary('workflow', 'parent'), before)
        response = self.read()
        self.assertEqual(response.json()['resumption']['runId'], 'child')
        self.assertNotIn('metadata-child', response.text)

    def test_late_accepted_receipt_cannot_replace_newer_uncertain_attempt(self):
        self._late_receipt_order(newer_uncertain=True)

    def test_late_older_receipt_cannot_replace_newer_accepted_child(self):
        self._late_receipt_order(newer_uncertain=False)

    def _late_receipt_order(self, *, newer_uncertain):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        entered, release = threading.Event(), threading.Event()
        remote = Mock()
        def resume(workflow, run, context, inputs):
            if context == 'older-A':
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('test receipt gate timed out')
                child = 'childA'
            elif newer_uncertain:
                raise SubmissionUncertain(run, context)
            else:
                child = 'childB'
            return {'workflowId': workflow, 'runId': run, 'contextId': context,
                    'resumedRunId': child, 'transportStatus': 'accepted'}
        remote.resume.side_effect = resume
        with ThreadPoolExecutor(max_workers=2) as pool:
            older = pool.submit(self.journal.resume, remote, 'workflow', 'parent', 'older-A', {})
            try:
                self.assertTrue(entered.wait(3), 'A must have committed intent before B begins')
                newer = pool.submit(self.journal.resume, remote, 'workflow', 'parent', 'newer-B', {})
                self.assertEqual(newer.result(timeout=3)['status'],
                                 'uncertain' if newer_uncertain else 'accepted')
            finally:
                release.set()
            self.assertEqual(older.result(timeout=3)['status'], 'accepted')
        summary = SimJournal(self.journal.path).resume_summary('workflow', 'parent')
        self.assertEqual(summary['latestAttempt']['contextId'], 'newer-B')
        self.assertEqual(summary['latestAttempt']['status'], 'uncertain' if newer_uncertain else 'accepted')
        expected_context = 'older-A' if newer_uncertain else 'newer-B'
        expected_child = 'childA' if newer_uncertain else 'childB'
        self.assertEqual(summary['latestAccepted']['contextId'], expected_context)
        self.assertEqual(summary['latestAccepted']['runId'], expected_child)
        response = self.read()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['resumeSubmission']['contextId'], 'newer-B')
        self.assertEqual(response.json()['resumeSubmission']['status'], 'uncertain' if newer_uncertain else 'accepted')
        self.assertEqual(response.json()['resumption']['runId'], expected_child)
        self.assertEqual(remote.resume.call_count, 2)
        self.assertTrue(all(method == 'GET' for method, _ in self.requests))
