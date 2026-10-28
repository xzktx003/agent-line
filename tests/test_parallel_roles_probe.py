"""Offline gates for the real probe; no Sim, QwenPaw, models or Docker are run."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from agent_line.sim_authoring import native_id
from agent_line.store import TaskStore
from probes.full_workflow import parallel_roles as probe


class ParallelRolesProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.suffix = 'a1b2c3d4e5f60718'
        self.revision = 'qwenpaw-role-v1:' + 'a' * 64


    def test_distinct_artifacts_nonces_and_frozen_bytes_preserve_original_role(self):
        items = probe.work_items(self.suffix)
        bindings = probe.policies(items, self.revision, self.suffix)
        self.assertEqual(len({i['artifact'] for i in items.values()}), 2)
        self.assertEqual(len({i['nonce'] for i in items.values()}), 2)
        self.assertEqual(len({i['marker'] for i in items.values()}), 2)
        for branch in probe.BRANCHES:
            binding = bindings[probe.BINDINGS[branch]]
            self.assertEqual(binding['roleAgentId'], probe.ROLE)
            self.assertEqual(binding['projectId'], probe.PROJECT)
            self.assertEqual(binding['roleRevision'], self.revision)
            artifact = binding['verificationPolicy']['artifacts'][0]
            actual = (items[branch]['marker'] + '\n').encode()
            self.assertEqual(artifact['path'], items[branch]['artifact'])
            self.assertEqual(artifact['exactSize'], len(actual))
            self.assertEqual(artifact['sha256'], hashlib.sha256(actual).hexdigest())
        for suffix in ('../../bad', 'g' * 16, 'a' * 17, None):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                probe.work_items(suffix)

    def test_once_commits_intent_before_operation_and_never_retries_unknown_or_received(self):
        state, snapshots = {'steps': {}}, []
        def persist(): snapshots.append(copy.deepcopy(state))
        def unknown():
            self.assertEqual(snapshots[-1]['steps']['create'], {'status': 'intent'})
            raise TimeoutError('unknown')
        with self.assertRaises(TimeoutError):
            probe.once(state, 'create', unknown, persist)
        operation = Mock()
        with self.assertRaisesRegex(RuntimeError, 'operation_already_has_intent'):
            probe.once(state, 'create', operation, persist)
        self.assertEqual(probe.once(state, 'save', lambda: {'saved': True}, persist), {'saved': True})
        with self.assertRaisesRegex(RuntimeError, 'operation_already_has_intent'):
            probe.once(state, 'save', operation, persist)
        operation.assert_not_called()

    def test_existing_unknown_create_observation_opens_no_writer_or_native_calls(self):
        store = TaskStore.initialize(self.root / 'tasks.sqlite')
        before = probe.digest(store.path)
        state = {'steps': {'create': {'status': 'intent'}}}
        sim = Mock()
        with patch.object(probe, 'TaskStore', side_effect=AssertionError('must use sqlite ro')):
            result = probe.observe_existing(self.root, state, sim, [])
        self.assertEqual(result, {'mode': 'observation_only', 'newSubmissions': 0,
                                  'nativeWritesThisInvocation': 0, 'taskCount': 0})
        self.assertEqual(probe.digest(store.path), before)
        sim.assert_not_called()
        sim.observe.assert_not_called()

    def plugin_files(self):
        source = self.root / 'plugins/agent-line'
        installed = self.root / 'var/qwenpaw/plugins/agent-line'
        for directory in (source, installed):
            directory.mkdir(parents=True)
            (directory / 'execution_bridge.py').write_text('native_execution = True\n')
        pid = os.getpid()
        stat = (Path('/proc') / str(pid) / 'stat').read_text()
        process = {'pid': pid, 'start_ticks': stat[stat.rfind(')') + 1:].split()[19]}
        return source, installed, process

    def test_plugin_disk_equality_and_new_process_required_before_any_model(self):
        _, installed, process = self.plugin_files()
        with patch.object(probe.psutil, 'Process') as running:
            running.return_value.create_time.return_value = time.time() + 100
            result = probe.installed_plugin_ready(self.root, process)
            self.assertEqual(result['sourceFileCount'], 1)
            self.assertIs(result['runningDeploymentLoadedAfterInstall'], True)
            running.return_value.create_time.return_value = 1
            with self.assertRaisesRegex(RuntimeError, 'running_plugin_deployment_unproven'):
                probe.installed_plugin_ready(self.root, process)
            running.return_value.create_time.return_value = time.time() + 100
            with self.assertRaisesRegex(RuntimeError, 'qwenpaw_process_identity_changed'):
                probe.installed_plugin_ready(self.root, {**process, 'start_ticks': '0'})
        (installed / 'execution_bridge.py').write_text('old_plugin = True\n')
        with self.assertRaisesRegex(RuntimeError, 'installed_plugin_source_mismatch'):
            probe.installed_plugin_ready(self.root, process)

    def test_main_readiness_failure_leaves_no_once_state_no_host_no_service(self):
        directory = self.root / 'attempt'
        (self.root / 'var/sim').mkdir(parents=True)
        (self.root / 'var/qwenpaw').mkdir()
        (self.root / 'var/sim/probe-auth.json').write_text(json.dumps({'baseUrl': 'http://fake', 'apiKey': 'fake'}))
        (self.root / 'var/qwenpaw/agent-line-process.json').write_text(json.dumps({'pid': 1}))
        with patch.object(probe, 'ROOT', self.root), patch.object(probe, 'DATA', directory), \
                patch.object(probe, 'SimClient') as sim, patch.object(probe, 'quiet_host'), \
                patch.object(probe, 'installed_plugin_ready', side_effect=RuntimeError('installed_plugin_source_mismatch')), \
                patch.object(probe.httpx, 'Client') as host, patch.object(probe, 'create_app') as app:
            self.assertEqual(probe.main(), 1)
            host.assert_not_called()
            app.assert_not_called()
            sim.return_value.execute.assert_not_called()
            sim.return_value.resume.assert_not_called()
            sim.return_value.close.assert_called_once()
        self.assertFalse((directory / 'state.json').exists())
        readiness = json.loads((directory / 'readiness.json').read_text())
        self.assertEqual(readiness['nativeWritesThisInvocation'], 0)
        self.assertEqual(readiness['code'], 'installed_plugin_source_mismatch')

    def test_main_existing_unknown_state_never_performs_live_preflight_or_starts_app(self):
        directory = self.root / 'attempt'
        directory.mkdir()
        state = {'steps': {'create': {'status': 'intent'}}}
        (directory / 'state.json').write_text(json.dumps(state))
        (self.root / 'var/sim').mkdir(parents=True)
        (self.root / 'var/sim/probe-auth.json').write_text(json.dumps({'baseUrl': 'http://fake', 'apiKey': 'fake'}))
        with patch.object(probe, 'ROOT', self.root), patch.object(probe, 'DATA', directory), \
                patch.object(probe, 'SimClient'), patch.object(probe, 'quiet_host') as quiet, \
                patch.object(probe, 'installed_plugin_ready') as ready, \
                patch.object(probe.httpx, 'Client') as host, patch.object(probe, 'create_app') as app:
            self.assertEqual(probe.main(execute=True), 0)
            for operation in (quiet, ready, host, app): operation.assert_not_called()
        self.assertEqual(json.loads((directory / 'state.json').read_text()), state)

    def test_default_after_successful_readiness_does_not_create_state_app_or_submit(self):
        directory = self.root / 'attempt'
        for name in ('var/sim', 'var/qwenpaw', 'var/qwenpaw-secrets',
                     'var/native-memory-roundtrip', 'deploy/executor', 'workspace'):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        def saved(relative, value):
            (self.root / relative).write_text(json.dumps(value))
        saved('var/sim/probe-auth.json', {'baseUrl': 'http://fake', 'apiKey': 'fake'})
        saved('var/qwenpaw/agent-line-process.json', {'pid': 1, 'port': 1234})
        bridge = {'container_control': str(self.root / 'deploy/executor/control.py'),
                  'container_launcher': str(self.root / 'deploy/executor/launcher.py'),
                  'workspace': str(self.root / 'workspace')}
        saved('var/qwenpaw/config.json', {'plugins': {'agent-line': {'execution_bridge': bridge}}})
        (self.root / 'deploy/executor/control.py').write_text('# launcherExitProof')
        (self.root / 'deploy/executor/launcher.py').write_text('# current launcher')
        saved('var/native-memory-roundtrip/attempt4-report.json',
              {'verified': True, 'activeLeaseCount': 0, 'ownedActiveContainerCount': 0})
        saved('var/native-memory-roundtrip/state-attempt-4.json', {'roleWorkspace': 'original-native-workspace'})
        saved('var/qwenpaw-secrets/local-login.json', {'username': 'fake', 'password': 'fake'})
        (self.root / 'var/qwenpaw-secrets/execution-service-token').write_text('fake-service-token')
        profile = Mock()
        profile.json.return_value = {'running': {'memory_manager_backend': 'remelight'},
                                     'workspace_dir': 'original-native-workspace'}
        descriptor = Mock()
        descriptor.json.return_value = {'workspace': bridge['workspace'], 'managedRoleIds': [probe.ROLE],
                                        'roleRevisions': {probe.ROLE: self.revision}}
        with patch.object(probe, 'ROOT', self.root), patch.object(probe, 'DATA', directory), \
                patch.object(probe, 'SimClient') as sim, patch.object(probe, 'quiet_host'), \
                patch.object(probe, 'installed_plugin_ready', return_value={'matched': True}), \
                patch.object(probe, 'main_snapshot', return_value={'unchanged': True}), \
                patch.object(probe.subprocess, 'run'), patch.object(probe.httpx, 'Client') as host, \
                patch.object(probe, 'create_app') as app, patch.object(probe, 'RoleDispatchGateway') as parent:
            host.return_value.post.return_value.json.return_value = {'token': 'fake-token'}
            host.return_value.get.side_effect = [profile, descriptor]
            self.assertEqual(probe.main(), 0)
            app.assert_not_called()
            parent.assert_not_called()
            sim.return_value.execute.assert_not_called()
            sim.return_value.resume.assert_not_called()
            sim.return_value._client.post.assert_not_called()
        self.assertFalse((directory / 'state.json').exists())
        self.assertEqual(list(directory.glob('*.sqlite')), [])
        readiness = json.loads((directory / 'readiness.json').read_text())
        self.assertEqual(readiness['status'], 'ready_not_executed')
        self.assertEqual(readiness['newSubmissions'], 0)
        self.assertEqual(readiness['nativeWritesThisInvocation'], 0)

    def test_serialization_requires_original_two_claims_release_and_verification_between(self):
        store = TaskStore.initialize(self.root / 'tasks.sqlite')
        tasks = [{'id': 'left'}, {'id': 'right'}]
        with store.connect() as db:
            for task in tasks:
                tid = task['id']
                db.execute('INSERT INTO tasks(id,idempotency_key,activation_key,input_hash,payload,'
                           'execution_status,submission_status,created_at) VALUES (?,?,?,?,?,?,?,?)',
                           (tid, tid, tid, 'fixture', '{}', 'registered', 'pending', time.time()))
        def event(task, kind):
            with store.connect() as db: store._event(db, task, kind)
        event('left', 'submission_intent')
        event('left', 'resources_released')
        event('right', 'submission_intent')
        with self.assertRaisesRegex(RuntimeError, 'shared_workspace_serialization_unproven'):
            probe.serialized_events(store, tasks)
        with store.connect() as db: db.execute('DELETE FROM events')
        for task, kind in [('right', 'submission_intent'), ('right', 'resources_released'),
                           ('right', 'verification_passed'), ('left', 'submission_intent')]:
            event(task, kind)
        result = probe.serialized_events(store, tasks)
        self.assertEqual(result['firstTaskId'], 'right')
        self.assertIs(result['concurrentWorkspaceWriters'], False)
        self.assertEqual(result['activeLeaseCount'], 0)
        event('left', 'submission_intent')
        with self.assertRaisesRegex(RuntimeError, 'expected_two_original_claims'):
            probe.serialized_events(store, tasks)

    def frozen_fixture(self):
        items = probe.work_items(self.suffix)
        bindings = probe.policies(items, self.revision, self.suffix)
        wid, tid = 'workflow', 'task-left'
        wait = native_id(wid, 'left')
        policy = {**bindings[probe.BINDINGS['left']], 'roleBindingId': probe.BINDINGS['left']}
        document = probe.document()
        state = {'workflowId': wid, 'document': document, 'roleRevision': self.revision,
                 'input': {'branches': items}, 'publication': {'externalTasks': {'agentPolicies': {wait: policy}}}}
        task = {'id': tid, 'payload': {'roleBindingId': probe.BINDINGS['left'],
                'workflow': {'nodeExecutionId': wait}, 'roleRevision': self.revision,
                'verificationPolicy': policy['verificationPolicy'],
                'input': {'taskInput': state['input'], 'instruction': next(
                    n['config']['instruction'] for n in document['nodes'] if n['id'] == 'left')}}}
        parent = {'payload': {'roleRevision': self.revision}}
        runs = {'agentPolicies': {wait: policy}}
        path = self.root / 'native-receipts.sqlite'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE role_bindings(task_id TEXT, role_id TEXT, session_id TEXT, role_revision TEXT)')
            db.execute('INSERT INTO role_bindings VALUES (?,?,?,?)', (tid, probe.ROLE, 'agent-line-' + tid, self.revision))
        return task, parent, state, runs, bindings, {'receipt_db': str(path)}

    def test_original_branch_role_revision_is_checked_in_all_five_places(self):
        arguments = self.frozen_fixture()
        result = probe.check_frozen('left', *arguments)
        self.assertEqual(set(result['roleRevisions']), {'publication', 'run', 'task', 'parent', 'pluginBinding'})
        self.assertEqual(set(result['roleRevisions'].values()), {self.revision})
        changed = copy.deepcopy(arguments)
        changed[1]['payload']['roleRevision'] = 'qwenpaw-role-v1:' + 'b' * 64
        with self.assertRaisesRegex(RuntimeError, 'frozen_role_revision_mismatch'):
            probe.check_frozen('left', *changed)
        changed = copy.deepcopy(arguments)
        changed[0]['payload']['input']['instruction'] = 'Read right instead'
        with self.assertRaisesRegex(RuntimeError, 'branch_task_scope_mismatch'):
            probe.check_frozen('left', *changed)
        changed = copy.deepcopy(arguments)
        changed[0]['payload']['verificationPolicy'] = copy.deepcopy(changed[0]['payload']['verificationPolicy'])
        changed[0]['payload']['verificationPolicy']['artifacts'][0]['sha256'] = 'b' * 64
        with self.assertRaisesRegex(RuntimeError, 'frozen_verification_policy_mismatch'):
            probe.check_frozen('left', *changed)

    def test_exit_requires_original_container_receipt_and_current_launcher_proof(self):
        identity = {'upstreamTaskId': 'native', 'codexThreadId': 'thread', 'codexTurnId': 'turn'}
        receipt = {'source': 'docker-inspect-before-remove', 'workspace': '/workspace',
                   'running': False, 'pid': 0, 'status': 'exited', 'finishedAt': '2026-01-01',
                   'owner': 'a' * 32, 'containerId': 'b' * 64}
        proof = {'resourcesReleased': True, 'codexThreadId': 'thread', 'codexTurnId': 'turn',
                 'exitReceipt': receipt, 'launcherExitProof': {'source': 'procfs-launcher-exit',
                     'state': 'absent', 'owner': receipt['owner'], 'containerId': receipt['containerId']}}
        task = {'id': 'task-original', 'resourcesReleased': True, 'executionIdentity': identity,
                'evidence': [{'kind': 'resource_exit', 'executionIdentity': identity, 'receipt': receipt}]}
        path = self.root / 'receipts.sqlite'
        bridge = {'container_control': '/control', 'workspace': '/workspace', 'receipt_db': str(path),
                  'target_agent_id': 'original-codex'}
        retired = {'schemaVersion': 1, 'source': 'qwenpaw-harness-runtime-stop', 'taskId': task['id'],
                   'targetAgentId': bridge['target_agent_id'], 'workspace': bridge['workspace'],
                   'identity': identity, 'owner': receipt['owner'], 'containerId': receipt['containerId'], 'state': 'stopped'}
        payload = {'taskId': task['id'], 'roleSessionId': 'agent-line-' + task['id'],
                   'targetAgentId': bridge['target_agent_id'], 'workspace': bridge['workspace']}
        record = {'taskId': task['id'], 'identity': identity, 'terminalConfirmed': True, 'resourcesReleased': True,
                  'resourceExitReceipt': receipt, 'status': 'succeeded', 'runtimeRetirement': retired}
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE receipts(task_id TEXT PRIMARY KEY, payload TEXT, record TEXT)')
            db.execute('INSERT INTO receipts VALUES(?,?,?)', (task['id'], json.dumps(payload), json.dumps(record)))
        with patch.object(probe.subprocess, 'run') as command:
            command.return_value.stdout = json.dumps(proof)
            self.assertEqual(probe.check_exit(task, bridge), {**proof, 'runtimeRetirement': retired})
            args = command.call_args.args[0]
            self.assertEqual(args[2], 'observe')
            for field, value in [('launcherExitProof', None), ('codexTurnId', 'other'), ('resourcesReleased', False)]:
                changed = {**proof, field: value}
                command.return_value.stdout = json.dumps(changed)
                with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, 'launcher_and_container_exit_unproven'):
                    probe.check_exit(task, bridge)
            command.return_value.stdout = json.dumps(proof)
            for field, value in [('runtimeRetirement', None), ('identity', {**identity, 'codexTurnId': 'other'}),
                                 ('resourcesReleased', False)]:
                with sqlite3.connect(path) as db:
                    db.execute('UPDATE receipts SET record=?', (json.dumps({**record, field: value}),))
                with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, 'original_runtime_retirement_unproven'):
                    probe.check_exit(task, bridge)

    def test_native_join_and_end_must_keep_both_original_results(self):
        state = {'workflowId': 'workflow', 'finalRunId': 'child'}
        tasks = {branch: {'id': 'task-' + branch, 'output': {'nonce': branch}} for branch in probe.BRANCHES}
        result = {'executionStatus': 'succeeded', 'verificationStatus': 'passed', 'output': {
            'policy': 'all_success', 'successfulBranches': ['left', 'right'], 'branches': {
                branch: {'taskId': tasks[branch]['id'], 'executionStatus': 'succeeded',
                         'verificationStatus': 'passed', 'output': tasks[branch]['output']} for branch in probe.BRANCHES}}}
        native = {'workflowId': 'workflow', 'runId': 'child', 'status': 'completed', 'blockOutputs': {
            native_id('workflow', 'merge'): {'result': result},
            native_id('workflow', 'end') + '.data': {'agentLineResult': result}}}
        sim = Mock()
        sim.base_url = 'http://fake'
        sim._key = 'fake'
        sim._client.get.return_value.json.return_value = {'data': native}
        self.assertEqual(probe.check_native_end(sim, state, tasks), native)
        self.assertEqual(sim._client.get.call_args.kwargs['params']['selectedOutputs'],
                         native_id('workflow', 'merge') + ',' + native_id('workflow', 'end') + '.data')
        for changed in ('taskId', 'output', 'verificationStatus'):
            bad = copy.deepcopy(native)
            bad['blockOutputs'][native_id('workflow', 'merge')]['result']['output']['branches']['left'][changed] = 'wrong'
            sim._client.get.return_value.json.return_value = {'data': bad}
            with self.subTest(changed=changed), self.assertRaises(RuntimeError):
                probe.check_native_end(sim, state, tasks)

    def test_contexts_require_both_original_native_points_bindings_and_frozen_receipts(self):
        wid, run = 'workflow', 'root'
        state = {'workflowId': wid, 'runId': run}
        tasks = {branch: {'id': 'task-' + branch, 'executionStatus': 'succeeded',
            'verificationStatus': 'passed', 'executionIdentity': {'upstreamTaskId': branch},
            'output': {'nonce': branch}, 'evidence': []} for branch in probe.BRANCHES}
        by_wait = {native_id(wid, branch): task for branch, task in tasks.items()}
        by_task = {task['id']: SimpleNamespace(task_id=task['id'], workflow_id=wid, run_id=run,
                   context_id=wait, block_id=wait, allow_multiple=True) for wait, task in by_wait.items()}
        projection = {'totalPauseCount': 2, 'resumedCount': 2, 'pausePoints': [
            {'blockId': wait, 'contextId': wait, 'resumeStatus': 'resumed'} for wait in by_wait]}
        runtime, journal = Mock(), Mock()
        runtime.discovery.wait_client.read_pauses.return_value = projection
        runtime.pauses.get.side_effect = by_task.__getitem__
        journal.get.return_value = {'status': 'accepted'}
        journal.resume_input.side_effect = lambda w, r, c: {
            **by_wait[c], 'taskId': by_wait[c]['id']}
        self.assertEqual(probe.check_contexts(runtime, journal, state, tasks), projection)
        by_task['task-left'].allow_multiple = False
        with self.assertRaisesRegex(RuntimeError, 'original_branch_pause_or_resume_result_mismatch'):
            probe.check_contexts(runtime, journal, state, tasks)
        by_task['task-left'].allow_multiple = True
        projection['pausePoints'][1]['resumeStatus'] = 'resuming'
        with self.assertRaisesRegex(RuntimeError, 'original_two_native_pause_contexts_unproven'):
            probe.check_contexts(runtime, journal, state, tasks)


if __name__ == '__main__':
    unittest.main()
