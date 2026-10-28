#!/usr/bin/env python3
"""Compiler → native Sim → local discovery/ledgers/relay; synthetic execution only."""
import argparse
import json
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agent_line.approval_discovery import WorkflowRunLedger
from agent_line.completion_relay import CompletionRelay
from agent_line.pause_bindings import PauseBindings
from agent_line.publication import PublicationVerifier
from agent_line.sim_authoring import compile_document, decode_document, native_id
from agent_line.sim_journal import SimJournal
from agent_line.store import TaskStore
from agent_line.task_discovery import ExternalTaskDiscovery
from probes.sim.authoring_probe import write
from probes.sim.parallel_native_probe import Probe as NativeProbe

DATA = ROOT / 'var/sim-compiled-parallel'
PROJECT = 'synthetic-parallel-project'
ROLE = 'synthetic-role-binding'
AGENT = 'synthetic-no-executor'
CASES = (
    ('all_pass', 'all_success', True, True, 'completed'),
    ('all_verification_failure', 'all_success', True, False, 'failed'),
    ('partial_verification_failure', 'allow_partial', True, False, 'completed'),
    ('required_verification_failure', 'required_success', False, True, 'failed'),
)


def document(policy='all_success'):
    def node(nid, kind, name, config, x, y=0):
        return {'id': nid, 'type': kind, 'name': name,
                'position': {'x': x, 'y': y}, 'config': config}
    def agent():
        return {'roleBindingId': ROLE, 'instruction': '仅合成测试结果；禁止模型、Codex和记忆调用',
                'inputFrom': {'nodeId': 'start', 'field': 'input'},
                'memoryPolicyId': 'native-role', 'outputSchemaId': 'task-result',
                'requireMemoryCommit': False, 'allowFailureBranch': True}
    return {'schemaVersion': 1, 'variables': {}, 'nodes': [
        node('start', 'start', '开始', {}, 0),
        node('fork', 'parallel', '并行', {'branches': ['left', 'right'],
             'join': 'merge', 'maxConcurrency': 2}, 200),
        node('left', 'agent', '甲', agent(), 400),
        node('right', 'agent', '乙', agent(), 400, 250),
        node('merge', 'join', '汇合', {'parallelId': 'fork', 'policy': policy,
             'requiredBranches': ['left'] if policy == 'required_success' else []}, 650),
        node('end', 'end', '结束', {'resultFrom': 'merge'}, 900)], 'edges': [
        {'id': 'a', 'source': 'start', 'target': 'fork'},
        {'id': 'b', 'source': 'fork', 'target': 'left'},
        {'id': 'c', 'source': 'fork', 'target': 'right'},
        {'id': 'd', 'source': 'left', 'target': 'merge'},
        {'id': 'e', 'source': 'right', 'target': 'merge'},
        {'id': 'f', 'source': 'merge', 'target': 'end'}]}


def same_compiled_document(actual, expected, workflow):
    """Node list order is serialization detail; every compiled value remains compared."""
    return compile_document(actual, workflow) == compile_document(expected, workflow)


def policies(doc, workflow):
    return {native_id(workflow, node['id']): {
        key: value for key, value in node['config'].items() if key != 'inputFrom'
    } | {'roleAgentId': AGENT} for node in doc['nodes'] if node['type'] == 'agent'}


