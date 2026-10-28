"""Synthetic deployment fixtures, not evidence of a live business acceptance."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess

import httpx
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_line import execution_readiness as readiness
from agent_line.automatic_verification import TEST_SCOPE
from agent_line.store import SCHEMA, canonical
from agent_line.verification import ArtifactPolicy, _check, _directory
from agent_line.verification_policy import validate_policy, policy_hash


class ReadinessTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.now = time.time() - 60
        self.boundary = self.now - 10
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir(mode=0o700)
        self.verification = self.root / 'verification'
        self.verification.mkdir(mode=0o700)
        self.task_id = 'synthetic-task'
        self.role = 'synthetic-parent'
        self.revision = 'qwenpaw-role-v1:' + 'a' * 64
        self.identity = dict(upstreamTaskId='native-task', codexThreadId='synthetic-thread', codexTurnId='turn-1')
        self.artifact = b'synthetic fixture\n'
        (self.workspace / 'result.txt').write_bytes(self.artifact)
        self.policy = validate_policy(dict(schemaVersion=1, policyId='synthetic-policy', kind='artifact_sha256',
            requireTests=False, artifacts=[dict(path='result.txt', sha256=hashlib.sha256(self.artifact).hexdigest(),
                                               exactSize=len(self.artifact))]))
        self.projects = {'p': {'workspace': str(self.workspace)}}
        self.bindings = {'b': dict(projectId='p', roleAgentId=self.role, roleRevision=self.revision,
                                   verificationPolicy=self.policy)}
        self.parent_path = self.root / 'parent.sqlite'
        self.receipt_path = self.root / 'native.sqlite'
        self.task_path = self.root / 'tasks.sqlite'
        self.tasks = SimpleNamespace(path=self.task_path)
        native = SimpleNamespace(base='http://127.0.0.1:52403/api', ledger=SimpleNamespace(path=self.parent_path))
        self.gateway = SimpleNamespace(project_id='p', role_binding_id='b', role=self.role,
            role_revision=self.revision, workspace=str(self.workspace), installation_id='installation', _gateway=native)
        self.gateways = {('p', 'b'): self.gateway}
        self.context = dict(root=self.root, projects=self.projects, bindings=self.bindings,
            gateways=self.gateways, tasks=self.tasks, verification_directory=self.verification)
        self.fingerprint = dict(host={'bornAt': self.boundary}, sourcesLatestAt=self.boundary,
                                sourcesSha256='synthetic-fingerprint')
        self.coverage = [dict(projectId='p', roleBindingId='b', taskId=self.task_id)]
        self.fingerprint_patch = patch.object(readiness, '_fingerprint', return_value=self.fingerprint)
        self.fingerprint_patch.start()
        self.addCleanup(self.fingerprint_patch.stop)
        self.payload = dict(projectId='p', roleBindingId='b', roleAgentId=self.role,
            roleRevision=self.revision, workspace=str(self.workspace), verificationPolicy=self.policy,
            input={'instruction': 'synthetic task'})
        self.exit = dict(source='docker-inspect-before-remove', owner='b' * 32, containerId='c' * 64,
            running=False, pid=0, status='exited', workspace=str(self.workspace), imageId='sha256:' + '1' * 64,
            startedAt=self.iso(self.now), finishedAt=self.iso(self.now + 1),
            identities=[{**self.identity, 'nativeTerminal': True}])
        self.evidence = [dict(kind='execution_terminal', executionId='turn-1', confirmed=True),
            dict(kind='resource_exit', taskId=self.task_id, executionIdentity=self.identity, receipt=self.exit)]
        fd = _directory(self.workspace)
        try:
            artifact = self.policy['artifacts'][0]
            check = _check(fd, ArtifactPolicy(artifact['path'], artifact['sha256'], artifact['exactSize'], artifact['maxBytes']))
        finally:
            os.close(fd)
        self.receipt = dict(kind='verification', schemaVersion=1, taskId=self.task_id,
            executionIdentity=self.identity, checkedAt=self.now + 3, status='passed', checks=[check],
            scope='Only the declared artifact byte/size policies; not full code or test acceptance',
            verificationPolicy=self.policy, policyId=self.policy['policyId'], policySha256=policy_hash(self.policy))
        self.set_verification_name()
        self.evidence.append(self.receipt)
        self.make_tasks()
        with sqlite3.connect(self.parent_path) as db:
            db.execute('CREATE TABLE deliveries(task_id TEXT PRIMARY KEY,payload TEXT,parent_id TEXT,status TEXT,observation TEXT,created REAL)')
            db.execute('CREATE TABLE cancellations(task_id TEXT)')
            db.execute('INSERT INTO deliveries VALUES (?,?,?,?,?,?)', (self.task_id, canonical(dict(
                roleAgentId=self.role, roleSessionId='agent-line-' + self.task_id, deploymentId='installation',
                roleRevision=self.revision, instruction=canonical(self.payload['input']))), 'parent-native', 'submitted', None, self.now))
        self.parent_path.chmod(0o600)
        self.native_payload = dict(taskId=self.task_id, roleSessionId='agent-line-' + self.task_id,
            targetAgentId='codex', workspace=str(self.workspace), instruction='synthetic delegated instruction')
        self.native_record = dict(taskId=self.task_id, identity=self.identity, status='succeeded', terminalConfirmed=True,
            resourcesReleased=True, executorInterrupted=False, nativeStatus='task_complete', resourceExitReceipt=self.exit,
            runtimeRetirement=dict(schemaVersion=1, source='qwenpaw-harness-runtime-stop', taskId=self.task_id,
                targetAgentId='codex', workspace=str(self.workspace), identity=self.identity,
                owner=self.exit['owner'], containerId=self.exit['containerId'], state='stopped'))
        with sqlite3.connect(self.receipt_path) as db:
            db.execute('CREATE TABLE receipts(task_id TEXT PRIMARY KEY,payload TEXT,record TEXT)')
            db.execute('CREATE TABLE role_bindings(task_id TEXT PRIMARY KEY,role_id TEXT,session_id TEXT,role_revision TEXT)')
            db.execute('CREATE TABLE revoked_delegations(task_id TEXT)')
            db.execute('INSERT INTO receipts VALUES (?,?,?)', (self.task_id, canonical(self.native_payload), canonical(self.native_record)))
            db.execute('INSERT INTO role_bindings VALUES (?,?,?,?)', (self.task_id, self.role, 'agent-line-' + self.task_id, self.revision))
            db.execute('PRAGMA user_version=4')
        self.receipt_path.chmod(0o600)
        self.launch_path = self.root / 'var/executor/launches' / (self.exit['owner'] + '.json')
        self.launch = dict(owner=self.exit['owner'], containerId=self.exit['containerId'], workspace=str(self.workspace),
            exitReceipt=self.exit, accepting=False, pendingTurns=[], identities=self.exit['identities'],
            pid=2**30, start='1234', uid=os.getuid())
        self.write(self.launch_path, self.launch)
        home = self.root / 'codex-home'
        binary = self.root / 'bin/codex'
        binary.parent.mkdir()
        binary.write_text('synthetic executable')
        binary.chmod(0o755)
        (binary.parent / 'codex-code-mode-host').write_text('synthetic code mode executable')
        (binary.parent / 'codex-code-mode-host').chmod(0o755)
        self.write(self.root / 'var/executor/config.json', dict(image=self.exit['imageId'], binary=str(binary),
            workspace=str(self.workspace), codex_home=str(home)))
        self.write(self.root / 'var/qwenpaw/config.json', {'plugins': {'agent-line': {'managed_role_ids': [self.role], 'codex_target_agent_id': 'codex', 'execution_bridge': dict(
            workspace=str(self.workspace), receipt_db=str(self.receipt_path),
            target_agent_id='codex', codex_home=str(home), container_control=str(self.root / 'deploy/executor/control.py'),
            container_launcher=str(self.root / 'deploy/executor/launcher.py'))}}})
        self.write(self.workspace / 'harnesses/codex_sessions.json', {'agent-line-' + self.task_id: self.identity['codexThreadId']})
        self.rollout = home / 'sessions/rollout-synthetic-thread.jsonl'
        entries = [dict(type='session_meta', payload=dict(id=self.identity['codexThreadId'], cwd=str(self.workspace))),
            dict(type='event_msg', timestamp=readiness.datetime.fromtimestamp(self.now, readiness.datetime.now().astimezone().tzinfo).isoformat(),
                 payload=dict(type='task_started', turn_id='turn-1')),
            dict(type='event_msg', payload=dict(type='task_complete', turn_id='turn-1'))]
        self.rollout.parent.mkdir(parents=True)
        self.rollout.write_text('\n'.join(canonical(e) for e in entries))
        self.proof = self.root / 'readiness.json'
        self.write_proof()

    @staticmethod
    def iso(value):
        return readiness.datetime.fromtimestamp(value, readiness.datetime.now().astimezone().tzinfo).isoformat()

    @staticmethod
    def write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    def set_verification_name(self):
        name = 'verification-' + hashlib.sha256(canonical(
            [self.task_id, self.identity, policy_hash(self.policy)]).encode()).hexdigest() + '.json'
        self.receipt['evidenceFile'] = name
        self.write(self.verification / name, self.receipt)

    def make_tasks(self):
        with sqlite3.connect(self.task_path) as db:
            db.executescript(SCHEMA)
            db.execute('INSERT INTO installation VALUES (?,?)', ('synthetic', 2))
            db.execute('INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (
                self.task_id, 'key', 'activation', 'inputhash', canonical(self.payload), 'succeeded', 'accepted',
                1, self.now, canonical(self.identity), canonical({'text': 'synthetic'}), canonical(self.evidence),
                'passed', 'not_requested', 1))
            for kind in ('accepted', 'succeeded', 'resources_released', 'verification_passed'):
                db.execute('INSERT INTO events(task_id,kind,created_at) VALUES (?,?,?)', (self.task_id, kind, self.now + 1))
        self.task_path.chmod(0o600)

    def update_task(self):
        with sqlite3.connect(self.task_path) as db:
            db.execute('UPDATE tasks SET payload=?,evidence=? WHERE id=?',
                       (canonical(self.payload), canonical(self.evidence), self.task_id))

    def update_native(self):
        with sqlite3.connect(self.receipt_path) as db:
            db.execute('UPDATE receipts SET payload=?,record=? WHERE task_id=?',
                       (canonical(self.native_payload), canonical(self.native_record), self.task_id))

    def write_proof(self):
        self.write(self.proof, readiness.prepare_readiness(**self.context, coverage=self.coverage))

    def evaluate(self):
        return readiness.evaluate_readiness(self.proof, **self.context)

    def assert_unknown(self):
        value = self.evaluate()
        self.assertFalse(value.confirmed)
        self.assertIn('历史记录仍可查询', value.reason)
        with self.assertRaisesRegex(ValueError, '^execution_readiness_stale$'):
            value.assert_current()

    def test_complete_synthetic_facts_confirm_and_read_only(self):
        before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
                  for p in self.root.rglob('*') if p.is_file()}
        value = self.evaluate()
        self.assertTrue(value.confirmed)
        self.assertIsNone(value.reason)
        value.assert_current()
        after = {str(p): (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
                 for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_no_configuration_defaults_unknown(self):
        self.assertFalse(readiness.evaluate_readiness(None, **self.context).confirmed)

    def test_fingerprint_change_invalidates_existing_object(self):
        value = self.evaluate()
        self.assertTrue(value.confirmed)
        self.fingerprint['sourcesSha256'] = 'changed'
        self.assert_unknown()
        with self.assertRaisesRegex(ValueError, 'execution_readiness_stale'):
            value.assert_current()

    def test_old_task_even_with_fresh_fingerprint_cannot_confirm(self):
        self.fingerprint['host']['bornAt'] = self.now + 30
        self.assert_unknown()

    def test_missing_extra_duplicate_and_self_reported_grants_rejected(self):
        original = json.loads(self.proof.read_text())
        for coverage in ([], self.coverage * 2, [dict(projectId='p', roleBindingId='other', taskId=self.task_id)],
                         [{**self.coverage[0], 'passed': True}]):
            with self.subTest(coverage=coverage):
                self.write(self.proof, {**original, 'coverage': coverage})
                self.assert_unknown()

    def test_wrong_project_role_workspace_revision_policy_rejected(self):
        original = json.loads(canonical(self.payload))
        for key, value in [('projectId', 'other'), ('roleBindingId', 'other'), ('roleAgentId', 'other'),
                           ('workspace', '/other'), ('roleRevision', 'qwenpaw-role-v1:' + 'd' * 64),
                           ('verificationPolicy', {**self.policy, 'policyId': 'other'})]:
            with self.subTest(key=key):
                self.payload = {**original, key: value}
                self.update_task()
                self.assert_unknown()

    def test_parent_scope_and_pin_rejected(self):
        with sqlite3.connect(self.parent_path) as db:
            value = json.loads(db.execute('SELECT payload FROM deliveries').fetchone()[0])
            value['roleRevision'] = 'qwenpaw-role-v1:' + 'd' * 64
            db.execute('UPDATE deliveries SET payload=?', (canonical(value),))
        self.assert_unknown()

    def test_plugin_pin_differs_from_current_binding(self):
        with sqlite3.connect(self.receipt_path) as db:
            db.execute('UPDATE role_bindings SET role_revision=?', ('qwenpaw-role-v1:' + 'd' * 64,))
        self.assert_unknown()

    def test_native_failure_cannot_be_hidden_by_passed_task_and_verifier(self):
        self.native_record['status'] = 'failed'
        self.update_native()
        self.assert_unknown()

    def test_original_container_image_must_match_current_executor_config(self):
        path = self.root / 'var/executor/config.json'
        value = json.loads(path.read_text())
        value['image'] = 'sha256:' + '2' * 64
        self.write(path, value)
        self.assert_unknown()

    def test_active_wal_commit_is_read_without_business_writes_or_checkpoint(self):
        db = sqlite3.connect(self.task_path)
        self.addCleanup(db.close)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA wal_autocheckpoint=0')
        db.execute('UPDATE tasks SET version=version+1')
        db.commit()
        before = list(db.execute('SELECT * FROM tasks'))
        events = list(db.execute('SELECT * FROM events'))
        self.assertGreater(self.task_path.with_name(self.task_path.name + '-wal').stat().st_size, 0)
        self.assertTrue(self.evaluate().confirmed)
        with readiness._database(self.task_path) as readonly:
            self.assertEqual(readonly.execute('SELECT version FROM tasks').fetchone()[0], 2)
            with self.assertRaises(sqlite3.OperationalError):
                readonly.execute('UPDATE tasks SET version=version+1')
        self.assertEqual(list(db.execute('SELECT * FROM tasks')), before)
        self.assertEqual(list(db.execute('SELECT * FROM events')), events)
        self.assertGreater(self.task_path.with_name(self.task_path.name + '-wal').stat().st_size, 0)

    def test_missing_runtime_retirement_rejected(self):
        self.native_record.pop('runtimeRetirement')
        self.update_native()
        self.assert_unknown()

    def test_missing_executor_state_and_launch_identity_rejected(self):
        self.launch_path.unlink()
        self.assert_unknown()

    def test_original_launcher_still_alive_rejected(self):
        data = Path(f'/proc/{os.getpid()}/stat').read_text()
        self.launch.update(pid=os.getpid(), start=data[data.rfind(')') + 1:].split()[19])
        self.write(self.launch_path, self.launch)
        self.assert_unknown()

    def test_replaced_exit_receipt_rejected(self):
        self.launch['exitReceipt'] = {**self.exit, 'containerId': 'd' * 64}
        self.write(self.launch_path, self.launch)
        self.assert_unknown()

    def test_independent_artifact_bytes_are_rechecked(self):
        (self.workspace / 'result.txt').write_bytes(b'wrong actual bytes')
        self.assert_unknown()

    def test_missing_database_is_never_created(self):
        missing = self.root / 'missing.sqlite'
        with self.assertRaises(FileNotFoundError):
            with readiness._database(missing):
                pass
        self.assertFalse(missing.exists())

    def test_another_task_lease_does_not_invalidate_original_completed_task(self):
        with sqlite3.connect(self.task_path) as db:
            db.execute('INSERT INTO workspace_leases VALUES (?,?,?)', (str(self.workspace), 'another-task', self.now))
        self.assertTrue(self.evaluate().confirmed)

    def test_deleted_independent_verification_file_rejected(self):
        (self.verification / self.receipt['evidenceFile']).unlink()
        self.assert_unknown()

    def test_active_workspace_lease_rejected(self):
        with sqlite3.connect(self.task_path) as db:
            db.execute('INSERT INTO workspace_leases VALUES (?,?,?)', (str(self.workspace), self.task_id, self.now))
        self.assert_unknown()

    def test_native_rollout_aborted_after_complete_rejected(self):
        with self.rollout.open('a') as stream:
            stream.write('\n' + canonical(dict(type='event_msg', payload=dict(type='turn_aborted', turn_id='turn-1'))))
        self.assert_unknown()

    def test_symlink_proof_does_not_grant(self):
        other = self.proof.with_suffix('.saved')
        self.proof.rename(other)
        self.proof.symlink_to(other)
        self.assert_unknown()

    def add_tests(self, *, exit_code=0):
        self.policy = validate_policy(dict(schemaVersion=2, policyId='tests', kind='command_and_artifacts',
            requireTests=True, artifacts=self.policy['artifacts'], testEnvironmentId='docker-test-v1:' + 'e' * 64,
            tests=[dict(id='unit', argv=['python', '-m', 'unittest'], timeoutSeconds=60, maxOutputBytes=65536)]))
        test = self.policy['tests'][0]
        proof = dict(source='docker-inspect-before-remove', containerId='f' * 64, taskId=self.task_id,
            executionIdentity=self.identity, policySha256=policy_hash(self.policy), testId=test['id'],
            environmentId=self.policy['testEnvironmentId'], exitCode=exit_code, running=False, pid=0,
            status='exited', oomKilled=False, owner='a' * 32, workspace=str(self.workspace),
            startedAt=self.iso(self.now + 1), finishedAt=self.iso(self.now + 2))
        check = dict(kind='test_command', testId=test['id'], argv=test['argv'], timeoutSeconds=60,
            maxOutputBytes=65536, environmentId=self.policy['testEnvironmentId'], exitCode=exit_code,
            status='passed' if exit_code == 0 else 'failed', reason='command_exit_zero' if exit_code == 0 else 'command_exit_nonzero',
            containerId='f' * 64, resourceExitReceipt=proof, stdout='synthetic test output', stderr='', outputTruncated=False)
        self.bindings['b']['verificationPolicy'] = self.policy
        self.payload['verificationPolicy'] = self.policy
        self.receipt.update(verificationPolicy=self.policy, policyId=self.policy['policyId'],
                            policySha256=policy_hash(self.policy), scope=TEST_SCOPE)
        self.receipt['checks'] = [self.receipt['checks'][0], check]
        self.set_verification_name()
        self.update_task()
        key = readiness._digest({'taskId': self.task_id, 'testId': test['id']})
        binding = dict(taskId=self.task_id, executionIdentity=self.identity, policySha256=policy_hash(self.policy),
            testId=test['id'], environmentId=self.policy['testEnvironmentId'], workspace=str(self.workspace), test=test)
        self.test_path = self.verification / ('test-' + key + '.json')
        self.test_record = dict(evidenceFile=self.test_path.name, binding=binding, name='agent-line-test-' + key[:40],
            check=check, containerId='f' * 64, removed=True, startIntent=True)
        self.write(self.test_path, self.test_record)

    def test_declared_test_requires_independent_original_executor_record(self):
        self.add_tests()
        self.write_proof()
        self.assertTrue(self.evaluate().confirmed)
        self.test_path.unlink()
        self.assert_unknown()

    def test_failed_test_cannot_be_promoted_by_passed_summary(self):
        self.add_tests(exit_code=1)
        self.assert_unknown()

    def test_test_output_or_container_identity_cannot_be_replaced(self):
        self.add_tests()
        self.write_proof()
        self.test_record['check'] = {**self.test_record['check'], 'stdout': 'different'}
        self.write(self.test_path, self.test_record)
        self.assert_unknown()

    def test_new_binding_requires_its_own_successful_scope(self):
        self.gateways[('p', 'another')] = self.gateway
        self.assert_unknown()

    def test_verification_time_before_native_success_rejected(self):
        self.receipt['checkedAt'] = self.now - 2
        self.write(self.verification / self.receipt['evidenceFile'], self.receipt)
        self.update_task()
        self.assert_unknown()


class FingerprintTest(unittest.TestCase):
    """Real temporary git/source files plus one HTTP MockTransport descriptor."""
    def setUp(self):
        self.fixture = ReadinessTest('test_complete_synthetic_facts_confirm_and_read_only')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.fingerprint_patch.stop()
        self.root = self.fixture.root
        for name in ('agent_line/core.py', 'deploy/executor/control.py', 'deploy/executor/launcher.py',
                     'plugins/agent-line/plugin.py', 'plugins/agent-line/plugin.json',
                     'plugins/agent-line/dist/index.js', 'requirements-runtime.lock.txt'):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('{}' if name.endswith('.json') else 'synthetic fixture')
        plugin = self.root / 'plugins/agent-line'
        installed = self.root / 'var/qwenpaw/plugins/agent-line'
        for source in plugin.rglob('*'):
            if source.is_file():
                target = installed / source.relative_to(plugin)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
        pins = {}
        for name in ('qwenpaw', 'sim'):
            target = self.root / 'upstream' / name
            target.mkdir(parents=True)
            def git(*args):
                return subprocess.check_output(['git', '-C', str(target), *args],
                    stderr=subprocess.DEVNULL, text=True).strip()
            git('init')
            git('config', 'user.name', 'Synthetic Test')
            git('config', 'user.email', 'synthetic@example.invalid')
            git('remote', 'add', 'origin', 'https://example.invalid/' + name)
            (target / 'README').write_text('synthetic upstream')
            git('add', 'README')
            git('commit', '-m', 'Synthetic fixture')
            pins[name] = dict(path='upstream/' + name, commit=git('rev-parse', 'HEAD'),
                              remote=git('remote', 'get-url', 'origin'))
        self.fixture.write(self.root / 'upstreams.lock.json', dict(repositories=pins))
        for path in self.root.rglob('*'):
            if path.is_file() and 'upstream' not in path.parts:
                os.utime(path, (self.fixture.now - 120, self.fixture.now - 120))
        self.host = patch.object(readiness, '_host', return_value=dict(
            pid=123, startTicks='1234', port=52403, bornAt=self.fixture.now - 30))
        self.host.start()
        self.addCleanup(self.host.stop)
        self.descriptor = dict(schemaVersion=1, ownedContainer=True, workspace=str(self.fixture.workspace),
            managedRoleIds=[self.fixture.role], targetAgentId='codex',
            roleRevisions={self.fixture.role: self.fixture.revision})
        self.requests = []
        def request(value):
            self.requests.append(value)
            return httpx.Response(200, json=self.descriptor)
        self.client = httpx.Client(transport=httpx.MockTransport(request))
        self.addCleanup(self.client.close)
        self.fixture.gateway._gateway.client = self.client
        self.fixture.gateway._gateway.headers = {'Authorization': 'Bearer synthetic'}

    def fingerprint(self):
        return readiness._fingerprint(**self.fixture.context)

    def test_executor_image_configuration_changes_fingerprint(self):
        before = self.fingerprint()
        path = self.root / 'var/executor/config.json'
        value = json.loads(path.read_text())
        value['image'] = 'sha256:' + '2' * 64
        self.fixture.write(path, value)
        self.assertNotEqual(before, self.fingerprint())

    def test_mounted_codex_or_code_mode_content_changes_fingerprint(self):
        before = self.fingerprint()
        for name in ('codex', 'codex-code-mode-host'):
            with self.subTest(name=name):
                (self.root / 'bin' / name).write_text('changed executable')
                self.assertNotEqual(before, self.fingerprint())

    def test_binary_digest_cache_reuses_unchanged_inode_and_invalidates_content_change(self):
        binary = self.root / 'bin/codex'
        with patch.object(readiness.hashlib, 'sha256', wraps=hashlib.sha256) as hashing:
            first = readiness._file_digest(binary)
            self.assertEqual(readiness._file_digest(binary), first)
            self.assertEqual(hashing.call_count, 1)
            binary.write_text('replacement content')
            self.assertNotEqual(readiness._file_digest(binary), first)
            self.assertEqual(hashing.call_count, 2)

    def test_cached_binary_loses_readiness_when_executable_permission_is_removed(self):
        binary = self.root / 'bin/codex'
        readiness._file_digest(binary)
        binary.chmod(0o600)
        with self.assertRaises(ValueError):
            readiness._file_digest(binary)

    def test_registered_executable_with_normal_group_write_mode_is_content_bound(self):
        binary = self.root / 'bin/codex'
        expected = readiness._file_digest(binary)
        binary.chmod(0o775)
        self.assertEqual(readiness._file_digest(binary), expected)
        binary.write_text('changed registered executable')
        self.assertNotEqual(readiness._file_digest(binary), expected)

    def test_validator_change_requires_new_proof_without_reclassifying_execution_age(self):
        validator = self.root / 'agent_line/execution_readiness.py'
        validator.write_text('validation version one')
        before = self.fingerprint()
        validator.write_text('validation version two')
        os.utime(validator, (time.time() + 10, time.time() + 10))
        after = self.fingerprint()
        self.assertNotEqual(before['sourcesSha256'], after['sourcesSha256'])
        self.assertEqual(before['sourcesLatestAt'], after['sourcesLatestAt'])

    def test_execution_implementation_change_still_advances_required_task_age(self):
        before = self.fingerprint()
        control = self.root / 'deploy/executor/control.py'
        control.write_text('changed actual execution implementation')
        os.utime(control, (time.time() + 10, time.time() + 10))
        after = self.fingerprint()
        self.assertNotEqual(before['sourcesSha256'], after['sourcesSha256'])
        self.assertGreater(after['sourcesLatestAt'], before['sourcesLatestAt'])

    def test_binary_digest_cache_is_bounded_and_rechecks_replacement_inode(self):
        for index in range(10):
            binary = self.root / 'bin' / ('executable-' + str(index))
            binary.write_text('cache fixture')
            binary.chmod(0o755)
            readiness._file_digest(binary)
        self.assertLessEqual(len(readiness._BINARY_DIGESTS), 8)
        binary = self.root / 'bin/codex'
        old = readiness._file_digest(binary)
        replacement = binary.with_suffix('.replacement')
        replacement.write_text('new inode')
        replacement.chmod(0o755)
        replacement.replace(binary)
        self.assertNotEqual(readiness._file_digest(binary), old)

    def test_large_executable_is_hashed_in_stream(self):
        binary = self.root / 'bin/codex'
        with binary.open('wb') as stream:
            for _ in range(34):
                stream.write(b'x' * 1_000_000)
        self.assertEqual(len(readiness._file_digest(binary)), 64)

    def test_clean_pinned_sources_and_installed_plugin_hash(self):
        value = self.fingerprint()
        self.assertEqual(len(value['sourcesSha256']), 64)
        self.assertEqual(value['scopes'][0]['roleRevision'], self.fixture.revision)
        self.assertEqual([r.method for r in self.requests], ['GET'])
        self.assertEqual(self.requests[0].url.path, '/api/agent-line/execution-binding')

    def test_source_modification_changes_fingerprint(self):
        before = self.fingerprint()
        (self.root / 'agent_line/core.py').write_text('changed executable')
        self.assertNotEqual(self.fingerprint(), before)

    def test_installed_plugin_mismatch_rejected(self):
        (self.root / 'var/qwenpaw/plugins/agent-line/plugin.py').write_text('old installed executable')
        with self.assertRaises(ValueError):
            self.fingerprint()

    def test_unsupported_added_installed_code_rejected(self):
        (self.root / 'var/qwenpaw/plugins/agent-line/extra.py').write_text('unexpected executable')
        with self.assertRaises(ValueError):
            self.fingerprint()

    def test_upstream_local_change_rejected(self):
        (self.root / 'upstream/qwenpaw/README').write_text('modified upstream')
        with self.assertRaises(ValueError):
            self.fingerprint()

    def test_upstream_untracked_code_rejected(self):
        (self.root / 'upstream/sim/extra.py').write_text('unexpected upstream')
        with self.assertRaises(ValueError):
            self.fingerprint()

    def test_descriptor_transport_error_does_not_grant(self):
        def unavailable(request):
            raise httpx.ConnectError('synthetic unavailable', request=request)
        with httpx.Client(transport=httpx.MockTransport(unavailable)) as client:
            self.fixture.gateway._gateway.client = client
            value = readiness.evaluate_readiness(self.fixture.proof, **self.fixture.context)
            self.assertFalse(value.confirmed)

    def test_remote_role_hot_change_rejected(self):
        self.descriptor['roleRevisions'][self.fixture.role] = 'qwenpaw-role-v1:' + 'f' * 64
        with self.assertRaises(ValueError):
            self.fingerprint()

    def test_approval_policy_drift_keeps_pinned_revision(self):
        self.descriptor['roleRevisions'][self.fixture.role] = 'qwenpaw-role-v1:' + 'b' * 64
        with patch.object(readiness, '_codex_approval_policy_drift', return_value=True):
            value = self.fingerprint()
        self.assertEqual(value['scopes'][0]['roleRevision'], self.fixture.revision)
        self.assertNotIn('executionControls', value['scopes'][0])

    def test_readonly_role_rejects_approval_policy_drift(self):
        path = self.root / 'var/qwenpaw/config.json'
        config = json.loads(path.read_text())
        config['plugins']['agent-line']['execution_bridge']['readonly_role_ids'] = [self.fixture.role]
        self.fixture.write(path, config)
        self.descriptor['roleRevisions'][self.fixture.role] = 'qwenpaw-role-v1:' + 'b' * 64
        with patch.object(readiness, '_codex_approval_policy_drift', return_value=True):
            with self.assertRaises(ValueError):
                self.fingerprint()



    def test_remote_target_mismatch_rejected(self):
        self.descriptor['targetAgentId'] = 'other'
        with self.assertRaises(ValueError):
            self.fingerprint()

    def test_host_older_than_plugin_install_rejected(self):
        target = self.root / 'var/qwenpaw/plugins/agent-line/plugin.py'
        os.utime(target, None)
        with self.assertRaises(ValueError):
            self.fingerprint()


class ApprovalPolicyDriftTests(unittest.TestCase):
    def explain(self, pinned, live, current, mapping):
        return readiness._approval_policy_explains_revision(
            pinned, live, current, lambda policy: mapping[policy])

    def test_same_revision_needs_no_policy_change(self):
        self.assertTrue(self.explain('pin', 'pin', 'never', {None: 'pin'}))

    def test_only_approval_policy_difference_is_accepted(self):
        mapping = {None: 'live-untrusted', 'never': 'pin', 'on-request': 'other',
                   'on-failure': 'other', 'untrusted': 'live-untrusted'}
        self.assertTrue(self.explain('pin', 'live-untrusted', 'untrusted', mapping))

    def test_sandbox_or_model_difference_is_rejected(self):
        mapping = {None: 'live', 'never': 'different', 'on-request': 'different',
                   'on-failure': 'different', 'untrusted': 'live'}
        self.assertFalse(self.explain('pin', 'live', 'untrusted', mapping))

    def test_live_revision_must_match_the_file(self):
        mapping = {None: 'actual', 'never': 'pin'}
        self.assertFalse(self.explain('pin', 'claimed', 'untrusted', mapping))

    def test_admission_returns_live_revision_for_policy_drift_only(self):
        with patch.object(readiness, '_role_is_readonly', return_value=False), \
                patch.object(readiness, '_codex_target_id', return_value='codex'), \
                patch.object(readiness, '_codex_approval_policy_drift', return_value=True):
            self.assertEqual(readiness._approval_policy_admission('parent', 'pin', 'live'), 'live')

    def test_admission_rejects_readonly_and_unexplained_drift(self):
        with patch.object(readiness, '_role_is_readonly', return_value=True):
            self.assertIsNone(readiness._approval_policy_admission('parent', 'pin', 'live'))
        with patch.object(readiness, '_role_is_readonly', return_value=False), \
                patch.object(readiness, '_codex_target_id', return_value='codex'), \
                patch.object(readiness, '_codex_approval_policy_drift', return_value=False):
            self.assertIsNone(readiness._approval_policy_admission('parent', 'pin', 'live'))

    def test_submit_rewrites_only_the_admitted_proof(self):
        import threading
        proof = object()
        gateway = type('Gateway', (), {'_revision_proofs': {proof: ('task', 'pin', 'live')},
                                     '_revision_proof_lock': threading.Lock()})()
        self.assertEqual(readiness._rewrite_admitted_revision(gateway, proof, 'task', 'pin'), 'live')
        self.assertEqual(gateway._revision_proofs[proof], ('task', 'live'))
        gateway._revision_proofs[proof] = ('task', 'pin')
        self.assertEqual(readiness._rewrite_admitted_revision(gateway, proof, 'task', 'pin'), 'pin')



if __name__ == '__main__':
    unittest.main()
