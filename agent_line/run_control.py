"""Local run admission and durable control commands in the existing Sim journal."""
import fcntl
import hashlib
import json
import os
import stat
import threading
import time
from contextlib import contextmanager

from .adapters.sim import segment
from .store import Conflict, canonical


_LOCAL = threading.local()
_PREFIX = 'run-control-'
_STATES = {'pause': 'paused', 'continue': 'active', 'stop': 'stop_requested'}
_PAYLOAD_BASE = {'projectId', 'action', 'expectedVersion'}
_PAUSE_CHOICES = {'continue', 'interrupt'}
_INTERRUPTIBLE = frozenset({'accepted', 'submitting', 'uncertain'})


class RunControlClosed(Conflict):
    def __init__(self, state):
        self.state = state
        super().__init__('Run admission closed: ' + state)


class RunControl:
    def __init__(self, journal, tasks):
        self.journal = journal
        self.tasks = tasks

    def _lock_path(self, workflow_id, run_id):
        segment(workflow_id)
        segment(run_id)
        digest = hashlib.sha256(canonical([workflow_id, run_id]).encode()).hexdigest()
        return self.journal.path.with_name(self.journal.path.name + '.' + digest + '.control.lock')

    @contextmanager
    def _lock(self, workflow_id, run_id, exclusive=False):
        path = self._lock_path(workflow_id, run_id)
        held = getattr(_LOCAL, 'held', None)
        if held is None:
            held = _LOCAL.held = {}
        key = str(path)
        if key in held:
            if exclusive:
                raise Conflict('Cannot apply a command inside run admission or another command')
            yield
            return
        fd = None
        try:
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise Conflict('Unsafe run control lock')
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            held[key] = exclusive
            try:
                yield
            finally:
                held.pop(key, None)
        except OSError as exc:
            raise Conflict('Run control lock unavailable') from exc
        finally:
            if fd is not None:
                os.close(fd)

    def _records(self, db, workflow_id, run_id):
        info = self.journal.path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise Conflict('Unsafe run control journal')
        state = {'state': 'active', 'version': 0, 'operationId': None}
        records = {}
        project = None
        rows = db.execute('SELECT * FROM operations WHERE workflow=? AND run=? ORDER BY rowid',
                          (workflow_id, run_id))
        for row in rows:
            if not row['context'].startswith(_PREFIX):
                continue
            try:
                op = row['context'][len(_PREFIX):]
                segment(op)
                payload, receipt = json.loads(row['input']), json.loads(row['receipt'])
                if (not isinstance(payload, dict) or payload.get('action') not in _STATES
                        or type(payload.get('expectedVersion')) is not int
                        or payload['expectedVersion'] != state['version']
                        or row['status'] != 'accepted' or state['state'] == 'stop_requested'):
                    raise ValueError()
                keys = set(payload)
                if payload['action'] == 'pause':
                    if keys == _PAYLOAD_BASE:
                        pass
                    elif (keys != _PAYLOAD_BASE | {'activeExecution'}
                            or payload.get('activeExecution') not in _PAUSE_CHOICES):
                        raise ValueError()
                elif keys != _PAYLOAD_BASE:
                    raise ValueError()
                segment(payload['projectId'])
                if project is not None and project != payload['projectId']:
                    raise ValueError()
                project = payload['projectId']
                expected = {'state': _STATES[payload['action']], 'version': state['version'] + 1,
                            'operationId': op}
                if (receipt != expected or not isinstance(receipt, dict)
                        or type(receipt.get('version')) is not int):
                    raise ValueError()
                records[op] = (payload, receipt)
                state = receipt
            except (TypeError, ValueError, KeyError) as exc:
                raise Conflict('Invalid run control history') from exc
        return state, records, project

    def status(self, workflow_id, run_id):
        with self._lock(workflow_id, run_id):
            try:
                with self.journal.connect() as db:
                    state, records, _project = self._records(db, workflow_id, run_id)
                    return self._public_status(state, records)
            except Conflict:
                raise
            except Exception as exc:
                raise Conflict('Run control journal unavailable') from exc

    @contextmanager
    def admission(self, workflow_id, run_id):
        with self._lock(workflow_id, run_id):
            current = self.status(workflow_id, run_id)
            if current['state'] != 'active':
                raise RunControlClosed(current['state'])
            yield

    def _public_status(self, state, records):
        if state['state'] != 'paused':
            return dict(state)
        payload = records[state['operationId']][0]
        choice = payload['activeExecution'] if 'activeExecution' in payload else 'unrecorded'
        return {**state, 'activeExecution': choice}

    def _interrupt_active(self, project_id, workflow_id, run_id):
        """Ask only accepted, submitting, or uncertain tasks to cancel.

        Registered work stays queued. This does not call the native cancel
        journal, and a cancel request is not confirmation that Codex stopped.
        """
        for task in self.run_tasks(project_id, workflow_id, run_id):
            if task['executionStatus'] in _INTERRUPTIBLE:
                self.tasks.request_cancel(task['id'])

    def apply(self, *, project_id, workflow_id, run_id, action, operation_id,
              expected_version, validate_continue=None, active_execution=None):
        segment(project_id)
        segment(operation_id)
        if action not in _STATES or type(expected_version) is not int or expected_version < 0:
            raise ValueError('Invalid run control command')
        if action == 'pause':
            if active_execution not in (None, *_PAUSE_CHOICES):
                raise ValueError('Invalid run control command')
        elif active_execution is not None:
            raise ValueError('Invalid run control command')
        payload = {'projectId': project_id, 'action': action, 'expectedVersion': expected_version}
        if active_execution is not None:
            payload['activeExecution'] = active_execution
        with self._lock(workflow_id, run_id, exclusive=True):
            try:
                with self.journal.connect() as db:
                    state, records, project = self._records(db, workflow_id, run_id)
                if operation_id in records:
                    previous, receipt = records[operation_id]
                    if previous != payload:
                        raise Conflict('Run control operation payload changed')
                    if previous.get('activeExecution') == 'interrupt':
                        self._interrupt_active(project_id, workflow_id, run_id)
                    return receipt
                if action == 'pause' and active_execution not in _PAUSE_CHOICES:
                    raise ValueError('Invalid run control command')
                if project is not None and project != project_id:
                    raise Conflict('Run control project mismatch')
                if state['state'] == 'stop_requested':
                    raise RunControlClosed('stop_requested')
                if state['version'] != expected_version:
                    raise Conflict('Run control version changed')
                if action == 'continue' and (validate_continue is None or validate_continue() is not True):
                    raise Conflict('Continue validation required')
                receipt = {'state': _STATES[action], 'version': expected_version + 1,
                           'operationId': operation_id}
                with self.journal.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    if self._records(db, workflow_id, run_id)[0] != state:
                        raise Conflict('Run control version changed')
                    db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?)',
                               (workflow_id, run_id, _PREFIX + operation_id, canonical(payload),
                                'accepted', canonical(receipt), time.time()))
                if active_execution == 'interrupt':
                    self._interrupt_active(project_id, workflow_id, run_id)
                return receipt
            except (Conflict, ValueError):
                raise
            except Exception as exc:
                raise Conflict('Run control command could not be committed') from exc

    def run_tasks(self, project_id, workflow_id, run_id):
        for value in (project_id, workflow_id, run_id):
            segment(value)
        with self.tasks.connect() as db:
            ids = [row['id'] for row in db.execute(
                "SELECT id FROM tasks WHERE json_extract(payload,'$.projectId')=? "
                "AND json_extract(payload,'$.workflow.id')=? "
                "AND json_extract(payload,'$.workflow.runId')=? ORDER BY id",
                (project_id, workflow_id, run_id))]
        return [self.tasks.get(task_id) for task_id in ids]

    def cancel_tasks(self, project_id, workflow_id, run_id):
        for task in self.run_tasks(project_id, workflow_id, run_id):
            self.tasks.request_cancel(task['id'])
        return self.run_tasks(project_id, workflow_id, run_id)

    def reconcile_stop(self, project_id, workflow_id, run_id, client):
        """Recover a committed stop before its first native cancel intent.

        The original cancel journal identity is reused. Existing intent or
        uncertain records are returned as observations, never reposted.
        """
        if self.status(workflow_id, run_id)['state'] != 'stop_requested':
            return None
        self.cancel_tasks(project_id, workflow_id, run_id)
        return self.journal.cancel(client, workflow_id, run_id)
