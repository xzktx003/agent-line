"""Return verified external-node results to a bound Sim pause context.

This never evaluates edges, selects successor nodes, or approves human gates.
Bindings come from trusted, versioned workflow configuration and durable pause
registration; callers must retain the exact context across process restarts.
"""

from dataclasses import dataclass
from .adapters.sim import SimClient, segment
from .adapters.sim_wait import SimWaitClient, WaitInputUnavailable
from .store import Conflict, canonical
from .run_control import RunControlClosed
from .output_contract import flow_output
from .batch_item import task_batch_item, batch_summary


def frozen_result_matches(task, result):
    """Core terminal facts remain fixed; later memory events do not rewrite them."""
    try:
        output = flow_output(task)
        item = task_batch_item(task['payload'])
        if item is not None:
            if not isinstance(result, dict) or canonical(result.get('batchItem')) != canonical(batch_summary(item)):
                return False
        elif isinstance(result, dict) and 'batchItem' in result:
            return False
    except (ValueError, TypeError, KeyError):
        return False
    if not (isinstance(result, dict) and result.get('taskId') == task['id']
            and all(result.get(key) == task[key] for key in
                    ('executionStatus', 'verificationStatus', 'executionIdentity'))
            and result.get('output') == output
            and isinstance(result.get('evidence'), list)
            and all(item in task['evidence'] for item in result['evidence'])):
        return False
    workflow = task.get('payload', {}).get('workflow') if isinstance(task.get('payload'), dict) else None
    if not isinstance(workflow, dict):
        return not any(key in result for key in ('nodeExecutionId', 'attemptId'))
    return all(result.get(key) == workflow.get(key) for key in ('nodeExecutionId', 'attemptId') if key in result)


_ORIGINAL_CONTROLS = {'sandbox': 'read-only', 'approval_policy': 'never'}


def _loop_iteration(block_id, context_id):
    """Return a proven single-layer loop iteration, or None for any other context."""
    prefix = block_id + '_loop'
    if not isinstance(context_id, str) or not context_id.startswith(prefix):
        return None
    suffix = context_id[len(prefix):]
    if not suffix.isdigit() or (len(suffix) > 1 and suffix.startswith('0')):
        return None
    return int(suffix)


def interrupted_failure(task, identity):
    """Consume Store-owned paired evidence, without inventing a native terminal."""
    if task['executionStatus'] != 'failed' or not all(
        identity.get(key) for key in ('codexThreadId', 'codexTurnId')
    ):
        return False
    evidence = task['evidence']
    interrupted = [item for item in evidence if isinstance(item, dict)
                   and item.get('kind') == 'execution_interrupted']
    if len(interrupted) != 1:
        return False
    event = interrupted[0]
    if (event.get('taskId') != task['id'] or event.get('executionIdentity') != identity
            or event.get('reason') != 'owned_executor_exited_without_native_terminal'
            or event.get('nativeStatus') != 'unknown'
            or event.get('cancellationRequested') is not False):
        return False
    exits = [item for item in evidence if isinstance(item, dict)
             and item.get('kind') == 'resource_exit']
    if len(exits) != 1:
        return False
    exited = exits[0]
    receipt = exited.get('receipt')
    return (exited.get('taskId') == task['id'] and exited.get('executionIdentity') == identity
            and isinstance(receipt, dict) and receipt.get('source') == 'docker-inspect-before-remove'
            and receipt.get('running') is False and type(receipt.get('pid')) is int
            and receipt['pid'] == 0 and receipt.get('status') in ('exited', 'dead'))


