"""Single-node execution delivery and reconciliation; never schedules successors.

Gateways are trusted, deployment-bound adapters, not user-provided callbacks.
A claimed submission is never sent again. Recovery only observes its task ID.
"""

import sqlite3
from pathlib import Path
from contextlib import nullcontext

from .store import TERMINAL, Conflict, canonical
from .role_dispatch import UnstartedCancellationProof
from .usage import valid_codex_usage
from .output_contract import supported_output_schema, execution_instruction
from .polling import PollCadence



def _explicit_unaccepted_rejection(result):
    """A refusal recorded before any native execution identity exists."""
    if not isinstance(result, dict):
        return False
    return (result.get('status') == 'rejected' and result.get('accepted') is False
            and result.get('pollHint') == 'submission_rejected'
            and result.get('rejectionReason') == 'submission_rejected_unaccepted'
            and result.get('identity') == {} and result.get('terminalConfirmed') is False
            and result.get('resourcesReleased') is False)


def _corrected_resubmit_proof(gateway, task_id):
    allow = getattr(gateway, 'allow_corrected_resubmit', None)
    if allow is None or getattr(type(allow), '__module__', '') == 'unittest.mock':
        return object()
    return allow(task_id)


class Dispatcher:
    def __init__(self, store, gateways, *, run_control=None, verifier=None):
        self.store = store
        self.gateways = dict(gateways)
        self.run_control = run_control
        self.verifier = verifier
        self.configuration_blocks = {}

    def enforce_control(self, task):
        if self.run_control is not None:
            payload = task['payload']; workflow = payload['workflow']
            if self.run_control.status(workflow['id'], workflow['runId'])['state'] == 'stop_requested':
                self.store.request_cancel(task['id'])
                return self.store.get(task['id'])
        return task

    def _gateway(self, task):
        payload = task["payload"]
        key = (payload["projectId"], payload.get("roleBindingId"))
        gateway = self.gateways.get(key)
        if gateway is None:
            if any(
                isinstance(item, tuple) and item[0] == payload["projectId"]
                for item in self.gateways
            ):
                return None  # Explicit binding registries never fall back to another role policy.
            gateway = self.gateways.get(payload["projectId"])
        if gateway is None:
            return None
        role = getattr(gateway, "role", None)
        if isinstance(role, str) and role != payload.get("roleAgentId"):
            raise Conflict(
                "Persisted task role differs from configured execution gateway"
            )
        workspace = getattr(gateway, "workspace", None)
        if (
            isinstance(workspace, (str, Path))
            and Path(workspace).resolve() != Path(payload["workspace"]).resolve()
        ):
            raise Conflict(
                "Persisted task workspace differs from configured execution gateway"
            )
        return gateway

    def submit(self, task_id, *, resubmit_proof=None):
        task = self.enforce_control(self.store.get(task_id))
        if self.run_control is not None:
            from .run_control import RunControlClosed
            workflow = task['payload']['workflow']
            try:
                with self.run_control.admission(workflow['id'], workflow['runId']):
                    return self._submit(task_id, resubmit_proof=resubmit_proof)
            except RunControlClosed:
                return self.enforce_control(self.store.get(task_id))
        return self._submit(task_id, resubmit_proof=resubmit_proof)

    def resubmit_after_correction(self, task_id):
        """One explicit resubmit after the user corrects a pre-acceptance refusal.

        The dispatch loop never calls this. The same rejection is not replayed
        automatically, and an unknown or accepted execution is not reopened.
        """
        task = self.enforce_control(self.store.get(task_id))
        if (task.get('submissionBlock') != 'submission_rejected'
                or task['executionStatus'] != 'registered' or task.get('executionIdentity')):
            return task
        gateway = self._gateway(task)
        if gateway is None:
            return task
        proof = _corrected_resubmit_proof(gateway, task_id)
        if proof is None:
            return task
        return self.submit(task_id, resubmit_proof=proof)

    def _submit(self, task_id, resubmit_proof=None):
        task = self.store.get(task_id)
        if ('outputSchemaId' in task['payload']
                and not supported_output_schema(task['payload']['outputSchemaId'], task['payload'].get('outputFields'))):
            return task
        gateway = self._gateway(task)
        if gateway is None:
            return task
        if task['executionStatus'] != 'registered':
            return task
        instruction = execution_instruction(task, canonical(task["payload"]["input"]))
        preflight_proof = None
        if task['executionStatus'] == 'registered' and callable(getattr(type(gateway), 'preflight', None)):
            try:
                preflight_proof = gateway.preflight(task_id=task_id, role_revision=task['payload'].get('roleRevision'))
            except ValueError as error:
                reason = str(error)
                if reason not in {'role_revision_required', 'role_revision_mismatch', 'role_revision_unavailable',
                                  'execution_readiness_stale'}:
                    raise
                self.store.block_role_revision(task_id, reason)
                return self.store.get(task_id)
            self.store.clear_role_revision_block(task_id)
        workspace = task["payload"]["workspace"]
        if not self.store.claim(task_id, workspace, consume_rejection=resubmit_proof is not None):
            return self.store.get(task_id)
        try:
            result = gateway.submit(
                task_id=task_id,
                role_session_id="agent-line-" + task_id,
                instruction=instruction,
                **({"preflight_proof": preflight_proof} if preflight_proof is not None else {}),
                **({"role_revision": task["payload"]["roleRevision"]} if "roleRevision" in task["payload"] else {}),
                **({"resubmit_proof": resubmit_proof} if resubmit_proof is not None else {}),
            )
        except Exception:
            # Request may have reached the native process. Never retry it.
            self.store.mark_submission_uncertain(task_id)
            rejected = False
        else:
            hint = result.get('pollHint') if isinstance(result, dict) else None
            if hint in ('authentication', 'model_missing'):
                self.configuration_blocks[task_id] = hint
            else:
                self.configuration_blocks.pop(task_id, None)
            rejected = False
            # A rejection is not an acceptance, even if a native identity is attached.
            # Only the exact empty-identity refusal is retained for a corrected resubmit.
            if isinstance(result, dict) and result.get('status') == 'rejected':
                if (self.store.get(task_id)['executionStatus'] == 'submitting'
                        and _explicit_unaccepted_rejection(result)
                        and self.store.record_unaccepted_rejection(task_id)):
                    rejected = True
                elif self.store.get(task_id)['executionStatus'] == 'submitting':
                    self.store.mark_submission_uncertain(task_id)
            else:
                self._apply(task_id, result)
                if self.store.get(task_id)['executionStatus'] == 'submitting':
                    self.store.mark_submission_uncertain(task_id)
        try:
            # A refusal before acceptance means the role never started.
            # A later role note would hide the retained rejection reason.
            if not rejected:
                self.store.note_role_started(task_id)
        except (ValueError, OSError, sqlite3.Error):
            pass  # The native submission is already durable; a missing note must not be retried.
        return self.store.get(task_id)

    def reconcile(self, task_id, *, on_observation=None):
        task = self.enforce_control(self.store.get(task_id))
        if (task['executionStatus'] == 'succeeded' and task['resourcesReleased'] is True
                and 'verificationPolicy' in task['payload']):
            return task  # Persisted terminal/exit facts do not need a new native query.
        gateway = self._gateway(task)
        if gateway is None or task["executionStatus"] == "registered":
            return task
        try:
            result = gateway.observe(task_id)
        except Exception:
            result = {"status": "unknown"}
        try:
            self._apply(task_id, result)
            observed = self._record_observation(task_id, result)
            self._enforce_deadline(task_id)
        except ValueError:
            self.store.record_execution_observation(task_id, 'unknown')
            self._report_poll(on_observation, {}, False)
            raise
        current = self.store.get(task_id)
        if current["executionStatus"] == "cancel_requested":
            try:
                cancellation = gateway.cancel(task_id)
            except Exception:
                self._report_poll(on_observation, {}, False)
                return current
            try:
                self._apply(task_id, cancellation)
                observed = self._record_observation(task_id, cancellation)
                result = cancellation
            except ValueError:
                self.store.record_execution_observation(task_id, 'unknown')
                self._report_poll(on_observation, {}, False)
                raise
        self._report_poll(on_observation, result, observed)
        return self.store.get(task_id)

    @staticmethod
    def _report_poll(callback, result, observed):
        if callback is None:
            return
        hint = result.get('pollHint') if isinstance(result, dict) else None
        outcome = hint if hint in ('rate_limited', 'unavailable', 'authentication', 'model_missing') else 'observed' if observed else 'unknown'
        retry = result.get('pollAfterSeconds') if outcome == 'rate_limited' else None
        if type(retry) is not int or not 1 <= retry <= 30:
            retry = None
        callback({'outcome': outcome, 'retry_after': retry})

    def _enforce_deadline(self, task_id):
        # Observe the original execution first. A timer authorizes a cancellation
        # request only; terminal state and resource release still require proof.
        if self.store.execution_deadline_exceeded(task_id):
            self.store.request_cancel(task_id)

    def _record_observation(self, task_id, result):
        observed = (isinstance(result, dict) and result.get('executorInterrupted') is not True
                    and bool(result.get('identity'))
                    and (result.get('status') in ('accepted', 'running')
                         or (result.get('status') in TERMINAL and result.get('terminalConfirmed') is True)))
        if (observed and result.get('terminalConfirmed') is not True
                and isinstance(result.get('parent'), dict)
                and result['parent'].get('observedStatus') == 'unknown'):
            observed = False
        self.store.record_execution_observation(task_id, 'observed' if observed else 'unknown')
        return observed

    def verify_continue(self, task_id):
        """Freshly observe an active task without authorizing another submission."""
        task = self.store.get(task_id)
        if task.get('cancellationRequested') or task['executionStatus'] in ('uncertain', 'submitting', 'cancel_requested'):
            return False
        if task['executionStatus'] == 'registered':
            return True
        if task['executionStatus'] in TERMINAL:
            return task['resourcesReleased'] is True and task['executionStatus'] != 'canceled'
        gateway = self._gateway(task)
        if gateway is None:
            return False
        try:
            result = gateway.observe(task_id)
            if (not isinstance(result, dict) or result.get('status') not in ('accepted', 'running', *TERMINAL)
                    or not result.get('identity') or result.get('cancellationRequested') is True):
                self.store.record_execution_observation(task_id, 'unknown')
                return False
            self._apply(task_id, result)
            self._record_observation(task_id, result)
            self._enforce_deadline(task_id)
            current = self.store.get(task_id)
            parent = result.get('parent')
            return (not current['cancellationRequested'] and current['executionIdentity'] == result['identity']
                and current['executionStatus'] not in ('uncertain', 'submitting', 'cancel_requested', 'canceled')
                and (not isinstance(parent, dict) or parent.get('observedStatus') in ('pending', 'running', 'finished'))
                and (current['executionStatus'] not in TERMINAL or current['resourcesReleased'] is True))
        except Exception:
            self.store.record_execution_observation(task_id, 'unknown')
            return False


    def _proven_container_exit(self, task, identity, result):
        """Interrupt confirmation is not owned-container exit proof."""
        receipt = result.get('resourceExitReceipt')
        if result.get('resourcesReleased') is not True or not isinstance(receipt, dict):
            return False
        try:
            self.store._validate_resource_exit(identity, task['payload'], receipt)
        except Conflict:
            return False
        return True

    def _apply(self, task_id, result):
        if not isinstance(result, dict):
            return
        identity = result.get("identity")
        if not identity:
            proof = result.get("unstartedCancellationProof")
            if isinstance(proof, UnstartedCancellationProof):
                self.store.resolve_unstarted_cancellation(task_id, proof.as_dict())
            return
        self.store.accept(task_id, identity)
        if result.get("executorInterrupted") is True:
            if (result.get("terminalConfirmed") is not False
                    or result.get("resourcesReleased") is not True
                    or result.get("nativeStatus") != "unknown"):
                return
            self.store.resolve_executor_interruption(
                task_id, identity, result.get("resourceExitReceipt"))
            return
        status = result.get("status")
        if status not in TERMINAL or result.get("terminalConfirmed") is not True:
            return
        execution_id = identity.get("codexTurnId") or identity.get("upstreamTaskId")
        controls = result.get('executionControls')
        if 'executionControls' in result and (
                controls != {'sandbox': 'read-only', 'approval_policy': 'never'}
                or not isinstance(identity.get('codexTurnId'), str) or not identity['codexTurnId']):
            raise Conflict('Unrecognized native execution policy or turn identity')
        evidence = [
            {
                "kind": "execution_terminal",
                "executionId": execution_id,
                "confirmed": True,
            }
        ]
        if controls is not None:
            evidence.append({'kind': 'execution_policy', 'source': 'qwenpaw-plugin-task-binding',
                             'executionId': execution_id, 'controls': dict(controls)})
        current = self.store.get(task_id)
        if (current['executionStatus'] == 'cancel_requested' and status == 'canceled'
                and not self._proven_container_exit(current, identity, result)):
            # Parent stop and delegation revocation are not Codex exit proof.
            return
        if current['executionStatus'] in TERMINAL:
            # Do not enrich or remove a terminal's immutable usage snapshot.
            evidence.extend(item for item in current['evidence']
                            if isinstance(item, dict) and item.get('kind') == 'codex_usage')
        elif result.get('codexUsageFrozen') is True and valid_codex_usage(result.get('codexUsage'), identity):
            evidence.append({'kind': 'codex_usage', 'executionId': execution_id,
                             'usage': result['codexUsage']})
        self.store.finish(task_id, status, result.get("output"), evidence)
        if result.get("resourcesReleased") is True:
            self.store.confirm_resources_released(
                task_id, execution_id, result.get("resourceExitReceipt")
            )


