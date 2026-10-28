"""Independent human authority for native permission requests; no Sim resume."""
import copy
import unittest
from fastapi.testclient import TestClient

from agent_line.dispatcher import Dispatcher
from agent_line.service import create_app
from tests import test_approval_api as fixtures


class NativeGateway:
    role = 'parent'

    def __init__(self, workspace):
        self.workspace = workspace
        self.posts = []
        self.records = []
        self.after_list = lambda: None

    def execution_approvals(self, task_id):
        result = {'taskId': task_id, 'identity': self.identity,
                  'approvals': copy.deepcopy(self.records)}
        self.after_list()
        return result

    def decide_execution_approval(self, task_id, request_id, payload):
        self.posts.append((task_id, request_id, payload))
        record = copy.deepcopy(self.records[0])
        record.update(status='decision_recorded', deliveryStatus='accepted',
                      decision={key: payload[key] for key in ('decisionId', 'decision', 'decidedBy')})
        self.records = [record]
        return record


class NativeApprovalAPITests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ApprovalAPITests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.identity = {'upstreamTaskId': 'native', 'codexThreadId': 'thread', 'codexTurnId': 'turn'}
        self.gateway = NativeGateway(str(self.f.root))
        self.gateway.identity = self.identity
        self.dispatcher = Dispatcher(self.f.store, {'project': self.gateway})
        task, _ = self.f.store.register('native', {
            'projectId': 'project', 'roleAgentId': 'parent', 'workspace': str(self.f.root),
            'workflow': {'id': 'flow', 'runId': 'run', 'nodeExecutionId': 'node', 'attemptId': '1'}})
        self.task = task['id']
        self.f.store.claim(self.task, self.f.root)
        self.f.store.accept(self.task, self.identity)
        self.gateway.records = [{'requestId': 'request', 'purpose': 'execution_permission',
            'taskId': self.task, 'identity': self.identity, 'roleSessionId': 'agent-line-' + self.task,
            'targetAgentId': 'codex', 'providerItemId': 'item',
            'method': 'item/commandExecution/requestApproval', 'createdAt': 900,
            'expiresAt': 1500, 'summary': 'Run project tests', 'severity': 'high',
            'status': 'waiting', 'bound': True, 'decision': None, 'deliveryStatus': 'not_submitted'}]
        app = create_app(self.f.store, self.f.config, approvals=self.f.api, dispatcher=self.dispatcher)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.url = '/extension/v1/node-tasks/' + self.task + '/execution-approvals'

    def get(self, token='human-token', url=None):
        return self.client.get(url or self.url, headers={'Authorization': 'Bearer ' + token})

    def decide(self, token='human-token', **changes):
        payload = {'decisionId': 'decision', 'decision': 'approve', 'expectedIdentity': self.identity}
        payload.update(changes)
        return self.client.post(self.url + '/request/decision', json=payload,
                                headers={'Authorization': 'Bearer ' + token})

    def test_human_only_project_scoped_listing(self):
        for token, expected in (('bad', 401), ('service-token', 401), ('read-token', 403),
                                ('expired-token', 403), ('other-token', 404)):
            self.assertEqual(self.get(token).status_code, expected)
            self.assertEqual(self.decide(token).status_code, expected)
        result = self.get()
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['approvals'][0]['purpose'], 'execution_permission')
        self.assertFalse(self.gateway.posts)
        self.assertEqual(self.f.sim.posts, [])

    def test_approve_and_replay_only_respond_to_native_request(self):
        first = self.decide()
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()['deliveryStatus'], 'accepted')
        self.assertEqual(self.gateway.posts[0][2]['decidedBy'], 'human')
        self.assertEqual(self.decide().json(), first.json())
        self.assertEqual(len(self.gateway.posts), 1)
        self.assertEqual(self.decide(decision='reject').status_code, 409)
        self.assertEqual(self.f.sim.posts, [])
        task = self.f.store.get(self.task)
        self.assertEqual(task['executionStatus'], 'accepted')
        self.assertEqual(task['verificationStatus'], 'pending')

    def test_injected_actor_scope_and_stale_turn_rejected(self):
        for field in ('decidedBy', 'scope', 'sessionId', 'targetAgentId', 'projectId'):
            self.assertEqual(self.decide(**{field: 'injected'}).status_code, 422)
        stale = {**self.identity, 'codexTurnId': 'old'}
        self.assertEqual(self.decide(expectedIdentity=stale).status_code, 409)
        self.assertFalse(self.gateway.posts)

    def test_expired_unbound_and_disappeared_requests_not_approved(self):
        for change in ({'expiresAt': 999}, {'bound': False, 'status': 'unbound'},
                       {'status': 'not_observed'}):
            original = copy.deepcopy(self.gateway.records[0])
            self.gateway.records[0].update(change)
            self.assertEqual(self.decide().status_code, 409)
            self.gateway.records = [original]
        self.gateway.records = []
        self.assertEqual(self.decide().status_code, 404)
        self.assertFalse(self.gateway.posts)

    def test_cancellation_maintenance_and_live_revocation_prevent_delivery(self):
        self.gateway.after_list = lambda: self.f.access.revoke('human-token')
        self.assertEqual(self.decide().status_code, 403)
        self.f.access.replace_credentials({'human-token': self.f.principal})
        self.gateway.after_list = lambda: None
        owner = self.f.store.enter_maintenance()
        self.assertEqual(self.decide().status_code, 503)
        self.f.store.leave_maintenance(owner)
        self.gateway.after_list = lambda: self.f.store.request_cancel(self.task)
        self.assertEqual(self.decide().status_code, 409)
        self.assertFalse(self.gateway.posts)

    def test_unknown_response_is_not_reposted_when_native_intent_survives(self):
        original = self.gateway.decide_execution_approval
        def lost(*args):
            result = original(*args)
            self.gateway.records[0]['deliveryStatus'] = 'intent'
            raise TimeoutError('private-host-token-and-path')
        self.gateway.decide_execution_approval = lost
        response = self.decide()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('private-host', response.text)
        response = self.decide()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['deliveryStatus'], 'intent')
        self.assertEqual(len(self.gateway.posts), 1)

    def test_wrong_task_session_identity_and_duplicate_request_fail_closed(self):
        for change in ({'taskId': 'another'}, {'roleSessionId': 'another'},
                       {'identity': {**self.identity, 'codexTurnId': 'another'}}):
            original = copy.deepcopy(self.gateway.records[0])
            self.gateway.records[0].update(change)
            self.assertEqual(self.get().status_code, 503)
            self.gateway.records = [original]
        self.gateway.records.append(copy.deepcopy(self.gateway.records[0]))
        self.assertEqual(self.get().status_code, 503)
        self.assertFalse(self.gateway.posts)

    def test_summary_and_extra_fields_do_not_expose_credentials(self):
        self.gateway.records[0].update(summary='human-token service-token details',
                                       extra={'private': 'never-public'})
        result = self.get()
        self.assertEqual(result.status_code, 200, result.text)
        self.assertNotIn('human-token', result.text)
        self.assertNotIn('service-token', result.text)
        self.assertNotIn('never-public', result.text)

    def test_cancel_waits_for_inflight_human_delivery_then_blocks_new_decisions(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        entered, release, cancel_started = threading.Event(), threading.Event(), threading.Event()
        original = self.gateway.decide_execution_approval
        def delivery(*args):
            entered.set()
            self.assertTrue(release.wait(5))
            return original(*args)
        def cancel():
            cancel_started.set()
            return self.f.store.request_cancel(self.task)
        self.gateway.decide_execution_approval = delivery
        with ThreadPoolExecutor(max_workers=2) as pool:
            response = pool.submit(self.decide)
            self.assertTrue(entered.wait(5))
            canceled = pool.submit(cancel)
            self.assertTrue(cancel_started.wait(5))
            try:
                self.assertFalse(canceled.done())
                self.assertFalse(self.f.store.get(self.task)['cancellationRequested'])
            finally:
                release.set()
            self.assertEqual(response.result().status_code, 200)
            self.assertTrue(canceled.result())
        self.assertTrue(self.f.store.get(self.task)['cancellationRequested'])
        self.assertEqual(self.decide().status_code, 200)  # History read, zero additional native POST.
        self.assertEqual(len(self.gateway.posts), 1)

    def test_run_pause_and_stop_close_native_decision_admission(self):
        from agent_line.run_control import RunControl
        self.dispatcher.run_control = RunControl(self.f.journal, self.f.store)
        control = self.dispatcher.run_control
        control.apply(project_id='project', workflow_id='flow', run_id='run', action='pause',
                      operation_id='pause', expected_version=0, active_execution='continue')
        self.assertEqual(self.decide().status_code, 409)
        control.apply(project_id='project', workflow_id='flow', run_id='run', action='stop',
                      operation_id='stop', expected_version=1)
        self.assertEqual(self.decide().status_code, 409)
        self.assertFalse(self.gateway.posts)

    def test_malformed_display_metadata_is_not_forwarded(self):
        for change in ({'severity': {'private': 'secret'}}, {'method': {'private': 'secret'}},
                       {'providerItemId': {'private': 'secret'}}, {'expiresAt': True}):
            original = copy.deepcopy(self.gateway.records[0])
            self.gateway.records[0].update(change)
            response = self.get()
            self.assertEqual(response.status_code, 503)
            self.assertNotIn('secret', response.text)
            self.gateway.records = [original]
