"""Trusted human-decision ledger and Sim pause delivery; never a graph scheduler."""
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import sqlite3
import time

from .adapters.sim import segment
from .store import Conflict, canonical


@dataclass(frozen=True)
class ApprovalBinding:
    approval_id: str
    project_id: str
    workflow_id: str
    run_id: str
    block_id: str
    context_id: str
    revision: str
    activation_id: str
    attempt_id: str
    purpose: str
    expires_at: float

    def __post_init__(self):
        for key, value in asdict(self).items():
            if key not in ('purpose', 'expires_at'): segment(value)
        if self.purpose != 'human_approval': raise ValueError('Explicit human approval binding required')
        if type(self.expires_at) not in (int, float) or not math.isfinite(self.expires_at) or self.expires_at <= 0:
            raise ValueError('Finite approval expiration required')


@dataclass(frozen=True)
class TrustedPrincipal:
    """Construct only from service authentication, never deserialize request actor."""
    subject: str
    project_scopes: frozenset[str]
    capabilities: frozenset[str]
    expires_at: float


class ApprovalLedger:
    def __init__(self, path):
        self.path = Path(path).resolve(strict=True)
        with self.connect() as db:
            if db.execute('PRAGMA user_version').fetchone()[0] != 1:
                raise ValueError('Unsupported approval schema')

    @classmethod
    def initialize(cls, path):
        path = Path(path).resolve()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600); os.close(fd)
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE approvals(id TEXT PRIMARY KEY, workflow TEXT NOT NULL, run TEXT NOT NULL, context TEXT NOT NULL, binding TEXT NOT NULL, UNIQUE(workflow,run,context))')
            db.execute('CREATE TABLE decisions(approval_id TEXT PRIMARY KEY REFERENCES approvals(id), decision_id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL, decided_at REAL NOT NULL)')
            db.execute('PRAGMA user_version=1')
        return cls(path)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, timeout=15)
        db.execute('PRAGMA foreign_keys=ON'); db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback(); raise
        finally: db.close()

    def bind(self, binding):
        if not isinstance(binding, ApprovalBinding): raise ValueError('Trusted ApprovalBinding required')
        encoded = canonical(asdict(binding))
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT binding FROM approvals WHERE id=?', (binding.approval_id,)).fetchone()
            if old:
                if old['binding'] != encoded: raise Conflict('Approval binding is immutable')
                return False
            try:
                db.execute('INSERT INTO approvals VALUES (?,?,?,?,?)',
                           (binding.approval_id, binding.workflow_id, binding.run_id, binding.context_id, encoded))
            except sqlite3.IntegrityError:
                raise Conflict('Pause context already belongs to a fixed approval activation') from None
            return True

    def get(self, approval_id):
        with self.connect() as db:
            row = db.execute('SELECT binding FROM approvals WHERE id=?', (approval_id,)).fetchone()
            if row is None: raise KeyError(approval_id)
            decision = db.execute('SELECT payload,decided_at FROM decisions WHERE approval_id=?', (approval_id,)).fetchone()
            return {'binding': ApprovalBinding(**json.loads(row['binding'])),
                    'decision': json.loads(decision['payload']) if decision else None,
                    'decidedAt': decision['decided_at'] if decision else None}

    def list_approvals(self, project_scopes, *, after='', limit=50):
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError('Page limit must be 1..500')
        scopes = sorted(project_scopes)
        if not scopes:
            return {'approvals': [], 'nextCursor': None}
        with self.connect() as db:
            rows = db.execute(
                "SELECT id FROM approvals WHERE id>? AND json_extract(binding, '$.project_id') IN ("
                + ','.join('?' for _ in scopes) + ') ORDER BY id LIMIT ?',
                [after, *scopes, limit + 1]).fetchall()
        return {'approvals': [self.get(row['id']) for row in rows[:limit]],
                'nextCursor': rows[limit - 1]['id'] if len(rows) > limit else None}

    def _record(self, approval_id, decision, now):
        encoded = canonical(decision)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT payload FROM decisions WHERE approval_id=?', (approval_id,)).fetchone()
            if old:
                if old['payload'] != encoded: raise Conflict('Human decision is immutable')
                return False
            try:
                db.execute('INSERT INTO decisions VALUES (?,?,?,?)', (approval_id, decision['decisionId'], encoded, now))
            except sqlite3.IntegrityError:
                raise Conflict('Decision identity already belongs to another approval') from None
            return True


