"""Scoped run controls use real journals and frozen machine-run policies."""
import unittest

from fastapi.testclient import TestClient
from agent_line.adapters.sim import SubmissionUncertain
from agent_line.dispatcher import Dispatcher
from agent_line.service import ServiceConfig, create_app
from tests import test_machine_workflow_gateway as fixtures


class RunControlAPITests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.MachineWorkflowGatewayTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.f.sim.cancel.return_value = {'transportStatus': 'accepted', 'verifiedStopped': False}
        self.started = self.f.start(); self.assertEqual(self.started.status_code, 202)
        self.run = self.started.json()['runId']
        self.url = self.f.url+'/'+self.run
        self.control = self.f.app.state.run_control
        self.f.sim.observe.side_effect = lambda wid, rid: {'status': 'paused', 'response': {'data': {
            'workflowId': wid, 'runId': rid, 'status': 'paused', 'paused': {
                'contextId': 'wait', 'blockedOnBlockId': 'wait', 'pauseKind': 'human',
                'pausePointCount': 2, 'resumedCount': 1}}}}
        self.f.gateway.publication_verifier.verify_run.return_value = True

    def command(self, action, op='command', version=0, client=None, body_extra=None, include_pause_choice=True):
        body = {'action': action, 'operationId': op, 'expectedVersion': version}
        if action == 'pause' and include_pause_choice:
            body['activeExecution'] = 'continue'
        if body_extra:
            body.update(body_extra)
        return (client or self.f.client).post(self.url+'/control', headers=self.f.headers, json=body)

    def task(self, key='task'):
        return self.f.tasks.register(key, {'projectId': 'project', 'roleAgentId': 'parent',
            'roleBindingId': 'role', 'workspace': str(self.f.root), 'input': {},
            'workflow': {'id': 'workflow', 'revision': '1', 'runId': self.run,
                         'nodeId': key, 'nodeExecutionId': key, 'attemptId': '1'}})[0]

    def test_pause_query_and_checked_continue_preserve_original_run(self):
        paused = self.command('pause'); self.assertEqual(paused.status_code, 200, paused.text)
        self.assertEqual(paused.json()['control']['state'], 'paused')
        self.assertEqual(paused.json()['control']['activeExecution'], 'continue')
        self.assertEqual(paused.json()['control']['version'], 1)
        view = self.f.client.get(self.url, headers=self.f.headers)
        self.assertEqual(view.json()['control']['state'], 'paused')
        self.assertEqual(view.json()['execution']['status'], 'paused')
        continued = self.command('continue', 'continue', 1)
        self.assertEqual(continued.status_code, 200, continued.text)
        self.assertEqual(continued.json()['control']['state'], 'active')
        self.f.sim.execute.assert_called_once(); self.f.sim.resume.assert_not_called()

    def test_version_conflict_or_changed_operation_never_changes_state(self):
        self.assertEqual(self.command('pause').status_code, 200)
        self.assertEqual(self.command('continue', 'new', 0).status_code, 409)
        self.assertEqual(self.command('stop', 'command', 0).status_code, 409)
        self.assertEqual(self.control.status('workflow', self.run)['state'], 'paused')
        self.f.sim.cancel.assert_not_called()

    def test_auth_and_unknown_run_do_not_create_control_history(self):
        payload = {'action': 'stop', 'operationId': 'stop', 'expectedVersion': 0}
        self.assertEqual(self.f.client.post(self.url+'/control', json=payload).status_code, 401)
        self.assertEqual(self.f.client.post(self.f.url+'/unknown/control', json=payload,
            headers=self.f.headers).status_code, 404)
        self.assertEqual(self.control.status('workflow', self.run)['version'], 0)

    def test_stop_ack_keeps_active_executor_unreleased_and_replays_once(self):
        task = self.task(); self.f.tasks.claim(task['id'], str(self.f.root))
        self.f.tasks.accept(task['id'], {'upstreamTaskId': 'native'})
        first = self.command('stop'); self.assertEqual(first.status_code, 200, first.text)
        counts = first.json()['control']['taskCounts']
        self.assertEqual(first.json()['control']['state'], 'stop_requested')
        self.assertEqual(counts['active'], 1); self.assertEqual(counts['resourcesUnreleased'], 1)
        self.assertIs(first.json()['cancelSubmission']['verifiedStopped'], False)
        self.assertEqual(self.f.tasks.get(task['id'])['executionStatus'], 'cancel_requested')
        self.assertEqual(self.command('stop').json(), first.json())
        self.f.sim.cancel.assert_called_once_with('workflow', self.run)
        self.assertEqual(self.command('continue', 'continue', 1).status_code, 409)

    def test_unknown_cancel_receipt_is_not_reposted_on_same_stop(self):
        self.f.sim.cancel.side_effect = SubmissionUncertain(self.run)
        self.assertEqual(self.command('stop').json()['cancelSubmission']['status'], 'uncertain')
        self.assertEqual(self.command('stop').json()['cancelSubmission']['status'], 'uncertain')
        self.f.sim.cancel.assert_called_once()

    def test_continue_requires_actual_pause_and_exact_publication(self):
        self.assertEqual(self.command('pause').status_code, 200)
        self.f.gateway.publication_verifier.verify_run.return_value = False
        self.assertEqual(self.command('continue', 'continue', 1).status_code, 409)
        self.f.gateway.publication_verifier.verify_run.return_value = True
        original = self.f.sim.observe.side_effect
        self.f.sim.observe.side_effect = lambda w,r: {'status': 'unknown'}
        self.assertEqual(self.command('continue', 'continue', 1).status_code, 409)
        self.f.sim.observe.side_effect = original
        self.assertEqual(self.command('continue', 'continue', 1).status_code, 200)

    def test_unknown_prior_resume_prevents_continue_without_reposting(self):
        self.f.sim.resume.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.f.journal.resume(self.f.sim, 'workflow', self.run, 'old-wait', {})
        self.assertEqual(self.command('pause').status_code, 200)
        self.assertEqual(self.command('continue', 'continue', 1).status_code, 409)
        self.f.sim.resume.assert_called_once()

    def test_unknown_active_native_task_prevents_continue(self):
        from unittest.mock import Mock
        task = self.task(); self.f.tasks.claim(task['id'], str(self.f.root))
        self.f.tasks.accept(task['id'], {'upstreamTaskId': 'native'})
        gateway = Mock(); gateway.observe.return_value = {'status': 'unknown'}
        self.f.gateway.dispatcher = Dispatcher(self.f.tasks, {'project': gateway}, run_control=self.control)
        self.assertEqual(self.command('pause').status_code, 200)
        self.assertEqual(self.command('continue', 'continue', 1).status_code, 409)
        self.assertEqual(self.control.status('workflow', self.run)['state'], 'paused')

    def test_control_is_preserved_when_service_is_reassembled(self):
        self.assertEqual(self.command('pause').status_code, 200)
        app = create_app(self.f.tasks, ServiceConfig(tokens={'key': {'project'}},
            projects={'project': {'workspace': str(self.f.root)}}, bindings={}),
            workflows=self.f.gateway, task_flow=self.f.flow)
        with TestClient(app) as client:
            self.assertEqual(client.get(self.url, headers=self.f.headers).json()['control']['state'], 'paused')
            self.assertEqual(self.command('continue', 'continue', 1, client).status_code, 200)

    def test_public_task_endpoint_does_not_bypass_run_pause(self):
        config = ServiceConfig(tokens={'key': {'project'}}, projects={'project': {'workspace': str(self.f.root)}},
            bindings={'role': {'projectId': 'project', 'roleAgentId': 'parent'}}, controlled_execution='confirmed')
        client = TestClient(create_app(self.f.tasks, config, workflows=self.f.gateway, task_flow=self.f.flow))
        self.addCleanup(client.close)
        payload = {'schemaVersion': '1.0', 'idempotencyKey': 'manual', 'projectId': 'project',
            'roleBindingId': 'role', 'input': {}, 'workflow': {'id': 'workflow', 'runId': self.run,
                'revision': '1', 'nodeId': 'manual', 'nodeExecutionId': 'manual', 'attemptId': '1'},
            'memoryPolicyId': 'native-role', 'outputSchemaId': 'task-result'}
        old = client.post('/extension/v1/node-tasks', headers=self.f.headers, json=payload)
        self.assertEqual(old.status_code, 202, old.text)
        self.assertEqual(self.command('pause').status_code, 200)
        replay = client.post('/extension/v1/node-tasks', headers=self.f.headers, json=payload)
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(replay.json()['taskId'], old.json()['taskId'])
        payload['idempotencyKey'] = 'other'; payload['workflow']['nodeExecutionId'] = 'other'
        response = client.post('/extension/v1/node-tasks', headers=self.f.headers, json=payload)
        self.assertEqual(response.status_code, 409, response.text)

    def test_pause_choice_is_explicit_and_interrupt_does_not_claim_stop(self):
        missing = self.command('pause', include_pause_choice=False)
        self.assertEqual(missing.status_code, 422, missing.text)
        misplaced = self.command('stop', 'stop-choice', body_extra={'activeExecution': 'interrupt'})
        self.assertEqual(misplaced.status_code, 422, misplaced.text)
        active = self.task('active')
        self.assertTrue(self.f.tasks.claim(active['id'], str(self.f.root)))
        self.f.tasks.accept(active['id'], {'upstreamTaskId': 'native'})
        queued = self.task('queued')
        paused = self.command('pause', body_extra={'activeExecution': 'interrupt'})
        self.assertEqual(paused.status_code, 200, paused.text)
        self.assertEqual(paused.json()['control']['state'], 'paused')
        self.assertEqual(paused.json()['control']['activeExecution'], 'interrupt')
        self.assertNotIn('cancelSubmission', paused.json())
        self.assertEqual(self.f.tasks.get(queued['id'])['executionStatus'], 'registered')
        active_task = self.f.tasks.get(active['id'])
        self.assertEqual(active_task['executionStatus'], 'cancel_requested')
        self.assertFalse(active_task['resourcesReleased'])
        replay = self.command('pause', body_extra={'activeExecution': 'interrupt'})
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(replay.json()['operation'], paused.json()['operation'])
        self.f.sim.cancel.assert_not_called()
        self.assertEqual(self.control.status('workflow', self.run)['state'], 'paused')

    def test_recorded_waiting_approval_blocks_continue_until_decided(self):
        self.assertEqual(self.command('pause').status_code, 200)
        finished = self.task('finished')
        self.assertTrue(self.f.tasks.claim(finished['id'], str(self.f.root)))
        self.f.tasks.accept(finished['id'], {'upstreamTaskId': 'turn'})
        self.f.tasks.finish(finished['id'], 'succeeded', {'ok': True}, [
            {'kind': 'execution_terminal', 'executionId': 'turn', 'confirmed': True}])
        self.f.tasks.confirm_resources_released(finished['id'], 'turn')
        queued = self.task('queued')
        self.f.tasks.note_approval_created(finished['id'], 'request-1', status='waiting')
        self.f.tasks.note_approval_created(queued['id'], 'request-old', status='expired')
        blocked = self.command('continue', 'continue-waiting', 1)
        self.assertEqual(blocked.status_code, 409, blocked.text)
        self.assertEqual(self.control.status('workflow', self.run)['state'], 'paused')
        self.assertEqual(self.f.tasks.get(queued['id'])['executionStatus'], 'registered')
        self.f.tasks.note_approval_decided(finished['id'], 'request-1', 'decision-1', 'approve')
        continued = self.command('continue', 'continue-decided', 1)
        self.assertEqual(continued.status_code, 200, continued.text)
        self.assertEqual(continued.json()['control']['state'], 'active')

