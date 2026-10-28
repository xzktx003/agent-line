"""Declared artifact and test verification under the activation's original lease."""
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import sqlite3
import time

from .store import Conflict, canonical
from .verification import ArtifactPolicy, _check, _directory, _persist, verify_artifacts
from .verification_policy import validate_policy, policy_hash, validate_cancellation_receipt
from .output_contract import blocks_success

TEST_SCOPE = 'Only declared artifact byte/size checks and configured isolated test commands; not full product acceptance'


class AutomaticVerifier:
    def __init__(self, store, evidence_directory, *, test_executor=None):
        self.store = store
        self.directory = Path(evidence_directory).absolute()
        if test_executor is not None:
            from .test_executor import DockerTestExecutor
            if not isinstance(test_executor, DockerTestExecutor) or test_executor.directory != self.directory:
                raise ValueError('Test executor must use this private evidence directory')
        self.test_executor = test_executor
        fd = self._directory()
        os.close(fd)

    def validate_policy(self, policy):
        policy = validate_policy(policy)
        if policy['requireTests']:
            if self.test_executor is None:
                raise ValueError('Test policy requires a configured isolated test executor')
            self.test_executor.validate_policy(policy)
        return policy

    def _directory(self):
        fd = _directory(self.directory)
        try:
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError('Private evidence directory required')
            with self.store.connect() as db:
                workspaces = [json.loads(row['payload'])['workspace']
                              for row in db.execute('SELECT payload FROM tasks')]
            for workspace in workspaces:
                if self.directory.resolve().is_relative_to(Path(workspace).resolve()):
                    raise ValueError('Evidence directory overlaps task workspace')
            return fd
        except BaseException:
            os.close(fd)
            raise

    @staticmethod
    def _private(fd):
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise ValueError('Unsafe verification evidence')

    def _validate(self, receipt, task, policy, name):
        if not isinstance(receipt, dict) or set(receipt) != {
            'kind', 'schemaVersion', 'taskId', 'executionIdentity', 'checkedAt', 'status',
            'checks', 'scope', 'evidenceFile', 'verificationPolicy', 'policyId', 'policySha256'
        }:
            raise ValueError('Invalid verification receipt')
        if (receipt['kind'] != 'verification' or type(receipt['schemaVersion']) is not int
                or receipt['schemaVersion'] != 1 or receipt['taskId'] != task['id']
                or receipt['executionIdentity'] != task['executionIdentity']
                or canonical(receipt['verificationPolicy']) != canonical(policy)
                or receipt['policyId'] != policy['policyId']
                or receipt['policySha256'] != policy_hash(policy) or receipt['evidenceFile'] != name
                or receipt['scope'] != (TEST_SCOPE if policy['requireTests'] else
                                       'Only the declared artifact byte/size policies; not full code or test acceptance')):
            raise ValueError('Verification identity mismatch')
        with self.store.connect() as db:
            row = db.execute("SELECT max(created_at) AS latest FROM events WHERE task_id=? AND kind IN ('succeeded','resources_released')",
                             (task['id'],)).fetchone()
        checked = receipt['checkedAt']
        if (type(checked) not in (int, float) or not math.isfinite(checked)
                or not row or row['latest'] is None or checked < row['latest'] or checked > time.time()):
            raise ValueError('Invalid verification timestamp')
        checks = receipt['checks']
        tests = policy.get('tests', [])
        if not isinstance(checks, list) or len(checks) != len(policy['artifacts']) + len(tests):
            raise ValueError('Invalid verification checks')
        for check, artifact in zip(checks, policy['artifacts']):
            expected = dict(kind='artifact_sha256', path=artifact['path'],
                            expectedSha256=artifact['sha256'], expectedSize=artifact['exactSize'],
                            maxBytes=artifact['maxBytes'])
            if (not isinstance(check, dict) or canonical({k: check.get(k) for k in expected}) != canonical(expected)
                    or check.get('status') not in ('passed', 'failed')
                    or check.get('reason') not in {
                        'artifact_not_regular_file', 'artifact_exceeds_limit', 'artifact_path_replaced_during_read',
                        'artifact_changed_during_read', 'sha256_mismatch', 'size_mismatch',
                        'exact_artifact_policy_matched', 'artifact_unavailable_or_symlink'}
                    or set(check) - (set(expected) | {'status', 'reason', 'actualSha256', 'actualSize'})):
                raise ValueError('Verification check mismatch')
            if check['status'] == 'passed' and (
                    check.get('actualSha256') != artifact['sha256']
                    or type(check.get('actualSize')) is not int
                    or not 0 <= check['actualSize'] <= artifact['maxBytes']
                    or (artifact['exactSize'] is not None and check['actualSize'] != artifact['exactSize'])
                    or check['reason'] != 'exact_artifact_policy_matched'):
                raise ValueError('Invalid passed check')
        if tests:
            from .verification_policy import validate_test_check
            for check, test in zip(checks[len(policy['artifacts']):], tests):
                validate_test_check(check, test, policy, task['id'], task['executionIdentity'])
                if check['resourceExitReceipt']['workspace'] != task['payload']['workspace']:
                    raise ValueError('Test workspace identity mismatch')
        if receipt['status'] != ('passed' if all(c['status'] == 'passed' for c in checks) else 'failed'):
            raise ValueError('Verification summary mismatch')

    def _command_receipt(self, task, policy, workspace, name):
        self.validate_policy(policy)
        tests = []
        for test in policy['tests']:
            result = self.test_executor.check(task, policy, test)
            if result.get('status') != 'completed':
                return None, {'status': result.get('status') if result.get('status') in ('waiting', 'blocked')
                              else 'blocked', 'reason': 'test_command_not_confirmed'}
            from .verification_policy import validate_test_check
            check = result['check']
            validate_test_check(check, test, policy, task['id'], task['executionIdentity'])
            if check['resourceExitReceipt']['workspace'] != workspace:
                raise ValueError('Test workspace mismatch')
            tests.append(check)
        # Tests have read-only workspace mounts. Retain the original write lease
        # through the final artifact read and atomic receipt/database commit.
        if self.store.get(task['id']) != task or self.store.verification_lease(task['id']) != workspace:
            raise ValueError('Task changed during test verification')
        fd = _directory(workspace)
        try:
            checks = [_check(fd, ArtifactPolicy(a['path'], a['sha256'], a['exactSize'], a['maxBytes']))
                      for a in policy['artifacts']]
        finally:
            os.close(fd)
        checks.extend(tests)
        receipt = dict(kind='verification', schemaVersion=1, taskId=task['id'],
                       executionIdentity=task['executionIdentity'], checkedAt=time.time(),
                       status='passed' if all(c['status'] == 'passed' for c in checks) else 'failed',
                       checks=checks, scope=TEST_SCOPE, evidenceFile=name,
                       verificationPolicy=policy, policyId=policy['policyId'], policySha256=policy_hash(policy))
        self._validate(receipt, task, policy, name)
        _persist(self.directory, receipt)
        return receipt, None

    def _cancel_commands(self, task, policy, workspace, root, name):
        filename = name.replace('verification-', 'cancellation-', 1)
        try:
            descriptor = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
        except FileNotFoundError:
            self.validate_policy(policy)
            if os.path.lexists(self.directory / ('.' + filename + '.tmp')):
                raise ValueError('Interrupted cancellation evidence')
            proofs = []
            pending = []
            for test in policy['tests']:
                result = self.test_executor.cancel(task, policy, test)
                if result.get('status') != 'completed':
                    pending.append(result.get('status'))
                else:
                    proofs.append(result['cancellation'])
            if pending:
                return {'status': 'blocked' if 'blocked' in pending else 'waiting',
                        'reason': 'test_cancellation_not_confirmed'}
            receipt = dict(kind='verification_cancellation', schemaVersion=1, taskId=task['id'],
                           executionIdentity=task['executionIdentity'], policySha256=policy_hash(policy),
                           canceledAt=time.time(), evidenceFile=filename, tests=proofs)
            validate_cancellation_receipt(receipt, policy, task['id'], task['executionIdentity'], workspace)
            _persist(self.directory, receipt)
        else:
            with os.fdopen(descriptor, 'rb') as stream:
                self._private(stream.fileno())
                raw = stream.read(32_000_001)
                if len(raw) > 32_000_000:
                    raise ValueError('Oversized cancellation evidence')
                receipt = json.loads(raw)
            validate_cancellation_receipt(receipt, policy, task['id'], task['executionIdentity'], workspace)
        if receipt['evidenceFile'] != filename or receipt['canceledAt'] > time.time():
            raise ValueError('Cancellation receipt identity mismatch')
        self.store.record_verification_cancellation(task['id'], receipt)
        return {'status': 'recorded', 'verificationStatus': 'canceled'}

    def verify_pending(self, task_id):
        # The same per-task scope protects a committed cancellation from a
        # stale verifier snapshot starting a later test. No DB transaction is
        # held while Docker is contacted.
        with self.store.verification_scope(task_id):
            return self._verify_pending(task_id)

    def _verify_pending(self, task_id):
        task = self.store.get(task_id)
        if task['verificationStatus'] != 'pending':
            return {'status': 'already_recorded', 'verificationStatus': task['verificationStatus']}
        if task['executionStatus'] != 'succeeded' or task['resourcesReleased'] is not True:
            return {'status': 'waiting', 'reason': 'execution_not_released'}
        if blocks_success(task):
            return {'status': 'blocked', 'reason': 'declared_output_needs_human'}
        try:
            policy = validate_policy(task['payload'].get('verificationPolicy'))
            workspace = self.store.verification_lease(task_id)
            if workspace != task['payload']['workspace']:
                raise ValueError('Workspace binding mismatch')
            identity = task['executionIdentity']
            terminal = identity.get('codexTurnId') or identity.get('upstreamTaskId')
            exits = [e for e in task['evidence'] if isinstance(e, dict) and e.get('kind') == 'resource_exit']
            if (len(exits) != 1 or exits[0].get('taskId') != task_id
                    or exits[0].get('executionIdentity') != identity
                    or not any(isinstance(e, dict) and e.get('kind') == 'execution_terminal'
                               and e.get('executionId') == terminal and e.get('confirmed') is True
                               for e in task['evidence'])):
                raise ValueError('Owned terminal evidence required')
            self.store._validate_resource_exit(identity, task['payload'], exits[0].get('receipt'), strict=True)
            name = 'verification-' + hashlib.sha256(canonical(
                [task_id, task['executionIdentity'], policy_hash(policy)]).encode()).hexdigest() + '.json'
            root = self._directory()
            lock = None
            try:
                lock = os.open(name + '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=root)
                self._private(lock)
                fcntl.flock(lock, fcntl.LOCK_EX)
                current = self.store.get(task_id)
                if current['verificationStatus'] != 'pending':
                    return {'status': 'already_recorded', 'verificationStatus': current['verificationStatus']}
                if current != task or self.store.verification_lease(task_id) != workspace:
                    raise ValueError('Task changed during verification')
                if policy['requireTests'] and task.get('cancellationRequested') is True:
                    return self._cancel_commands(task, policy, workspace, root, name)
                try:
                    evidence = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
                except FileNotFoundError:
                    # A leftover atomic-write temporary is evidence of an interrupted read;
                    # verify_artifacts refuses its exclusive creation rather than rereading silently.
                    if os.path.lexists(self.directory / ('.' + name + '.tmp')):
                        raise ValueError('Interrupted verification evidence')
                    if policy['requireTests']:
                        receipt, pending = self._command_receipt(task, policy, workspace, name)
                        if pending is not None:
                            return pending
                    else:
                        receipt = verify_artifacts(
                            self.store, task_id, workspace=workspace,
                            policies=[ArtifactPolicy(a['path'], a['sha256'], a['exactSize'], a['maxBytes'])
                                      for a in policy['artifacts']],
                            evidence_dir=self.directory, evidence_file=name, verification_policy=policy)
                else:
                    with os.fdopen(evidence, 'rb') as stream:
                        self._private(stream.fileno())
                        limit = 32_000_000 if policy['requireTests'] else 1_000_000
                        raw = stream.read(limit + 1)
                        if len(raw) > limit:
                            raise ValueError('Oversized verification evidence')
                        receipt = json.loads(raw)
                self._validate(receipt, task, policy, name)
                self.store.record_verification(task_id, receipt)
                return {'status': 'recorded', 'verificationStatus': receipt['status']}
            finally:
                if lock is not None:
                    os.close(lock)
                os.close(root)
        except (ValueError, OSError, Conflict, sqlite3.Error):
            return {'status': 'blocked', 'reason': 'verification_evidence_unavailable'}