def inject_synthetic(tasks, task_id, *, branch, nonce, verification_passed):
    """Trusted test fixture only; no real executor, container, model or test is claimed."""
    task = tasks.get(task_id)
    if task['executionStatus'] != 'registered' or task['executionIdentity']:
        raise RuntimeError('synthetic_task_already_started')
    identity = {'upstreamTaskId': 'synthetic-' + task_id}
    if not tasks.claim(task_id, task['payload']['workspace']):
        raise RuntimeError('synthetic_claim_failed')
    tasks.accept(task_id, identity)
    evidence = [
        {'kind': 'execution_terminal', 'executionId': identity['upstreamTaskId'],
         'confirmed': True, 'synthetic': True, 'source': 'compiled-parallel-probe-fixture'},
        {'kind': 'synthetic_resources', 'synthetic': True, 'noExecutorCreated': True,
         'source': 'compiled-parallel-probe-fixture', 'taskId': task_id},
    ]
    tasks.finish(task_id, 'succeeded', {
        'text': json.dumps({'synthetic': True, 'branch': branch, 'nonce': nonce,
                            'fixtureVerificationPassed': verification_passed}),
        'nativeStatus': 'task_complete', 'toolEvidence': [{'synthetic': True,
            'source': 'compiled-parallel-probe-fixture', 'noExecutorCreated': True}]}, evidence)
    # There was no executor/resource to exit; do not fabricate Docker or Codex receipts.
    tasks.confirm_resources_released(task_id, identity['upstreamTaskId'])
    status = 'passed' if verification_passed else 'failed'
    receipt = {'kind': 'verification', 'taskId': task_id, 'executionIdentity': identity,
               'status': status, 'synthetic': True, 'source': 'compiled-parallel-probe-fixture',
               'checks': [{'kind': 'synthetic-fixture', 'status': status, 'synthetic': True,
                           'realCommandExecuted': False}]}
    tasks.record_verification(task_id, receipt)
    result = tasks.get(task_id)
    if (result['executionIdentity'] != identity or result['memoryStatus'] != 'not_requested'
            or result['resourcesReleased'] is not True or result['verificationStatus'] != status):
        raise RuntimeError('synthetic_store_facts_mismatch')
    return result


def open_context(directory, *, initialize=False):
    def opened(cls, filename):
        return cls.initialize(directory / filename) if initialize else cls(directory / filename)
    return {'tasks': opened(TaskStore, 'tasks.sqlite'), 'runs': opened(WorkflowRunLedger, 'runs.sqlite'),
            'pauses': opened(PauseBindings, 'pauses.sqlite'), 'journal': opened(SimJournal, 'journal.sqlite')}


def discovery(context, sim, waits, verifier, workspace):
    return ExternalTaskDiscovery(context['runs'], context['tasks'], context['pauses'], sim, waits,
        publication_verifier=verifier,
        projects={PROJECT: {'enabled': True, 'workspace': str(workspace), 'synthetic': True}},
        role_bindings={ROLE: {'enabled': True, 'projectId': PROJECT,
                             'roleAgentId': AGENT, 'synthetic': True, 'dispatcherConfigured': False}},
        allow_submissions=True, batch_size=50, journal=context['journal'])