def confirmed_permission_denial(task):
    """A stored rejection matches this task only after execution has ended.

    The original read-only policy must still be the only recorded policy.
    A rejection does not widen the sandbox or start another attempt.
    """
    decisions = task.get('approvalDecisions')
    identity = task.get('executionIdentity')
    if (not isinstance(decisions, list) or task.get('executionStatus') not in ('succeeded', 'failed')
            or not isinstance(identity, dict)
            or not isinstance(identity.get('codexThreadId'), str) or not identity['codexThreadId']
            or not isinstance(identity.get('codexTurnId'), str) or not identity['codexTurnId']):
        return False
    legacy = {'decision', 'taskId', 'executionIdentity', 'deliveryStatus'}
    matches = []
    for item in decisions:
        if not isinstance(item, dict):
            continue
        keys = set(item)
        if keys == legacy | {'requestId'}:
            request_id = item.get('requestId')
            if (not isinstance(request_id, str) or not request_id or request_id != request_id.strip()
                    or len(request_id) > 256
                    or any(ord(char) < 32 or ord(char) == 127 for char in request_id)):
                continue
        elif keys != legacy:
            continue
        if (item.get('decision') == 'reject' and item.get('taskId') == task.get('id')
                and item.get('deliveryStatus') == 'accepted'
                and item.get('executionIdentity') == identity):
            matches.append(item)
    if len(matches) != 1:
        return False
    policies = [
        item for item in task.get('evidence') or []
        if isinstance(item, dict) and item.get('kind') == 'execution_policy'
    ]
    if len(policies) != 1:
        return False
    policy = policies[0]
    return (set(policy) == {'kind', 'source', 'executionId', 'controls'}
            and policy['source'] == 'qwenpaw-plugin-task-binding'
            and policy['executionId'] == identity['codexTurnId']
            and policy['controls'] == _ORIGINAL_CONTROLS)


@dataclass(frozen=True)
class ResumeBinding:
    task_id: str
    workflow_id: str
    run_id: str
    block_id: str
    context_id: str
    purpose: str
    require_memory_commit: bool = True
    allow_failure_branch: bool = False
    allow_multiple: bool = False


