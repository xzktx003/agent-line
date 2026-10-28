"""Bounded native loop activations require durable prior-round evidence."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from agent_line.approval_discovery import WorkflowRunLedger
from agent_line.completion_relay import CompletionRelay
from agent_line.pause_bindings import PauseBindings
from agent_line.sim_journal import SimJournal
from agent_line.store import TaskStore
from agent_line.task_discovery import ExternalTaskDiscovery
from agent_line.adapters.sim import SubmissionUncertain


class LoopTaskDiscoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.tasks = TaskStore.initialize(self.root/'tasks.sqlite')
        self.runs = WorkflowRunLedger.initialize(self.root/'runs.sqlite')
        self.pauses = PauseBindings.initialize(self.root/'pauses.sqlite')
        self.journal = SimJournal.initialize(self.root/'journal.sqlite')
        self.sim, self.wait, self.verifier = Mock(), Mock(), Mock()
        self.verifier.verify_run.return_value = True
        self.projects = {'project': {'workspace': str(self.root)}}
        self.roles = {'role': {'projectId': 'project', 'roleAgentId': 'parent'}}
        self.policy = {'roleBindingId': 'role', 'roleAgentId': 'parent', 'instruction': 'Repair carefully',
            'memoryPolicyId': 'native-role', 'outputSchemaId': 'task-result',
            'requireMemoryCommit': False, 'allowFailureBranch': True}
        self.reserve()
        self.iteration = 0; self.context = None; self.input_override = None
        self.sim.observe.side_effect = self.observe
        self.wait.read_input.side_effect = self.read_input
        self.discovery = self.reopen()

    def reserve(self, *, loop=True, policies=None):
        kwargs = {'loop_policies': {'body': {'loopId': 'loop', 'maxIterations': 3}}} if loop else {}
        if policies is not None: kwargs['loop_policies'] = policies
        self.runs.reserve_tasks(project_id='project', workflow_id='workflow', run_id='run',
            revision='1', deployment_version_id='deployment', agent_policies={'body': self.policy}, **kwargs)

    def reopen(self, **changes):
        kwargs = dict(publication_verifier=self.verifier, projects=self.projects, role_bindings=self.roles,
                      allow_submissions=True, journal=SimJournal(self.journal.path))
        kwargs.update(changes)
        return ExternalTaskDiscovery(WorkflowRunLedger(self.runs.path), TaskStore(self.tasks.path),
            PauseBindings(self.pauses.path), self.sim, self.wait, **kwargs)

    def observe(self, workflow, run):
        return {'status': 'paused', 'response': {'data': {'workflowId': workflow, 'runId': run,
            'status': 'paused', 'paused': {'blockedOnBlockId': 'body',
            'contextId': self.context or 'body_loop'+str(self.iteration), 'pauseKind': 'human',
            'pausePointCount': self.iteration+1, 'resumedCount': self.iteration}}}}

    def read_input(self, workflow, run, block, *, context_id=None):
        return {'input': self.input_override or {'iteration': self.iteration}, 'run': {
            'workflowId': workflow, 'runId': run, 'blockId': block,
            'contextId': self.context or 'body_loop'+str(self.iteration), 'status': 'paused'}}

    def tick(self): return self.discovery.tick()[0]

    def count(self):
        with self.tasks.connect() as db: return db.execute('SELECT count(*) FROM tasks').fetchone()[0]

    def bound(self):
        result = self.tick(); self.assertEqual(result['status'], 'external_task_bound', result)
        return result['taskId']

    def finish(self, task, *, release=True, verification='failed', execution='succeeded'):
        identity = {'upstreamTaskId': 'native-'+task, 'codexThreadId': 'thread-'+task, 'codexTurnId': 'turn-'+task}
        self.tasks.claim(task, str(self.root)); self.tasks.accept(task, identity)
        self.tasks.finish(task, execution, {'text': 'Synthetic round output: '+task,
            'nativeStatus': 'task_complete', 'toolEvidence': []}, [
            {'kind': 'execution_terminal', 'executionId': identity['codexTurnId'], 'confirmed': True}])
        if release:
            self.tasks.confirm_resources_released(task, identity['codexTurnId'])
            if verification and execution == 'succeeded':
                self.tasks.record_verification(task, {'kind': 'verification', 'taskId': task,
                    'executionIdentity': identity, 'status': verification,
                    'checks': [{'status': verification, 'detail': 'independent fixture check'}]})
        return self.tasks.get(task)

    def resume(self, task, *, uncertain=False, receipt_changes=None):
        binding = self.pauses.get(task)
        remote = Mock()
        if uncertain:
            remote.resume.side_effect = SubmissionUncertain('run', binding.context_id)
        else:
            remote.resume.return_value = {'workflowId': 'workflow', 'runId': 'run',
                'contextId': binding.context_id, 'resumedRunId': 'child-'+binding.context_id,
                'transportStatus': 'accepted', **(receipt_changes or {})}
        previous = self.tasks.get(task)
        value = {'taskId': task, **{key: previous[key] for key in ('executionStatus', 'verificationStatus', 'memoryStatus', 'executionIdentity', 'output', 'evidence')}}
        return self.journal.resume(remote, 'workflow', 'run', binding.context_id, value)

    def test_three_rounds_distinct_tasks_repeat_deduplicates_and_real_feedback(self):
        ids = []; previous = None
        for iteration in range(3):
            self.iteration = iteration
            task = self.bound(); ids.append(task)
            again = self.tick(); self.assertEqual(again['taskId'], task); self.assertTrue(again['reusedExistingTask'])
            payload = self.tasks.get(task)['payload']; context = 'body_loop'+str(iteration)
            self.assertEqual(payload['workflow']['nodeExecutionId'], context)
            self.assertEqual(payload['workflow']['attemptId'], str(iteration+1))
            self.assertEqual(self.pauses.get(task).context_id, context)
            repair = payload['input']['repair']
            self.assertEqual(repair['iteration'], iteration); self.assertEqual(repair['maxIterations'], 3)
            if previous is None: self.assertIsNone(repair['previousResult'])
            else:
                feedback = repair['previousResult']
                self.assertEqual(feedback['taskId'], previous['id'])
                for field in ('executionStatus', 'verificationStatus', 'executionIdentity', 'output', 'evidence'):
                    self.assertEqual(feedback[field], previous[field])
            self.wait.read_input.assert_called_with('workflow', 'run', 'body', context_id=context)
            previous = self.finish(task)
            self.resume(task)
            self.discovery = self.reopen()
        self.assertEqual(len(set(ids)), 3); self.assertEqual(self.count(), 3)
        self.sim.execute.assert_not_called(); self.sim.resume.assert_not_called()

    def test_changed_input_in_same_activation_conflicts(self):
        task = self.bound(); self.input_override = {'iteration': 0, 'changed': True}
        self.assertEqual(self.tick()['status'], 'external_task_conflict')
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.tasks.get(task)['payload']['input']['taskInput'], {'iteration': 0})

    def test_missing_previous_task_jump_and_bad_context_never_register(self):
        for context in ('body_loop1', 'body_loop2', 'body_loop3', 'body_loop-1', 'body_loop01',
                        'other_loop0', 'body', 'body_loopx'):
            self.context = context
            with self.subTest(context=context):
                self.assertNotEqual(self.tick()['status'], 'external_task_bound')
                self.assertEqual(self.count(), 0)

    def test_no_previous_resume_even_finished_task_cannot_authorize_next_round(self):
        task = self.bound(); self.finish(task); self.iteration = 1
        self.assertNotEqual(self.tick()['status'], 'external_task_bound'); self.assertEqual(self.count(), 1)

    def test_uncertain_previous_resume_cannot_authorize_next_round(self):
        task = self.bound(); self.finish(task); self.resume(task, uncertain=True); self.iteration = 1
        self.assertNotEqual(self.tick()['status'], 'external_task_bound'); self.assertEqual(self.count(), 1)

    def test_unreleased_previous_task_cannot_authorize_next_round(self):
        task = self.bound(); self.finish(task, release=False); self.resume(task); self.iteration = 1
        self.assertNotEqual(self.tick()['status'], 'external_task_bound'); self.assertEqual(self.count(), 1)

    def test_unverified_success_cannot_authorize_next_round(self):
        task = self.bound(); self.finish(task, verification=None); self.resume(task); self.iteration = 1
        self.assertNotEqual(self.tick()['status'], 'external_task_bound'); self.assertEqual(self.count(), 1)

    def test_publication_admission_and_maintenance_remain_required(self):
        self.verifier.verify_run.return_value = False
        self.assertEqual(self.tick()['status'], 'version_unverified'); self.assertEqual(self.count(), 0)
        self.verifier.verify_run.return_value = True; self.discovery.allow_submissions = False
        self.assertNotEqual(self.tick()['status'], 'external_task_bound'); self.assertEqual(self.count(), 0)
        self.discovery.allow_submissions = True; self.tasks.enter_maintenance()
        self.assertNotEqual(self.tick()['status'], 'external_task_bound'); self.assertEqual(self.count(), 0)

    def test_missing_journal_cannot_authorize_loop(self):
        self.discovery = self.reopen(journal=None)
        self.assertNotEqual(self.tick()['status'], 'external_task_bound'); self.assertEqual(self.count(), 0)

    def test_no_loop_policy_retains_default_refusal(self):
        with self.runs.connect() as db: db.execute('DELETE FROM runs')
        self.reserve(loop=False)
        self.assertNotEqual(self.tick()['status'], 'external_task_bound'); self.assertEqual(self.count(), 0)

    def test_old_completed_task_cannot_resume_current_activation(self):
        task = self.bound(); complete = self.finish(task, verification='passed')
        self.iteration = 1
        relay = CompletionRelay(self.sim, self.journal)
        result = relay.deliver(complete, self.pauses.get(task))
        self.assertEqual(result['status'], 'waiting')
        self.sim.resume.assert_not_called()
        with self.assertRaises(KeyError): self.journal.get('workflow', 'run', 'body_loop0')

    def test_previous_receipt_must_bind_exact_workflow_root_context_and_child(self):
        task = self.bound(); self.finish(task); self.iteration = 1
        for changes in ({'workflowId': 'other'}, {'runId': 'other'}, {'contextId': 'body_loop9'},
                        {'resumedRunId': None}, {'resumedRunId': '../child'},
                        {'workflowId': None}, {'runId': None}, {'contextId': None}):
            with self.journal.connect() as db:
                db.execute("DELETE FROM operations WHERE context='body_loop0'")
            self.resume(task, receipt_changes=changes)
            with self.subTest(changes=changes):
                self.assertNotEqual(self.tick()['status'], 'external_task_bound')
                self.assertEqual(self.count(), 1)

    def test_terminal_failure_feedback_requires_explicit_failure_branch(self):
        task = self.bound(); previous = self.finish(task, execution='failed')
        self.resume(task); self.iteration = 1
        second = self.bound()
        feedback = self.tasks.get(second)['payload']['input']['repair']['previousResult']
        self.assertEqual(feedback['executionStatus'], 'failed')
        self.assertEqual(feedback['evidence'], previous['evidence'])

    def test_reader_response_from_another_activation_is_rejected(self):
        self.wait.read_input.side_effect = None
        self.wait.read_input.return_value = {'input': {'iteration': 1}, 'run': {
            'workflowId': 'workflow', 'runId': 'run', 'blockId': 'body',
            'contextId': 'body_loop1', 'status': 'paused'}}
        self.assertNotEqual(self.tick()['status'], 'external_task_bound'); self.assertEqual(self.count(), 0)

    def test_loop_policy_bounds_and_exact_body_are_validated(self):
        for policy in ({'body': {'loopId': 'loop', 'maxIterations': 0}},
                       {'body': {'loopId': 'loop', 'maxIterations': 4}},
                       {'body': {'loopId': 'loop', 'maxIterations': True}},
                       {'body': {'loopId': 'loop', 'maxIterations': '3'}},
                       {'other': {'loopId': 'loop', 'maxIterations': 3}},
                       {'body': {'loopId': '../loop', 'maxIterations': 3}}):
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                self.reserve(policies=policy)

    def test_terminal_failure_without_branch_permission_cannot_register_next(self):
        with self.runs.connect() as db: db.execute('DELETE FROM runs')
        self.policy['allowFailureBranch'] = False
        with self.assertRaises(ValueError): self.reserve()
        self.assertEqual(self.count(), 0)
        self.sim.execute.assert_not_called()


    def test_passed_round_is_not_repair_authorization(self):
        task = self.bound(); self.finish(task, verification='passed'); self.resume(task)
        self.iteration = 1
        self.assertNotEqual(self.tick()['status'], 'external_task_bound'); self.assertEqual(self.count(), 1)

    def test_accepted_resume_with_forged_previous_result_cannot_authorize_next(self):
        task = self.bound(); self.finish(task); self.resume(task); self.iteration = 1
        import json
        with self.journal.connect() as db:
            row = db.execute("SELECT input FROM operations WHERE context='body_loop0'").fetchone()
            value = json.loads(row[0]); value['output'] = {'invented': 'not previous output'}
            db.execute("UPDATE operations SET input=? WHERE context='body_loop0'", (json.dumps(value),))
        self.assertNotEqual(self.tick()['status'], 'external_task_bound'); self.assertEqual(self.count(), 1)

    def test_resolved_iteration_must_be_exact_integer_matching_context(self):
        for value in (True, False, '0', 0.0, None, 1, -1):
            self.input_override = {'iteration': value}
            with self.subTest(value=value):
                self.assertNotEqual(self.tick()['status'], 'external_task_bound')
                self.assertEqual(self.count(), 0)
        self.input_override = {'missingIteration': True}
        self.assertNotEqual(self.tick()['status'], 'external_task_bound'); self.assertEqual(self.count(), 0)

    def test_later_memory_status_does_not_rewrite_frozen_repair_input_on_replay(self):
        first = self.bound(); self.finish(first); self.resume(first); self.iteration = 1
        second = self.bound()
        frozen = self.tasks.get(second)['payload']['input']['repair']['previousResult']
        self.tasks.set_memory_status(first, 'committed')
        self.discovery = self.reopen()
        replay = self.tick()
        self.assertEqual(replay['status'], 'external_task_bound', replay)
        self.assertTrue(replay['reusedExistingTask']); self.assertEqual(replay['taskId'], second)
        self.assertEqual(self.tasks.get(second)['payload']['input']['repair']['previousResult'], frozen)
        self.assertEqual(self.count(), 2)
