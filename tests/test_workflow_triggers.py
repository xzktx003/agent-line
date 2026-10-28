"""Trigger ingress shares real gateway/journal ownership and publication semantics."""
import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from agent_line.adapters.sim import SubmissionUncertain
from agent_line.approval_discovery import WorkflowRunLedger
from agent_line.service import ServiceConfig, create_app
from agent_line.sim_journal import SimJournal
from agent_line.triggers import TriggerAPI, event_key
from agent_line.workflows import WorkflowGateway
from tests import test_published_workflows as published_fixtures


class WorkflowTriggerTests(unittest.TestCase):
    def setUp(self):
        # Reuse the existing authoring fixture; the catalog and ownership /
        # mutation stores are real. Only remote Sim transport is simulated.
        self.fixture = published_fixtures.PublishedWorkflowTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.workflow, self.binding = self.fixture.published()
        self.config = {
            'hook': {'projectId': 'one', 'bindingId': self.binding, 'type': 'webhook'},
            'hook2': {'projectId': 'one', 'bindingId': self.binding, 'type': 'webhook'},
            'timer': {'projectId': 'one', 'bindingId': self.binding, 'type': 'schedule'},
        }
        self.gateway = self.fixture.gateway
        self.open_app()

    def open_app(self):
        self.triggers = TriggerAPI(self.gateway, self.config, projects=self.fixture.projects)
        config = ServiceConfig(tokens={'run-one': {'one'}, 'run-two': {'two'}, 'run-both': {'one', 'two'}},
                               projects=self.fixture.projects, bindings={})
        app = create_app(self.fixture.tasks, config, authoring=self.fixture.api,
                         workflows=self.gateway, triggers=self.triggers)
        self.http = TestClient(app)
        self.addCleanup(self.http.close)

    def send(self, trigger='hook', kind='webhook', *, identity='event-1', inputs=None, token='run-one', extra=None):
        body = {'eventId' if kind == 'webhook' else 'slot': identity,
                'input': {} if inputs is None else inputs}
        body.update(extra or {})
        return self.http.post(f'/extension/v1/triggers/{trigger}/{kind}', json=body,
                              headers={'Authorization': 'Bearer '+token} if token else {})

    def test_replayed_webhook_sends_once_and_returns_original_run(self):
        first = self.send(inputs={'value': 3})
        second = self.send(inputs={'value': 3})
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(self.fixture.sim.execute.call_count, 1)
        self.assertFalse(first.json()['safeToResubmit'])
        self.assertFalse(first.json()['verifiedComplete'])
        self.assertEqual(self.fixture.sim.execute.call_args.args[2], {'value': 3})

    def test_input_change_is_conflict_without_second_execution(self):
        self.assertEqual(self.send(inputs={'value': 1}).status_code, 202)
        changed = self.send(inputs={'value': 2})
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(self.fixture.sim.execute.call_count, 1)

    def test_unknown_receipt_restart_never_resubmits(self):
        self.fixture.sim.execute.side_effect = SubmissionUncertain('private-secret')
        first = self.send()
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(first.json()['status'], 'uncertain')
        self.assertNotIn('private-secret', first.text)
        self.gateway = WorkflowGateway(self.fixture.sim, SimJournal(self.fixture.journal.path), {},
                                       installation_id='installation')
        self.open_app()
        again = self.send()
        self.assertEqual(again.json(), first.json())
        self.assertEqual(self.fixture.sim.execute.call_count, 1)
        self.assertEqual(self.send(inputs={'changed': True}).status_code, 409)
        self.assertEqual(self.fixture.sim.execute.call_count, 1)

    def test_accepted_receipt_survives_restart_and_later_publication(self):
        first = self.send()
        self.fixture.publish(self.workflow, 'new-publish')
        self.gateway = WorkflowGateway(self.fixture.sim, SimJournal(self.fixture.journal.path), {},
                                       installation_id='installation')
        self.open_app()
        self.assertEqual(self.send().json(), first.json())
        self.assertEqual(self.send(identity='event-2').status_code, 503)
        self.assertEqual(self.fixture.sim.execute.call_count, 1)

    def test_inactive_publication_never_follows_latest(self):
        self.fixture.publish(self.workflow, 'new-publish')
        response = self.send()
        self.assertEqual(response.status_code, 503, response.text)
        self.fixture.sim.execute.assert_not_called()

    def test_schedule_requires_explicit_utc_minute_and_ignores_arrival_time(self):
        slot = '2026-10-22T01:23:00Z'
        first = self.send('timer', 'schedule', identity=slot, inputs={'fixed': True})
        self.assertEqual(first.status_code, 202, first.text)
        # There is no current-time source in the ingress. Failing any accidental
        # receiver-clock access also demonstrates replay after delayed delivery.
        with patch('agent_line.triggers.datetime', wraps=datetime) as clock:
            clock.now.side_effect = AssertionError('arrival time is not occurrence identity')
            clock.utcnow.side_effect = AssertionError('arrival time is not occurrence identity')
            second = self.send('timer', 'schedule', identity=slot, inputs={'fixed': True})
        self.assertEqual(second.json(), first.json())
        self.assertEqual(self.fixture.sim.execute.call_count, 1)
        next_slot = self.send('timer', 'schedule', identity='2026-10-22T01:24:00Z')
        self.assertEqual(next_slot.status_code, 202)
        self.assertNotEqual(next_slot.json()['runId'], first.json()['runId'])
        self.assertEqual(self.fixture.sim.execute.call_count, 2)

    def test_schedule_replayed_slot_input_change_conflicts(self):
        slot = '2026-10-22T01:23:00Z'
        self.assertEqual(self.send('timer', 'schedule', identity=slot).status_code, 202)
        self.assertEqual(self.send('timer', 'schedule', identity=slot, inputs={'changed': 1}).status_code, 409)
        self.assertEqual(self.fixture.sim.execute.call_count, 1)

    def test_invalid_schedule_slots_do_not_submit(self):
        for value in ('2026-10-22T01:23:01Z', '2026-10-22T01:23:00+00:00',
                      '2026-10-22T01:23Z', '2026-02-30T01:23:00Z',
                      '2026-10-22T24:23:00Z', '0000-10-22T01:23:00Z', '', 123, True):
            with self.subTest(value=value):
                self.assertEqual(self.send('timer', 'schedule', identity=value).status_code, 422)
        self.fixture.sim.execute.assert_not_called()

    def test_missing_identity_input_and_payload_selected_routing_are_rejected(self):
        for body in ({'input': {}}, {'eventId': 'event'}, {'eventId': 'event', 'input': []},
                     {'eventId': True, 'input': {}}, {'eventId': ' ', 'input': {}},
                     {'eventId': 'bad\nidentity', 'input': {}},
                     {'eventId': 'event', 'input': {}, 'bindingId': 'other'},
                     {'eventId': 'event', 'input': {}, 'projectId': 'two'},
                     {'eventId': 'event', 'input': {}, 'roleAgentId': 'other'},
                     {'eventId': 'event', 'input': {}, 'deploymentVersionId': 'latest'}):
            with self.subTest(body=body):
                response = self.http.post('/extension/v1/triggers/hook/webhook', json=body,
                                         headers={'Authorization': 'Bearer run-one'})
                self.assertEqual(response.status_code, 422, response.text)
        self.fixture.sim.execute.assert_not_called()

    def test_auth_scope_unknown_and_disabled_project_are_hidden(self):
        self.assertEqual(self.send(token='').status_code, 401)
        self.assertEqual(self.send(token='bad-token').status_code, 401)
        self.assertEqual(self.send(token='run-two').status_code, 404)
        self.assertEqual(self.send(trigger='unknown').status_code, 404)
        self.triggers.projects['one']['enabled'] = False
        self.assertEqual(self.send().status_code, 404)
        self.fixture.sim.execute.assert_not_called()

    def test_wrong_event_type_does_not_execute(self):
        self.assertEqual(self.send('timer', 'webhook').status_code, 409)
        self.assertEqual(self.send('hook', 'schedule', identity='2026-10-22T01:23:00Z').status_code, 409)
        self.fixture.sim.execute.assert_not_called()

    def test_configured_binding_in_other_project_is_not_authorized_even_with_both_scopes(self):
        self.triggers.bindings['hook']['projectId'] = 'two'
        self.assertEqual(self.send(token='run-both').status_code, 404)
        self.fixture.sim.execute.assert_not_called()

    def test_configuration_is_copied_and_event_ids_are_trigger_scoped(self):
        self.config['hook']['bindingId'] = 'not-the-original'
        one, two = self.send(), self.send('hook2')
        self.assertEqual(one.status_code, 202)
        self.assertEqual(two.status_code, 202)
        self.assertNotEqual(one.json()['runId'], two.json()['runId'])
        self.assertEqual(self.fixture.sim.execute.call_count, 2)

    def test_hash_key_is_bounded_unambiguous_and_preserves_input_conflicts(self):
        self.assertLessEqual(len(event_key('x'*128, 'webhook', '字'*1024)), 256)
        self.assertNotEqual(event_key('ab', 'webhook', 'c'), event_key('a', 'webhook', 'bc'))
        self.assertNotEqual(event_key('x', 'webhook', 'slot'), event_key('x', 'schedule', 'slot'))
        result = self.send(identity='字'*1024)
        self.assertEqual(result.status_code, 202, result.text)
        self.assertEqual(self.send(identity='字'*1025).status_code, 422)

    def test_manual_route_and_trigger_share_original_identity_and_boundary(self):
        first = self.send()
        manual = self.http.post('/extension/v1/workflows/'+self.binding+'/runs',
            headers={'Authorization': 'Bearer run-one'},
            json={'idempotencyKey': event_key('hook', 'webhook', 'event-1'), 'input': {}})
        self.assertEqual(manual.json(), first.json())
        self.assertEqual(self.fixture.sim.execute.call_count, 1)

    def test_maintenance_prevents_new_events_but_does_not_replay_old_intents(self):
        original = self.send()
        self.fixture.tasks.enter_maintenance()
        self.assertEqual(self.send().json(), original.json())
        self.assertEqual(self.send(identity='different').status_code, 503)
        self.assertEqual(self.fixture.sim.execute.call_count, 1)

    def test_restore_guard_blocks_new_schedule_without_second_execution(self):
        slot = '2026-10-27T01:00:00Z'
        original = self.send('timer', 'schedule', identity=slot, inputs={'fixed': True})
        self.assertEqual(original.status_code, 202, original.text)
        with self.fixture.tasks.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute(
                'INSERT INTO admission_guard VALUES (1,?)',
                ('restored_snapshot_requires_external_reconciliation',))
        self.assertEqual(self.send('timer', 'schedule', identity=slot, inputs={'fixed': True}).json(), original.json())
        blocked = self.send('timer', 'schedule', identity='2026-10-27T01:01:00Z', inputs={'fixed': True})
        self.assertEqual(blocked.status_code, 503, blocked.text)
        self.assertEqual(self.fixture.sim.execute.call_count, 1)

    def test_concurrent_event_replays_issue_one_mutation(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(lambda _: self.send(), range(4)))
        self.assertTrue(all(response.status_code == 202 for response in responses))
        self.assertEqual(len({response.json()['runId'] for response in responses}), 1)
        self.assertEqual(self.fixture.sim.execute.call_count, 1)

    def test_legacy_unpinned_binding_cannot_be_auto_started(self):
        self.gateway.bindings['legacy'] = {'projectId': 'one', 'workflowId': 'legacy-flow'}
        self.triggers.bindings['hook']['bindingId'] = 'legacy'
        response = self.send()
        self.assertEqual(response.status_code, 503)
        self.fixture.sim.execute.assert_not_called()

    def test_config_rejects_unknown_fields_projects_and_types(self):
        for configs in ([], {'bad/id': self.config['hook']}, {'x': {}},
                        {'x': {**self.config['hook'], 'projectId': 'missing'}},
                        {'x': {**self.config['hook'], 'type': 'jira'}},
                        {'x': {**self.config['hook'], 'bindingId': '../escape'}},
                        {'x': {**self.config['hook'], 'enabled': True}}):
            with self.subTest(config=configs), self.assertRaises(ValueError):
                TriggerAPI(self.gateway, configs, projects=self.fixture.projects)

    def test_machine_trigger_reserves_original_run_schema_before_native_execute(self):
        from types import SimpleNamespace
        policy = dict(roleBindingId='role', roleAgentId='parent', instruction='Implement task',
                      memoryPolicyId='native', outputSchemaId='artifact',
                      requireMemoryCommit=False, allowFailureBranch=False)
        self.gateway.bindings['machine'] = {
            'projectId': 'one', 'workflowId': self.workflow,
            'publication': copy.deepcopy(self.gateway.binding_for(self.binding, {'one'})['publication']),
            'externalTasks': dict(revision='1', deploymentVersionId='deployment-'+self.workflow+'-1',
                                  agentPolicies={'wait': policy}, ttlSeconds=600)}
        runs = WorkflowRunLedger.initialize(self.fixture.root/'runs.sqlite')
        self.gateway.task_flow = SimpleNamespace(runs=runs, discovery=SimpleNamespace(allow_submissions=True))
        self.gateway.publication_verifier = Mock()
        self.triggers.bindings['hook']['bindingId'] = 'machine'
        def execute(workflow, run, inputs):
            frozen = WorkflowRunLedger(runs.path).get(workflow, run)
            self.assertEqual(frozen['agentPolicies'], {'wait': policy})
            self.assertEqual(frozen['revision'], '1')
            self.assertEqual(frozen['purpose'], 'external_task_result')
            self.assertEqual(self.fixture.journal.get_publication(workflow, run)['bindingId'], 'machine')
            return {'accepted': True}
        self.fixture.sim.execute.side_effect = execute
        first = self.send()
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(self.send().json(), first.json())
        self.assertEqual(self.fixture.sim.execute.call_count, 1)
        self.assertEqual(runs.get(self.workflow, first.json()['runId'])['submissionStatus'], 'accepted')

    def test_trigger_id_cannot_be_rebound_after_restart_even_for_new_event(self):
        original = self.send()
        self.assertEqual(original.status_code, 202)
        newer = self.fixture.publish(self.workflow, 'new-publish').json()['receipt']['bindingId']
        self.config['hook']['bindingId'] = newer
        self.gateway = WorkflowGateway(self.fixture.sim, SimJournal(self.fixture.journal.path), {},
                                       installation_id='installation')
        self.open_app()
        self.assertEqual(self.send().status_code, 409)
        self.assertEqual(self.send(identity='entirely-new-event').status_code, 409)
        self.assertEqual(self.fixture.sim.execute.call_count, 1)
        self.config['hook']['bindingId'] = self.binding
        self.open_app()
        self.assertEqual(self.send().json(), original.json())
        self.assertEqual(self.fixture.sim.execute.call_count, 1)

    def test_trigger_kind_cannot_change_but_new_trigger_identity_is_explicitly_new(self):
        self.assertEqual(self.send().status_code, 202)
        self.config['hook']['type'] = 'schedule'
        self.open_app()
        response = self.send('hook', 'schedule', identity='2026-10-22T01:23:00Z')
        self.assertEqual(response.status_code, 409)
        self.config['new-timer'] = copy.deepcopy(self.config['hook'])
        self.open_app()
        self.assertEqual(self.send('new-timer', 'schedule', identity='2026-10-22T01:23:00Z').status_code, 202)
        self.assertEqual(self.fixture.sim.execute.call_count, 2)

    def test_first_event_under_maintenance_does_not_freeze_a_definition(self):
        with self.fixture.journal.connect() as db:
            before = db.execute('SELECT count(*) FROM operations').fetchone()[0]
        self.fixture.tasks.enter_maintenance()
        self.assertEqual(self.send().status_code, 503)
        with self.fixture.journal.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM operations').fetchone()[0], before)
        self.fixture.sim.execute.assert_not_called()

    def test_journal_trigger_binding_uses_original_schema_and_freezes_project(self):
        from agent_line.store import Conflict
        self.assertEqual(self.send().status_code, 202)
        reopened = SimJournal(self.fixture.journal.path)
        reopened.reserve_trigger_binding('installation', 'hook', self.config['hook'])
        with self.assertRaises(Conflict):
            reopened.reserve_trigger_binding('installation', 'hook', {**self.config['hook'], 'projectId': 'two'})
        with self.fixture.journal.connect() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM operations WHERE context='author-trigger-binding'").fetchone()[0], 1)