class CompletionRelay:
    def __init__(self, client, journal, *, wait_client=None):
        self.client, self.journal = client, journal
        self.wait_client = wait_client
        if self.wait_client is None and isinstance(client, SimClient):
            self.wait_client = SimWaitClient(client)

    def _static_pause_ready(self, binding, total, resumed):
        # The v2 summary represents one branch only. An independently read
        # native list must prove the original bound point; it cannot be inferred
        # from a different branch's summary or a count of outstanding pauses.
        if (binding.allow_multiple is not True
                or binding.context_id != binding.block_id or self.wait_client is None
                or type(total) is not int or not 1 <= total <= 200
                or type(resumed) is not int or not 0 <= resumed < total):
            return False
        try:
            details = self.wait_client.read_pauses(binding.workflow_id, binding.run_id)
        except WaitInputUnavailable:
            return False
        if (not isinstance(details, dict)
                or details.get('workflowId') != binding.workflow_id
                or details.get('runId') != binding.run_id
                or type(details.get('totalPauseCount')) is not int
                or type(details.get('resumedCount')) is not int
                or details['totalPauseCount'] != total or details['resumedCount'] != resumed
                or type(details.get('pausePoints')) is not list):
            return False
        native_points = details['pausePoints']
        if (len(native_points) != total or not all(isinstance(p, dict) for p in native_points)
                or resumed != sum(p.get('resumeStatus') == 'resumed' for p in native_points)):
            return False
        points = [point for point in native_points
                  if point.get('contextId') == binding.context_id
                  and point.get('blockId') == binding.block_id]
        return (len(points) == 1 and points[0].get('resumeStatus') == 'paused'
                and points[0].get('snapshotReady') is True
                and points[0].get('pauseKind') == 'human'
                and points[0].get('parallelScope') is None
                and points[0].get('loopScope') is None)

    def _later_activation_accepted(self, binding):
        summary = getattr(self.journal, 'resume_summary', None)
        if not callable(summary):
            return False
        try:
            found = summary(binding.workflow_id, binding.run_id)
        except (KeyError, ValueError, TypeError):
            return False
        if not isinstance(found, dict):
            return False
        accepted = found.get('latestAccepted')
        if not isinstance(accepted, dict):
            return False
        later = accepted.get('contextId')
        if later == binding.context_id or not isinstance(later, str):
            return False
        current = _loop_iteration(binding.block_id, binding.context_id)
        other = _loop_iteration(binding.block_id, later)
        return current is not None and other is not None and other > current

    def deliver(self, task, binding):
        if binding.purpose != "external_task_result":
            raise ValueError(
                "Only explicitly designated external task waits may be resumed"
            )
        for value in (
            binding.task_id,
            binding.workflow_id,
            binding.run_id,
            binding.block_id,
            binding.context_id,
        ):
            segment(value)
        workflow = task["payload"]["workflow"]
        if (
            task["id"] != binding.task_id
            or workflow["id"] != binding.workflow_id
            or workflow["runId"] != binding.run_id
            or workflow.get("nodeId") != binding.block_id
            or workflow.get("nodeExecutionId") != binding.context_id
        ):
            raise ValueError("Task does not belong to the bound workflow activation")
        if (
            type(binding.allow_failure_branch) is not bool
            or type(binding.require_memory_commit) is not bool
            or type(binding.allow_multiple) is not bool
        ):
            raise ValueError("Explicit result delivery policy required")
        try:
            existing = self.journal.get(binding.workflow_id, binding.run_id, binding.context_id)
        except KeyError:
            pass
        else:
            frozen = self.journal.resume_input(binding.workflow_id, binding.run_id, binding.context_id)
            if not frozen_result_matches(task, frozen):
                raise Conflict('Recorded result belongs to different terminal facts')
            # Returning this durable record never sends a second request, even
            # if memory later commits or cancellation is recorded after delivery.
            return existing
        if task.get('cancellationRequested') is True:
            return {'status': 'waiting', 'reason': 'task_cancel_requested'}
        if task['executionStatus'] == 'succeeded':
            from .output_contract import blocks_success
            if blocks_success(task):
                # The same result could not be normalized. Hand it to a person.
                # Do not query the pause or resume it, and do not start an attempt.
                return {'status': 'waiting', 'reason': 'declared_output_needs_human'}
        success = (
            task["executionStatus"] == "succeeded"
            and task["verificationStatus"] == "passed"
        )
        identity = task.get("executionIdentity") or {}
        execution_id = identity.get("codexTurnId") or identity.get("upstreamTaskId")
        terminal_failure = (
            task["executionStatus"] == "failed"
            and execution_id
            and any(
                isinstance(item, dict)
                and item.get("kind") == "execution_terminal"
                and item.get("executionId") == execution_id
                and item.get("confirmed") is True
                for item in task["evidence"]
            )
        )
        verification_failure = (
            task["executionStatus"] == "succeeded"
            and task["verificationStatus"] == "failed"
            and any(
                isinstance(item, dict)
                and item.get("kind") == "verification"
                and item.get("taskId") == task["id"]
                and item.get("executionIdentity") == identity
                and item.get("status") == "failed"
                and any(
                    isinstance(check, dict) and check.get("status") == "failed"
                    for check in item.get("checks", [])
                )
                for item in task["evidence"]
            )
        )
        delivered = (
            task["resourcesReleased"] is True
            and (not binding.require_memory_commit or task["memoryStatus"] == "committed")
        )
        # A confirmed permission rejection is not a pause query and not a new attempt.
        # The existing failure branch receives the original status once; otherwise the task stays put.
        permission_denial = confirmed_permission_denial(task)
        repair_permission_denial = False
        if permission_denial:
            ended = (
                task["executionStatus"] == "failed"
                and (terminal_failure or interrupted_failure(task, identity))
            ) or (
                task["executionStatus"] == "succeeded"
                and task["verificationStatus"] in ("passed", "failed")
            )
            if not ended or not delivered:
                return {"status": "waiting", "reason": "task_result_not_ready"}
            if not binding.allow_failure_branch:
                return {"status": "waiting", "reason": "permission_denied_needs_human"}
            repair_permission_denial = True
        # A confirmed test failure is not an execution retry and not a pause query.
        # The existing repair branch receives the original result once; otherwise the task stays put.
        repair_test_failure = (
            not repair_permission_denial
            and verification_failure and binding.allow_failure_branch and delivered
        )
        if verification_failure and not repair_test_failure and not repair_permission_denial:
            if not delivered:
                return {"status": "waiting", "reason": "task_result_not_ready"}
            return {"status": "waiting", "reason": "verification_failure_needs_human"}
        # A confirmed Codex execution failure is not a pause query and not a new attempt.
        # The existing failure branch receives the original status once; otherwise the task stays put.
        repair_execution_failure = (
            not repair_permission_denial
            and terminal_failure and binding.allow_failure_branch and delivered
        )
        if terminal_failure and delivered and not repair_execution_failure and not repair_permission_denial:
            return {"status": "waiting", "reason": "execution_failure_needs_human"}
        # A confirmed executor interruption is also not a pause query and not a new attempt.
        # Unknown native status without this paired exit still only waits.
        interrupted = interrupted_failure(task, identity)
        repair_interruption = (
            not repair_permission_denial
            and interrupted and binding.allow_failure_branch and delivered
        )
        if interrupted and delivered and not repair_interruption and not repair_permission_denial:
            return {"status": "waiting", "reason": "execution_interrupted_needs_human"}
        ready = success or repair_test_failure or repair_permission_denial or repair_execution_failure or repair_interruption or (
            binding.allow_failure_branch
            and (terminal_failure or interrupted)
        )
        memory_failed = binding.require_memory_commit and task["memoryStatus"] == "failed"
        if (
            not ready
            or task["resourcesReleased"] is not True
            or (binding.require_memory_commit and task["memoryStatus"] != "committed")
        ):
            # A confirmed memory write failure retries only that stage.
            # The finished code task is not queried, resumed, or executed again.
            if ready and task["resourcesReleased"] is True and memory_failed:
                return {"status": "waiting", "reason": "memory_write_needs_retry"}
            return {"status": "waiting", "reason": "task_result_not_ready"}
        try:
            attempt_id = segment(workflow.get('attemptId'))
        except ValueError:
            return {'status': 'waiting', 'reason': 'attempt_correlation_unavailable'}
        result = {
            "taskId": task["id"],
            "nodeExecutionId": workflow["nodeExecutionId"],
            "attemptId": attempt_id,
            "executionStatus": task["executionStatus"],
            "verificationStatus": task["verificationStatus"],
            "memoryStatus": task["memoryStatus"],
            "executionIdentity": task["executionIdentity"],
            "output": flow_output(task),
            "evidence": task["evidence"],
        }
        try:
            item = task_batch_item(task['payload'])
            if item is not None:
                result['batchItem'] = batch_summary(item)
        except ValueError:
            return {'status': 'waiting', 'reason': 'batch_item_invalid'}
        # A later accepted activation owns the current round. This older result
        # may only replay its own recorded history, never resume that newer round.
        if self._later_activation_accepted(binding):
            return {'status': 'waiting', 'reason': 'stale_activation_not_current'}
        if not repair_test_failure and not repair_permission_denial and not repair_execution_failure and not repair_interruption:
            observed = self.client.observe(binding.workflow_id, binding.run_id)
            if not isinstance(observed, dict):
                return {"status": "waiting", "reason": "bound_pause_not_observed"}
            payload = observed.get("response", {})
            if not isinstance(payload, dict):
                return {"status": "waiting", "reason": "bound_pause_not_observed"}
            run = payload.get("data", payload)
            if not isinstance(run, dict):
                return {"status": "waiting", "reason": "bound_pause_not_observed"}
            pause = run.get("paused") or {}
            if not isinstance(pause, dict):
                return {"status": "waiting", "reason": "bound_pause_not_observed"}
            total = pause.get('pausePointCount')
            resumed = pause.get('resumedCount', 0 if type(total) is int and total == 1 else None)
            # Native counters include already resumed contexts in this run. Trusted
            # bindings still require ordinary single activation and an exact context.
            single_active = (type(total) is int and type(resumed) is int
                             and total >= 1 and resumed >= 0 and total - resumed == 1)
            if (
                run.get("runId") != binding.run_id
                or run.get("workflowId") != binding.workflow_id
                or run.get("status") != "paused"
                or pause.get("pauseKind") != "human"
            ):
                return {"status": "waiting", "reason": "bound_pause_not_observed"}
            precise_single = (single_active and pause.get('contextId') == binding.context_id
                              and pause.get('blockedOnBlockId') == binding.block_id)
            if ((binding.allow_multiple is True or not precise_single)
                    and not self._static_pause_ready(binding, total, resumed)):
                return {"status": "waiting", "reason": "bound_pause_not_observed"}
        # The native Human block is used here only as an explicitly configured
        # external-result wait. Genuine human approvals require a human decision.
        try:
            return self.journal.resume(
                self.client, binding.workflow_id, binding.run_id, binding.context_id, result
            )
        except RunControlClosed as exc:
            return {'status': 'waiting', 'reason': 'run_' + exc.state}
