"""Distinct native pause points use the existing durable task identities."""
import copy
import json
import unittest
from unittest.mock import Mock

from agent_line.adapters.sim_wait import WaitInputUnavailable
from tests import test_task_discovery as fixtures


class ParallelTaskDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TaskDiscoveryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        with self.fixture.runs.connect() as db:
            snapshot = json.loads(db.execute('SELECT snapshot FROM runs').fetchone()[0])
            snapshot['staticParallel'] = True
            db.execute('UPDATE runs SET snapshot=?', (json.dumps(snapshot),))
        self.sim, self.wait = self.fixture.sim, self.fixture.wait
        self.sim.points = 2
        self.points = [dict(contextId=block, blockId=block, pauseKind='human',
                            resumeStatus='paused', snapshotReady=True)
                       for block in ('wait-one', 'wait-two')]
        self.projection = dict(workflowId='workflow', runId='run', totalPauseCount=2,
                               resumedCount=0, pausePoints=self.points)
        self.wait.read_pauses = lambda workflow, run: copy.deepcopy(self.projection)
        original = self.wait.read_input
        self.input_calls = []
        def read_input(workflow, run, block, **kwargs):
            self.input_calls.append((block, kwargs))
            value = original(workflow, run, block)
            value['input']['branch'] = block
            return value
        self.wait.read_input = read_input

    def test_registers_both_branches_with_distinct_immutable_identity(self):
        result = self.fixture.discovery.tick()[0]
        self.assertEqual(result['status'], 'external_task_bound')
        self.assertEqual(len(result['tasks']), 2)
        self.assertEqual(self.fixture.count(), 2)
        tasks = [self.fixture.tasks.get(t['taskId']) for t in result['tasks']]
        self.assertEqual(len({t['id'] for t in tasks}), 2)
        self.assertEqual({t['payload']['workflow']['nodeExecutionId'] for t in tasks},
                         {'wait-one', 'wait-two'})
        for task in tasks:
            self.assertEqual(task['payload']['input']['taskInput']['branch'],
                             task['payload']['workflow']['nodeId'])
            self.assertEqual(self.fixture.pauses.get(task['id']).context_id,
                             task['payload']['workflow']['nodeId'])
            self.assertIs(self.fixture.pauses.get(task['id']).allow_multiple, True)
        self.assertEqual(self.input_calls,
                         [('wait-one', {'allow_multiple': True}), ('wait-two', {'allow_multiple': True})])
        self.sim.execute.assert_not_called(); self.sim.resume.assert_not_called()

    def test_rebuild_reuses_each_branch_without_new_task(self):
        first = self.fixture.discovery.tick()[0]
        rebuilt = self.fixture.reopen().tick()[0]
        self.assertEqual([t['taskId'] for t in first['tasks']], [t['taskId'] for t in rebuilt['tasks']])
        self.assertTrue(all(t['reusedExistingTask'] for t in rebuilt['tasks']))
        self.assertEqual(self.fixture.count(), 2)

    def test_last_remaining_branch_retains_frozen_multiple_policy(self):
        first = self.fixture.discovery.tick()[0]
        self.sim.resumed = 1
        self.projection['resumedCount'] = 1
        self.points[1]['resumeStatus'] = 'resumed'
        last = self.fixture.reopen().tick()[0]
        self.assertEqual(last['taskId'], first['tasks'][0]['taskId'])
        self.assertIs(self.fixture.pauses.get(last['taskId']).allow_multiple, True)
        self.assertEqual(self.input_calls[-1], ('wait-one', {'allow_multiple': True}))
        self.assertEqual(self.fixture.count(), 2)

    def terminal_summary(self):
        journal = Mock()
        journal.resume_summary.return_value = {
            'latestAttempt': {'status': 'accepted', 'contextId': 'wait-two', 'runId': 'child'},
            'latestAccepted': {'status': 'accepted', 'contextId': 'wait-two', 'runId': 'child'}}
        self.fixture.discovery.journal = journal
        return journal

    def test_failed_final_child_closes_original_run_only_after_all_native_points_settle(self):
        from probes.sim.compiled_parallel_probe import inject_synthetic
        self.fixture.runs.record_submission('workflow', 'run', 'accepted')
        first = self.fixture.discovery.tick()[0]
        for branch, item in enumerate(first['tasks']):
            inject_synthetic(self.fixture.tasks, item['taskId'], branch=str(branch),
                             nonce='synthetic-only', verification_passed=True)
        journal = self.terminal_summary()
        journal.get.return_value = {'status': 'accepted'}
        def original_result(workflow, run, context):
            binding = self.fixture.pauses.for_context(workflow, run, context)
            task = self.fixture.tasks.get(binding.task_id)
            return {'taskId': task['id'], **{k: task[k] for k in
                    ('executionStatus', 'verificationStatus', 'executionIdentity', 'output', 'evidence')}}
        journal.resume_input.side_effect = original_result
        self.points[0]['resumeStatus'] = 'resumed'
        self.points[1]['resumeStatus'] = 'failed'
        self.projection['resumedCount'] = self.sim.resumed = 1
        original = self.sim.observe
        def observe(workflow, run):
            if run == 'child':
                return {'response': {'data': dict(workflowId=workflow, runId=run, status='failed')}}
            return original(workflow, run)
        self.sim.observe = observe
        result = self.fixture.discovery.tick()[0]
        self.assertEqual(result['status'], 'terminal_observed')
        self.assertEqual(result['terminalRunId'], 'child')
        with self.fixture.runs.connect() as db:
            self.assertEqual(db.execute('SELECT complete FROM runs').fetchone()[0], 1)
        self.assertEqual(self.fixture.count(), 2)
        self.sim.resume.assert_not_called()

    def test_a_completed_resume_child_does_not_close_other_pending_branch(self):
        self.terminal_summary()
        self.points[1]['resumeStatus'] = 'resumed'
        self.projection['resumedCount'] = self.sim.resumed = 1
        result = self.fixture.discovery.tick()[0]
        self.assertEqual(result['status'], 'external_task_bound')
        with self.fixture.runs.connect() as db:
            self.assertEqual(db.execute('SELECT complete FROM runs').fetchone()[0], 0)

    def test_unknown_latest_resume_does_not_infer_terminal_from_an_older_child(self):
        journal = self.terminal_summary()
        journal.resume_summary.return_value['latestAttempt']['status'] = 'uncertain'
        self.points[0]['resumeStatus'] = self.points[1]['resumeStatus'] = 'resumed'
        self.projection['resumedCount'] = self.sim.resumed = 2
        self.sim.block = None
        self.assertNotEqual(self.fixture.discovery.tick()[0]['status'], 'terminal_observed')
        with self.fixture.runs.connect() as db:
            self.assertEqual(db.execute('SELECT complete FROM runs').fetchone()[0], 0)

    def test_unproven_version_does_not_read_or_register_any_input(self):
        self.fixture.verifier.verify_run.return_value = False
        self.assertEqual(self.fixture.discovery.tick()[0]['status'], 'version_unverified')
        self.assertEqual(self.fixture.count(), 0)
        self.assertEqual(self.input_calls, [])

    def test_invalid_projection_cannot_authorize_a_partial_subset(self):
        changes = [lambda d: d.update(runId='other'), lambda d: d.update(workflowId='other'),
                   lambda d: d.update(resumedCount=1), lambda d: d.update(totalPauseCount=True),
                   lambda d: d['pausePoints'][1].update(blockId='unknown', contextId='unknown'),
                   lambda d: d['pausePoints'].__setitem__(1, copy.deepcopy(d['pausePoints'][0])),
                   lambda d: d['pausePoints'][0].update(contextId='wait-one_loop0'),
                   lambda d: d['pausePoints'][0].update(pauseKind='time')]
        for change in changes:
            saved = copy.deepcopy(self.projection); change(self.projection)
            with self.subTest(change=change):
                self.assertEqual(self.fixture.discovery.tick()[0]['status'], 'external_task_input_unavailable')
                self.assertEqual(self.fixture.count(), 0)
            self.projection = saved

    def test_native_resuming_failed_or_not_ready_is_never_registered(self):
        self.points[0]['resumeStatus'] = 'resuming'
        result = self.fixture.discovery.tick()[0]
        self.assertEqual(len(result['tasks']), 1)
        self.assertEqual(self.fixture.pauses.get(result['tasks'][0]['taskId']).block_id, 'wait-two')
        self.assertEqual(self.fixture.count(), 1)
        self.points[0]['resumeStatus'] = 'failed'
        self.points[1]['snapshotReady'] = False
        self.assertEqual(self.fixture.discovery.tick()[0]['status'], 'waiting')
        self.assertEqual(self.fixture.count(), 1)

    def test_unavailable_read_and_admission_do_not_send_native_mutations(self):
        self.wait.read_pauses = lambda *args: (_ for _ in ()).throw(WaitInputUnavailable('unavailable'))
        self.assertEqual(self.fixture.discovery.tick()[0]['status'], 'external_task_input_unavailable')
        self.assertEqual(self.fixture.count(), 0)
        self.sim.execute.assert_not_called(); self.sim.resume.assert_not_called()

    def test_one_bad_input_preserves_other_registered_branch_for_checked_retry(self):
        original = self.wait.read_input
        def read(workflow, run, block, **kwargs):
            if block == 'wait-two':
                raise WaitInputUnavailable('hidden')
            return original(workflow, run, block, **kwargs)
        self.wait.read_input = read
        result = self.fixture.discovery.tick()[0]
        self.assertEqual(result['status'], 'external_task_input_unavailable')
        self.assertEqual(self.fixture.count(), 1)
        first = result['tasks'][0]['taskId']
        self.wait.read_input = original
        recovered = self.fixture.reopen().tick()[0]
        self.assertEqual(recovered['tasks'][0]['taskId'], first)
        self.assertTrue(recovered['tasks'][0]['reusedExistingTask'])
        self.assertEqual(self.fixture.count(), 2)

    def test_independent_human_pause_is_not_bound_as_machine_task(self):
        self.points[1].update(blockId='human', contextId='human')
        with self.fixture.runs.connect() as db:
            snapshot = json.loads(db.execute('SELECT snapshot FROM runs').fetchone()[0])
            snapshot['approvalBlockIds'] = ['human']
            snapshot['humanBlockIds'].append('human')
            db.execute('UPDATE runs SET snapshot=?', (json.dumps(snapshot),))
        result = self.fixture.discovery.tick()[0]
        self.assertEqual(result['status'], 'external_task_bound')
        self.assertEqual(len(result['tasks']), 1)
        self.assertEqual(self.fixture.count(), 1)
        self.assertEqual(self.fixture.pauses.get(result['tasks'][0]['taskId']).block_id, 'wait-one')

    def test_legacy_snapshot_does_not_implicitly_enable_multiple_tasks(self):
        with self.fixture.runs.connect() as db:
            snapshot = json.loads(db.execute('SELECT snapshot FROM runs').fetchone()[0])
            snapshot.pop('staticParallel')
            db.execute('UPDATE runs SET snapshot=?', (json.dumps(snapshot),))
        original = self.wait.read_input
        def legacy(workflow, run, block, **kwargs):
            if not kwargs.get('allow_multiple'):
                raise WaitInputUnavailable('pause_unsupported')
            return original(workflow, run, block, **kwargs)
        self.wait.read_input = legacy
        self.assertEqual(self.fixture.discovery.tick()[0]['status'], 'external_task_input_unavailable')
        self.assertEqual(self.fixture.count(), 0)
