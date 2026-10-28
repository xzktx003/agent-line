"""Durable outbound Sim mutation intents; no automatic mutation retries.

An interrupted intent must be reconciled using Sim's read API. Even a missing
remote run never authorizes replay. This journal is bound to one Sim deployment;
its database must be backed up with that deployment, not copied to a new server.
"""

import hashlib
import json
import math
import sqlite3
import time
import threading
from contextlib import closing, contextmanager, nullcontext
from pathlib import Path

from .adapters.sim import SubmissionUncertain, UpstreamFailure, segment
from .store import Conflict, canonical
from .sim_input import require_sim_input


class RunInputTooLarge(Exception):
    """The frozen root input exceeds the bounded read limit."""


class SimJournal:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.mutation_guard = nullcontext
        self.run_control = None
        self._mutation_local = threading.local()
        with self.connect() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise ValueError("Unsupported Sim journal schema")

    @classmethod
    def initialize(cls, path):
        path = Path(path).resolve()
        with path.open("xb"):
            pass
        path.chmod(0o600)
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE operations (
                workflow TEXT NOT NULL, run TEXT NOT NULL, context TEXT NOT NULL,
                input TEXT NOT NULL, status TEXT NOT NULL, receipt TEXT,
                updated REAL NOT NULL, PRIMARY KEY(workflow,run,context))""")
            db.execute("PRAGMA user_version=1")
        return cls(path)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _public(row):
        return {
            "workflowId": row["workflow"],
            "runId": row["run"],
            "contextId": row["context"] or None,
            "status": row["status"],
            "safeToResubmit": False,
            "receipt": json.loads(row["receipt"]) if row["receipt"] else None,
        }

    def get(self, workflow_id, run_id, context_id=None):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM operations WHERE workflow=? AND run=? AND context=?",
                (workflow_id, run_id, context_id or ""),
            ).fetchone()
            if row is None:
                raise KeyError((workflow_id, run_id, context_id))
            return self._public(row)

    @staticmethod
    def trigger_namespace(installation_id, trigger_id):
        if not isinstance(installation_id, str) or not installation_id.strip():
            raise ValueError('Stable installation identity required')
        segment(trigger_id)
        return 'trigger-definition-' + hashlib.sha256(
            canonical([installation_id, trigger_id]).encode()).hexdigest()

    def reserve_trigger_binding(self, installation_id, trigger_id, binding):
        """Freeze a trigger definition in the existing journal, across restarts.

        The namespace is independent of the selected workflow/version/project.
        Reusing a trigger ID cannot turn the same event into a second run after
        a configuration edit. An existing identical definition is read-only,
        including during maintenance; new definitions require normal admission.
        """
        if not isinstance(installation_id, str) or not installation_id.strip():
            raise ValueError('Stable installation identity required')
        segment(trigger_id)
        if (not isinstance(binding, dict)
                or set(binding) != {'projectId', 'bindingId', 'type'}
                or not isinstance(binding['projectId'], str) or not binding['projectId']
                or binding['type'] not in ('webhook', 'schedule')):
            raise ValueError('Trusted trigger definition required')
        segment(binding['bindingId'])
        namespace = self.trigger_namespace(installation_id, trigger_id)
        identity = (namespace, 'configuration', 'author-trigger-binding')
        encoded = canonical(binding)

        def matches(row):
            if (row['input'] != encoded or row['status'] != 'accepted'
                    or row['receipt'] != encoded):
                raise Conflict('Trigger definition changed; use a new trigger ID')

        with self.connect() as db:
            row = db.execute('SELECT input,status,receipt FROM operations WHERE workflow=? AND run=? AND context=?',
                             identity).fetchone()
        if row is not None:
            matches(row)
            return
        with self.mutation_scope(), self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT input,status,receipt FROM operations WHERE workflow=? AND run=? AND context=?',
                             identity).fetchone()
            if row is not None:
                matches(row)
                return
            db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?)',
                       (*identity, encoded, 'accepted', encoded, time.time()))

    def reserve_publication(self, workflow_id, run_id, snapshot):
        """Freeze a catalog binding before execute, in this same existing journal."""
        segment(workflow_id)
        segment(run_id)
        encoded = canonical(snapshot)
        with self.mutation_scope(), self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT input FROM operations WHERE workflow=? AND run=? AND context='run-publication'",
                             (workflow_id, run_id)).fetchone()
            if row is not None:
                if row['input'] != encoded:
                    raise Conflict('Run belongs to a different publication')
                return
            db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?)',
                       (workflow_id, run_id, 'run-publication', encoded, 'accepted', encoded, time.time()))

    def get_publication(self, workflow_id, run_id):
        return self.get(workflow_id, run_id, 'run-publication')['receipt']

    def read_run_input(self, workflow_id, run_id):
        """Read and hash one bounded root input without opening a write connection."""
        segment(workflow_id)
        segment(run_id)

        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('Duplicate JSON object key')
                result[key] = value
            return result

        def reject_constant(_value):
            raise ValueError('Non-finite JSON number')

        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=15)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT length(CAST(input AS BLOB)) AS input_size, "
                "CASE WHEN length(CAST(input AS BLOB)) <= 64000 THEN input END AS input "
                "FROM operations WHERE workflow=? AND run=? AND context=''",
                (workflow_id, run_id),
            ).fetchone()
        if row is None:
            raise KeyError((workflow_id, run_id))
        if type(row['input_size']) is not int or row['input_size'] < 0:
            raise ValueError('Invalid journal input size')
        if row['input_size'] > 64000:
            raise RunInputTooLarge
        try:
            value = json.loads(row['input'], object_pairs_hook=unique_object,
                               parse_constant=reject_constant)
            if type(value) is not dict:
                raise ValueError('Root input must be an object')

            require_sim_input(value)
            digest = hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise ValueError('Invalid journal input') from None
        return value, digest

    def summarize_runs(self, workflow_ids):
        """Metadata for owned workflow roots across versions, in one snapshot."""
        if (type(workflow_ids) is not list or len(workflow_ids) > 100
                or any(type(item) is not str for item in workflow_ids)
                or len(set(workflow_ids)) != len(workflow_ids)):
            raise ValueError('Bounded unique workflow identities required')
        for workflow_id in workflow_ids:
            segment(workflow_id)
        if not workflow_ids:
            return {}
        marks = ','.join('?' for _ in workflow_ids)
        with self.connect() as db:
            db.execute('BEGIN')
            rows = db.execute(
                'SELECT e.workflow,e.run,e.status,e.updated,g.total FROM operations e JOIN '
                '(SELECT workflow,count(*) AS total,max(rowid) AS latest FROM operations '
                "WHERE context='' AND workflow IN (" + marks + ') GROUP BY workflow) g '
                'ON e.rowid=g.latest', workflow_ids).fetchall()
        result = {wid: {'source': 'local_root_submissions_all_versions', 'count': 0,
                        'latest': None} for wid in workflow_ids}
        for row in rows:
            try:
                run_id = segment(row['run'])
            except ValueError:
                raise Conflict('Invalid run history identity') from None
            count, updated = row['total'], row['updated']
            if type(count) is not int or not 1 <= count <= 9_007_199_254_740_991:
                raise Conflict('Invalid run history count')
            valid_time = (type(updated) in (int, float) and math.isfinite(updated)
                          and 0 <= updated <= 8_640_000_000_000)
            result[row['workflow']] = {
                'source': 'local_root_submissions_all_versions', 'count': count,
                'latest': {'runId': run_id,
                           'submissionStatus': row['status'] if row['status'] in
                           ('intent', 'accepted', 'uncertain', 'rejected') else 'unknown',
                           'updatedAt': updated if valid_time else None}}
        return result

    def list_runs(self, workflow_id, *, publication=None, after=None, limit=50):
        """Root submission metadata only; no native observations or input reads."""
        segment(workflow_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Run page limit must be 1..100')
        if after is not None:
            segment(after)
        join = ''
        where = "e.workflow=? AND e.context=''"
        params = [workflow_id]
        if publication is not None:
            if type(publication) is not dict or not publication:
                raise ValueError('Explicit publication scope required')
            encoded = canonical(publication)
            join = (" JOIN operations p ON p.workflow=e.workflow AND p.run=e.run "
                    "AND p.context='run-publication'")
            where += " AND p.status='accepted' AND p.input=? AND p.receipt=?"
            params.extend((encoded, encoded))
        source = 'operations e' + join
        with self.connect() as db:
            db.execute('BEGIN')
            if after is not None:
                cursor = db.execute('SELECT e.rowid FROM ' + source + ' WHERE ' + where
                                    + ' AND e.run=?', (*params, after)).fetchone()
                if cursor is None:
                    raise KeyError(after)
                where += ' AND e.rowid<?'
                params.append(cursor[0])
            rows = db.execute('SELECT e.run,e.status,e.updated FROM ' + source
                              + ' WHERE ' + where + ' ORDER BY e.rowid DESC LIMIT ?',
                              (*params, limit + 1)).fetchall()
        runs = []
        for row in rows[:limit]:
            try:
                run_id = segment(row['run'])
            except ValueError:
                raise Conflict('Invalid run history identity') from None
            updated = row['updated']
            valid_time = (type(updated) in (int, float) and math.isfinite(updated)
                          and 0 <= updated <= 8_640_000_000_000)
            status = row['status']
            runs.append({'runId': run_id, 'workflowId': workflow_id,
                         'submissionStatus': status if status in ('intent', 'accepted', 'uncertain', 'rejected') else 'unknown',
                         'updatedAt': updated if valid_time else None})
        return {'runs': runs, 'nextCursor': runs[-1]['runId'] if len(rows) > limit else None}

    def resume_input(self, workflow_id, run_id, context_id):
        """Trusted internal read of the original frozen result, without resending."""
        for value in (workflow_id, run_id, context_id):
            segment(value)
        with self.connect() as db:
            row = db.execute('SELECT input FROM operations WHERE workflow=? AND run=? AND context=?',
                             (workflow_id, run_id, context_id)).fetchone()
        if row is None:
            raise KeyError((workflow_id, run_id, context_id))
        return json.loads(row['input'])

    def resume_summary(self, workflow_id, run_id):
        """Read accepted child identity separately from the latest submission.

        A rejected or unknown newer attempt must not erase the accepted child's
        identity. No upstream request, reconciliation write or retry occurs here.
        """
        segment(workflow_id)
        segment(run_id)
        with self.connect() as db:
            rows = db.execute(
                "SELECT context,status,receipt,updated FROM operations WHERE workflow=? AND run=? "
                "AND context<>'' AND context<>'run-publication' AND context NOT LIKE 'author-%' "
                "AND context<>'run-cancel' AND context NOT LIKE 'run-control-%' "
                # Receipt arrival updates `updated`; it must not reorder an
                # older intent after a newer unknown one. Normal journal writes
                # only INSERT/UPDATE, and SQLite backup preserves this order.
                "ORDER BY rowid DESC",
                (workflow_id, run_id)).fetchall()
        if not rows:
            return None

        def identity(row):
            target = None
            receipt = json.loads(row['receipt']) if row['receipt'] else None
            if (row['status'] == 'accepted' and isinstance(receipt, dict)
                    and receipt.get('workflowId', workflow_id) == workflow_id
                    and receipt.get('runId', run_id) == run_id
                    and receipt.get('contextId', row['context']) == row['context']):
                try:
                    target = segment(receipt.get('resumedRunId'))
                except ValueError:
                    pass
            return {'contextId': row['context'], 'status': row['status'],
                    'runId': target, 'safeToResubmit': False}

        accepted = next((row for row in rows if row['status'] == 'accepted'), None)
        return {'latestAttempt': identity(rows[0]),
                'latestAccepted': identity(accepted) if accepted is not None else None}

    @contextmanager
    def mutation_scope(self):
        # Approval decision recording and its nested journal submission share one lease.
        if getattr(self._mutation_local, "active", False):
            yield
            return
        with self.mutation_guard():
            self._mutation_local.active = True
            try:
                yield
            finally:
                self._mutation_local.active = False

    def _submit(self, client, workflow_id, run_id, context_id, inputs, *, operation=None, work_item=None):
        segment(workflow_id)
        segment(run_id)
        if context_id is not None:
            segment(context_id)
        encoded = canonical(inputs)
        with self.connect() as db:
            db.execute('BEGIN')
            old = db.execute(
                "SELECT * FROM operations WHERE workflow=? AND run=? AND context=?",
                (workflow_id, run_id, context_id or ""),
            ).fetchone()
            if old is not None:
                if old["input"] != encoded:
                    raise Conflict("Sim mutation identity reused with different input")
                if context_id is None:
                    from .work_items import bind_run
                    bind_run(db, workflow_id, run_id, encoded, work_item, existing=True)
                return self._public(old)
        # No journal transaction is held while waiting on admission.
        with self.mutation_scope(), (self.run_control.admission(workflow_id, run_id)
                if self.run_control is not None and operation != 'cancel' else nullcontext()):
            return self._submit_admitted(client, workflow_id, run_id, context_id, inputs, operation=operation, work_item=work_item)

    def _submit_admitted(self, client, workflow_id, run_id, context_id, inputs, *, operation=None, work_item=None):
        segment(workflow_id)
        segment(run_id)
        if context_id is not None:
            segment(context_id)
        identity = (workflow_id, run_id, context_id or "")
        encoded = canonical(inputs)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM operations WHERE workflow=? AND run=? AND context=?",
                identity,
            ).fetchone()
            if context_id is None:
                from .work_items import bind_run
                bind_run(db, workflow_id, run_id, encoded, work_item, existing=old is not None)
            if old is not None:
                if old["input"] != encoded:
                    raise Conflict("Sim mutation identity reused with different input")
                return self._public(old)
            require_sim_input(json.loads(encoded))
            db.execute(
                "INSERT INTO operations VALUES (?,?,?,?,?,?,?)",
                (*identity, encoded, "intent", None, time.time()),
            )
        # The transaction is committed before calling the upstream service.
        # Process death anywhere after here leaves a non-replayable intent.
        try:
            if operation == 'cancel':
                receipt = client.cancel(workflow_id, run_id)
            elif context_id is None:
                receipt = client.execute(workflow_id, run_id, json.loads(encoded))
            else:
                receipt = client.resume(
                    workflow_id, run_id, context_id, json.loads(encoded)
                )
            status = "accepted"
        except UpstreamFailure as exc:
            status, receipt = "rejected", {"httpStatus": exc.status_code}
        except SubmissionUncertain:
            status, receipt = "uncertain", None
        except Exception:
            # A transport implementation may raise after sending the request.
            # Persist ambiguity, but preserve the original error for the caller.
            self._record(identity, "uncertain", None)
            raise
        self._record(identity, status, receipt)
        return self.get(workflow_id, run_id, context_id)

    def _record(self, identity, status, receipt):
        with self.connect() as db:
            db.execute(
                "UPDATE operations SET status=?,receipt=?,updated=? WHERE workflow=? AND run=? AND context=?",
                (
                    status,
                    canonical(receipt) if receipt is not None else None,
                    time.time(),
                    *identity,
                ),
            )

    def mutate(self, namespace, operation_id, kind, payload, operation):
        """Submit one authoring mutation; persisted intents never authorize replay.

        The caller binds the callback and normalized payload to a trusted project
        and actor. Receipts describe transport outcomes, not authoritative graph
        state. Existing records remain observable while admission is closed.
        """
        segment(namespace)
        segment(operation_id)
        if kind not in ("create", "save", "publish", "archive"):
            raise ValueError("Unsupported Sim authoring mutation")
        if not callable(operation):
            raise ValueError("Authoring operation callback required")
        identity = (namespace, operation_id, "author-" + kind)
        encoded = canonical(payload)
        with self.connect() as db:
            old = db.execute(
                "SELECT * FROM operations WHERE workflow=? AND run=? AND context=?",
                identity,
            ).fetchone()
        if old is not None:
            if old["input"] != encoded:
                raise Conflict("Sim mutation identity reused with different input")
            return self._public(old)
        with self.mutation_scope():
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                old = db.execute(
                    "SELECT * FROM operations WHERE workflow=? AND run=? AND context=?",
                    identity,
                ).fetchone()
                if old is not None:
                    if old["input"] != encoded:
                        raise Conflict("Sim mutation identity reused with different input")
                    return self._public(old)
                db.execute(
                    "INSERT INTO operations VALUES (?,?,?,?,?,?,?)",
                    (*identity, encoded, "intent", None, time.time()),
                )
            # No transaction spans the network. BaseException leaves the durable
            # intent intact; ordinary failures retain ambiguity without secrets.
            try:
                receipt = operation()
                if not isinstance(receipt, dict):
                    raise ValueError("Authoring receipt must be a JSON object")
                canonical(receipt)
            except Exception:
                self._record(identity, "uncertain", None)
            else:
                self._record(identity, "accepted", receipt)
            return self.get(*identity)

    def execute(self, client, workflow_id, run_id, inputs, *, work_item=None):
        return self._submit(client, workflow_id, run_id, None, inputs, work_item=work_item)

    def resume(self, client, workflow_id, run_id, context_id, inputs):
        segment(context_id)
        if context_id in ('run-cancel', 'run-publication') or context_id.startswith(('run-control-', 'author-')):
            raise ValueError('Reserved local journal context')
        return self._submit(client, workflow_id, run_id, context_id, inputs)

    def cancel(self, client, workflow_id, run_id):
        # Cancellation has one durable identity per root run. Unknown receipts
        # are never retried, and cancellation bypasses only the run pause gate.
        return self._submit(client, workflow_id, run_id, 'run-cancel', {}, operation='cancel')