class ApprovalCoordinator:
    def __init__(self, ledger, client, journal, *, authorize, clock=time.time):
        if not callable(authorize): raise ValueError('Live authorization callback required')
        self.ledger, self.client, self.journal = ledger, client, journal
        self.authorize, self.clock = authorize, clock
        self.run_control = None

    def _authorize(self, binding, principal, *, deciding):
        if (not isinstance(principal, TrustedPrincipal) or not isinstance(principal.subject, str)
                or not principal.subject.strip() or len(principal.subject) > 256
                or not isinstance(principal.project_scopes, frozenset)
                or not isinstance(principal.capabilities, frozenset)
                or binding.project_id not in principal.project_scopes or 'approve' not in principal.capabilities
                or type(principal.expires_at) not in (int, float) or not math.isfinite(principal.expires_at)
                or principal.expires_at <= self.clock() or self.authorize(principal) is not True):
            raise PermissionError('Current principal lacks valid project approval authority')
        if deciding and binding.expires_at <= self.clock():
            raise PermissionError('Approval request expired; no delivery permitted')

    def _observe(self, binding):
        observed = self.client.observe(binding.workflow_id, binding.run_id)
        response = observed.get('response', {}) if isinstance(observed, dict) else {}
        run = response.get('data', response) if isinstance(response, dict) else {}
        if not isinstance(run, dict) or run.get('workflowId') != binding.workflow_id or run.get('runId') != binding.run_id:
            return 'unknown', False
        pause = run.get('paused') or {}
        # Counts cover all pauses in the run; only one may remain outstanding.
        # Missing resumedCount is compatible only with the legacy first pause.
        matches = (isinstance(run, dict) and isinstance(pause, dict)
                   and run.get('workflowId') == binding.workflow_id and run.get('runId') == binding.run_id
                   and run.get('status') == 'paused' and pause.get('contextId') == binding.context_id
                   and pause.get('blockedOnBlockId') == binding.block_id and pause.get('pauseKind') == 'human'
                   and type(pause.get('pausePointCount')) is int and pause['pausePointCount'] >= 1
                   and type(pause.get('resumedCount', 0)) is int
                   and pause.get('resumedCount', 0) >= 0
                   and pause['pausePointCount'] - pause.get('resumedCount', 0) == 1)
        return (run.get('status', 'unknown') if isinstance(run, dict) else 'unknown'), matches

    def _journal(self, binding):
        try: return self.journal.get(binding.workflow_id, binding.run_id, binding.context_id)
        except KeyError: return None

    def decide(self, approval_id, *, decision_id, decision, principal):
        segment(approval_id); segment(decision_id)
        if decision not in ('approve', 'reject'): raise ValueError('Explicit approve or reject required')
        record = self.ledger.get(approval_id); binding = record['binding']
        self._authorize(binding, principal, deciding=True)
        value = {'approvalId': approval_id, 'decisionId': decision_id, 'decision': decision,
                 'approved': decision == 'approve', 'decidedBy': principal.subject,
                 'projectId': binding.project_id, 'revision': binding.revision,
                 'activationId': binding.activation_id, 'attemptId': binding.attempt_id}
        if record['decision'] is not None and record['decision'] != value:
            raise Conflict('Human decision, actor, and decisionId are immutable')
        prior = self._journal(binding)
        run_status, matches = self._observe(binding)
        if prior is None and not matches:
            return {'approvalId': approval_id, 'status': 'waiting', 'reason': 'bound_human_pause_not_observed',
                    'runStatus': run_status, 'deliveryStatus': 'not_submitted', 'decision': record['decision']}
        if prior is not None and record['decision'] is None:
            # Another request may have committed this same decision and its
            # outbound intent after our initial audit read. Only an exact,
            # durably recorded decision can authorize replay of that intent.
            current = self.ledger.get(approval_id)
            if current['binding'] != binding or current['decision'] != value:
                raise Conflict('Pause already has an outbound operation without this human decision')
        with (self.journal.mutation_scope() if prior is None else nullcontext()), (
                self.run_control.admission(binding.workflow_id, binding.run_id)
                if self.run_control is not None and prior is None else nullcontext()):
            self._authorize(binding, principal, deciding=True)
            self.ledger._record(approval_id, value, self.clock())
            self._authorize(binding, principal, deciding=True)
            # SimJournal commits mutation intent first; an existing intent is never reposted.
            delivery = self.journal.resume(self.client, binding.workflow_id, binding.run_id, binding.context_id,
                                           {'approval': value})
        return {'approvalId': approval_id, 'status': 'decision_recorded', 'decision': value,
                'deliveryStatus': delivery['status'], 'runStatus': run_status, 'delivery': delivery}

    def reconcile(self, approval_id, *, principal):
        record = self.ledger.get(approval_id); binding = record['binding']
        self._authorize(binding, principal, deciding=False)
        run_status, matches = self._observe(binding)
        delivery = self._journal(binding)
        return {'approvalId': approval_id, 'decision': record['decision'], 'runStatus': run_status,
                'boundPauseObserved': matches, 'deliveryStatus': delivery['status'] if delivery else 'not_submitted',
                'delivery': delivery}
