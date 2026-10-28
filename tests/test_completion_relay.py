import unittest
from dataclasses import replace
from unittest.mock import Mock
from agent_line.completion_relay import CompletionRelay, ResumeBinding


class CompletionRelayTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.journal = Mock()
        self.journal.get.side_effect = KeyError
        self.binding = ResumeBinding(
            task_id="task",
            workflow_id="workflow",
            run_id="run",
            block_id="wait",
            context_id="context",
            purpose="external_task_result",
        )
        self.task = {
            "id": "task",
            "payload": {"workflow": {"id": "workflow", "runId": "run",
                                     "nodeId": "wait", "nodeExecutionId": "context",
                                     "attemptId": "attempt-1"}},
            "executionStatus": "succeeded",
            "verificationStatus": "passed",
            "resourcesReleased": True,
            "memoryStatus": "committed",
            "executionIdentity": {"codexThreadId": "thread", "codexTurnId": "turn"},
            "output": {"ok": True},
            "evidence": [],
        }
        self.client.observe.return_value = {
            "status": "paused",
            "response": {
                "data": {
                    "runId": "run",
                    "workflowId": "workflow",
                    "status": "paused",
                    "paused": {
                        "contextId": "context",
                        "blockedOnBlockId": "wait",
                        "pauseKind": "human",
                        "pausePointCount": 1,
                    },
                }
            },
        }
        self.journal.resume.return_value = {"status": "accepted"}
        self.relay = CompletionRelay(self.client, self.journal)

    def test_verified_result_is_delivered_via_journal_not_graph_scheduling(self):
        result = self.relay.deliver(self.task, self.binding)
        self.assertEqual(result["status"], "accepted")
        args = self.journal.resume.call_args.args
        self.assertEqual(args[1:4], ("workflow", "run", "context"))
        self.assertEqual(args[4]["taskId"], "task")
        self.assertEqual(args[4]["nodeExecutionId"], "context")
        self.assertEqual(args[4]["attemptId"], "attempt-1")
        self.assertEqual(args[4]["verificationStatus"], "passed")
        self.client.execute.assert_not_called()

    def test_sequential_native_pause_counts_include_resumed_history(self):
        pause = self.client.observe.return_value['response']['data']['paused']
        pause.update(pausePointCount=2, resumedCount=1)
        self.assertEqual(self.relay.deliver(self.task, self.binding)['status'], 'accepted')

    def test_ambiguous_or_boolean_pause_counters_do_not_resume(self):
        pause = self.client.observe.return_value['response']['data']['paused']
        for total, resumed in [(2, 0), (2, 2), (True, 0), (2, True), (1, -1), (2, None)]:
            pause.update(pausePointCount=total, resumedCount=resumed)
            self.assertEqual(self.relay.deliver(self.task, self.binding)['status'], 'waiting')
        self.journal.resume.assert_not_called()

    def test_unverified_or_uncommitted_results_do_not_resume(self):
        for field, value in [
            ("verificationStatus", "pending"),
            ("resourcesReleased", False),
            ("memoryStatus", "pending"),
        ]:
            result = self.relay.deliver({**self.task, field: value}, self.binding)
            self.assertEqual(result["status"], "waiting")
        self.journal.resume.assert_not_called()

    def test_failed_execution_requires_explicit_branch_policy_and_terminal_evidence(
        self,
    ):
        task = {
            **self.task,
            "executionStatus": "failed",
            "verificationStatus": "pending",
        }
        binding = replace(self.binding, allow_failure_branch=True)
        self.assertEqual(
            self.relay.deliver(task, binding),
            {"status": "waiting", "reason": "task_result_not_ready"},
        )
        task["evidence"] = [
            {"kind": "execution_terminal", "executionId": "turn", "confirmed": True}
        ]
        held = self.relay.deliver(task, self.binding)
        self.assertEqual(held, {"status": "waiting", "reason": "execution_failure_needs_human"})
        self.assertEqual(self.relay.deliver(task, self.binding), held)
        self.assertEqual(task["id"], "task")
        self.assertEqual(task["payload"]["workflow"]["attemptId"], "attempt-1")
        self.assertEqual(self.relay.deliver(task, binding)["status"], "accepted")
        delivered = self.journal.resume.call_args.args[4]
        self.assertEqual(delivered["executionStatus"], "failed")
        self.assertEqual(delivered["attemptId"], "attempt-1")
        self.assertEqual(delivered["taskId"], "task")
        self.client.observe.assert_not_called()
        self.client.execute.assert_not_called()
        self.journal.resume.assert_called_once()
        for changes in (
            {"resourcesReleased": False},
            {"memoryStatus": "pending"},
            {"evidence": [{"kind": "execution_terminal", "executionId": "turn", "confirmed": False}]},
            {"evidence": [{"kind": "execution_terminal", "executionId": "other", "confirmed": True}]},
        ):
            waiting = self.relay.deliver({**task, **changes}, binding)
            self.assertEqual(waiting, {"status": "waiting", "reason": "task_result_not_ready"})
        self.client.observe.assert_not_called()
        self.journal.resume.assert_called_once()
        self.client.execute.assert_not_called()

    def test_interrupted_executor_needs_paired_exit_and_preserves_cancel_intent(self):
        import copy
        binding = replace(self.binding, allow_failure_branch=True)
        identity = self.task['executionIdentity']
        interruption = {'kind': 'execution_interrupted', 'taskId': 'task',
                        'executionIdentity': identity,
                        'reason': 'owned_executor_exited_without_native_terminal',
                        'nativeStatus': 'unknown', 'cancellationRequested': False}
        exited = {'kind': 'resource_exit', 'taskId': 'task', 'executionIdentity': identity,
                  'receipt': {'source': 'docker-inspect-before-remove', 'running': False,
                              'pid': 0, 'status': 'exited'}}
        task = {**self.task, 'executionStatus': 'failed', 'verificationStatus': 'pending',
                'evidence': [interruption, exited]}
        held = self.relay.deliver(task, self.binding)
        self.assertEqual(held, {'status': 'waiting', 'reason': 'execution_interrupted_needs_human'})
        self.assertEqual(self.relay.deliver(task, self.binding), held)
        self.assertEqual(task['payload']['workflow']['attemptId'], 'attempt-1')
        self.assertEqual(self.relay.deliver(task, binding)['status'], 'accepted')
        delivered = self.journal.resume.call_args.args[4]
        self.assertEqual(delivered['verificationStatus'], 'pending')
        self.assertEqual(delivered['executionStatus'], 'failed')
        self.assertEqual(delivered['attemptId'], 'attempt-1')
        self.client.observe.assert_not_called()
        self.client.execute.assert_not_called()
        self.journal.resume.reset_mock()
        for change in ('canceled', 'missing_exit', 'other_identity', 'running', 'missing_cancel'):
            candidate = copy.deepcopy(task)
            if change == 'canceled': candidate['evidence'][0]['cancellationRequested'] = True
            elif change == 'missing_cancel': candidate['evidence'][0].pop('cancellationRequested')
            elif change == 'missing_exit': candidate['evidence'].pop()
            elif change == 'other_identity': candidate['evidence'][1]['executionIdentity'] = {'codexTurnId': 'other'}
            else: candidate['evidence'][1]['receipt']['running'] = True
            with self.subTest(change=change):
                self.assertEqual(
                    self.relay.deliver(candidate, binding),
                    {'status': 'waiting', 'reason': 'task_result_not_ready'},
                )
        self.journal.resume.assert_not_called()
        self.client.observe.assert_not_called()
        self.client.execute.assert_not_called()

    def test_failed_verification_is_returned_without_claiming_success(self):
        task = {**self.task, "verificationStatus": "failed"}
        binding = replace(self.binding, allow_failure_branch=True)
        self.assertEqual(self.relay.deliver(task, binding)["status"], "waiting")
        task["evidence"] = [
            {
                "kind": "verification",
                "taskId": "task",
                "executionIdentity": task["executionIdentity"],
                "status": "failed",
                "checks": [{"status": "failed"}],
            }
        ]
        self.client.observe.side_effect = AssertionError("pause query")
        self.assertEqual(self.relay.deliver(task, binding)["status"], "accepted")
        delivered = self.journal.resume.call_args.args[4]
        self.assertEqual(delivered["verificationStatus"], "failed")
        self.assertEqual(delivered["executionStatus"], "succeeded")
        self.assertEqual(delivered["attemptId"], "attempt-1")
        self.client.execute.assert_not_called()
        self.journal.resume.assert_called_once()

    def test_confirmed_test_failure_without_repair_branch_stays_on_the_task(self):
        task = {
            **self.task,
            "verificationStatus": "failed",
            "evidence": [
                {
                    "kind": "verification",
                    "taskId": "task",
                    "executionIdentity": self.task["executionIdentity"],
                    "status": "failed",
                    "checks": [{"status": "failed"}],
                }
            ],
        }
        first = self.relay.deliver(task, self.binding)
        second = self.relay.deliver(task, self.binding)
        self.assertEqual(first, {"status": "waiting", "reason": "verification_failure_needs_human"})
        self.assertEqual(second, first)
        self.client.observe.assert_not_called()
        self.journal.resume.assert_not_called()
        self.client.execute.assert_not_called()
        for changes in (
            {"resourcesReleased": False},
            {"memoryStatus": "pending"},
            {"evidence": []},
        ):
            waiting = self.relay.deliver({**task, **changes}, replace(self.binding, allow_failure_branch=True))
            self.assertEqual(waiting, {"status": "waiting", "reason": "task_result_not_ready"})
        self.client.observe.assert_not_called()
        self.journal.resume.assert_not_called()

    def test_cancel_or_unreleased_process_cannot_trigger_failure_branch(self):
        binding = replace(self.binding, allow_failure_branch=True)
        evidence = [
            {"kind": "execution_terminal", "executionId": "turn", "confirmed": True}
        ]
        for changes in (
            {"executionStatus": "canceled"},
            {"executionStatus": "failed", "resourcesReleased": False},
        ):
            task = {**self.task, "evidence": evidence, **changes}
            self.assertEqual(self.relay.deliver(task, binding)["status"], "waiting")
        self.journal.resume.assert_not_called()

    def test_other_pause_context_or_block_does_not_receive_result(self):
        self.client.observe.return_value["response"]["data"]["paused"][
            "contextId"
        ] = "later-loop-context"
        self.assertEqual(
            self.relay.deliver(self.task, self.binding)["status"], "waiting"
        )
        self.journal.resume.assert_not_called()

    def test_human_approval_and_wrong_run_cannot_be_auto_resumed(self):
        human = ResumeBinding(
            task_id="task",
            workflow_id="workflow",
            run_id="run",
            block_id="wait",
            context_id="context",
            purpose="human_approval",
        )
        with self.assertRaises(ValueError):
            self.relay.deliver(self.task, human)
        task = {
            **self.task,
            "payload": {"workflow": {"id": "workflow", "runId": "other"}},
        }
        with self.assertRaises(ValueError):
            self.relay.deliver(task, self.binding)
        self.journal.resume.assert_not_called()

    def test_existing_delivery_is_returned_after_sim_leaves_pause(self):
        self.journal.get.side_effect = None
        self.journal.get.return_value = {"status": "accepted"}
        self.journal.resume_input.return_value = {'taskId': self.task['id'], **{
            key: self.task[key] for key in ('executionStatus', 'verificationStatus',
                                          'executionIdentity', 'output', 'evidence', 'memoryStatus')}}
        self.client.observe.return_value = {"status": "completed"}
        self.assertEqual(
            self.relay.deliver(self.task, self.binding)["status"], "accepted"
        )
        self.client.observe.assert_not_called()
        self.journal.resume.assert_not_called()

    def test_new_callback_names_activation_and_attempt_without_resending_history(self):
        from agent_line.store import Conflict
        missing = {
            **self.task,
            'payload': {'workflow': {**self.task['payload']['workflow']}},
        }
        missing['payload']['workflow'].pop('attemptId')
        self.assertEqual(
            self.relay.deliver(missing, self.binding),
            {'status': 'waiting', 'reason': 'attempt_correlation_unavailable'},
        )
        unsafe = {
            **self.task,
            'payload': {'workflow': {**self.task['payload']['workflow'], 'attemptId': 'attempt 1'}},
        }
        self.assertEqual(
            self.relay.deliver(unsafe, self.binding),
            {'status': 'waiting', 'reason': 'attempt_correlation_unavailable'},
        )
        unknown = {**self.task, 'executionStatus': 'uncertain', 'verificationStatus': 'pending'}
        self.assertEqual(self.relay.deliver(unknown, self.binding)['status'], 'waiting')
        self.journal.resume.assert_not_called()

        self.journal.get.side_effect = None
        self.journal.get.return_value = {'status': 'accepted'}
        historical = {'taskId': self.task['id'], **{
            key: self.task[key] for key in (
                'executionStatus', 'verificationStatus', 'executionIdentity', 'output', 'evidence')}}
        self.journal.resume_input.return_value = historical
        self.assertEqual(self.relay.deliver(self.task, self.binding)['status'], 'accepted')
        self.journal.resume.assert_not_called()
        self.journal.resume_input.return_value = {**historical, 'attemptId': 'older-attempt'}
        with self.assertRaises(Conflict):
            self.relay.deliver(self.task, self.binding)
        self.journal.resume.assert_not_called()

    def test_unrecoverable_output_is_a_human_handoff_not_a_delivery_retry(self):
        task = {
            **self.task,
            'payload': {
                **self.task['payload'],
                'outputSchemaId': 'coding-result-v1',
            },
            'output': {
                'text': '结果如下\n```json\n{"summary":"已完成","status":"completed","changedFiles":[]}\n```\n',
                'nativeStatus': 'task_complete',
                'toolEvidence': [],
            },
        }
        first = self.relay.deliver(task, self.binding)
        second = self.relay.deliver(task, self.binding)
        self.assertEqual(first, {'status': 'waiting', 'reason': 'declared_output_needs_human'})
        self.assertEqual(second, first)
        self.client.observe.assert_not_called()
        self.journal.resume.assert_not_called()
        self.client.execute.assert_not_called()

    def test_same_body_fence_still_reaches_delivery(self):
        task = {
            **self.task,
            'payload': {
                **self.task['payload'],
                'outputSchemaId': 'coding-result-v1',
            },
            'output': {
                'text': '```json\n{"summary":"已完成","status":"completed","changedFiles":[]}\n```\n',
                'nativeStatus': 'task_complete',
                'toolEvidence': [],
            },
        }
        self.assertEqual(self.relay.deliver(task, self.binding)['status'], 'accepted')
        self.client.observe.assert_called_once()
        self.journal.resume.assert_called_once()
        delivered = self.journal.resume.call_args.args[4]
        self.assertEqual(delivered['output']['text'].startswith('```json'), True)
        self.assertEqual(delivered['output']['result']['status'], 'completed')
        self.client.execute.assert_not_called()

    def _permission_task(self, **changes):
        identity = dict(self.task['executionIdentity'])
        task = {
            **self.task,
            'evidence': [{
                'kind': 'execution_policy',
                'source': 'qwenpaw-plugin-task-binding',
                'executionId': 'turn',
                'controls': {'sandbox': 'read-only', 'approval_policy': 'never'},
            }],
            'approvalDecisions': [{
                'decision': 'reject',
                'taskId': 'task',
                'executionIdentity': identity,
                'deliveryStatus': 'accepted',
            }],
        }
        task.update(changes)
        return task

    def test_confirmed_permission_denial_stays_without_widening_or_resubmitting(self):
        task = self._permission_task()
        controls = task['evidence'][0]['controls']
        first = self.relay.deliver(task, self.binding)
        second = self.relay.deliver(task, self.binding)
        self.assertEqual(first, {'status': 'waiting', 'reason': 'permission_denied_needs_human'})
        self.assertEqual(second, first)
        self.assertEqual(task['evidence'][0]['controls'], controls)
        self.client.observe.assert_not_called()
        self.journal.resume.assert_not_called()
        self.client.execute.assert_not_called()

    def test_confirmed_permission_denial_uses_the_existing_failure_branch_once(self):
        task = self._permission_task()
        binding = replace(self.binding, allow_failure_branch=True)
        self.client.observe.side_effect = AssertionError('pause query')
        self.assertEqual(self.relay.deliver(task, binding)['status'], 'accepted')
        delivered = self.journal.resume.call_args.args[4]
        self.assertEqual(delivered['executionStatus'], 'succeeded')
        self.assertEqual(delivered['verificationStatus'], 'passed')
        self.assertEqual(delivered['evidence'][0]['controls'], {
            'sandbox': 'read-only', 'approval_policy': 'never'})
        self.journal.resume.assert_called_once()
        self.client.execute.assert_not_called()

    def test_unfinished_or_unmatched_permission_denial_does_not_fail_the_task(self):
        task = self._permission_task()
        running = self._permission_task(executionStatus='accepted', verificationStatus='pending')
        self.assertEqual(
            self.relay.deliver(running, self.binding),
            {'status': 'waiting', 'reason': 'task_result_not_ready'},
        )
        waiting = self.relay.deliver(
            {**task, 'resourcesReleased': False},
            replace(self.binding, allow_failure_branch=True),
        )
        self.assertEqual(waiting, {'status': 'waiting', 'reason': 'task_result_not_ready'})
        self.client.observe.assert_not_called()
        self.journal.resume.assert_not_called()
        widened = self._permission_task()
        widened['evidence'] = [{
            **widened['evidence'][0],
            'controls': {'sandbox': 'danger-full-access', 'approval_policy': 'never'},
        }]
        self.assertEqual(self.relay.deliver(widened, self.binding)['status'], 'accepted')
        self.assertEqual(widened['evidence'][0]['controls']['sandbox'], 'danger-full-access')
        self.client.execute.assert_not_called()
        self.journal.resume.reset_mock()
        self.client.observe.reset_mock()
        plain = {**self.task, 'approvalDecisions': [{'decision': 'reject'}]}
        self.assertEqual(self.relay.deliver(plain, self.binding)['status'], 'accepted')
        self.client.execute.assert_not_called()

    def test_confirmed_memory_write_failure_retries_only_the_memory_stage(self):
        task = {**self.task, "memoryStatus": "failed"}
        first = self.relay.deliver(task, self.binding)
        second = self.relay.deliver(task, self.binding)
        self.assertEqual(first, {"status": "waiting", "reason": "memory_write_needs_retry"})
        self.assertEqual(second, first)
        self.assertEqual(task["id"], "task")
        self.assertEqual(task["executionStatus"], "succeeded")
        self.client.observe.assert_not_called()
        self.journal.resume.assert_not_called()
        self.client.execute.assert_not_called()
        pending = self.relay.deliver({**self.task, "memoryStatus": "pending"}, self.binding)
        self.assertEqual(pending, {"status": "waiting", "reason": "task_result_not_ready"})
        unreleased = self.relay.deliver({**task, "resourcesReleased": False}, self.binding)
        self.assertEqual(unreleased, {"status": "waiting", "reason": "task_result_not_ready"})
        failed = {
            **task,
            "executionStatus": "failed",
            "verificationStatus": "pending",
            "evidence": [{"kind": "execution_terminal", "executionId": "turn", "confirmed": True}],
        }
        branch = replace(self.binding, allow_failure_branch=True)
        self.assertEqual(
            self.relay.deliver(failed, branch),
            {"status": "waiting", "reason": "memory_write_needs_retry"},
        )
        self.client.observe.assert_not_called()
        self.journal.resume.assert_not_called()
        self.client.execute.assert_not_called()
        optional = self.relay.deliver(task, replace(self.binding, require_memory_commit=False))
        self.assertEqual(optional["status"], "accepted")
        delivered = self.journal.resume.call_args.args[4]
        self.assertEqual(delivered["taskId"], "task")
        self.assertEqual(delivered["attemptId"], "attempt-1")
        self.assertEqual(delivered["memoryStatus"], "failed")
        self.journal.resume.assert_called_once()
        self.client.execute.assert_not_called()

    def test_late_result_does_not_resume_a_later_loop_activation(self):
        task = {
            **self.task,
            'payload': {'workflow': {**self.task['payload']['workflow'],
                                     'nodeId': 'body', 'nodeExecutionId': 'body_loop0',
                                     'attemptId': '1'}},
        }
        binding = replace(self.binding, block_id='body', context_id='body_loop0')
        self.journal.resume_summary.return_value = {
            'latestAccepted': {'contextId': 'body_loop1', 'status': 'accepted'},
        }
        first = self.relay.deliver(task, binding)
        second = self.relay.deliver(task, binding)
        self.assertEqual(first, {'status': 'waiting', 'reason': 'stale_activation_not_current'})
        self.assertEqual(second, first)
        self.assertEqual(task['payload']['workflow']['attemptId'], '1')
        self.journal.resume.assert_not_called()
        self.client.observe.assert_not_called()
        self.client.execute.assert_not_called()
        self.journal.get.side_effect = None
        self.journal.get.return_value = {'status': 'accepted'}
        self.journal.resume_input.return_value = {
            'taskId': task['id'],
            'nodeExecutionId': 'body_loop0',
            'attemptId': '1',
            **{key: task[key] for key in (
                'executionStatus', 'verificationStatus', 'executionIdentity', 'output', 'evidence', 'memoryStatus')},
        }
        self.assertEqual(self.relay.deliver(task, binding)['status'], 'accepted')
        self.journal.resume.assert_not_called()
        self.client.execute.assert_not_called()
        current = replace(binding, context_id='body_loop1')
        current_task = {
            **task,
            'payload': {'workflow': {**task['payload']['workflow'], 'nodeExecutionId': 'body_loop1', 'attemptId': '2'}},
        }
        self.journal.get.side_effect = KeyError
        self.journal.resume_summary.return_value = {
            'latestAccepted': {'contextId': 'body_loop0', 'status': 'accepted'},
        }
        pause = self.client.observe.return_value['response']['data']['paused']
        pause.update(contextId='body_loop1', blockedOnBlockId='body')
        self.assertEqual(self.relay.deliver(current_task, current)['status'], 'accepted')
        self.assertEqual(self.journal.resume.call_args.args[3], 'body_loop1')
        self.assertEqual(self.journal.resume.call_args.args[4]['attemptId'], '2')
        self.client.execute.assert_not_called()