class CompiledProbe(NativeProbe):
    def __init__(self, authpath, directory):
        super().__init__(authpath, directory)
        if self.existed and self.state.get('kind') != 'compiled-static-parallel':
            raise RuntimeError('probe_state_kind_mismatch')
        self.state['kind'] = 'compiled-static-parallel'
        self.context = None  # Observation of native identities never recreates or requires local ledgers.
        self.verifier = PublicationVerifier(self.sim)

    def observe_run(self, run):
        wid, rid = self.state['workflowId'], run['runId']
        snapshots = {}
        for target in dict.fromkeys([rid] + run['children']):
            value = self.request('GET', f'/api/v2/workflows/{wid}/runs/{target}',
                                 params={'includeOutput': 'true'}, missing=True)
            if value is not None and (value.get('runId') != target or value.get('workflowId') != wid):
                raise RuntimeError('run_identity_mismatch')
            snapshots[target] = value
        raw = self.request('GET', f'/api/resume/{wid}/{rid}', bare=True, missing=True)
        if raw is not None and (raw.get('workflowId') != wid or raw.get('executionId') != rid):
            raise RuntimeError('pause_detail_identity_mismatch')
        projected = self.waits.read_pauses(wid, rid) if raw is not None else None
        observation = {'time': time.time(), 'runs': snapshots, 'pauseDetail': raw,
                       'pauseProjection': projected}
        run.setdefault('observations', []).append(observation)
        self.save()
        return observation

    def points_for(self, run, observed):
        projection = observed.get('pauseProjection') or {}
        points = projection.get('pausePoints', [])
        expected = {native_id(self.state['workflowId'], b) for b in ('left', 'right')}
        if not isinstance(points, list):
            raise RuntimeError('pause_points_invalid')
        mapped = {p['blockId']: p for p in points}
        if len(mapped) != len(points) or not set(mapped) <= expected:
            raise RuntimeError('pause_scope_changed')
        return projection, mapped

    def no_early_join(self, run, observed):
        encoded = ((observed.get('pauseDetail') or {}).get('executionSnapshot') or {}).get('snapshot')
        if not isinstance(encoded, str) or len(encoded.encode()) > 1048576:
            raise RuntimeError('snapshot_missing')
        snapshot = json.loads(encoded)
        state = snapshot.get('state') if isinstance(snapshot, dict) else None
        if not isinstance(state, dict):
            raise RuntimeError('snapshot_state_invalid')
        states, executed, logs = (state.get(k) for k in ('blockStates', 'executedBlocks', 'blockLogs'))
        if (not isinstance(states, dict) or not isinstance(executed, list)
                or not isinstance(logs, list) or not logs
                or any(not isinstance(item, str) for item in executed)
                or any(not isinstance(item, dict) or not isinstance(item.get('blockId'), str) for item in logs)):
            raise RuntimeError('snapshot_execution_collections_invalid')
        seen = set(states) | set(executed) | {item['blockId'] for item in logs}
        allowed = {native_id(self.state['workflowId'], b) for b in ('start', 'fork', 'left', 'right')}
        if not allowed.issubset(seen) or not seen.issubset(allowed):
            raise RuntimeError('compiled_join_executed_early')
        for result in observed['runs'].values():
            if result and result.get('output'):
                raise RuntimeError('compiled_end_output_before_both_results')
        return {'blockStates': sorted(states), 'executedBlocks': executed,
                'loggedBlocks': [item['blockId'] for item in logs]}

    def poll(self, run, predicate, code):
        for _ in range(100):
            observed = self.observe_run(run)
            if predicate(observed):
                return observed
            time.sleep(0.25)
        raise RuntimeError(code + '_observation_timeout_no_resubmit')

    def initial(self, run, observed):
        projection, points = self.points_for(run, observed)
        return (projection.get('totalPauseCount') == 2 and projection.get('resumedCount') == 0
                and len(points) == 2 and all(p['resumeStatus'] == 'paused' and p['snapshotReady'] is True
                                             for p in points.values()))

    def after_first(self, run, observed):
        projection, points = self.points_for(run, observed)
        first, second = (points.get(native_id(self.state['workflowId'], b), {}) for b in run['submissionOrder'])
        if not (projection.get('totalPauseCount') == 2 and projection.get('resumedCount') == 1
                and second.get('resumeStatus') == 'paused' and second.get('snapshotReady') is True
                and first.get('resumeStatus') == 'resumed'):
            return False
        run['afterFirstNoEarlyJoin'] = self.no_early_join(run, observed)
        return True

    def publish(self, policy):
        wid = self.state['workflowId']
        doc = document(policy)
        if self.state.get('currentDocument') == doc:
            return self.state['publication']
        graph = compile_document(doc, wid)
        base = '/api/v2/workflows/' + wid
        if getattr(self, '_reuse_initial_save', False):
            if policy != 'all_success':
                raise RuntimeError('initial_recovery_policy_changed')
            self._reuse_initial_save = False
            saved = self.state['steps']['all_success-save']['result']
        else:
            saved = self.mutate(policy + '-save', 'PUT', base + '/state', graph)
        if saved.get('warnings'):
            raise RuntimeError('compiled_graph_normalization_warning')
        readback = self.request('GET', base + '/state')
        if not same_compiled_document(decode_document(readback), doc, wid):
            raise RuntimeError('compiled_readback_roundtrip_mismatch')
        deployment = self.mutate(policy + '-publish', 'POST', base + '/deploy', {})
        version = self.request('GET', base + '/versions/' + str(deployment['version']))
        if (not same_compiled_document(decode_document(version['state']), doc, wid)
                or version.get('isActive') is not True
                or version.get('id') != deployment['activeDeployment'].get('deploymentVersionId')):
            raise RuntimeError('compiled_publication_roundtrip_mismatch')
        self.state['currentDocument'] = doc
        self.state['publication'] = {'revision': str(deployment['version']),
            'deploymentVersionId': version['id'], 'document': doc, 'compiledGraph': graph,
            'published': version, 'readback': readback}
        self.save()
        return self.state['publication']

    def continue_initial(self):
        # Exactly one witnessed pre-publication serialization-order interruption.
        # No generic retry/continuation path exists for any execute or resume.
        required = {'create', 'all_success-save'}
        if (not self.existed or set(self.state['steps']) != required
                or any(v.get('status') != 'received' for v in self.state['steps'].values())
                or self.state.get('runs') != {} or self.state.get('recovery')
                or self.state.get('currentDocument') or self.state.get('publication')):
            raise RuntimeError('initial_recovery_state_invalid')
        wid = self.state.get('workflowId')
        if wid != 'fcad35eb-e5ed-421d-a2e7-3071c16bcaf1':
            raise RuntimeError('initial_recovery_workflow_changed')
        graph = compile_document(document(), wid)
        created, saved = (self.state['steps'][key] for key in ('create', 'all_success-save'))
        if (created.get('method') != 'POST' or created.get('path') != '/api/v2/workflows'
                or created.get('result', {}).get('id') != wid
                or saved.get('method') != 'PUT'
                or saved.get('path') != f'/api/v2/workflows/{wid}/state'
                or saved.get('payload') != graph or saved.get('result', {}).get('id') != wid
                or saved.get('result', {}).get('warnings')):
            raise RuntimeError('initial_recovery_original_receipt_changed')
        if self.directory.stat().st_mode & 0o077:
            raise RuntimeError('initial_recovery_directory_not_private')
        for filename in ('tasks.sqlite', 'runs.sqlite', 'pauses.sqlite', 'journal.sqlite'):
            path = self.directory / filename
            if not path.is_file() or path.is_symlink() or path.stat().st_mode & 0o077:
                raise RuntimeError('initial_recovery_database_not_private')
        self.context = open_context(self.directory)
        table_sets = {'tasks': ('tasks', 'workspace_leases', 'events', 'admission_guard'),
                      'runs': ('runs', 'pauses'), 'pauses': ('bindings',), 'journal': ('operations',)}
        counts = {}
        for key, tables in table_sets.items():
            with self.context[key].connect() as db:
                for table in tables:  # Closed names above, never user-supplied SQL identifiers.
                    count = db.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                    counts[key + '.' + table] = count
                    if count != 0:
                        raise RuntimeError('initial_recovery_database_not_empty')
        self.workspace_path = self.directory / 'synthetic-workspace'
        expected_config = {'projectId': PROJECT, 'roleBindingId': ROLE, 'roleAgentId': AGENT,
            'synthetic': True, 'dispatcherConfigured': False,
            'workspace': str(self.workspace_path.resolve(strict=True)), 'requireMemoryCommit': False}
        if self.state.get('syntheticConfiguration') != expected_config:
            raise RuntimeError('initial_recovery_fixture_config_changed')
        base = '/api/v2/workflows/' + wid
        detail = self.request('GET', base)
        versions = self.request('GET', base + '/versions', allow_list=True)
        draft = self.request('GET', base + '/state')
        if (detail.get('id') != wid or detail.get('workspaceId') != self.workspace
                or detail.get('isDeployed') is not False or detail.get('activeDeployment')
                or versions != []
                or not same_compiled_document(decode_document(draft), document(), wid)):
            raise RuntimeError('initial_recovery_native_draft_or_publication_changed')
        self.state['recovery'] = {'status': 'validated', 'workflowId': wid,
            'recordedAt': time.time(), 'emptyDatabaseCounts': counts,
            'scope': 'original create/save received; no publication/run exists; no mutations replayed'}
        self.save()
        self._reuse_initial_save = True
        for case in CASES:
            self.run_case(case)
        self.state['result'] = 'passed_live_synthetic_execution'
        self.state['recovery']['status'] = 'completed'
        self.save()
        return self.report(self.state['result'])

    def execute(self):
        if self.existed:
            raise RuntimeError('existing_state_observation_only')
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.stat().st_mode & 0o077:
            raise RuntimeError('private_directory_required')
        self.save()
        self.context = open_context(self.directory, initialize=True)
        self.workspace_path = self.directory / 'synthetic-workspace'
        self.workspace_path.mkdir(mode=0o700)
        self.state['syntheticConfiguration'] = {'projectId': PROJECT, 'roleBindingId': ROLE,
            'roleAgentId': AGENT, 'synthetic': True, 'dispatcherConfigured': False,
            'workspace': str(self.workspace_path.resolve()), 'requireMemoryCommit': False}
        self.save()
        payload = {'workspaceId': self.workspace, 'name': 'agent-line-compiled-parallel-probe',
            'description': 'Compiler/discovery/relay proof; synthetic execution only; no models or memory'}
        created = self.mutate('create', 'POST', '/api/v2/workflows', payload)
        self.state['workflowId'] = created['id']
        self.save()
        for case in CASES:
            self.run_case(case)
        self.state['result'] = 'passed_live_synthetic_execution'
        self.save()
        return self.report(self.state['result'])

    def run_case(self, case):
        name, policy, left_passed, right_passed, expected_status = case
        publication = self.publish(policy)
        wid = self.state['workflowId']
        run = {'runId': str(uuid.uuid4()), 'children': [], 'nonce': uuid.uuid4().hex,
               'policy': policy, 'expectedStatus': expected_status, 'publication': publication,
               'taskIds': {}, 'syntheticVerificationPassed': {'left': left_passed, 'right': right_passed}}
        self.state['runs'][name] = run
        self.save()
        agent_policies = policies(publication['document'], wid)
        blocks = sorted(agent_policies)
        self.verifier.check_definition(wid, publication['revision'], publication['deploymentVersionId'],
                                       blocks, require_active=True, static_parallel=True)
        self.context['runs'].reserve_tasks(project_id=PROJECT, workflow_id=wid, run_id=run['runId'],
            revision=publication['revision'], deployment_version_id=publication['deploymentVersionId'],
            agent_policies=agent_policies, static_parallel=True)
        inputs = {'synthetic': True, 'nonce': run['nonce'], 'case': name,
                  'instruction': '合成验证；不会分派模型或真实执行器', 'nested': {'preserve': [True, False, None]}}
        run['input'] = inputs
        self.save()
        record = self.once(name + '-execute', 'POST', f'/api/v2/workflows/{wid}/execute',
            {'workflowId': wid, 'runId': run['runId'], 'input': {'input': inputs}},
            lambda: self.context['journal'].execute(self.sim, wid, run['runId'], {'input': inputs}))
        if record.get('status') != 'accepted':
            raise RuntimeError('compiled_execute_not_accepted_no_resubmit')
        self.context['runs'].record_submission(wid, run['runId'], record['status'])
        initial = self.poll(run, lambda o: self.initial(run, o), 'compiled_dual_pause')
        run['initialNoEarlyJoin'] = self.no_early_join(run, initial)
        observer = discovery(self.context, self.sim, self.waits, self.verifier, self.workspace_path)
        registration = observer.tick()
        run['discovery'] = registration
        self.save()
        for branch in ('left', 'right'):
            binding = self.context['pauses'].for_context(wid, run['runId'], native_id(wid, branch))
            task = self.context['tasks'].get(binding.task_id)
            if (task['payload']['input']['taskInput'] != inputs or binding.allow_multiple is not True
                    or task['payload']['roleAgentId'] != AGENT or task['executionIdentity']):
                raise RuntimeError('compiled_discovery_task_or_binding_mismatch')
            run['taskIds'][branch] = task['id']
        if len(set(run['taskIds'].values())) != 2:
            raise RuntimeError('compiled_branch_task_identity_collision')
        self.save()
        repeated = discovery(open_context(self.directory), self.sim, self.waits, self.verifier,
                             self.workspace_path).tick()
        run['reopenedDiscovery'] = repeated
        current = [item for item in repeated if item.get('workflowId') == wid
                   and item.get('runId') == run['runId']]
        repeated_tasks = current[0].get('tasks', []) if len(current) == 1 else []
        if (len(repeated_tasks) != 2
                or {item.get('taskId') for item in repeated_tasks} != set(run['taskIds'].values())
                or any(item.get('reusedExistingTask') is not True for item in repeated_tasks)):
            raise RuntimeError('compiled_reopened_discovery_identity_changed')
        if not self.verifier.verify_run(self.context['runs'].get(wid, run['runId'])):
            raise RuntimeError('compiled_run_publication_unverified')
        run['submissionOrder'] = sorted(run['taskIds'], key=lambda name: self.context['tasks'].events(run['taskIds'][name])[0]['seq'])
        for branch in run['submissionOrder']:
            task_id = run['taskIds'][branch]
            # This injection is explicitly fixture data in the private local TaskStore.
            task = inject_synthetic(self.context['tasks'], task_id, branch=branch,
                nonce=run['nonce'], verification_passed=run['syntheticVerificationPassed'][branch])
            run.setdefault('syntheticTasks', {})[branch] = task
            self.save()
            binding = self.context['pauses'].get(task_id)
            relay = CompletionRelay(self.sim, self.context['journal'], wait_client=self.waits)
            result = self.once(name + '-relay-' + branch, 'POST',
                f'/api/v2/workflows/{wid}/runs/{run["runId"]}/resume',
                {'taskId': task_id, 'contextId': binding.context_id, 'synthetic': True},
                lambda: relay.deliver(task, binding))
            if result.get('status') != 'accepted':
                raise RuntimeError('compiled_relay_not_accepted_no_resubmit')
            receipt = result.get('receipt') or {}
            child = receipt.get('resumedRunId')
            if not child or child in [run['runId']] + run['children']:
                raise RuntimeError('compiled_relay_child_identity_invalid')
            run['children'].append(child)
            resumed = self.context['journal'].resume_input(wid, run['runId'], binding.context_id)
            if (resumed['taskId'] != task_id or resumed['output'] != task['output']
                    or resumed['evidence'] != task['evidence']
                    or resumed['executionIdentity'] != {'upstreamTaskId': 'synthetic-' + task_id}):
                raise RuntimeError('compiled_relay_frozen_result_changed')
            run.setdefault('frozenResults', {})[branch] = resumed
            self.save()
            before = len(self.calls)
            duplicate = CompletionRelay(self.sim, SimJournal(self.context['journal'].path),
                wait_client=self.waits).deliver(self.context['tasks'].get(task_id), binding)
            if duplicate != result or len(self.calls) != before:
                raise RuntimeError('compiled_relay_duplicate_not_read_only')
            if branch == run['submissionOrder'][0]:
                run['afterFirst'] = self.poll(run, lambda o: self.after_first(run, o), 'compiled_first_relay')
                self.save()
        self.verify_terminal(run)
        # Only original upstream status can mark the discovery ledger terminal.
        run['terminalDiscovery'] = observer.tick()
        run['result'] = 'passed_live_synthetic_execution'
        self.save()
        print(json.dumps({'case': name, 'status': run['result'], 'nativeStatus': expected_status}), flush=True)

    def verify_terminal(self, run):
        wid, final_id = self.state['workflowId'], run['children'][-1]
        def terminal(observed):
            value = observed['runs'].get(final_id) or {}
            projection, points = self.points_for(run, observed)
            return (value.get('status') in ('completed', 'failed', 'cancelled') and len(points) == 2
                    and all(p['resumeStatus'] in ('resumed', 'failed') for p in points.values()))
        final = self.poll(run, terminal, 'compiled_terminal')
        run['final'] = final
        result = final['runs'][final_id]
        if result.get('status') != run['expectedStatus']:
            raise RuntimeError('compiled_terminal_status_mismatch')
        projection, points = self.points_for(run, final)
        statuses = {b: points[native_id(wid, b)]['resumeStatus'] for b in ('left', 'right')}
        first, last = run.get('submissionOrder', ['right', 'left'])
        allowed_last = {'resumed'} if run['expectedStatus'] == 'completed' else {'resumed', 'failed'}
        if (statuses[first] != 'resumed' or statuses[last] not in allowed_last
                or projection['resumedCount'] != sum(v == 'resumed' for v in statuses.values())):
            raise RuntimeError('compiled_terminal_pause_states_mismatch')
        if run['expectedStatus'] == 'completed':
            successful = [b for b in ('left', 'right') if run['syntheticVerificationPassed'][b]]
            expected = {'executionStatus': 'succeeded', 'verificationStatus': 'passed',
                        'output': {'policy': run['policy'], 'branches': run['frozenResults'],
                                   'successfulBranches': successful}}
            if (result.get('output') or {}).get('data') != {'agentLineResult': expected}:
                raise RuntimeError('compiled_terminal_original_branch_results_mismatch')
            selected = self.request('GET', f'/api/v2/workflows/{wid}/runs/{final_id}',
                params={'includeOutput': 'true', 'selectedOutputs': native_id(wid, 'merge')})
            if (selected.get('runId') != final_id or selected.get('workflowId') != wid
                    or (selected.get('blockOutputs') or {}).get(native_id(wid, 'merge'), {}).get('result') != expected):
                raise RuntimeError('compiled_join_output_mismatch')
            run['nativeJoinOutput'] = selected
        elif 'join_policy_unsatisfied' not in json.dumps(result.get('error'), ensure_ascii=False):
            raise RuntimeError('compiled_join_policy_failure_not_proven')
        self.save()

    def inspect(self):
        if self.existed and self.state.get('workflowId'):
            for run in self.state['runs'].values():
                self.observe_run(run)
        return self.report('observation_only' if self.existed else 'not_executed')

    def report(self, status):
        report = {'status': status, 'priorResult': self.state.get('result'),
            'workflowId': self.state.get('workflowId'),
            'nativeWritesThisInvocation': sum(c['method'] != 'GET' for c in self.calls),
            'nativeCalls': self.calls,
            'cases': {name: {'runId': run['runId'], 'children': run['children'],
                'taskIds': run['taskIds'], 'expectedStatus': run['expectedStatus'], 'result': run.get('result')}
                for name, run in self.state['runs'].items()},
            'syntheticExecutionOnly': True, 'realModelCalls': 0, 'realMemoryCalls': 0,
            'scope': 'Compiler/native Sim/real local discovery, task and pause ledgers, CompletionRelay, '
                     'SimJournal; synthetic execution/verification/resource fixture only; no QwenPaw/Codex closure'}
        if self.directory.exists():
            write(self.directory / ('report.json' if status == 'passed_live_synthetic_execution' else 'observation.json'), report)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--execute', action='store_true')
    mode.add_argument('--continue-initial', action='store_true',
        help='one witnessed initial pre-publication readback interruption only')
    parser.add_argument('--config', type=Path, default=ROOT / 'var/sim/probe-auth.json')
    parser.add_argument('--directory', type=Path, default=DATA)
    parser.add_argument('--graph', action='store_true', help='credential-free compile preview; no network')
    args = parser.parse_args()
    if args.graph:
        print(json.dumps(compile_document(document(), 'compiled_parallel_preview'), ensure_ascii=False, indent=2))
        return 0
    probe = CompiledProbe(args.config, args.directory)
    try:
        if args.continue_initial:
            result = probe.continue_initial()
        else:
            result = probe.execute() if args.execute and not probe.existed else probe.inspect()
        print(json.dumps({k: result[k] for k in ('status', 'nativeWritesThisInvocation')}))
        return 0
    except Exception as exc:
        failure = {'status': 'incomplete', 'errorType': type(exc).__name__,
            'code': str(exc) if type(exc) is RuntimeError else getattr(exc, 'code', 'probe_check_failed'),
            'nativeCalls': probe.calls,
            'nativeWritesThisInvocation': sum(c['method'] != 'GET' for c in probe.calls)}
        if args.directory.exists():
            write(args.directory / ('failure-' + uuid.uuid4().hex + '.json'), failure)
        print(json.dumps({k: failure[k] for k in ('status', 'errorType', 'code')}))
        return 1
    finally:
        probe.sim.close()


if __name__ == '__main__':
    raise SystemExit(main())
