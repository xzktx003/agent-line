"""Authored static forks freeze capability before any native execute."""
import copy
import unittest
from unittest.mock import patch

from agent_line.authoring_api import state_hash
from agent_line.workflows import WorkflowGateway
from tests import test_loop_workflows as authoring
from tests import test_machine_workflow_gateway as machine
from tests import test_static_parallel_compiler as compiler


class StaticParallelWorkflowAPITests(unittest.TestCase):
    def setUp(self):
        self.f = authoring.LoopWorkflowTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)

    def publish(self):
        doc = compiler.document()
        for node in doc['nodes']:
            if node['type'] == 'agent':
                node['config'].update(roleBindingId='role', memoryPolicyId='native-role',
                                      outputSchemaId='task-result', requireMemoryCommit=False)
        workflow = self.f.edit('', {'operationId': 'create', 'name': '双分支'})['workflowId']
        self.f.edit('/'+workflow+'/save', {'operationId': 'save', 'document': doc,
            'expectedStateSha256': state_hash(self.f.remote.states[workflow])})
        receipt = self.f.edit('/'+workflow+'/publish', {'operationId': 'publish',
            'expectedStateSha256': state_hash(self.f.remote.states[workflow])})
        self.assertEqual(receipt['outcome'], 'published')
        return workflow, receipt

    def test_publish_and_start_freeze_capability_before_execute_and_replay_once(self):
        workflow, receipt = self.publish()
        self.assertIs(receipt['externalTasks']['staticParallel'], True)
        self.assertEqual(len(receipt['externalTasks']['agentPolicies']), 2)
        def execute(wid, rid, inputs):
            self.assertIs(self.f.runs.get(wid, rid)['staticParallel'], True)
            self.assertIs(self.f.journal.get_publication(wid, rid)['externalTasks']['staticParallel'], True)
            return {'accepted': True}
        self.f.sim.execute.side_effect = execute
        first = self.f.start(receipt['bindingId'])
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(self.f.start(receipt['bindingId']).json(), first.json())
        self.f.sim.execute.assert_called_once()
        self.f.verifier.check_definition.assert_called_once_with(workflow, '1', receipt['deploymentVersionId'],
            list(receipt['externalTasks']['agentPolicies']), require_active=True, static_parallel=True)

    def test_configured_static_flag_is_strict_and_cannot_mix_loop_capability(self):
        workflow, receipt = self.publish()
        policy = receipt['externalTasks']
        for value in (False, None, 'true', 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                WorkflowGateway(self.f.sim, self.f.journal, {'flow': dict(projectId='one',
                    workflowId=workflow, externalTasks={**policy, 'staticParallel': value})}, installation_id='test')


class ParallelContinueTests(unittest.TestCase):
    def setUp(self):
        self.f = machine.MachineWorkflowGatewayTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.addCleanup(self.f.client.close)
        policy = self.f.gateway.bindings['flow']['externalTasks']
        policy['staticParallel'] = True
        policy['agentPolicies']['other'] = copy.deepcopy(policy['agentPolicies']['wait'])
        start = self.f.start(); self.assertEqual(start.status_code, 202, start.text)
        self.run = start.json()['runId']; self.url = self.f.url+'/'+self.run
        self.f.gateway.publication_verifier.verify_run.return_value = True
        self.f.sim.observe.side_effect = lambda w,r: {'response': {'data': dict(workflowId=w, runId=r,
            status='paused', paused=dict(contextId='wait', blockedOnBlockId='wait', pauseKind='human',
                                        pausePointCount=2, resumedCount=0))}}
        self.detail = dict(workflowId='workflow', runId=self.run, totalPauseCount=2, resumedCount=0,
            pausePoints=[dict(contextId=b, blockId=b, resumeStatus='paused', snapshotReady=True,
                              pauseKind='human') for b in ('wait', 'other')])

    def command(self, action, op, version):
        body = dict(action=action, operationId=op, expectedVersion=version)
        if action == 'pause':
            body['activeExecution'] = 'continue'
        return self.f.client.post(self.url+'/control', headers=self.f.headers, json=body)

    def test_checked_continue_accepts_two_ready_pauses_without_native_resume(self):
        self.assertEqual(self.command('pause', 'pause', 0).status_code, 200)
        with patch('agent_line.adapters.sim_wait.SimWaitClient.read_pauses', return_value=self.detail):
            response = self.command('continue', 'continue', 1)
        self.assertEqual(response.status_code, 200, response.text)
        self.f.sim.resume.assert_not_called(); self.f.sim.execute.assert_called_once()

    def test_unknown_native_branch_state_prevents_continue(self):
        self.assertEqual(self.command('pause', 'pause', 0).status_code, 200)
        self.detail['pausePoints'][1]['resumeStatus'] = 'resuming'
        with patch('agent_line.adapters.sim_wait.SimWaitClient.read_pauses', return_value=self.detail):
            self.assertEqual(self.command('continue', 'continue', 1).status_code, 409)
        self.f.sim.resume.assert_not_called()