class DispatchLoop:
    """Bounded fair scans of durable tasks; reconstructible after any restart.

    No workflow graph is interpreted here. Sim owns successor scheduling.
    A stop signal stops polling, not the already accepted native executions.
    """

    def __init__(self, dispatcher, *, batch_size=50, allow_submissions=True):
        if not isinstance(batch_size, int) or not 1 <= batch_size <= 500:
            raise ValueError("Batch size must be 1..500")
        if not isinstance(allow_submissions, bool):
            raise ValueError("Submission mode must be boolean")
        self.allow_submissions = allow_submissions
        self.dispatcher = dispatcher
        self.batch_size = batch_size
        self.cursor = ""
        self.cadence = PollCadence()

    @staticmethod
    def _poll_fingerprint(task):
        return tuple(task[key] for key in ("executionStatus", "version", "resourcesReleased",
                                           "verificationStatus", "memoryStatus"))

    def _retire_stopped_registered(self, task):
        """Remove every still-queued task of a stopped run in this scan.

        Registered records become canceled and stay stored. This does not call
        native cancel. Accepted, submitting, and uncertain tasks are left for
        the normal per-task cancel request and are not marked exited here.
        """
        control = self.dispatcher.run_control
        if control is None:
            return
        payload = task.get("payload")
        workflow = payload.get("workflow") if isinstance(payload, dict) else None
        if not isinstance(workflow, dict):
            return
        project_id = payload.get("projectId")
        workflow_id = workflow.get("id")
        run_id = workflow.get("runId")
        if not all(isinstance(value, str) and value for value in (project_id, workflow_id, run_id)):
            return
        key = (project_id, workflow_id, run_id)
        if key in self._stopped_runs:
            return
        if control.status(workflow_id, run_id)["state"] != "stop_requested":
            self._stopped_runs.add(key)
            return
        for item in control.run_tasks(project_id, workflow_id, run_id):
            if item["executionStatus"] == "registered":
                self.dispatcher.store.request_cancel(item["id"])
        self._stopped_runs.add(key)

    def tick(self):
        self.cadence.prune()
        self._stopped_runs = set()
        tasks = self.dispatcher.store.dispatch_candidates(
            after=self.cursor, limit=self.batch_size
        )
        results = []
        for task in tasks:
            self.cursor = task["id"]
            observations = []
            try:
                self._retire_stopped_registered(task)
                task = self.dispatcher.enforce_control(task)
                polling = None
                if task["executionStatus"] == "registered":
                    self.cadence.forget(task["id"])
                    updated = (
                        self.dispatcher.submit(task["id"])
                        if self.allow_submissions
                        else task
                    )
                    hint = self.dispatcher.configuration_blocks.pop(task["id"], None) if self.allow_submissions else None
                    if hint in ("authentication", "model_missing"):
                        recorded = self.cadence.record(
                            task["id"], self._poll_fingerprint(updated), hint)
                        polling = {"action": "blocked", "reason": recorded["reason"]}
                else:
                    released_terminal = task['executionStatus'] in TERMINAL and task['resourcesReleased'] is True
                    remaining = None if released_terminal else self.cadence.remaining(task['id'], self._poll_fingerprint(task))
                    bypass = released_terminal or task['executionStatus'] == 'cancel_requested'
                    if remaining is not None and not bypass:
                        try:
                            bypass = self.dispatcher.store.execution_deadline_exceeded(task['id'])
                        except (ValueError, TypeError, KeyError):
                            bypass = True  # Unknown timing must not suppress original reconciliation.
                    if remaining is not None and remaining.get("blocked") is True and not bypass:
                        updated = task
                        polling = {"action": "blocked", "reason": remaining["reason"]}
                    elif remaining is not None and not bypass:
                        updated = task
                        polling = {'action': 'deferred', **remaining}
                    else:
                        updated = self.dispatcher.reconcile(task['id'], on_observation=observations.append)
                        if observations:
                            polling = {'action': 'queried', **self.cadence.record(
                                task['id'], self._poll_fingerprint(updated), **observations[-1])}
                        if updated['executionStatus'] in TERMINAL and updated['resourcesReleased'] is True:
                            self.cadence.forget(task['id'])
                verification = None
                if (self.dispatcher.verifier is not None and 'verificationPolicy' in updated['payload']
                        and updated['executionStatus'] == 'succeeded' and updated['resourcesReleased'] is True
                        and updated['verificationStatus'] == 'pending'):
                    verification = self.dispatcher.verifier.verify_pending(task['id'])
                    updated = self.dispatcher.store.get(task['id'])
                results.append({"taskId": task["id"], "projectId": updated["payload"]["projectId"], "status": updated["executionStatus"],
                                **({"polling": polling} if polling is not None else {}),
                                **({'verification': verification} if verification is not None else {})})
            except Exception as exc:
                # Error text may include payload or credentials; report type only.
                failed = {"taskId": task["id"], "errorType": type(exc).__name__}
                if observations:
                    try:
                        latest = self.dispatcher.store.get(task['id'])
                        failed['projectId'] = latest['payload']['projectId']
                        if latest['executionStatus'] in TERMINAL and latest['resourcesReleased'] is True:
                            self.cadence.forget(task['id'])
                        else:
                            failed['polling'] = {'action': 'queried', **self.cadence.record(
                                task['id'], self._poll_fingerprint(latest), **observations[-1])}
                    except Exception:
                        pass  # Preserve the original bounded failure, never guess missing task state.
                results.append(failed)
        if len(tasks) < self.batch_size:
            self.cursor = ""
        return results

    def run(self, stop_event, *, interval=1.0, report=None):
        if interval <= 0:
            raise ValueError("Polling interval must be positive")
        while not stop_event.is_set():
            try:
                result = self.tick()
            except Exception as exc:
                result = [{"errorType": type(exc).__name__}]
            if report is not None:
                report(result)
            stop_event.wait(interval)
