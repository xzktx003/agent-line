"""Unknown observation never replaces accepted facts or authorizes another run."""
import copy
import unittest

import httpx

from agent_line.dispatcher import Dispatcher
from agent_line.service import create_app
from agent_line.store import Conflict, TaskStore
from fastapi.testclient import TestClient
from tests import test_dispatcher as dispatch_fixture
from tests import test_service as service_fixture
from tests import test_role_dispatch as role_fixture


class ObservationDispatcherTests(unittest.TestCase):
    setUp = dispatch_fixture.DispatcherTests.setUp

    def test_timeout_is_persistent_unknown_without_repost_or_lease_release(self):
        self.dispatcher.submit(self.task['id'])
        self.gateway.observe.side_effect = TimeoutError('private diagnostic must not persist')
        for _ in range(2):
            task = self.dispatcher.reconcile(self.task['id'])
            self.assertEqual(task['executionStatus'], 'accepted')
            self.assertEqual(task['executionIdentity'], {'upstreamTaskId': 'native-task'})
            self.assertFalse(task['resourcesReleased'])
        restarted = TaskStore(self.store.path)
        self.assertEqual(restarted.last_execution_observation(self.task['id'])['status'], 'unknown')
        with restarted.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM workspace_leases').fetchone()[0], 1)
        self.gateway.submit.assert_called_once()
        self.assertNotIn('private diagnostic', str(self.store.events(self.task['id'])))

    def test_unknown_then_matching_observation_clears_only_query_warning(self):
        self.dispatcher.submit(self.task['id'])
        self.gateway.observe.return_value = {'status': 'unknown'}
        self.dispatcher.reconcile(self.task['id'])
        self.assertEqual(self.store.last_execution_observation(self.task['id'])['status'], 'unknown')
        self.gateway.observe.return_value = {'status': 'accepted', 'identity': {'upstreamTaskId': 'native-task'}}
        task = self.dispatcher.reconcile(self.task['id'])
        self.assertEqual(task['executionStatus'], 'accepted')
        self.assertEqual(self.store.last_execution_observation(self.task['id'])['status'], 'observed')
        before = self.store.get(self.task['id'])
        self.dispatcher.reconcile(self.task['id'])
        self.assertEqual(self.store.get(self.task['id']), before)

    def test_invalid_or_unconfirmed_terminal_never_claims_observed_completion(self):
        self.dispatcher.submit(self.task['id'])
        for result in ([], {'status': 'accepted'}, {'status': 'unknown', 'identity': {'upstreamTaskId': 'native-task'}},
                       {'status': 'succeeded', 'identity': {'upstreamTaskId': 'native-task'}}):
            self.gateway.observe.return_value = result
            self.dispatcher.reconcile(self.task['id'])
            self.assertEqual(self.store.last_execution_observation(self.task['id'])['status'], 'unknown')
            self.assertEqual(self.store.get(self.task['id'])['executionStatus'], 'accepted')

    def test_conflicting_identity_records_unknown_but_does_not_hide_conflict(self):
        self.dispatcher.submit(self.task['id'])
        self.gateway.observe.return_value = {'status': 'accepted', 'identity': {'upstreamTaskId': 'another-task'}}
        with self.assertRaises(Conflict):
            self.dispatcher.reconcile(self.task['id'])
        self.assertEqual(self.store.last_execution_observation(self.task['id'])['status'], 'unknown')
        self.assertEqual(self.store.get(self.task['id'])['executionIdentity'], {'upstreamTaskId': 'native-task'})

    def test_malformed_identity_replaces_observed_warning_without_hiding_validation_error(self):
        self.dispatcher.submit(self.task['id'])
        self.gateway.observe.return_value = {'status': 'accepted', 'identity': {'upstreamTaskId': 'native-task'}}
        self.dispatcher.reconcile(self.task['id'])
        self.gateway.observe.return_value = {'status': 'accepted', 'identity': {'upstreamTaskId': 123}}
        with self.assertRaises(ValueError):
            self.dispatcher.reconcile(self.task['id'])
        self.assertEqual(self.store.last_execution_observation(self.task['id'])['status'], 'unknown')
        self.assertEqual(self.store.get(self.task['id'])['executionIdentity'], {'upstreamTaskId': 'native-task'})

    def test_missing_parent_is_unknown_until_native_terminal_is_proven(self):
        self.dispatcher.submit(self.task['id'])
        identity = {'upstreamTaskId': 'native-task'}
        self.gateway.observe.return_value = {'status': 'accepted', 'identity': identity,
                                             'parent': {'observedStatus': 'unknown'}}
        self.dispatcher.reconcile(self.task['id'])
        self.assertEqual(self.store.last_execution_observation(self.task['id'])['status'], 'unknown')
        self.gateway.observe.return_value = {'status': 'succeeded', 'identity': identity,
            'parent': {'observedStatus': 'unknown'}, 'terminalConfirmed': True, 'resourcesReleased': False}
        task = self.dispatcher.reconcile(self.task['id'])
        self.assertEqual(task['executionStatus'], 'succeeded')
        self.assertEqual(self.store.last_execution_observation(self.task['id'])['status'], 'observed')

    def test_positive_terminal_replaces_prior_unknown_before_resource_exit_freezes_task(self):
        self.dispatcher.submit(self.task['id'])
        self.gateway.observe.return_value = {'status': 'unknown'}
        self.dispatcher.reconcile(self.task['id'])
        self.gateway.observe.return_value = {'status': 'succeeded', 'identity': {'upstreamTaskId': 'native-task'},
            'terminalConfirmed': True, 'resourcesReleased': True}
        task = self.dispatcher.reconcile(self.task['id'])
        self.assertTrue(task['resourcesReleased'])
        self.assertEqual(self.store.last_execution_observation(self.task['id'])['status'], 'observed')

    def test_repeated_malformed_terminal_does_not_flip_observation_events(self):
        self.dispatcher.submit(self.task['id'])
        identity = {'upstreamTaskId': 'native-task', 'codexThreadId': 'thread', 'codexTurnId': 'turn'}
        for malformed in ({'executionControls': {'sandbox': 'danger-full-access'}},
                          {'resourceExitReceipt': {'source': 'untrusted-model-claim'}}):
            self.gateway.observe.return_value = {'status': 'succeeded', 'identity': identity,
                'terminalConfirmed': True, 'resourcesReleased': True, **malformed}
            with self.assertRaises(ValueError):
                self.dispatcher.reconcile(self.task['id'])
            before, events = self.store.get(self.task['id']), self.store.events(self.task['id'])
            with self.assertRaises(ValueError):
                self.dispatcher.reconcile(self.task['id'])
            self.assertEqual(self.store.get(self.task['id']), before)
            self.assertEqual(self.store.events(self.task['id']), events)


