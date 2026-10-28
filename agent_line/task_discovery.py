"""Turn a proven native machine pause into one durable existing-role task.

Sim selects the node and resolves its input. This observer neither evaluates
edges nor advances the graph. CompletionRelay exclusively returns task results.
"""
from pathlib import Path
from contextlib import nullcontext
import re
import uuid

from .adapters.sim import host_role_shared_across_projects, segment
from .adapters.sim_wait import WaitInputUnavailable
from .completion_relay import ResumeBinding, frozen_result_matches
from .store import AdmissionClosed, Conflict, canonical
from .run_control import RunControlClosed
from .execution_limits import DEFAULT_LIMITS, validate_limit_values


class ExternalTaskDiscovery:
    def __init__(self, runs, tasks, pauses, client, wait_client, *, publication_verifier,
                 projects, role_bindings, allow_submissions=False, batch_size=50, journal=None):
        if (type(allow_submissions) is not bool or type(batch_size) is not int
                or not 1 <= batch_size <= 500
                or not callable(getattr(publication_verifier, 'verify_run', None))):
            raise ValueError('Verified publication reader and bounded task discovery required')
        self.runs, self.tasks, self.pauses = runs, tasks, pauses
        self.client, self.wait_client = client, wait_client
        self.publication_verifier = publication_verifier
        self.projects, self.role_bindings = projects, role_bindings
        from .run_budget import validate_run_budget
        from .project_concurrency import validate_project_limit
        for project in projects.values():
            if 'maxConcurrentTasks' in project:
                validate_project_limit(project['maxConcurrentTasks'])
            if 'runBudget' in project and validate_run_budget(project['runBudget']) is None:
                raise ValueError('An explicit run budget must be configured')
        self.allow_submissions, self.batch_size, self.cursor = allow_submissions, batch_size, 0
        self.journal = journal
        self.run_control = None

    def _loop_context(self, run, block, context, policy):
        loop = run.get('loopPolicies', {}).get(block)
        if loop is None:
            if context != block:
                raise ValueError('Unproven repeated activation')
            return None
        if (self.journal is None or not isinstance(context, str) or not isinstance(loop, dict)
                or set(loop) != {'loopId', 'maxIterations'} or type(loop['maxIterations']) is not int
                or not 1 <= loop['maxIterations'] <= 3 or policy['allowFailureBranch'] is not True):
            raise ValueError('Unproven bounded loop policy')
        match = re.fullmatch(re.escape(block) + r'_loop([0-2])', context)
        if match is None or int(match[1]) >= loop['maxIterations']:
            raise ValueError('Pause outside bounded loop')
        iteration = int(match[1])
        previous = None
        if iteration:
            previous_context = block + '_loop' + str(iteration - 1)
            binding = self.pauses.for_context(run['workflowId'], run['runId'], previous_context)
            task = self.tasks.get(binding.task_id)
            workflow = task['payload']['workflow']
            if (binding.block_id != block or task['payload']['projectId'] != run['projectId']
                    or task['payload']['roleAgentId'] != policy['roleAgentId']
                    or task['payload']['roleBindingId'] != policy['roleBindingId']
                    or ('roleRevision' in task['payload']) != ('roleRevision' in policy)
                    or task['payload'].get('roleRevision') != policy.get('roleRevision')
                    or workflow['id'] != run['workflowId'] or workflow['runId'] != run['runId']
                    or workflow['revision'] != run['revision'] or workflow['nodeId'] != block
                    or workflow['nodeExecutionId'] != previous_context
                    or workflow['attemptId'] != str(iteration) or task['resourcesReleased'] is not True
                    or task.get('cancellationRequested') is True
                    or (policy['requireMemoryCommit'] and task['memoryStatus'] != 'committed')
                    or not (task['executionStatus'] == 'failed' or (
                        task['executionStatus'] == 'succeeded' and task['verificationStatus'] == 'failed'))):
                raise ValueError('Prior task cannot authorize another repair')
            record = self.journal.get(run['workflowId'], run['runId'], previous_context)
            receipt = record.get('receipt')
            if (record['status'] != 'accepted' or not isinstance(receipt, dict)
                    or receipt.get('workflowId') != run['workflowId'] or receipt.get('runId') != run['runId']
                    or receipt.get('contextId') != previous_context or receipt.get('transportStatus') != 'accepted'):
                raise ValueError('Previous native resume not confirmed')
            segment(receipt.get('resumedRunId'))
            previous = self.journal.resume_input(run['workflowId'], run['runId'], previous_context)
            if not frozen_result_matches(task, previous):
                raise ValueError('Previous result identity mismatch')
        return {'iteration': iteration, 'maxIterations': loop['maxIterations'], 'previousResult': previous}

    def _discover(self, run):
        if self.run_control is not None:
            self.run_control.reconcile_stop(run['projectId'], run['workflowId'], run['runId'], self.client)
        observed = self.client.observe(run['workflowId'], run['runId'])
        envelope = observed.get('response', {}) if isinstance(observed, dict) else {}
        native = envelope.get('data', envelope) if isinstance(envelope, dict) else {}
        if (not isinstance(native, dict) or native.get('workflowId') != run['workflowId']
                or native.get('runId') != run['runId']):
            return {'status': 'unknown'}
        if native.get('status') in ('completed', 'failed', 'cancelled', 'canceled'):
            self.runs.observe_transition(run, None, terminal=True)
            return {'status': 'terminal_observed'}
        if native.get('status') != 'paused':
            return {'status': 'waiting'}
        if run.get('staticParallel') is True:
            terminal = self._static_terminal(run)
            if terminal is not None:
                return terminal
        pause = native.get('paused')
        if not isinstance(pause, dict):
            return {'status': 'unknown'}
        total, resumed = pause.get('pausePointCount'), pause.get('resumedCount')
        if (run.get('staticParallel') is True and type(total) is int and type(resumed) is int
                and 1 < total <= 200 and 0 <= resumed < total - 1):
            return self._discover_multiple(run, pause)
        return self._bind_task(run, pause)

    def _static_terminal(self, run):
        # A failed resume child can leave Sim's original root historically paused.
        # Observe only an accepted child after every known native point settled;
        # an earlier completed child never ends another branch still waiting.
        if self.journal is None:
            return None
        summary = self.journal.resume_summary(run['workflowId'], run['runId'])
        if not isinstance(summary, dict):
            return None
        latest, accepted = summary.get('latestAttempt'), summary.get('latestAccepted')
        if (not isinstance(latest, dict) or not isinstance(accepted, dict)
                or latest.get('status') != 'accepted' or accepted.get('status') != 'accepted'
                or latest.get('runId') != accepted.get('runId')
                or latest.get('contextId') != accepted.get('contextId')):
            return None
        try:
            child_id = segment(accepted.get('runId'))
            details = self.wait_client.read_pauses(run['workflowId'], run['runId'])
            points = details['pausePoints']
            if (details['workflowId'] != run['workflowId'] or details['runId'] != run['runId']
                    or not points or accepted['contextId'] not in {p['contextId'] for p in points}
                    or any(p['blockId'] not in run['humanBlockIds']
                           or p['resumeStatus'] not in ('resumed', 'failed') for p in points)):
                return None
            for point in points:
                if point['blockId'] not in run['agentPolicies']:
                    continue
                binding = self.pauses.for_context(run['workflowId'], run['runId'], point['contextId'])
                task = self.tasks.get(binding.task_id)
                record = self.journal.get(run['workflowId'], run['runId'], point['contextId'])
                frozen = self.journal.resume_input(run['workflowId'], run['runId'], point['contextId'])
                if (record['status'] != 'accepted' or task['resourcesReleased'] is not True
                        or not frozen_result_matches(task, frozen)):
                    return None
        except (WaitInputUnavailable, ValueError, KeyError, TypeError):
            return None
        try:
            if self.publication_verifier.verify_run(run) is not True:
                return {'status': 'version_unverified'}
        except Exception:
            return {'status': 'version_unverified'}
        observed = self.client.observe(run['workflowId'], child_id)
        envelope = observed.get('response', {}) if isinstance(observed, dict) else {}
        child = envelope.get('data', envelope) if isinstance(envelope, dict) else {}
        if (not isinstance(child, dict) or child.get('workflowId') != run['workflowId']
                or child.get('runId') != child_id
                or child.get('status') not in ('completed', 'failed', 'cancelled', 'canceled')):
            return None
        self.runs.observe_transition(run, None, terminal=True)
        return {'status': 'terminal_observed', 'terminalRunId': child_id, 'nativeStatus': child['status']}

    def _discover_multiple(self, run, summary):
        # The native detail, rather than the single-context v2 summary, selects
        # pending branches. Validate the whole identity list before registering
        # any subset; each input is subsequently checked again by its reader.
        try:
            if self.publication_verifier.verify_run(run) is not True:
                return {'status': 'version_unverified'}
        except Exception:
            return {'status': 'version_unverified'}
        try:
            detail = self.wait_client.read_pauses(run['workflowId'], run['runId'])
            points = detail.get('pausePoints')
            if (detail.get('workflowId') != run['workflowId'] or detail.get('runId') != run['runId']
                    or type(points) is not list or not 1 <= len(points) <= 200
                    or type(detail.get('totalPauseCount')) is not int
                    or detail['totalPauseCount'] != len(points)
                    or detail['totalPauseCount'] != summary['pausePointCount']
                    or type(detail.get('resumedCount')) is not int
                    or detail['resumedCount'] != summary['resumedCount']):
                return {'status': 'external_task_input_unavailable'}
            seen = set()
            for point in points:
                if (not isinstance(point, dict) or not isinstance(point.get('blockId'), str)
                        or point.get('contextId') != point['blockId']
                        or point['blockId'] not in run['humanBlockIds'] or point['blockId'] in seen
                        or point.get('pauseKind') != 'human'
                        or type(point.get('snapshotReady')) is not bool
                        or not isinstance(point.get('resumeStatus'), str)
                        or point['resumeStatus'] not in {'paused', 'resumed', 'failed', 'queued', 'resuming'}):
                    return {'status': 'external_task_input_unavailable'}
                segment(point['blockId'])
                seen.add(point['blockId'])
            if detail['resumedCount'] != sum(p['resumeStatus'] == 'resumed' for p in points):
                return {'status': 'external_task_input_unavailable'}
        except (WaitInputUnavailable, ValueError, KeyError, TypeError, AttributeError):
            return {'status': 'external_task_input_unavailable'}
        results = []
        for point in points:
            if (point['resumeStatus'] != 'paused' or point['snapshotReady'] is not True
                    or point['blockId'] not in run['agentPolicies']):
                continue
            pause = {'blockedOnBlockId': point['blockId'], 'contextId': point['contextId']}
            result = self._bind_task(run, pause, allow_multiple=True, verified=True)
            results.append({'nodeId': point['blockId'], 'nodeExecutionId': point['contextId'], **result})
        errors = [r['status'] for r in results if r['status'] != 'external_task_bound']
        return {'status': errors[0] if errors else 'external_task_bound' if results else 'waiting',
                'tasks': results}

    def _bind_task(self, run, pause, *, allow_multiple=False, verified=False):
        block = pause.get('blockedOnBlockId')
        policy = run.get('agentPolicies', {}).get(block) if isinstance(block, str) else None
        if not isinstance(policy, dict):
            if isinstance(block, str) and block in run.get('approvalBlockIds', []):
                return {'status': 'waiting', 'otherPurpose': 'human_approval'}
            return {'status': 'not_allowed_human_pause'}
        try:
            if not verified and self.publication_verifier.verify_run(run) is not True:
                return {'status': 'version_unverified'}
        except Exception:
            return {'status': 'version_unverified'}
        project = self.projects.get(run['projectId'])
        role = self.role_bindings.get(policy['roleBindingId'])
        if (not isinstance(project, dict) or project.get('enabled', True) is not True
                or not isinstance(role, dict) or role.get('enabled', True) is not True
                or role.get('projectId') != run['projectId']
                or role.get('roleAgentId') != policy['roleAgentId']
                or ('roleRevision' in role) != ('roleRevision' in policy)
                or role.get('roleRevision') != policy.get('roleRevision')
                or host_role_shared_across_projects(self.role_bindings, role.get('roleAgentId'), run['projectId'])):
            return {'status': 'external_task_role_unavailable'}
        if not self.allow_submissions:
            return {'status': 'external_task_admission_closed'}
        readiness = getattr(self, 'execution_readiness', None)
        if readiness is not None:
            try:
                readiness.assert_current()
            except ValueError:
                return {'status': 'external_task_admission_closed'}
        context = pause.get('contextId')
        try:
            repair = self._loop_context(run, block, context, policy)
        except (ValueError, KeyError, TypeError):
            return {'status': 'ambiguous_activation'}
        try:
            resolved = self.wait_client.read_input(run['workflowId'], run['runId'], block,
                **({'context_id': context} if repair is not None else {}),
                **({'allow_multiple': True} if allow_multiple or run.get('staticParallel') is True else {}))
        except WaitInputUnavailable:
            return {'status': 'external_task_input_unavailable'}
        identity = resolved.get('run', {})
        if (identity.get('workflowId') != run['workflowId'] or identity.get('runId') != run['runId']
                or identity.get('blockId') != block or identity.get('contextId') != context
                or identity.get('status') != 'paused' or not isinstance(resolved.get('input'), dict)):
            return {'status': 'external_task_input_unavailable'}
        if repair is not None and (type(resolved['input'].get('iteration')) is not int
                                   or resolved['input']['iteration'] != repair['iteration']):
            return {'status': 'external_task_input_unavailable'}
        task_input = {'instruction': policy['instruction'], 'taskInput': resolved['input']}
        business = None
        if self.journal is not None:
            from .work_items import run_item, task_context
            try:
                item = run_item(self.journal, run['projectId'], run['workflowId'], run['runId'])
                if item is not None:
                    business = task_context(item)
                    task_input['workItem'] = business
            except (KeyError, Conflict, ValueError):
                return {'status': 'external_task_business_context_unavailable'}
        if repair is not None:
            task_input['repair'] = repair
        batch_item = None
        if 'batchItem' in policy:
            from .batch_item import validate_batch_item, batch_node_id
            from .sim_authoring import native_id
            try:
                batch_item = validate_batch_item(policy['batchItem'])
                if block != native_id(run['workflowId'], batch_node_id(batch_item)):
                    raise ValueError('batch_item_invalid')
                task_input['batchItem'] = batch_item
            except ValueError:
                return {'status': 'external_task_invalid'}
        attempt = str(repair['iteration'] + 1) if repair is not None else '1'
        if len(canonical(task_input)) > 24000:
            return {'status': 'external_task_invalid'}
        try:
            limits = validate_limit_values(policy.get('limits', DEFAULT_LIMITS))
        except ValueError:
            return {'status': 'external_task_invalid'}
        payload = {
            'schemaVersion': '1.0', 'projectId': run['projectId'],
            'roleBindingId': policy['roleBindingId'], 'roleAgentId': policy['roleAgentId'],
            'workspace': str(Path(project['workspace']).resolve(strict=True)),
            'workflow': {'id': run['workflowId'], 'revision': run['revision'],
                         'runId': run['runId'], 'nodeId': block, 'nodeExecutionId': context, 'attemptId': attempt},
            'input': task_input, 'memoryPolicyId': policy['memoryPolicyId'],
            'outputSchemaId': policy['outputSchemaId'],
            'limits': limits,
        }
        if batch_item is not None:
            payload['batchItem'] = batch_item
        if business is not None:
            payload['workItem'] = business
        if policy['outputSchemaId'] in ('fields-result-v1', 'fields-result-v2') or 'outputFields' in policy:
            from .output_contract import validate_output_fields
            if policy['outputSchemaId'] not in ('fields-result-v1', 'fields-result-v2'):
                return {'status': 'external_task_invalid'}
            try:
                payload['outputFields'] = validate_output_fields(policy.get('outputFields'), policy['outputSchemaId'])
            except (ValueError, TypeError):
                return {'status': 'external_task_invalid'}
        if 'roleRevision' in policy:
            from .role_revision import validate_role_revision
            payload['roleRevision'] = validate_role_revision(policy['roleRevision'])
        if 'verificationPolicy' in policy:
            from .verification_policy import validate_policy
            payload['verificationPolicy'] = validate_policy(policy['verificationPolicy'])
        key = 'sim-agent-' + uuid.uuid5(uuid.NAMESPACE_URL,
                canonical([run['projectId'], run['workflowId'], run['runId'], context, attempt])).hex
        try:
            with self.tasks.workflow_admission(), (self.run_control.admission(run['workflowId'], run['runId'])
                    if self.run_control is not None else nullcontext()):
                payload = self.tasks.bind_project_concurrency(key, payload, project.get('maxConcurrentTasks'))
                payload = self.tasks.bind_run_budget(payload, run.get('runBudget'))
                if payload.get('runBudget') != run.get('runBudget'):
                    raise Conflict('Task budget differs from its frozen workflow run')
                task, reused = self.tasks.register(key, payload)
                binding = ResumeBinding(task['id'], run['workflowId'], run['runId'], block, context,
                                        'external_task_result', policy['requireMemoryCommit'],
                                        policy['allowFailureBranch'], allow_multiple=run.get('staticParallel') is True)
                self.pauses.bind(binding)
        except AdmissionClosed:
            return {'status': 'external_task_admission_closed'}
        except RunControlClosed:
            return {'status': 'external_task_admission_closed'}
        except Conflict:
            return {'status': 'external_task_conflict'}
        return {'status': 'external_task_bound', 'taskId': task['id'], 'reusedExistingTask': reused}

    def tick(self):
        page = self.runs.page(after=self.cursor, limit=self.batch_size)
        if not page and self.cursor:
            self.cursor = 0
            page = self.runs.page(limit=self.batch_size)
        results = []
        for run in page:
            self.cursor = run['seq']
            if run.get('purpose') != 'external_task_result':
                continue
            try:
                result = self._discover(run)
            except (ValueError, KeyError, TypeError):
                result = {'status': 'external_task_invalid'}
            except Exception:
                result = {'status': 'unknown'}
            if 'otherPurpose' not in result:
                self.runs.record_registration(run, result['status'])
            results.append({'workflowId': run['workflowId'], 'runId': run['runId'], **result})
        if len(page) < self.batch_size:
            self.cursor = 0
        return results

    def run(self, stop_event, *, interval=1.0, report=None):
        if interval <= 0:
            raise ValueError('Positive task discovery interval required')
        while not stop_event.is_set():
            try:
                result = self.tick()
            except Exception:
                result = [{'status': 'unknown'}]
            if report is not None:
                report(result)
            stop_event.wait(interval)
