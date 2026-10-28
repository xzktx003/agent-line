"""Durable node admission. Only trusted execution adapters may record outcomes.

No method in this module schedules workflow successors or verifies model claims.
"""

import fcntl
import hashlib
import os
import stat
import json
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .execution_limits import deadline_for, group_limits, scope, scope_projection, validate_limits, valid_unaccepted_submission
from .run_budget import run_deadline, run_history, run_scope, validate_run_budget
from .usage import aggregate_task_usage, unknown_run_usage
from .run_health import summarize_run_health, unknown_run_health
from .project_concurrency import project_capacity, validate_project_limit

SCHEMA_VERSION = 5
READABLE_SCHEMA_VERSIONS = frozenset({2, 3, 4, 5})
RECOVERY_SCHEMA_VERSIONS = frozenset({3, 4, 5})
EVENT_CONTRACT_VERSIONS = frozenset({4, 5})
EVENT_CONTRACT_COLUMNS = frozenset({
    'event_id', 'run_id', 'occurred_at', 'received_at',
    'source_identity', 'task_version', 'payload',
})
EVENT_CATEGORY_SOURCES = {
    'task_registered': 'task-store',
    'submission_accepted': 'task-store',
    'role_started': 'role-binding',
    'memory_recalled': 'memory-store',
    'delegation_started': 'role-dispatcher',
    'native_execution': 'codex-executor',
    'approval_created': 'approval-store',
    'approval_decided': 'approval-store',
    'artifact_recorded': 'verifier',
    'verification_recorded': 'verifier',
    'memory_candidate': 'memory-ledger',
    'memory_writeback': 'memory-ledger',
    'recovery_checked': 'operator',
    'terminal': 'task-store',
}
KIND_CATEGORY = {
    'registered': 'task_registered',
    'submission_intent': 'submission_accepted',
    'submission_uncertain': 'submission_accepted',
    'accepted': 'submission_accepted',
    'execution_identity_enriched': 'native_execution',
    'execution_observed': 'native_execution',
    'execution_observation_unknown': 'native_execution',
    'verification_passed': 'verification_recorded',
    'verification_failed': 'verification_recorded',
    'verification_cancel_requested': 'verification_recorded',
    'verification_canceled': 'verification_recorded',
    'memory_pending': 'memory_candidate',
    'memory_needs_review': 'memory_candidate',
    'memory_committed': 'memory_writeback',
    'memory_failed': 'memory_writeback',
    'memory_deleting': 'memory_writeback',
    'memory_deleted': 'memory_writeback',
    'recovery_recorded': 'recovery_checked',
    'succeeded': 'terminal',
    'failed': 'terminal',
    'canceled': 'terminal',
    'canceled_without_execution': 'terminal',
    **{category: category for category in EVENT_CATEGORY_SOURCES},
}
TERMINAL = frozenset({"succeeded", "failed", "canceled"})
SCHEMA_V2 = """
CREATE TABLE installation (id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL);
CREATE TABLE admission_guard (id INTEGER PRIMARY KEY CHECK(id=1), reason TEXT NOT NULL);
CREATE TABLE tasks (
    id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
    activation_key TEXT NOT NULL UNIQUE, input_hash TEXT NOT NULL,
    payload TEXT NOT NULL, execution_status TEXT NOT NULL,
    submission_status TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL, execution_identity TEXT NOT NULL DEFAULT '{}',
    output TEXT, evidence TEXT NOT NULL DEFAULT '[]',
    verification_status TEXT NOT NULL DEFAULT 'pending',
    memory_status TEXT NOT NULL DEFAULT 'not_requested',
    resources_released INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE workspace_leases (
    workspace TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
    acquired_at REAL NOT NULL
);
CREATE TABLE events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id), kind TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""
RECOVERY_TABLE = """
CREATE TABLE recovery_records (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    action TEXT NOT NULL,
    finding TEXT NOT NULL,
    decision TEXT NOT NULL,
    allow_new_attempt INTEGER NOT NULL CHECK(allow_new_attempt IN (0, 1)),
    created_at REAL NOT NULL
);
CREATE INDEX recovery_records_task ON recovery_records(task_id, created_at);
"""
SCHEMA = SCHEMA_V2 + RECOVERY_TABLE
EVENT_CONTRACT_SQL = """
ALTER TABLE events ADD COLUMN event_id TEXT;
ALTER TABLE events ADD COLUMN run_id TEXT;
ALTER TABLE events ADD COLUMN occurred_at REAL;
ALTER TABLE events ADD COLUMN received_at REAL;
ALTER TABLE events ADD COLUMN source_identity TEXT;
ALTER TABLE events ADD COLUMN task_version INTEGER;
ALTER TABLE events ADD COLUMN payload TEXT;
CREATE UNIQUE INDEX events_source_dedupe ON events(source_identity, event_id)
    WHERE event_id IS NOT NULL AND source_identity IS NOT NULL;