class ObservationPublicTests(unittest.TestCase):
    setUp = service_fixture.ServiceTests.setUp
    register = service_fixture.ServiceTests.register

    def test_authorized_detail_and_list_expose_only_persisted_query_status(self):
        task_id = self.register()
        self.store.claim(task_id, self.root)
        self.store.accept(task_id, {'upstreamTaskId': 'native'})
        self.store.record_execution_observation(task_id, 'unknown')
        before, events = self.store.get(task_id), self.store.events(task_id)
        for client in (self.client, TestClient(create_app(TaskStore(self.store.path), self.config))):
            detail = client.get('/extension/v1/node-tasks/' + task_id, headers=self.headers)
            listing = client.get('/extension/v1/node-tasks', headers=self.headers)
            self.assertEqual(detail.status_code, 200)
            self.assertEqual(detail.json()['executionObservation']['status'], 'unknown')
            self.assertEqual(listing.json()['tasks'][0]['executionObservation'], detail.json()['executionObservation'])
            self.assertEqual(detail.json()['executionStatus'], 'accepted')
        denied = self.client.get('/extension/v1/node-tasks/' + task_id,
                                  headers={'Authorization': 'Bearer test-other-token'})
        self.assertEqual(denied.status_code, 404)
        self.assertEqual(self.store.get(task_id), before)
        self.assertEqual(self.store.events(task_id), events)


class FreshParentObservationTests(unittest.TestCase):
    setUp = role_fixture.RoleDispatchTests.setUp
    handler = role_fixture.RoleDispatchTests.handler
    gateway = role_fixture.RoleDispatchTests.gateway
    submit = role_fixture.RoleDispatchTests.submit

    def test_parent_404_does_not_return_cached_running_as_fresh(self):
        gateway = self.gateway()
        self.submit(gateway)
        gateway.observe('task-one')
        previous = copy.deepcopy(self.ledger.get('task-one')['observation'])
        self.assertEqual(previous['status'], 'running')
        def missing(request):
            if request.url.path == '/api/console/chat/task/parent-native-one':
                return httpx.Response(404, json={})
            return self.handler(request)
        result = self.gateway(missing).observe('task-one')
        self.assertEqual(result['parent']['observedStatus'], 'unknown')
        self.assertEqual(self.ledger.get('task-one')['observation'], previous)