"""
EVENT_CATEGORY_SQL = "ALTER TABLE events ADD COLUMN category TEXT"


ROLE_REVISION_BLOCKS = frozenset({'role_revision_required', 'role_revision_mismatch',
                                 'role_revision_unavailable', 'execution_readiness_stale'})
SUBMISSION_REJECTION_BLOCK = 'submission_rejected'
SUBMISSION_REJECTION_REASON = 'submission_rejected_unaccepted'


class Conflict(ValueError):
    pass


class AdmissionClosed(Conflict):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class TaskStore:
    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        if not self.path.is_file():
            raise ValueError("Task database missing; explicit initialization required")
        try:
            with self.connect() as db:
                row = db.execute("SELECT schema_version FROM installation").fetchone()
                if row is None or row[0] not in READABLE_SCHEMA_VERSIONS:
                    raise ValueError("Unsupported task database schema")
                if row[0] in RECOVERY_SCHEMA_VERSIONS and db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recovery_records'"
                ).fetchone() is None:
                    raise ValueError("Unsupported task database schema")
                if row[0] in EVENT_CONTRACT_VERSIONS and not TaskStore._has_event_contract(db):
                    raise ValueError("Unsupported task database schema")
                if row[0] == SCHEMA_VERSION and not TaskStore._has_event_category(db):
                    raise ValueError("Unsupported task database schema")
                if row[0] == 4 and TaskStore._has_event_category(db):
                    raise ValueError("Unsupported task database schema")
        except sqlite3.DatabaseError as exc:
            raise ValueError(
                "Invalid task database; restore or migrate explicitly"
            ) from exc

    @classmethod
    def initialize(cls, path: Path):
        path = Path(path).resolve()
        # Exclusive create prevents an initialization command overwriting existing data.
        with path.open("xb"):
            pass
        path.chmod(0o600)
        db = sqlite3.connect(path)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)
            TaskStore._apply_event_contract(db)
            TaskStore._apply_event_category(db)
            db.execute(
                "INSERT INTO installation VALUES (?,?)",
                (uuid.uuid4().hex, SCHEMA_VERSION),
            )
            db.commit()
        finally:
            db.close()
        return cls(path)

    @contextmanager
    def connect(self):
        # mode=rw prevents a missing volume from being silently replaced by an empty DB.
        db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def decode(row):
        return {
            "id": row["id"],
            "payload": json.loads(row["payload"]),
            "executionStatus": row["execution_status"],
            "upstreamSubmissionStatus": row["submission_status"],
            "version": row["version"],
            "executionIdentity": json.loads(row["execution_identity"]),
            "output": json.loads(row["output"]) if row["output"] is not None else None,
            "evidence": json.loads(row["evidence"]),
            "verificationStatus": row["verification_status"],
            "memoryStatus": row["memory_status"],
            "resourcesReleased": bool(row["resources_released"]),
        }

    @staticmethod
    def _row(db, task_id):
        row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return row

    @staticmethod
    def _verification_pending(row):
        return (row['execution_status'] == 'succeeded'
                and row['verification_status'] == 'pending'
                and 'verificationPolicy' in json.loads(row['payload']))

    @staticmethod
    def _delete_owned_workspace_lease(db, row):
        """Use inside the caller's completion transaction; never delete a successor's lease."""
        if TaskStore._verification_pending(row):
            return False
        configured = json.loads(row['payload']).get('workspace')
        if not isinstance(configured, str) or not configured.strip():
            raise Conflict('Resource completion requires the registered workspace')
        workspace = str(Path(configured).resolve())
        return db.execute('DELETE FROM workspace_leases WHERE workspace=? AND task_id=?',
                          (workspace, row['id'])).rowcount > 0

    @staticmethod
    def _has_event_contract(db):
        columns = {row[1] for row in db.execute('PRAGMA table_info(events)')}
        return EVENT_CONTRACT_COLUMNS <= columns

    @staticmethod
    def _apply_event_contract(db):
        # Do not use executescript here: it commits and can leave a half-migrated store.
        for statement in EVENT_CONTRACT_SQL.split(';'):
            statement = statement.strip()
            if statement:
                db.execute(statement)

    @staticmethod
    def _has_event_category(db):
        columns = {row[1] for row in db.execute('PRAGMA table_info(events)')}
        return 'category' in columns

    @staticmethod
    def _apply_event_category(db):
        if not TaskStore._has_event_category(db):
            db.execute('ALTER TABLE events ADD COLUMN category TEXT')

    @staticmethod
    def _category_for(kind, category):
        mapped = KIND_CATEGORY.get(kind)
        if category is None:
            return mapped
        if category not in EVENT_CATEGORY_SOURCES or (mapped is not None and mapped != category):
            raise ValueError('Unknown event category')
        return category

    @staticmethod
    def _bounded_event_id(value):
        return TaskStore._event_line(value, 'id', 80)

    @staticmethod
    def _latest_terminal_row(db, task_id):
        if TaskStore._has_event_category(db):
            return db.execute(
                "SELECT * FROM events WHERE task_id=? AND category='terminal' "
                'ORDER BY occurred_at DESC, seq DESC LIMIT 1',
                (task_id,),
            ).fetchone()
        if not TaskStore._has_event_contract(db):
            return None
        return db.execute(
            'SELECT * FROM events WHERE task_id=? AND kind IN '
            "('succeeded','failed','canceled','canceled_without_execution','terminal') "
            'ORDER BY occurred_at DESC, seq DESC LIMIT 1',
            (task_id,),
        ).fetchone()

    @staticmethod
    def _event_line(value, name, limit):
        if (not isinstance(value, str) or not value or value != value.strip()
                or len(value) > limit
                or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            raise ValueError(f'Event {name} must be one bounded line')
        return value

    @staticmethod
    def _event(db, task_id, kind, *, source_identity=None, source_event_id=None,
               run_id=None, payload=None, occurred_at=None, category=None):
        """Append one audit event. A repeated source event id does not change the task."""
        now = time.time()
        if not TaskStore._has_event_contract(db):
            if source_event_id is not None or source_identity is not None or payload is not None:
                raise ValueError('Event contract requires schema 4; explicit migration required')
            db.execute(
                "INSERT INTO events(task_id,kind,created_at) VALUES (?,?,?)",
                (task_id, kind, now),
            )
            return None
        task = db.execute('SELECT version, payload FROM tasks WHERE id=?', (task_id,)).fetchone()
        if run_id is None and task is not None:
            workflow = json.loads(task['payload']).get('workflow')
            candidate = workflow.get('runId') if isinstance(workflow, dict) else None
            run_id = candidate if isinstance(candidate, str) and candidate.strip() else None
        if source_identity is not None:
            source_identity = TaskStore._event_line(source_identity, 'source', 160)
        else:
            source_identity = 'task-store'
        if source_event_id is not None:
            source_event_id = TaskStore._event_line(source_event_id, 'id', 80)
            existing = db.execute(
                'SELECT * FROM events WHERE source_identity=? AND event_id=?',
                (source_identity, source_event_id),
            ).fetchone()
            if existing is not None:
                return existing
        else:
            source_event_id = uuid.uuid4().hex
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise ValueError('Event payload must be an object')
        encoded = canonical(payload)
        if len(encoded) > 1024 or any(ord(char) < 32 or ord(char) == 127 for char in encoded):
            raise ValueError('Event payload must be bounded and contain no control characters')
        if occurred_at is None:
            occurred_at = now
        if type(occurred_at) not in (int, float) or not (0 <= float(occurred_at) <= 8_640_000_000_000):
            raise ValueError('Event occurrence time is invalid')
        category = TaskStore._category_for(kind, category)
        before = task
        if TaskStore._has_event_category(db):
            db.execute(
                'INSERT INTO events(task_id,kind,created_at,event_id,run_id,occurred_at,'
                'received_at,source_identity,task_version,payload,category) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (task_id, kind, now, source_event_id, run_id, float(occurred_at), now,
                 source_identity, None if task is None else task['version'], encoded, category),
            )
        else:
            db.execute(
                'INSERT INTO events(task_id,kind,created_at,event_id,run_id,occurred_at,'
                'received_at,source_identity,task_version,payload) VALUES (?,?,?,?,?,?,?,?,?,?)',
                (task_id, kind, now, source_event_id, run_id, float(occurred_at), now,
                 source_identity, None if task is None else task['version'], encoded),
            )
        after = db.execute('SELECT version, payload, execution_status, submission_status FROM tasks WHERE id=?',
                           (task_id,)).fetchone()
        if before is not None and (
                after['version'] != before['version'] or after['payload'] != before['payload']):
            raise ValueError('Audit event must not change the task')
        return db.execute(
            'SELECT * FROM events WHERE source_identity=? AND event_id=?',
            (source_identity, source_event_id),
        ).fetchone()

    def register(self, key, payload, *, replay_only=False):
        if type(replay_only) is not bool:
            raise ValueError('Explicit registration replay policy required')
        if not isinstance(key, str) or not key.strip():
            raise ValueError("Missing idempotency key")
        try:
            wf = payload["workflow"]
            identity = [
                payload["projectId"],
                wf["runId"],
                wf["nodeExecutionId"],
                wf["attemptId"],
            ]
            if not all(
                isinstance(x, str) and x.strip()
                for x in identity + [payload["roleAgentId"]]
            ):
                raise ValueError("Invalid activation or role identity")
        except (KeyError, TypeError) as exc:
            raise ValueError("Missing activation identity") from exc
        if 'maxConcurrentTasks' in payload:
            validate_project_limit(payload['maxConcurrentTasks'])
        if 'batchItem' in payload:
            from .batch_item import task_batch_item
            payload = {**payload, 'batchItem': task_batch_item(payload)}
        if 'verificationPolicy' in payload:
            from .verification_policy import validate_policy
            payload = dict(payload)
            payload['verificationPolicy'] = validate_policy(payload['verificationPolicy'])
        if 'roleRevision' in payload:
            from .role_revision import validate_role_revision
            validate_role_revision(payload['roleRevision'])
        if 'runBudget' in payload:
            if payload['runBudget'] is None:
                raise ValueError('runBudget must be omitted when no budget is configured')
            validate_run_budget(payload['runBudget'])
        limits = validate_limits(payload)
        encoded = canonical(payload)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM tasks WHERE idempotency_key=?", (key,)
            ).fetchone()
            if old:
                if old["input_hash"] != digest:
                    raise Conflict("Idempotency key reused with different input")
                return self.decode(old), True
            if replay_only:
                raise ValueError('Only exact existing registration replay is allowed')
            if db.execute("SELECT 1 FROM admission_guard LIMIT 1").fetchone():
                raise AdmissionClosed("Task admission is guarded")
            target_scope = scope(payload)
            if target_scope is not None:
                for _row, previous in self._matching_limit_rows(db, target_scope):
                    if limits is not None and 'limits' not in previous:
                        raise Conflict('Cannot add an execution limit to a legacy attempt group')
                    if 'limits' in previous:
                        try:
                            previous_limits = validate_limits(previous)
                        except ValueError as exc:
                            raise Conflict('Existing execution limit group is invalid') from exc
                        if limits is None or previous_limits != limits:
                            raise Conflict('Execution limits are frozen for this workflow node')
            target_run_scope = run_scope(payload)
            if 'runBudget' in payload and target_run_scope is None:
                raise Conflict('Run budget requires complete workflow run scope')
            if target_run_scope is not None:
                try:
                    frozen_budget, _used, has_rows = run_history(db, target_run_scope)
                except (TypeError, ValueError, KeyError) as exc:
                    raise Conflict('Run budget history is unknown') from exc
                proposed_budget = payload.get('runBudget')
                if has_rows and frozen_budget != proposed_budget:
                    raise Conflict('Run budget declaration is frozen for this workflow run')
            task_id = "task-" + uuid.uuid4().hex
            try:
                db.execute(
                    """INSERT INTO tasks
                    (id,idempotency_key,activation_key,input_hash,payload,
                     execution_status,submission_status,version,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        task_id,
                        key,
                        canonical(identity),
                        digest,
                        encoded,
                        "registered",
                        "pending",
                        1,
                        time.time(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("Node attempt already registered") from exc
            self._event(db, task_id, "registered")
            return self.decode(self._row(db, task_id)), False

    @staticmethod
    def _matching_limit_rows(db, target_scope):
        from .execution_limits import matching_rows

        return matching_rows(db, target_scope)

    def _business_attempt_state(self, db, payload):
        limits = validate_limits(payload)
        if limits is None:
            return None
        frozen, used, consistent = group_limits(db, scope(payload))
        if not consistent or frozen is None:
            return limits['maxBusinessAttempts'], used, False
        return frozen['maxBusinessAttempts'], used, True

    @staticmethod
    def _run_budget_state(db, payload, *, now=None):
        target_scope = run_scope(payload)
        if target_scope is None:
            if 'runBudget' in payload:
                raise ValueError('Run budget requires complete workflow run scope')
            return None
        frozen, intent_times, _has_rows = run_history(db, target_scope, now=now)
        if frozen is None:
            return None
        return frozen, len(intent_times), run_deadline(frozen, intent_times)

    def _run_budget_block(self, db, payload, *, now=None, held_intent=False):
        state = self._run_budget_state(db, payload, now=now)
        if state is None:
            return None
        budget, used, deadline = state
        current = time.time() if now is None else now
        if deadline is not None and current >= deadline:
            return 'run_execution_deadline'
        counted = used - (1 if held_intent else 0)
        if counted >= budget['maxSubmissions']:
            return 'run_submission_limit'
        return None

    def bind_project_concurrency(self, key, payload, configured):
        """Preserve the original declaration when replaying a registration key."""
        if configured is not None:
            validate_project_limit(configured)
        bound = json.loads(canonical(payload))
        with self.connect() as db:
            row = db.execute('SELECT payload FROM tasks WHERE idempotency_key=?', (key,)).fetchone()
        value = configured
        if row is not None:
            original = json.loads(row['payload'])
            value = original.get('maxConcurrentTasks')
            if 'maxConcurrentTasks' in original:
                validate_project_limit(value)
        if value is None:
            bound.pop('maxConcurrentTasks', None)
        else:
            bound['maxConcurrentTasks'] = value
        # register still compares the complete payload hash and activation scope.
        return bound

    def bind_run_budget(self, payload, configured):
        """Return a detached payload copy with the run's first declaration applied."""
        configured = validate_run_budget(configured)
        bound = json.loads(canonical(payload))
        target_scope = run_scope(bound)
        if target_scope is None:
            if configured is not None:
                raise ValueError('Run budget requires complete workflow run scope')
            bound.pop('runBudget', None)
            return bound
        with self.connect() as db:
            try:
                frozen, _intent_times, has_rows = run_history(db, target_scope)
            except (TypeError, ValueError, KeyError) as exc:
                raise ValueError('Run budget history is unknown') from exc
        if has_rows:
            if frozen is None:
                bound.pop('runBudget', None)
            else:
                bound['runBudget'] = dict(frozen)
        elif configured is None:
            bound.pop('runBudget', None)
        else:
            bound['runBudget'] = dict(configured)
        return bound

    @staticmethod
    def _usable_event_time(value):
        if type(value) not in (int, float):
            return None
        value = float(value)
        if value != value or value < 0 or value > 8_640_000_000_000:
            return None
        return value

    @staticmethod
    def _role_start_times(db, task_ids):
        """Earliest persisted role start. Registration and later events are not a start."""
        if not task_ids:
            return {}
        columns = {info[1] for info in db.execute('PRAGMA table_info(events)')}
        has_occurred = 'occurred_at' in columns
        has_category = 'category' in columns
        slots = ','.join('?' for _ in task_ids)
        start_filter = ("(kind='role_started' OR category='role_started')"
                        if has_category else "kind='role_started'")
        selected = 'task_id,seq,created_at' + (',occurred_at' if has_occurred else '')
        recorded = db.execute(
            f'SELECT {selected} FROM events WHERE task_id IN ({slots}) AND {start_filter}',
            list(task_ids),
        ).fetchall()
        starts = {}
        for event in recorded:
            when = None
            if has_occurred:
                when = TaskStore._usable_event_time(event['occurred_at'])
            if when is None:
                when = TaskStore._usable_event_time(event['created_at'])
            if when is None:
                continue
            current = starts.get(event['task_id'])
            if current is None or (when, event['seq']) < current:
                starts[event['task_id']] = (when, event['seq'])
        return {task_id: when for task_id, (when, _seq) in starts.items()}

    @staticmethod
    def _attach_activity(db, rows, tasks):
        """Attach persisted metadata within the caller's existing read snapshot."""
        if not tasks:
            return
        slots = ','.join('?' for _ in tasks)
        ids = [task['id'] for task in tasks]
        latest = db.execute(
            'SELECT e.task_id,e.seq,e.kind,e.created_at,h.cancellation_requested,h.verification_cancel_requested '
            'FROM events e JOIN ('
            'SELECT task_id,MAX(seq) last_seq,MAX(CASE WHEN kind IN '
            "('cancel_requested','canceled','verification_cancel_requested') THEN 1 ELSE 0 END) cancellation_requested, "
            "MAX(CASE WHEN kind='verification_cancel_requested' THEN 1 ELSE 0 END) verification_cancel_requested "
            f'FROM events WHERE task_id IN ({slots}) GROUP BY task_id) h '
            'ON e.seq=h.last_seq AND e.task_id=h.task_id',
            ids,
        ).fetchall()
        events = {row['task_id']: {'sequence': row['seq'], 'kind': row['kind'],
                                 'recordedAt': row['created_at']} for row in latest}
        canceled = {row['task_id']: (bool(row['cancellation_requested']),
                                    bool(row['verification_cancel_requested'])) for row in latest}
        starts = TaskStore._role_start_times(db, ids)
        for row, task in zip(rows, tasks):
            task['cancellationRequested'], task['verificationCancellationRequested'] = canceled.get(
                task['id'], (False, False))
            task['activity'] = {'registeredAt': row['created_at'],
                                'startedAt': starts.get(task['id']),
                                'lastEvent': events.get(task['id'])}

    def get(self, task_id):
        with self.connect() as db:
            db.execute('BEGIN')
            row = self._row(db, task_id)
            task = self.decode(row)
            self._attach_activity(db, [row], [task])
            if task['executionStatus'] == 'registered':
                latest = task['activity']['lastEvent']
                if latest and (latest['kind'] in ROLE_REVISION_BLOCKS
                               or latest['kind'] == SUBMISSION_REJECTION_BLOCK):
                    task['submissionBlock'] = latest['kind']
            return task

    def block_role_revision(self, task_id, reason):
        """Record a bounded preflight refusal, without claiming execution or a lease."""
        if reason not in ROLE_REVISION_BLOCKS:
            raise ValueError('Unknown role revision preflight reason')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task = self._row(db, task_id)
            if task['execution_status'] != 'registered':
                return False
            latest = db.execute('SELECT kind FROM events WHERE task_id=? ORDER BY seq DESC LIMIT 1',
                                (task_id,)).fetchone()
            if latest and latest['kind'] == reason:
                return False
            db.execute('UPDATE tasks SET version=version+1 WHERE id=?', (task_id,))
            self._event(db, task_id, reason)
            return True

    def clear_role_revision_block(self, task_id):
        """Only a fresh successful preflight can clear an unstarted task diagnostic."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if self._row(db, task_id)['execution_status'] != 'registered':
                return False
            latest = db.execute('SELECT kind FROM events WHERE task_id=? ORDER BY seq DESC LIMIT 1',
                                (task_id,)).fetchone()
            if not latest or latest['kind'] not in ROLE_REVISION_BLOCKS:
                return False
            db.execute('UPDATE tasks SET version=version+1 WHERE id=?', (task_id,))
            self._event(db, task_id, 'role_revision_ready')
            return True

    def _acquire_workspace(self, db, workspace, task_id):
        task = self._row(db, task_id)
        if task["execution_status"] in TERMINAL:
            raise Conflict("Terminal task cannot acquire workspace")
        target = Path(workspace).resolve()
        for row in db.execute("SELECT workspace,task_id FROM workspace_leases"):
            held = Path(row["workspace"])
            overlaps = target.is_relative_to(held) or held.is_relative_to(target)
            if overlaps and row["task_id"] != task_id:
                return False
        db.execute(
            "INSERT OR IGNORE INTO workspace_leases VALUES (?,?,?)",
            (str(target), task_id, time.time()),
        )
        return True

    @staticmethod
    def _queue_conflicts(payload, other):
        same_role = (isinstance(payload.get('roleAgentId'), str)
                     and payload['roleAgentId'] == other.get('roleAgentId'))
        workspace, other_workspace = payload.get('workspace'), other.get('workspace')
        overlaps = False
        if isinstance(workspace, str) and workspace and isinstance(other_workspace, str) and other_workspace:
            target, held = Path(workspace).resolve(), Path(other_workspace).resolve()
            overlaps = target.is_relative_to(held) or held.is_relative_to(target)
        return same_role, overlaps

    def _queue_reason(self, db, row, *, ignore_submission_rejection=False):
        """Use the claim transaction or a read snapshot; never persist inferred state."""
        if db.execute('SELECT 1 FROM admission_guard LIMIT 1').fetchone():
            return 'admission_closed'
        if not ignore_submission_rejection:
            latest = db.execute('SELECT kind FROM events WHERE task_id=? ORDER BY seq DESC LIMIT 1',
                                (row['id'],)).fetchone()
            if latest is not None and latest['kind'] == SUBMISSION_REJECTION_BLOCK:
                return SUBMISSION_REJECTION_BLOCK
        payload = json.loads(row['payload'])
        own_events = db.execute(
            'SELECT seq,kind,created_at,payload FROM events WHERE task_id=? ORDER BY seq',
            (row['id'],),
        ).fetchall()
        own_unaccepted = valid_unaccepted_submission(row, own_events)
        try:
            attempt_state = self._business_attempt_state(db, payload)
        except ValueError:
            return 'state_unknown'
        if attempt_state is not None and not attempt_state[2]:
            return 'state_unknown'
        try:
            run_block = self._run_budget_block(db, payload, held_intent=own_unaccepted)
        except (TypeError, ValueError, KeyError):
            return 'state_unknown'
        if run_block:
            return run_block
        if attempt_state is not None:
            frozen, used, consistent = attempt_state
            counted = used - (1 if own_unaccepted else 0)
            if counted >= frozen:
                return 'business_attempt_limit'
        try:
            capacity = project_capacity(db, payload)
        except (ValueError, TypeError, KeyError):
            return 'state_unknown'
        if capacity is not None and capacity['admissionBlocked']:
            return 'project_concurrency_limit'
        workspace = payload.get('workspace')
        if not isinstance(workspace, str) or not workspace or not Path(workspace).is_absolute():
            return 'state_unknown'
        target = Path(workspace).resolve()
        for lease in db.execute('SELECT workspace,task_id FROM workspace_leases WHERE task_id!=?', (row['id'],)):
            held = Path(lease['workspace'])
            if target.is_relative_to(held) or held.is_relative_to(target):
                return 'workspace_in_use'
        registration = db.execute("SELECT MIN(seq),COUNT(*) FROM events WHERE task_id=? AND kind='registered'", (row['id'],)).fetchone()
        if registration[1] != 1:
            return 'state_unknown'
        # events.seq survives VACUUM, clock changes and UUID scan ordering.
        contenders = db.execute("""SELECT t.*, r.registration_seq, r.registrations,
            EXISTS(SELECT 1 FROM workspace_leases l WHERE l.task_id=t.id) AS owns_lease
            FROM tasks t LEFT JOIN (
                SELECT task_id, MIN(seq) registration_seq, COUNT(*) registrations
                FROM events WHERE kind='registered' GROUP BY task_id
            ) r ON r.task_id=t.id WHERE t.id!=? AND
            (t.execution_status NOT IN ('succeeded','failed','canceled') OR t.resources_released=0
             OR EXISTS(SELECT 1 FROM workspace_leases l WHERE l.task_id=t.id))""", (row['id'],))
        earlier = False
        for other in contenders:
            if other['execution_status'] == 'registered' and not other['owns_lease']:
                latest = db.execute('SELECT kind FROM events WHERE task_id=? ORDER BY seq DESC LIMIT 1',
                                    (other['id'],)).fetchone()
                if latest is not None and latest['kind'] not in ROLE_REVISION_BLOCKS:
                    try:
                        other_limits = self._business_attempt_state(db, json.loads(other['payload']))
                        other_run_block = self._run_budget_block(
                            db, json.loads(other['payload']),
                        )
                        state_known = (
                            other_limits is None or other_limits[2]
                        )
                    except (TypeError, ValueError, KeyError):
                        state_known = False
                        other_limits = None
                        other_run_block = None
                    business_exhausted = (
                        other_limits is not None
                        and other_limits[1] >= other_limits[0]
                    )
                    run_exhausted = other_run_block in {
                        'run_submission_limit', 'run_execution_deadline',
                    }
                    if state_known and (business_exhausted or run_exhausted):
                        # Both ledgers were validated before skipping the task.
                        continue
            same_role, overlaps = self._queue_conflicts(payload, json.loads(other['payload']))
            if not same_role and not overlaps:
                continue
            if other['execution_status'] != 'registered' or other['owns_lease']:
                return 'role_in_use' if same_role else 'workspace_in_use'
            if other['registrations'] != 1:
                return 'state_unknown'
            if other['registration_seq'] < registration[0]:
                earlier = True
        return 'earlier_registered_task' if earlier else 'awaiting_checks'

    def execution_queue(self, task_id):
        """Own-task diagnostic only: no contender identifiers, counts or paths."""
        with self.connect() as db:
            db.execute('BEGIN')
            row = self._row(db, task_id)
            if row['execution_status'] != 'registered':
                return None
            return {'policy': 'serial_fifo', 'state': 'waiting',
                    'reason': self._queue_reason(db, row)}

    def project_concurrency(self, task_id):
        with self.connect() as db:
            db.execute('BEGIN')
            row = self._row(db, task_id)
            return project_capacity(db, json.loads(row['payload']))

    def execution_limits(self, task_id):
        """Return a safe local limit projection; unavailable provider usage stays unknown."""
        with self.connect() as db:
            db.execute('BEGIN')
            row = self._row(db, task_id)
            payload = json.loads(row['payload'])
            limits = validate_limits(payload)
            if limits is None:
                return None
            frozen, used, consistent = group_limits(db, scope(payload))
            if not consistent or frozen is None:
                raise ValueError('Execution limit group history is unknown')
            deadline = deadline_for(db, row, payload)
            return {
                'taskId': task_id,
                'scope': scope_projection(payload),
                'limitSeconds': frozen['maxExecutionSeconds'] if frozen is not None else limits['maxExecutionSeconds'],
                'deadlineAt': deadline,
                'attemptsUsed': used,
                'maxBusinessAttempts': frozen['maxBusinessAttempts'] if frozen is not None else limits['maxBusinessAttempts'],
                'admissionAllowed': bool(
                    consistent and frozen is not None and used < frozen['maxBusinessAttempts']
                    and row['execution_status'] == 'registered'
                    and row['submission_status'] == 'pending'
                ),
                'usage': {'tokens': None, 'modelCalls': None, 'cost': None},
            }

    def run_budget(self, task_id):
        """Return a safe run-wide budget projection; provider usage remains unknown."""
        with self.connect() as db:
            db.execute('BEGIN')
            row = self._row(db, task_id)
            payload = json.loads(row['payload'])
            try:
                state = self._run_budget_state(db, payload)
            except (TypeError, ValueError, KeyError) as exc:
                raise ValueError('Run budget history is unknown') from exc
            if state is None:
                return None
            budget, used, deadline = state
            workflow = payload['workflow']
            now = time.time()
            return {
                'scope': {
                    'projectId': payload['projectId'],
                    'workflowId': workflow['id'],
                    'runId': workflow['runId'],
                },
                'maxSubmissions': budget['maxSubmissions'],
                'submissionsUsed': used,
                'maxExecutionSeconds': budget['maxExecutionSeconds'],
                'deadlineAt': deadline,
                'admissionAllowed': bool(
                    used < budget['maxSubmissions']
                    and (deadline is None or now < deadline)
                    and row['execution_status'] == 'registered'
                    and row['submission_status'] == 'pending'
                ),
                'usage': {'tokens': None, 'modelCalls': None, 'cost': None},
            }

    def run_budget_summary(self, project_id, workflow_id, run_id, *, frozen_run=None):
        """Read one run's frozen budget and submission evidence; never grant admission."""
        target = (project_id, workflow_id, run_id)
        scope_value = dict(zip(('projectId', 'workflowId', 'runId'), target))
        observed = time.time()
        base = {'schemaVersion': 1, 'scope': scope_value, 'state': 'unknown',
                'maxSubmissions': None, 'submissionsUsed': None, 'remainingSubmissions': None,
                'maxExecutionSeconds': None, 'deadlineAt': None, 'budgetAllowsSubmission': None,
                'observedAt': observed, 'usage': {'tokens': None, 'modelCalls': None, 'cost': None}}
        try:
            if any(not isinstance(value, str) or not value.strip() for value in target):
                raise ValueError('Run budget scope required')
            expected = None
            if frozen_run is not None:
                if (not isinstance(frozen_run, dict)
                        or frozen_run.get('projectId') != project_id
                        or frozen_run.get('workflowId') != workflow_id
                        or frozen_run.get('runId') != run_id):
                    raise ValueError('Frozen budget owner differs')
                expected = validate_run_budget(frozen_run.get('runBudget'))
            with self.connect() as db:
                db.execute('BEGIN')
                declared, times, has_rows = run_history(db, target, now=observed)
                if frozen_run is not None:
                    if has_rows and declared != expected:
                        raise ValueError('Frozen budget declaration differs')
                    declared = expected
                elif not has_rows:
                    return base
                if declared is None:
                    return {**base, 'state': 'not_configured'}
                used = len(times)
                deadline = run_deadline(declared, times)
                return {**base, 'state': 'configured', **declared,
                        'submissionsUsed': used,
                        'remainingSubmissions': declared['maxSubmissions'] - used,
                        'deadlineAt': deadline,
                        'budgetAllowsSubmission': used < declared['maxSubmissions']
                        and (deadline is None or observed < deadline)}
        except (sqlite3.Error, ValueError, TypeError, KeyError, OverflowError):
            return base

    def run_usage(self, project_id, workflow_id, run_id):
        """Snapshot local evidence for one authorized run without writing rows."""
        scope = {'projectId': project_id, 'workflowId': workflow_id, 'runId': run_id}
        try:
            if any(not isinstance(value, str) or not value.strip() for value in scope.values()):
                raise ValueError('Run usage scope required')
            with self.connect() as db:
                db.execute('BEGIN')
                if db.execute('SELECT 1 FROM tasks WHERE json_valid(payload)=0 LIMIT 1').fetchone():
                    raise ValueError('Task payload history cannot be scoped')
                rows = db.execute("""SELECT * FROM tasks
                    WHERE json_extract(payload,'$.projectId')=?
                      AND json_extract(payload,'$.workflow.id')=?
                      AND json_extract(payload,'$.workflow.runId')=?
                    ORDER BY id""", (project_id, workflow_id, run_id)).fetchall()
                return aggregate_task_usage([self.decode(row) for row in rows], scope)
        except (sqlite3.Error, ValueError, TypeError, KeyError, OverflowError):
            return unknown_run_usage(scope)

    def run_health(self, project_id, workflow_id, run_id):
        """Read exact run-local health counters from one read transaction."""
        target = (project_id, workflow_id, run_id)
        observed = time.time()
        base = unknown_run_health(target, observed)
        try:
            if any(type(value) is not str or not value.strip() for value in target):
                raise ValueError('Run health scope required')
            with self.connect() as db:
                db.execute('BEGIN')
                return summarize_run_health(db, target, observed)
        except (sqlite3.Error, ValueError, TypeError, KeyError, OverflowError,
                UnicodeError, AttributeError, IndexError, RecursionError):
            return base

    def execution_deadline_exceeded(self, task_id):
        """Whether a task deadline or its run-wide deadline has elapsed."""
        with self.connect() as db:
            db.execute('BEGIN')
            row = self._row(db, task_id)
            payload = json.loads(row['payload'])
            if row['execution_status'] in TERMINAL:
                return False
            limits = validate_limits(payload)
            now = time.time()
            task_deadline = deadline_for(db, row, payload) if limits is not None else None
            run_state = self._run_budget_state(db, payload, now=now)
            run_deadline_at = run_state[2] if run_state is not None else None
            return bool(
                (task_deadline is not None and now >= task_deadline)
                or (run_deadline_at is not None and now >= run_deadline_at)
            )

    def claim(self, task_id, workspace, *, consume_rejection=False):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self._row(db, task_id)
            if db.execute("SELECT 1 FROM admission_guard LIMIT 1").fetchone():
                raise Conflict(
                    "Restored installation requires external-state reconciliation"
                )
            if (
                task["execution_status"] != "registered"
                or task["submission_status"] != "pending"
            ):
                return False
            configured = json.loads(task['payload']).get('workspace')
            if not isinstance(configured, str) or Path(configured).resolve() != Path(workspace).resolve():
                return False
            reason = self._queue_reason(db, task)
            if reason == SUBMISSION_REJECTION_BLOCK:
                if not consume_rejection:
                    return False
                reason = self._queue_reason(db, task, ignore_submission_rejection=True)
            elif consume_rejection:
                return False
            if reason != 'awaiting_checks':
                return False
            if consume_rejection:
                intent_count = db.execute(
                    "SELECT COUNT(*) FROM events WHERE task_id=? AND kind='submission_intent'",
                    (task_id,),
                ).fetchone()[0]
                if intent_count != 1:
                    return False
            if not self._acquire_workspace(db, workspace, task_id):
                return False
            db.execute(
                "UPDATE tasks SET execution_status='submitting', version=version+1 WHERE id=?",
                (task_id,),
            )
            if consume_rejection:
                self._event(db, task_id, 'submission_rejection_released',
                            payload={'reason': SUBMISSION_REJECTION_REASON})
            else:
                self._event(db, task_id, "submission_intent")
            return True

    def record_unaccepted_rejection(self, task_id):
        """Keep a precise pre-acceptance refusal without an execution identity.

        The task returns to registered and is not eligible for automatic claim.
        Unknown transport results must use mark_submission_uncertain instead.
        """
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task = self._row(db, task_id)
            identity = json.loads(task['execution_identity'])
            if (task['execution_status'] != 'submitting'
                    or task['submission_status'] != 'pending' or identity):
                return False
            changed = db.execute(
                "UPDATE tasks SET execution_status='registered', version=version+1 "
                "WHERE id=? AND execution_status='submitting' AND submission_status='pending'",
                (task_id,),
            ).rowcount
            if not changed:
                return False
            db.execute('DELETE FROM workspace_leases WHERE task_id=?', (task_id,))
            self._event(db, task_id, SUBMISSION_REJECTION_BLOCK,
                        payload={'reason': SUBMISSION_REJECTION_REASON})
            return True

    def mark_submission_uncertain(self, task_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE tasks SET execution_status='uncertain', submission_status='uncertain', version=version+1 WHERE id=? AND execution_status='submitting'",
                (task_id,),
            ).rowcount
            if changed:
                self._event(db, task_id, "submission_uncertain")
            return bool(changed)

    def accept(self, task_id, identity):
        if (
            not isinstance(identity, dict)
            or not identity
            or not all(isinstance(v, str) and v.strip() for v in identity.values())
        ):
            raise ValueError("Native execution identity required")
        if not (
            identity.get("upstreamTaskId")
            or (identity.get("codexThreadId") and identity.get("codexTurnId"))
        ):
            raise ValueError("Queryable upstream identity required")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, task_id)
            known = json.loads(row["execution_identity"])
            if known:
                if known == identity:
                    return False
                if any(identity.get(key) != value for key, value in known.items()):
                    raise Conflict("Cannot replace native execution identity")
                if row["execution_status"] in TERMINAL:
                    raise Conflict("Cannot enrich identity after terminal evidence")
                db.execute(
                    "UPDATE tasks SET execution_identity=?, version=version+1 WHERE id=?",
                    (canonical(identity), task_id),
                )
                self._event(db, task_id, "execution_identity_enriched")
                return True
            if row["execution_status"] not in (
                "submitting",
                "uncertain",
                "cancel_requested",
            ):
                raise Conflict("Task is not awaiting acceptance")
            db.execute(
                "UPDATE tasks SET execution_status=?, submission_status='accepted', execution_identity=?, version=version+1 WHERE id=?",
                (
                    (
                        "cancel_requested"
                        if row["execution_status"] == "cancel_requested"
                        else "accepted"
                    ),
                    canonical(identity),
                    task_id,
                ),
            )
            self._event(db, task_id, "accepted")
            return True

    @contextmanager
    def verification_scope(self, task_id):
        # Lock ordering: per-task file, then SQLite/receipt/job locks. No DB
        # transaction spans waiting for this lock or trusted executor I/O.
        self.get(task_id)  # Refuse unknown IDs before constructing any lock path.
        key = hashlib.sha256(canonical(task_id).encode()).hexdigest()
        path = self.path.with_name(self.path.name + '.verification-' + key + '.lock')
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise Conflict('Invalid verification serialization lock')
            fcntl.flock(fd, fcntl.LOCK_EX)
            current = path.lstat()
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise Conflict('Verification serialization lock changed')
            yield
        finally:
            os.close(fd)

    def request_cancel(self, task_id):
        with self.verification_scope(task_id):
            return self._request_cancel(task_id)

    def _request_cancel(self, task_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, task_id)
            status = row["execution_status"]
            payload = json.loads(row['payload'])
            policy = payload.get('verificationPolicy', {})
            if (status == 'succeeded' and row['verification_status'] == 'pending'
                    and policy.get('schemaVersion') == 2 and policy.get('requireTests') is True):
                if db.execute("SELECT 1 FROM events WHERE task_id=? AND kind='verification_cancel_requested'", (task_id,)).fetchone():
                    return False
                db.execute('UPDATE tasks SET version=version+1 WHERE id=?', (task_id,))
                self._event(db, task_id, 'verification_cancel_requested')
                return True
            if status in TERMINAL or status == "cancel_requested":
                return False
            target = "canceled" if status == "registered" else "cancel_requested"
            db.execute(
                "UPDATE tasks SET execution_status=?, resources_released=?, version=version+1 WHERE id=?",
                (target, int(target == "canceled"), task_id),
            )
            self._event(db, task_id, target)
            return True

    def resolve_unstarted_cancellation(self, task_id, proof):
        """Trusted reconciliation only: revoked delegation, finished parent, no Codex claim.

        Parent identity is audit context, never an invented Codex execution identity.
        This method is not exposed as an HTTP mutation or model tool.
        """
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, task_id)
            payload = json.loads(row["payload"])
            if (
                not isinstance(proof, dict)
                or proof.get("source") != "revoked_delegation_and_finished_parent"
                or proof.get("taskId") != task_id
                or proof.get("roleAgentId") != payload["roleAgentId"]
                or proof.get("roleSessionId") != "agent-line-" + task_id
                or not isinstance(proof.get("parentTaskId"), str)
                or not proof["parentTaskId"]
                or proof.get("parentFinished") is not True
                or proof.get("delegationRevoked") is not True
                or proof.get("executionClaimed") is not False
                or json.loads(row["execution_identity"])
            ):
                raise Conflict(
                    "Unstarted cancellation requires matching positive absence proof"
                )
            if db.execute("SELECT 1 FROM events WHERE task_id=? AND kind='resources_quarantined' LIMIT 1", (task_id,)).fetchone():
                raise Conflict('Quarantined resources require independent reconciliation')
            evidence = [{"kind": "canceled_without_execution", "proof": proof}]
            if row["execution_status"] == "canceled" and row["evidence"] == canonical(
                evidence
            ):
                if not row['resources_released']:
                    raise Conflict('Cancellation resource evidence is not released')
                if self._delete_owned_workspace_lease(db, row):
                    self._event(db, task_id, 'workspace_lease_recovered')
                return False
            if row["execution_status"] != "cancel_requested":
                raise Conflict(
                    "Only a pending cancellation can be reconciled as unstarted"
                )
            db.execute(
                "UPDATE tasks SET execution_status='canceled', resources_released=1, evidence=?, version=version+1 WHERE id=?",
                (canonical(evidence), task_id),
            )
            self._delete_owned_workspace_lease(db, row)
            self._event(db, task_id, "canceled_without_execution")
            return True

    def finish(self, task_id, status, output, evidence):
        if status not in TERMINAL or not isinstance(evidence, list):
            raise ValueError("Invalid terminal result")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, task_id)
            identity = json.loads(row["execution_identity"])
            execution_id = identity.get("codexTurnId") or identity.get("upstreamTaskId")
            if not execution_id or not any(
                isinstance(e, dict)
                and e.get("kind") == "execution_terminal"
                and e.get("executionId") == execution_id
                and e.get("confirmed") is True
                for e in evidence
            ):
                raise Conflict("Trusted adapter must supply matching terminal evidence")
            if row["execution_status"] in TERMINAL:
                if (
                    row["execution_status"] == status
                    and row["output"] == canonical(output)
                    and [
                        item
                        for item in json.loads(row["evidence"])
                        if not isinstance(item, dict)
                        or item.get("kind") not in {"verification", "verification_cancellation", "resource_exit"}
                    ]
                    == evidence
                ):
                    return False
                raise Conflict("Terminal result is immutable")
            # Model output is never interpreted as independent verification.
            db.execute(
                "UPDATE tasks SET execution_status=?, output=?, evidence=?, version=version+1 WHERE id=?",
                (status, canonical(output), canonical(evidence), task_id),
            )
            self._event(db, task_id, status)
            return True

    @staticmethod
    def _validate_resource_exit(identity, payload, receipt, *, strict=False):
        if (
            not isinstance(receipt, dict)
            or receipt.get("source") != "docker-inspect-before-remove"
            or receipt.get("running") is not False
            or receipt.get("pid") != 0
            or receipt.get("status") not in ("exited", "dead")
            or not receipt.get("owner")
            or not receipt.get("containerId")
            or not receipt.get("finishedAt")
            or receipt.get("workspace") != payload.get("workspace")
            or not isinstance(receipt.get("identities"), list)
            or not identity.get("codexThreadId")
            or not identity.get("codexTurnId")
            or not any(
                isinstance(item, dict)
                and item.get("codexThreadId") == identity["codexThreadId"]
                and item.get("codexTurnId") == identity["codexTurnId"]
                for item in receipt.get("identities", [])
            )
        ):
            raise Conflict("Container exit evidence does not match this task")
        if strict and (
            not isinstance(receipt.get('owner'), str)
            or not isinstance(receipt.get('containerId'), str)
            or not re.fullmatch(r'[a-f0-9]{32}', str(receipt.get('owner', '')))
            or not re.fullmatch(r'[a-f0-9]{64}', str(receipt.get('containerId', '')))
            or type(receipt.get('pid')) is not int
            or sum(isinstance(item, dict)
                   and item.get('codexThreadId') == identity['codexThreadId']
                   and item.get('codexTurnId') == identity['codexTurnId']
                   for item in receipt['identities']) != 1
            or not isinstance(receipt.get('finishedAt'), str)
            or not receipt['finishedAt'].strip()
        ):
            raise Conflict('Full owned container exit identity required')

    def resolve_executor_interruption(self, task_id, identity, exit_receipt):
        """Trusted executor proof, not a claim that native execution terminated."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._row(db, task_id)
            known = json.loads(row['execution_identity'])
            if (not isinstance(identity, dict) or identity != known
                    or not all(isinstance(identity.get(key), str) and identity[key].strip()
                               for key in ('upstreamTaskId', 'codexThreadId', 'codexTurnId'))):
                raise Conflict('Executor interruption must match complete native identity')
            if db.execute("SELECT 1 FROM events WHERE task_id=? AND kind='resources_quarantined' LIMIT 1", (task_id,)).fetchone():
                raise Conflict('Quarantined resources require independent reconciliation')
            self._validate_resource_exit(identity, json.loads(row['payload']), exit_receipt, strict=True)
            canceled = (row['execution_status'] == 'cancel_requested' or bool(db.execute(
                "SELECT 1 FROM events WHERE task_id=? AND kind='cancel_requested' LIMIT 1", (task_id,)).fetchone()))
            evidence = [
                {'kind': 'execution_interrupted', 'taskId': task_id,
                 'executionIdentity': identity, 'reason': 'owned_executor_exited_without_native_terminal',
                 'nativeStatus': 'unknown', 'cancellationRequested': canceled},
                {'kind': 'resource_exit', 'taskId': task_id,
                 'executionIdentity': identity, 'receipt': exit_receipt}]
            if row['execution_status'] in TERMINAL:
                def valid_verification(item):
                    if (not isinstance(item, dict) or item.get('kind') != 'verification'
                            or item.get('taskId') != task_id or item.get('executionIdentity') != identity):
                        return False
                    checks = item.get('checks')
                    if (not isinstance(checks, list) or not checks
                            or any(not isinstance(check, dict) or check.get('status') not in ('passed', 'failed') for check in checks)):
                        return False
                    expected = 'passed' if all(check['status'] == 'passed' for check in checks) else 'failed'
                    return item.get('status') == expected
                core = [item for item in json.loads(row['evidence']) if not valid_verification(item)]
                if (row['execution_status'] == 'failed' and row['resources_released']
                        and core == evidence):
                    if self._delete_owned_workspace_lease(db, row):
                        self._event(db, task_id, 'workspace_lease_recovered')
                    return False
                raise Conflict('Existing terminal evidence cannot be replaced by interruption')
            if row['execution_status'] not in ('accepted', 'cancel_requested'):
                raise Conflict('Only accepted execution can be resolved as interrupted')
            db.execute("UPDATE tasks SET execution_status='failed', resources_released=1, output=NULL, evidence=?, version=version+1 WHERE id=?", (canonical(evidence), task_id))
            self._delete_owned_workspace_lease(db, row)
            self._event(db, task_id, 'execution_interrupted')
            self._event(db, task_id, 'resources_released')
            return True

    def confirm_resources_released(self, task_id, execution_id, exit_receipt=None):
        """Called only by the native adapter after its owned resources exit."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._row(db, task_id)
            identity = json.loads(row["execution_identity"])
            expected = identity.get("codexTurnId") or identity.get("upstreamTaskId")
            if (
                not expected
                or expected != execution_id
                or row["execution_status"] not in TERMINAL
            ):
                raise Conflict("Resource release does not match terminal execution")
            if db.execute(
                "SELECT 1 FROM events WHERE task_id=? AND kind='resources_quarantined' LIMIT 1",
                (task_id,),
            ).fetchone():
                raise Conflict(
                    "Owned resource exit must be independently reconciled after quarantine"
                )
            evidence = json.loads(row["evidence"])
            if not any(isinstance(item, dict) and item.get('kind') == 'execution_terminal'
                       and item.get('executionId') == expected and item.get('confirmed') is True
                       for item in evidence):
                raise Conflict('Resource release requires matching native terminal evidence')
            previous = [item for item in evidence
                        if isinstance(item, dict) and item.get('kind') == 'resource_exit']
            if previous:
                if (len(previous) != 1 or previous[0].get('taskId') != task_id
                        or previous[0].get('executionIdentity') != identity):
                    raise Conflict('Stored container exit evidence does not match this task')
                self._validate_resource_exit(identity, json.loads(row['payload']), previous[0].get('receipt'))
            added = False
            if exit_receipt is not None:
                payload = json.loads(row["payload"])
                receipt = exit_receipt
                self._validate_resource_exit(identity, payload, receipt)
                entry = {
                    "kind": "resource_exit",
                    "taskId": task_id,
                    "executionIdentity": identity,
                    "receipt": receipt,
                }
                if previous and previous != [entry]:
                    raise Conflict("Container exit evidence is immutable")
                if not previous:
                    evidence.append(entry)
                    added = True
            if row["resources_released"] and not added:
                if self._delete_owned_workspace_lease(db, row):
                    self._event(db, task_id, 'workspace_lease_recovered')
                return False
            db.execute(
                "UPDATE tasks SET resources_released=1, evidence=?, version=version+1 WHERE id=?",
                (canonical(evidence), task_id),
            )
            self._delete_owned_workspace_lease(db, row)
            self._event(db, task_id, "resources_released")
            return True

    def _set_memory_status(self, db, task_id, status):
        if status not in ("not_requested", "pending", "committed", "failed", "needs_review", "deleting", "deleted"):
            raise ValueError("Invalid memory status")
        row = self._row(db, task_id)
        if row["memory_status"] == status:
            return False
        db.execute(
            "UPDATE tasks SET memory_status=?, version=version+1 WHERE id=?",
            (status, task_id),
        )
        self._event(db, task_id, "memory_" + status)
        return True

    def set_memory_status(self, task_id, status):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self._set_memory_status(db, task_id, status)

    def sync_memory_status(self, task_id, read_status):
        # Read the current memory ledger only after obtaining the task write lock.
        # A delayed pending receipt cannot overwrite a newer committed projection.
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._row(db, task_id)
            return self._set_memory_status(db, task_id, read_status())

    def record_execution_observation(self, task_id, status):
        """Persist observation freshness without replacing execution evidence."""
        if not isinstance(status, str) or status not in ('observed', 'unknown'):
            raise ValueError('Execution observation must be observed or unknown')
        kind = 'execution_observed' if status == 'observed' else 'execution_observation_unknown'
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task = self._row(db, task_id)
            latest = db.execute(
                "SELECT kind FROM events WHERE task_id=? AND kind IN "
                "('execution_observed','execution_observation_unknown') ORDER BY seq DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if task['execution_status'] in TERMINAL and task['resources_released']:
                # Only clear an existing unknown after trusted terminal/exit
                # processing succeeds. Old terminal tasks gain no new metadata.
                if not (latest is not None and latest['kind'] == 'execution_observation_unknown'
                        and status == 'observed'):
                    return False
            if latest is not None and latest['kind'] == kind:
                return False
            db.execute('UPDATE tasks SET version=version+1 WHERE id=?', (task_id,))
            self._event(db, task_id, kind)
            return True

    def last_execution_observation(self, task_id):
        """Return the last observation independently of the frozen task body."""
        with self.connect() as db:
            self._row(db, task_id)
            latest = db.execute(
                "SELECT kind,created_at FROM events WHERE task_id=? AND kind IN "
                "('execution_observed','execution_observation_unknown') ORDER BY seq DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if latest is None:
                return None
            return {'status': 'observed' if latest['kind'] == 'execution_observed' else 'unknown',
                    'updatedAt': latest['created_at']}

    @staticmethod
    def _recovery_line(value, name, limit):
        if (not isinstance(value, str) or not value or value != value.strip()
                or len(value) > limit
                or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            raise ValueError(f'Recovery {name} must be one bounded line')
        return value

    def migrate_recovery_records(self):
        """Upgrade version 2 to 3. Version 3 is unchanged. This never registers a task."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            version = db.execute('SELECT schema_version FROM installation').fetchone()
            if version is None or version[0] not in READABLE_SCHEMA_VERSIONS:
                raise ValueError('Unsupported task database schema')
            present = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recovery_records'"
            ).fetchone() is not None
            if version[0] in RECOVERY_SCHEMA_VERSIONS:
                if not present:
                    raise ValueError('Unsupported task database schema')
                return False
            db.execute(
                'CREATE TABLE IF NOT EXISTS recovery_records ('
                'id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), '
                'action TEXT NOT NULL, finding TEXT NOT NULL, decision TEXT NOT NULL, '
                'allow_new_attempt INTEGER NOT NULL CHECK(allow_new_attempt IN (0, 1)), '
                'created_at REAL NOT NULL)'
            )
            db.execute(
                'CREATE INDEX IF NOT EXISTS recovery_records_task '
                'ON recovery_records(task_id, created_at)'
            )
            changed = db.execute(
                'UPDATE installation SET schema_version=3 WHERE schema_version=2',
            ).rowcount
            if changed != 1:
                raise ValueError('Recovery migration did not update the installation')
            return True

    def record_recovery(self, task_id, *, action, finding, decision, allow_new_attempt):
        """Store one human recovery decision. Allowing a later attempt does not create one."""
        action = self._recovery_line(action, 'action', 80)
        finding = self._recovery_line(finding, 'finding', 500)
        decision = self._recovery_line(decision, 'decision', 500)
        if type(allow_new_attempt) is not bool:
            raise ValueError('Recovery allow_new_attempt must be true or false')
        record_id = uuid.uuid4().hex
        created_at = time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            version = db.execute('SELECT schema_version FROM installation').fetchone()
            if version is None or version[0] not in RECOVERY_SCHEMA_VERSIONS:
                raise ValueError('Recovery records require schema 3 or 4; explicit migration required')
            before = self._row(db, task_id)
            db.execute(
                'INSERT INTO recovery_records'
                '(id,task_id,action,finding,decision,allow_new_attempt,created_at) '
                'VALUES (?,?,?,?,?,?,?)',
                (record_id, task_id, action, finding, decision, int(allow_new_attempt), created_at),
            )
            self._event(db, task_id, 'recovery_recorded')
            after = self._row(db, task_id)
            if (after['version'] != before['version']
                    or after['execution_status'] != before['execution_status']
                    or after['submission_status'] != before['submission_status']
                    or after['payload'] != before['payload']):
                raise ValueError('Recovery record must not change the task')
        return {
            'id': record_id, 'taskId': task_id, 'action': action, 'finding': finding,
            'decision': decision, 'allowNewAttempt': allow_new_attempt,
            'createdAt': created_at, 'submissionAttempted': False,
        }

    def recovery_records_available(self):
        """True only for an intact schema 3 store. Never migrates or creates a task."""
        with self.connect() as db:
            version = db.execute('SELECT schema_version FROM installation').fetchone()
            present = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recovery_records'"
            ).fetchone() is not None
        return version is not None and version[0] in RECOVERY_SCHEMA_VERSIONS and present

    def recovery_records(self, task_id):
        with self.connect() as db:
            self._row(db, task_id)
            version = db.execute('SELECT schema_version FROM installation').fetchone()
            if version is None or version[0] not in RECOVERY_SCHEMA_VERSIONS:
                raise ValueError('Recovery records require schema 3 or 4; explicit migration required')
            rows = db.execute(
                'SELECT * FROM recovery_records WHERE task_id=? ORDER BY created_at, id',
                (task_id,),
            ).fetchall()
        return [{
            'id': row['id'], 'taskId': row['task_id'], 'action': row['action'],
            'finding': row['finding'], 'decision': row['decision'],
            'allowNewAttempt': bool(row['allow_new_attempt']),
            'createdAt': row['created_at'], 'submissionAttempted': False,
        } for row in rows]

    def migrate_event_contract(self):
        """Upgrade version 3 to 4. Version 2 must migrate recovery records first."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            version = db.execute('SELECT schema_version FROM installation').fetchone()
            if version is None or version[0] not in READABLE_SCHEMA_VERSIONS:
                raise ValueError('Unsupported task database schema')
            present = self._has_event_contract(db)
            if version[0] in EVENT_CONTRACT_VERSIONS:
                if not present:
                    raise ValueError('Unsupported task database schema')
                return False
            if version[0] != 3:
                raise ValueError('Event contract requires schema 3; migrate recovery records first')
            if not db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='recovery_records'"
            ).fetchone():
                raise ValueError('Event contract requires recovery records')
            if present:
                raise ValueError('Unsupported task database schema')
            TaskStore._apply_event_contract(db)
            changed = db.execute(
                'UPDATE installation SET schema_version=4 WHERE schema_version=3'
            ).rowcount
            if changed != 1 or not self._has_event_contract(db):
                raise ValueError('Event migration did not update the installation')
            return True

    def migrate_event_categories(self):
        """Upgrade version 4 to 5. Version 3 must migrate the event contract first."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            version = db.execute('SELECT schema_version FROM installation').fetchone()
            if version is None or version[0] not in READABLE_SCHEMA_VERSIONS:
                raise ValueError('Unsupported task database schema')
            if version[0] == SCHEMA_VERSION:
                if not self._has_event_category(db):
                    raise ValueError('Unsupported task database schema')
                return False
            if version[0] != 4 or not self._has_event_contract(db):
                raise ValueError('Event categories require schema 4; migrate the event contract first')
            self._apply_event_category(db)
            for kind, category in KIND_CATEGORY.items():
                db.execute(
                    'UPDATE events SET category=? WHERE kind=? AND category IS NULL',
                    (category, kind),
                )
            changed = db.execute(
                'UPDATE installation SET schema_version=? WHERE schema_version=4',
                (SCHEMA_VERSION,),
            ).rowcount
            if changed != 1 or not self._has_event_category(db):
                raise ValueError('Event category migration did not update the installation')
            return True

    def _store_sourced_event(self, db, task_id, *, kind, source_identity, source_event_id,
                             payload=None, occurred_at=None, category=None):
        if not self._has_event_contract(db):
            raise ValueError('Event contract requires schema 4; explicit migration required')
        if payload is not None and not isinstance(payload, dict):
            raise ValueError('Event payload must be an object')
        resolved = self._category_for(kind, category)
        if self._has_event_category(db) and resolved is None:
            raise ValueError('Unknown event category')
        body = dict(payload or {})
        if resolved == 'terminal' and occurred_at is not None:
            latest = self._latest_terminal_row(db, task_id)
            if (latest is not None and latest['occurred_at'] is not None
                    and float(occurred_at) < float(latest['occurred_at'])):
                body['staleTerminal'] = True
        before = self._row(db, task_id)
        previous = db.execute(
            'SELECT MAX(seq) FROM events WHERE task_id=?', (task_id,)
        ).fetchone()[0] or 0
        row = self._event(
            db, task_id, kind, source_identity=source_identity,
            source_event_id=source_event_id, payload=body, occurred_at=occurred_at,
            category=resolved)
        after = self._row(db, task_id)
        if (after['version'] != before['version']
                or after['execution_status'] != before['execution_status']
                or after['submission_status'] != before['submission_status']
                or after['payload'] != before['payload']):
            raise ValueError('Audit event must not change the task')
        stored_payload = json.loads(row['payload']) if row['payload'] else None
        return {
            'seq': row['seq'], 'taskId': row['task_id'], 'kind': row['kind'],
            'category': row['category'] if 'category' in row.keys() else None,
            'eventId': row['event_id'], 'runId': row['run_id'],
            'occurredAt': row['occurred_at'], 'receivedAt': row['received_at'],
            'sourceIdentity': row['source_identity'], 'taskVersion': row['task_version'],
            'payload': stored_payload,
            'duplicate': row['seq'] <= previous,
            'staleTerminal': bool(stored_payload and stored_payload.get('staleTerminal') is True),
            'submissionAttempted': False,
        }

    def record_sourced_event(self, task_id, *, kind, source_identity, source_event_id,
                             payload=None, occurred_at=None):
        """Store one sourced audit event. Repeats and payloads do not change task state."""
        kind = self._event_line(kind, 'kind', 160)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            return self._store_sourced_event(
                db, task_id, kind=kind, source_identity=source_identity,
                source_event_id=source_event_id, payload=payload, occurred_at=occurred_at)

    @staticmethod
    def _flow_event_id(prefix, *parts):
        material = '\n'.join(parts)
        return prefix + '-' + hashlib.sha256(material.encode()).hexdigest()[:40]

    @staticmethod
    def _flow_token(value):
        if (not isinstance(value, str) or not value or value != value.strip()
                or len(value) > 256
                or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            return None
        return value

    def note_role_started(self, task_id):
        """Schema 5 only. Repeating the same role start does not change the task."""
        return self.note_category(
            task_id, 'role_started', self._flow_event_id('role', task_id))

    def note_memory_recalled(self, task_id, call_id):
        call_id = self._flow_token(call_id)
        if call_id is None:
            return None
        return self.note_category(
            task_id, 'memory_recalled', self._flow_event_id('memory', task_id, call_id))

    def note_delegation_started(self, task_id, call_id):
        call_id = self._flow_token(call_id)
        if call_id is None:
            return None
        return self.note_category(
            task_id, 'delegation_started', self._flow_event_id('deleg', task_id, call_id))

    def note_cached_trace(self, task_id, trace):
        """Record observed role, recall, and delegation facts. Unknown traces add nothing."""
        if not isinstance(trace, dict) or trace.get('state') != 'observed':
            return []
        noted = []
        role = self.note_role_started(task_id)
        if role is not None:
            noted.append(role)
        searches = trace.get('searches')
        delegations = trace.get('delegations')
        if not isinstance(searches, list) or not isinstance(delegations, list):
            return noted
        for search in searches:
            if isinstance(search, dict) and search.get('resultState') == 'success':
                item = self.note_memory_recalled(task_id, search.get('callId'))
                if item is not None:
                    noted.append(item)
        for delegation in delegations:
            if isinstance(delegation, dict):
                item = self.note_delegation_started(task_id, delegation.get('callId'))
                if item is not None:
                    noted.append(item)
        return noted

    def note_approval_created(self, task_id, request_id, *, status, occurred_at=None):
        request_id = self._flow_token(request_id)
        if request_id is None or status not in (
                'waiting', 'expired', 'unbound', 'decision_recorded'):
            return None
        when = occurred_at if type(occurred_at) in (int, float) and 0 <= float(occurred_at) <= 100_000_000_000 else None
        return self.note_category(
            task_id, 'approval_created', self._flow_event_id('appr-c', task_id, request_id),
            payload={'status': status, 'requestId': request_id}, occurred_at=when)

    @staticmethod
    def _accepted_rejection_binding(task_id, decision, binding):
        if decision != 'reject' or not isinstance(binding, dict):
            return False
        if set(binding) != {'taskId', 'executionIdentity', 'deliveryStatus'}:
            return False
        identity = binding['executionIdentity']
        if (binding['taskId'] != task_id or binding['deliveryStatus'] != 'accepted'
                or not isinstance(identity, dict) or not identity
                or any(not isinstance(key, str) or not isinstance(value, str) or not value
                       or len(value) > 160 for key, value in identity.items())
                or not isinstance(identity.get('codexThreadId'), str)
                or not identity['codexThreadId']
                or not isinstance(identity.get('codexTurnId'), str)
                or not identity['codexTurnId']):
            return False
        return True

    def note_approval_decided(self, task_id, request_id, decision_id, decision, *, binding=None):
        request_id = self._flow_token(request_id)
        decision_id = self._flow_token(decision_id)
        if request_id is None or decision_id is None or decision not in ('approve', 'reject'):
            return None
        payload = {'decision': decision, 'requestId': request_id}
        if binding is not None:
            if not self._accepted_rejection_binding(task_id, decision, binding):
                return None
            payload = {
                'decision': 'reject',
                'requestId': request_id,
                'taskId': task_id,
                'executionIdentity': dict(binding['executionIdentity']),
                'deliveryStatus': 'accepted',
            }
        return self.note_category(
            task_id, 'approval_decided',
            self._flow_event_id('appr-d', task_id, request_id, decision_id),
            payload=payload)

    def approval_decisions(self, task_id):
        """Read stored approval decisions without changing the task or its controls."""
        after = 0
        found = []
        while True:
            page = self.events(task_id, after=after, limit=500)
            if not page:
                return found
            for event in page:
                if event.get('kind') != 'approval_decided':
                    continue
                raw = event.get('payload')
                if isinstance(raw, dict):
                    payload = raw
                elif isinstance(raw, str):
                    try:
                        payload = json.loads(raw)
                    except ValueError:
                        raise ValueError('Approval decision payload is unreadable') from None
                else:
                    raise ValueError('Approval decision payload is unreadable')
                if not isinstance(payload, dict):
                    raise ValueError('Approval decision payload is unreadable')
                found.append(payload)
            after = page[-1].get('seq')
            if type(after) is not int:
                raise ValueError('Approval decision page is unreadable')
            if len(page) < 500:
                return found

    @staticmethod
    def _approval_event_payload(event):
        raw = event.get('payload')
        if isinstance(raw, dict):
            payload = raw
        elif isinstance(raw, str):
            try:
                payload = json.loads(raw)
            except ValueError:
                raise ValueError('Approval event payload is unreadable') from None
        else:
            raise ValueError('Approval event payload is unreadable')
        if not isinstance(payload, dict):
            raise ValueError('Approval event payload is unreadable')
        return payload

    @staticmethod
    def _created_approval(payload):
        if set(payload) not in ({'status'}, {'status', 'requestId'}):
            raise ValueError('Approval created payload is unreadable')
        status = payload.get('status')
        if status not in ('waiting', 'expired', 'unbound', 'decision_recorded'):
            raise ValueError('Approval created payload is unreadable')
        if 'requestId' not in payload:
            if status == 'waiting':
                raise ValueError('Waiting approval cannot be paired')
            return status, None
        request_id = TaskStore._flow_token(payload.get('requestId'))
        if request_id is None:
            raise ValueError('Approval created payload is unreadable')
        return status, request_id

    @staticmethod
    def _decided_approval_request(task_id, payload):
        keys = set(payload)
        plain = {'decision'}
        bound = {'decision', 'taskId', 'executionIdentity', 'deliveryStatus'}
        if keys not in (plain, plain | {'requestId'}, bound, bound | {'requestId'}):
            raise ValueError('Approval decision payload is unreadable')
        if 'requestId' not in keys or keys == plain:
            request_id = None
        else:
            request_id = TaskStore._flow_token(payload.get('requestId'))
            if request_id is None:
                raise ValueError('Approval decision payload is unreadable')
        if keys <= plain | {'requestId'}:
            if payload.get('decision') not in ('approve', 'reject'):
                raise ValueError('Approval decision payload is unreadable')
            return request_id
        if not TaskStore._accepted_rejection_binding(task_id, payload.get('decision'), {
                'taskId': payload.get('taskId'),
                'executionIdentity': payload.get('executionIdentity'),
                'deliveryStatus': payload.get('deliveryStatus'),
        }):
            raise ValueError('Approval decision payload is unreadable')
        return request_id

    def recorded_approval_blocks_continue(self, task_id):
        """True when a stored waiting approval has no decision for that request.

        Read-only. Absent approval history does not block. expired, unbound, and
        decision_recorded do not block. A waiting record without requestId, or any
        unreadable approval record, raises ValueError.
        """
        opened = {}
        decided = set()
        saw = False
        after = 0
        while True:
            page = self.events(task_id, after=after, limit=500)
            if not page:
                break
            for event in page:
                kind = event.get('kind')
                if kind not in ('approval_created', 'approval_decided'):
                    continue
                saw = True
                payload = self._approval_event_payload(event)
                if kind == 'approval_created':
                    status, request_id = self._created_approval(payload)
                    if request_id is None:
                        continue
                    previous = opened.get(request_id)
                    if previous is not None and previous != status:
                        raise ValueError('Approval created records conflict')
                    opened[request_id] = status
                else:
                    request_id = self._decided_approval_request(task_id, payload)
                    if request_id is not None:
                        decided.add(request_id)
            after = page[-1].get('seq')
            if type(after) is not int:
                raise ValueError('Approval event page is unreadable')
            if len(page) < 500:
                break
        if not saw:
            return False
        return any(status == 'waiting' and request_id not in decided
                   for request_id, status in opened.items())

    def note_category(self, task_id, category, source_event_id, *, payload=None, occurred_at=None):
        """Record a PRD category on schema 5. Older schemas are left unchanged."""
        if category not in EVENT_CATEGORY_SOURCES:
            raise ValueError('Unknown event category')
        source_event_id = self._bounded_event_id(source_event_id)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not self._has_event_category(db):
                return None
            return self._store_sourced_event(
                db, task_id, kind=category, source_identity=EVENT_CATEGORY_SOURCES[category],
                source_event_id=source_event_id, payload=payload, occurred_at=occurred_at,
                category=category)

    def latest_terminal_event(self, task_id):
        """The terminal event with the newest occurrence, not merely the latest insert."""
        with self.connect() as db:
            self._row(db, task_id)
            row = self._latest_terminal_row(db, task_id)
            if row is None:
                return None
            keys = row.keys()
            return {
                'seq': row['seq'], 'kind': row['kind'],
                'category': row['category'] if 'category' in keys else None,
                'occurredAt': row['occurred_at'] if 'occurred_at' in keys else None,
                'eventId': row['event_id'] if 'event_id' in keys else None,
            }

    def events(self, task_id, after=0, limit=100):
        if (
            not isinstance(after, int)
            or after < 0
            or not isinstance(limit, int)
            or not 1 <= limit <= 500
        ):
            raise ValueError("Invalid event cursor or limit")
        with self.connect() as db:
            self._row(db, task_id)
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM events WHERE task_id=? AND seq>? ORDER BY seq LIMIT ?",
                    (task_id, after, limit),
                )
            ]

    def acquire_workspace(self, workspace, task_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                return self._acquire_workspace(db, workspace, task_id)
            except KeyError as exc:
                raise Conflict("Unknown task") from exc

    def release_workspace(self, workspace, task_id):
        workspace = str(Path(workspace).resolve())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT task_id FROM workspace_leases WHERE workspace=?", (workspace,)
            ).fetchone()
            if row is None:
                return False
            if row["task_id"] != task_id:
                raise Conflict("Workspace owned by another task")
            task = self._row(db, task_id)
            if self._verification_pending(task):
                return False
            if (
                task["execution_status"] not in TERMINAL
                or not task["resources_released"]
            ):
                raise Conflict("Execution resources have not been confirmed released")
            db.execute(
                "DELETE FROM workspace_leases WHERE workspace=? AND task_id=?",
                (workspace, task_id),
            )
            return True

    def dispatch_candidates(self, *, after="", limit=50):
        """Read a bounded page; ownership is obtained only by atomic claim()."""
        if (
            not isinstance(after, str)
            or not isinstance(limit, int)
            or not 1 <= limit <= 500
        ):
            raise ValueError("Invalid dispatch page")
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM tasks WHERE id>? AND (execution_status NOT IN ('succeeded','failed','canceled') OR resources_released=0 OR EXISTS (SELECT 1 FROM workspace_leases AS lease WHERE lease.task_id=tasks.id)) ORDER BY id LIMIT ?",
                (after, limit),
            ).fetchall()
            return [self.decode(row) for row in rows]

    @staticmethod
    def _verification_lease(db, row):
        from .verification_policy import validate_policy
        payload = json.loads(row['payload'])
        if 'verificationPolicy' not in payload:
            raise Conflict('Task has no frozen automatic verification policy')
        validate_policy(payload['verificationPolicy'])
        configured = payload.get('workspace')
        if not isinstance(configured, str) or not configured:
            raise Conflict('Verification workspace missing')
        workspace = str(Path(configured).resolve())
        if configured != workspace:
            raise Conflict('Verification workspace is not canonical')
        lease = db.execute('SELECT task_id FROM workspace_leases WHERE workspace=?',
                           (workspace,)).fetchone()
        if lease is None or lease['task_id'] != row['id']:
            raise Conflict('Original verification workspace ownership was lost')
        return workspace

    def verification_lease(self, task_id):
        """Check original ownership only; never reacquire a released workspace."""
        with self.connect() as db:
            return self._verification_lease(db, self._row(db, task_id))

    def _validate_policy_verification(self, db, task, receipt, evidence):
        from .verification_policy import validate_policy, policy_hash
        payload = json.loads(task['payload'])
        policy = validate_policy(payload['verificationPolicy'])
        if receipt.get('policySha256') != policy_hash(policy):
            raise Conflict('Verification policy differs from frozen task policy')
        expected = sorted([{'path': item['path'], 'expectedSha256': item['sha256'],
                            'expectedSize': item['exactSize'], 'maxBytes': item['maxBytes']}
                           for item in policy['artifacts']], key=lambda item: item['path'])
        checks = receipt['checks']
        if policy['schemaVersion'] == 2:
            from .verification_policy import validate_test_check
            if len(checks) != len(policy['artifacts']) + len(policy['tests']):
                raise Conflict('Verification checks do not cover frozen commands')
            artifact_checks = checks[:len(policy['artifacts'])]
            test_checks = checks[len(policy['artifacts']):]
            for check, artifact in zip(artifact_checks, policy['artifacts']):
                if check.get('path') != artifact['path']:
                    raise Conflict('Artifact checks are not in frozen policy order')
            for check, test in zip(test_checks, policy['tests']):
                try:
                    validate_test_check(check, test, policy, task['id'], json.loads(task['execution_identity']))
                    if check['resourceExitReceipt']['workspace'] != payload['workspace']:
                        raise ValueError('Test exit workspace mismatch')
                except (ValueError, TypeError, KeyError):
                    raise Conflict('Test check does not match trusted frozen execution') from None
            checks = artifact_checks
        if any(check.get('kind') != 'artifact_sha256'
               or not {'path', 'expectedSha256', 'expectedSize', 'maxBytes'} <= check.keys()
               for check in checks):
            raise Conflict('Verification check kind does not match frozen policy')
        actual = [{key: check.get(key) for key in ('path', 'expectedSha256', 'expectedSize', 'maxBytes')}
                  for check in checks]
        if any(not isinstance(item['path'], str) for item in actual):
            raise Conflict('Verification artifact path missing')
        if canonical(sorted(actual, key=lambda item: item['path'])) != canonical(expected):
            raise Conflict('Verification checks do not exactly cover frozen policy')
        identity = json.loads(task['execution_identity'])
        terminal_id = identity.get('codexTurnId') or identity.get('upstreamTaskId')
        if not terminal_id or not any(isinstance(item, dict) and item.get('kind') == 'execution_terminal'
                and item.get('executionId') == terminal_id and item.get('confirmed') is True for item in evidence):
            raise Conflict('Verification requires matching native terminal evidence')
        exits = [item for item in evidence if isinstance(item, dict) and item.get('kind') == 'resource_exit']
        if (len(exits) != 1 or exits[0].get('taskId') != task['id']
                or exits[0].get('executionIdentity') != identity):
            raise Conflict('Verification requires owned resource exit proof')
        self._validate_resource_exit(identity, payload, exits[0].get('receipt'), strict=True)
        self._verification_lease(db, task)

    def _note_recorded_artifacts(self, db, task_id, checks):
        """Schema 5 only. Artifact notes do not change the task or its verification."""
        if not self._has_event_category(db):
            return
        for index, check in enumerate(checks):
            if not isinstance(check, dict):
                continue
            artifact_path = check.get('path')
            status = check.get('status')
            if (not isinstance(artifact_path, str) or not artifact_path.strip()
                    or len(artifact_path) > 240 or status not in ('passed', 'failed')
                    or any(ord(char) < 32 or ord(char) == 127 for char in artifact_path)):
                continue
            digest = hashlib.sha256(artifact_path.encode()).hexdigest()[:16]
            self._store_sourced_event(
                db, task_id, kind='artifact_recorded', source_identity='verifier',
                source_event_id=f'artifact-{index}-{digest}',
                payload={'path': artifact_path, 'status': status})

    def record_verification(self, task_id, receipt):
        """Trusted checker only. No HTTP route accepts arbitrary receipts."""
        if not isinstance(receipt, dict) or receipt.get("kind") != "verification":
            raise ValueError("Verification receipt required")
        checks = receipt.get("checks")
        status = receipt.get("status")
        if (
            not isinstance(checks, list)
            or not checks
            or status not in ("passed", "failed")
            or any(
                not isinstance(check, dict)
                or check.get("status") not in ("passed", "failed")
                for check in checks
            )
        ):
            raise ValueError("Nonempty completed verification checks required")
        expected = (
            "passed"
            if all(check["status"] == "passed" for check in checks)
            else "failed"
        )
        if status != expected:
            raise ValueError("Verification summary disagrees with checks")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self._row(db, task_id)
            if (
                task["execution_status"] != "succeeded"
                or not task["resources_released"]
            ):
                raise Conflict(
                    "Execution must succeed and release resources before verification"
                )
            if receipt.get("taskId") != task_id or receipt.get(
                "executionIdentity"
            ) != json.loads(task["execution_identity"]):
                raise Conflict("Verification does not match execution identity")
            evidence = json.loads(task["evidence"])
            previous = [
                item
                for item in evidence
                if isinstance(item, dict) and item.get("kind") == "verification"
            ]
            frozen_policy = json.loads(task['payload']).get('verificationPolicy', {})
            if (frozen_policy.get('schemaVersion') == 2 and frozen_policy.get('requireTests') is True
                    and (task['verification_status'] == 'canceled' or db.execute(
                    "SELECT 1 FROM events WHERE task_id=? AND kind IN ('cancel_requested','canceled','verification_cancel_requested')", (task_id,)).fetchone())):
                raise Conflict('Verification cancellation is already requested')
            if previous:
                if previous[-1] == receipt:
                    return False
                raise Conflict(
                    "Verification already recorded; use a new explicit attempt"
                )
            if 'verificationPolicy' in json.loads(task['payload']):
                self._validate_policy_verification(db, task, receipt, evidence)
            evidence.append(receipt)
            db.execute(
                "UPDATE tasks SET verification_status=?, evidence=?, version=version+1 WHERE id=?",
                (status, canonical(evidence), task_id),
            )
            self._event(db, task_id, "verification_" + status)
            self._note_recorded_artifacts(db, task_id, checks)
            if 'verificationPolicy' in json.loads(task['payload']):
                self._delete_owned_workspace_lease(db, self._row(db, task_id))
            return True

    def record_verification_cancellation(self, task_id, receipt):
        """Trusted runner only; native success remains immutable, cancellation is independent."""
        from .verification_policy import validate_cancellation_receipt
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            task = self._row(db, task_id)
            payload = json.loads(task['payload'])
            identity = json.loads(task['execution_identity'])
            evidence = json.loads(task['evidence'])
            if task['verification_status'] == 'canceled':
                old = [entry for entry in evidence if entry.get('kind') == 'verification_cancellation']
                if len(old) == 1 and canonical(old[0]) == canonical(receipt):
                    return False
                raise Conflict('Verification cancellation is immutable')
            if (task['execution_status'] != 'succeeded' or not task['resources_released']
                    or task['verification_status'] != 'pending' or not db.execute(
                        "SELECT 1 FROM events WHERE task_id=? AND kind IN ('cancel_requested','canceled','verification_cancel_requested')", (task_id,)).fetchone()):
                raise Conflict('Pending test verification cancellation required')
            try:
                validate_cancellation_receipt(receipt, payload.get('verificationPolicy'), task_id, identity, payload['workspace'])
            except (ValueError, TypeError, KeyError):
                raise Conflict('Invalid trusted test cancellation receipt') from None
            terminal_id = identity.get('codexTurnId') or identity.get('upstreamTaskId')
            if not terminal_id or not any(isinstance(entry, dict) and entry.get('kind') == 'execution_terminal'
                    and entry.get('executionId') == terminal_id and entry.get('confirmed') is True for entry in evidence):
                raise Conflict('Cancellation requires matching native terminal evidence')
            exits = [entry for entry in evidence if isinstance(entry, dict) and entry.get('kind') == 'resource_exit']
            if len(exits) != 1 or exits[0].get('taskId') != task_id or exits[0].get('executionIdentity') != identity:
                raise Conflict('Cancellation requires original owned executor exit')
            self._validate_resource_exit(identity, payload, exits[0].get('receipt'), strict=True)
            self._verification_lease(db, task)
            evidence.append(receipt)
            db.execute("UPDATE tasks SET verification_status='canceled',evidence=?,version=version+1 WHERE id=?",
                       (canonical(evidence), task_id))
            self._event(db, task_id, 'verification_canceled')
            self._delete_owned_workspace_lease(db, self._row(db, task_id))
            return True

    def list_tasks(self, project_ids, *, after="", limit=50, status=None,
                   project_id=None, workflow_id=None, run_id=None, include_queue=False,
                   work_item_id=None, node_id=None):
        """Scoped keyset page; authorization scopes are supplied by the service."""
        scopes = sorted(set(project_ids))
        if (
            not isinstance(after, str)
            or type(include_queue) is not bool
            or not isinstance(limit, int)
            or not 1 <= limit <= 500
        ):
            raise ValueError("Invalid task page")
        filters = {'projectId': project_id, 'workflow.id': workflow_id, 'workflow.runId': run_id}
        if node_id is not None:
            filters['workflow.nodeId'] = node_id
        if any(value is not None and (not isinstance(value, str) or not value.strip()
                                      or value != value.strip() or len(value) > 256)
               for value in filters.values()):
            raise ValueError('Invalid task filter')
        if ((workflow_id is None) != (run_id is None)
                or (workflow_id is not None and project_id is None)):
            raise ValueError('A run filter requires project, workflow and run identities')
        if node_id is not None and (project_id is None or workflow_id is None or run_id is None):
            raise ValueError('A node filter requires a complete run scope')
        if work_item_id is not None:
            if (not isinstance(work_item_id, str)
                    or re.fullmatch(r'[A-Za-z0-9_-]{1,128}', work_item_id) is None
                    or project_id is None):
                raise ValueError('A business task filter requires project and bounded task identities')
            filters['workItem.workItemId'] = work_item_id
        if project_id is not None and project_id not in scopes:
            raise KeyError('Task scope unavailable')
        if not scopes:
            return {"tasks": [], "nextCursor": None}
        placeholders = ",".join("?" for _ in scopes)
        conditions = [f"json_extract(payload,'$.projectId') IN ({placeholders})"]
        scope_params = [*scopes]
        for field, value in filters.items():
            if value is not None:
                conditions.append(f"json_extract(payload,'$.{field}')=?")
                scope_params.append(value)
        if work_item_id is not None:
            # A trusted frozen business context must belong to the same project
            # as its task; never match arbitrary input.workItem text.
            conditions.append("json_extract(payload,'$.workItem.projectId')=?")
            scope_params.append(project_id)
        where = ' AND '.join(conditions)
        query = 'SELECT * FROM tasks WHERE ' + where + ' AND id>?'
        params = [*scope_params, after]
        if status is not None:
            query += " AND execution_status=?"
            params.append(status)
        query += " ORDER BY id LIMIT ?"
        params.append(limit + 1)
        with self.connect() as db:
            db.execute('BEGIN')
            if after and any(value is not None for value in filters.values()):
                # Status is mutable: a task leaving the chosen status must not
                # invalidate an otherwise correctly scoped pagination cursor.
                if db.execute('SELECT 1 FROM tasks WHERE ' + where + ' AND id=?',
                              [*scope_params, after]).fetchone() is None:
                    raise KeyError('Task cursor unavailable')
            rows = db.execute(query, params).fetchall()
            page = [self.decode(row) for row in rows[:limit]]
            self._attach_activity(db, rows[:limit], page)
            for row, task in zip(rows, page):
                if include_queue and task['executionStatus'] == 'registered':
                    task['executionQueue'] = {'policy': 'serial_fifo', 'state': 'waiting',
                                              'reason': self._queue_reason(db, row)}
                latest = task['activity']['lastEvent']
                if (task['executionStatus'] == 'registered' and latest
                        and (latest['kind'] in ROLE_REVISION_BLOCKS
                             or latest['kind'] == SUBMISSION_REJECTION_BLOCK)):
                    task['submissionBlock'] = latest['kind']
        return {
            "tasks": page,
            "nextCursor": page[-1]["id"] if len(rows) > limit else None,
        }

    @contextmanager
    def _workflow_lock(self, *, exclusive=False):
        # Independent open descriptions are required for thread/process flock isolation.
        path = self.path.with_name(self.path.name + ".admission.lock")
        try:
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        except OSError:
            raise AdmissionClosed("Workflow admission lock unavailable") from None
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1):
                raise AdmissionClosed("Invalid workflow admission lock")
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            os.close(fd)

    @contextmanager
    def workflow_admission(self):
        # Do not hold a task DB transaction across the Sim network call.
        with self._workflow_lock():
            with self.connect() as db:
                if db.execute("SELECT 1 FROM admission_guard LIMIT 1").fetchone():
                    raise AdmissionClosed("Workflow submission is guarded")
            yield

    def enter_maintenance(self):
        """Atomically stop new admission/claims; running tasks may reconcile."""
        owner = secrets.token_urlsafe(32)
        with self._workflow_lock(exclusive=True), self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM admission_guard").fetchone():
                raise Conflict(
                    "Admission already guarded; do not replace another guard"
                )
            db.execute(
                "INSERT INTO admission_guard VALUES (1,?)",
                (canonical({"kind": "maintenance", "owner": owner}),),
            )
        return owner

    def leave_maintenance(self, owner):
        if not isinstance(owner, str):
            raise ValueError("Maintenance owner required")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT reason FROM admission_guard WHERE id=1").fetchone()
            try:
                guard = json.loads(row["reason"]) if row else {}
            except (ValueError, TypeError):
                guard = {}
            if (
                not isinstance(guard, dict)
                or guard.get("kind") != "maintenance"
                or not secrets.compare_digest(
                    str(guard.get("owner", "")).encode(), owner.encode()
                )
            ):
                raise Conflict("Only the maintenance owner may remove its own guard")
            db.execute("DELETE FROM admission_guard WHERE id=1")

    def maintenance_status(self):
        with self.connect() as db:
            guarded = (
                db.execute("SELECT 1 FROM admission_guard LIMIT 1").fetchone()
                is not None
            )
            active = db.execute(
                "SELECT COUNT(*) FROM tasks WHERE execution_status NOT IN ('registered','succeeded','failed','canceled') OR (execution_status IN ('succeeded','failed','canceled') AND resources_released=0)"
            ).fetchone()[0]
            queued = db.execute(
                "SELECT COUNT(*) FROM tasks WHERE execution_status='registered'"
            ).fetchone()[0]
        return {
            "guarded": guarded,
            "activeExecutions": active,
            "queuedTasks": queued,
            "executionQuiescent": guarded and active == 0,
        }

    def plugin_removal_decision(self):
        """Read-only gate: hot plugin unload is unsupported and never releases leases."""
        steps = [
            "不交付运行时热更新或热换版，不要在宿主里热卸载或热替换插件。",
            "先进入维护并停止新准入，再核对每个在途任务的身份。",
            "未知执行保持原来的写租约，不能当成已释放或已成功。",
            "在途任务和写租约都清空之后，停止宿主，再按冷维护删除插件文件。",
        ]
        with self.connect() as db:
            db.execute("BEGIN")
            guarded = (
                db.execute("SELECT 1 FROM admission_guard LIMIT 1").fetchone()
                is not None
            )
            lease_count = db.execute("SELECT COUNT(*) FROM workspace_leases").fetchone()[0]
            rows = db.execute(
                """SELECT id, execution_status, resources_released, execution_identity,
                          EXISTS(SELECT 1 FROM workspace_leases l WHERE l.task_id=tasks.id) AS owns_lease
                   FROM tasks ORDER BY id"""
            ).fetchall()
        blockers = []
        for row in rows:
            try:
                identity = json.loads(row["execution_identity"] or "{}")
            except (ValueError, TypeError):
                identity = None
            unknown = (
                identity is None
                or row["execution_status"] in {"uncertain", "unknown"}
                or (isinstance(identity, dict) and (
                    identity.get("nativeStatus") == "unknown"
                    or identity.get("status") == "unknown"))
            )
            inflight = row["execution_status"] not in TERMINAL
            released = bool(row["resources_released"])
            owns_lease = bool(row["owns_lease"])
            if inflight or unknown or owns_lease or not released:
                blockers.append({
                    "taskId": row["id"],
                    "executionStatus": row["execution_status"],
                    "unknown": unknown,
                    "ownsLease": owns_lease,
                    "resourcesReleased": released,
                })
        if blockers or lease_count:
            reason = "inflight_uninstall_forbidden"
        elif not guarded:
            reason = "admission_still_open"
        else:
            reason = "cold_removal_only"
        return {
            "hotUpdateSupported": False,
            "hotUnloadAllowed": False,
            "uninstallPerformed": False,
            "leasesReleased": False,
            "coldRemovalAllowed": guarded and not blockers and lease_count == 0,
            "admissionGuarded": guarded,
            "leaseCount": lease_count,
            "reason": reason,
            "blockers": blockers,
            "steps": steps,
        }

    def quarantine_resources(self, task_id):
        """Revoke disproven release evidence and stop new work pending audit."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._row(db, task_id)
            db.execute(
                "UPDATE tasks SET resources_released=0, version=version+1 WHERE id=?",
                (task_id,),
            )
            db.execute(
                "INSERT OR IGNORE INTO admission_guard VALUES (1,?)",
                (canonical({"kind": "resource_uncertainty", "taskId": task_id}),),
            )
            self._event(db, task_id, "resources_quarantined")
