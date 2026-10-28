"""Durable one-task/one-pause correlation, not a workflow scheduler."""

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from .completion_relay import ResumeBinding
from .adapters.sim import segment
from .store import Conflict, canonical


class PauseBindings:
    def __init__(self, path):
        self.path = Path(path).resolve()
        with self.connect() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise ValueError("Unsupported pause binding schema")

    @classmethod
    def initialize(cls, path):
        path = Path(path).resolve()
        with path.open("xb"):
            pass
        path.chmod(0o600)
        with sqlite3.connect(path) as db:
            db.execute(
                "CREATE TABLE bindings(task_id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, run_id TEXT NOT NULL, context_id TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(workflow_id,run_id,context_id))"
            )
            db.execute("PRAGMA user_version=1")
        return cls(path)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=15)
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def bind(self, binding):
        if (
            not isinstance(binding, ResumeBinding)
            or binding.purpose != "external_task_result"
        ):
            raise ValueError("Trusted external-task pause binding required")
        for value in (
            binding.task_id,
            binding.workflow_id,
            binding.run_id,
            binding.block_id,
            binding.context_id,
        ):
            segment(value)
        if not isinstance(binding.require_memory_commit, bool) or not isinstance(
            binding.allow_failure_branch, bool
        ) or type(binding.allow_multiple) is not bool:
            raise ValueError("Explicit memory policy required")
        encoded = canonical(asdict(binding))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT payload FROM bindings WHERE task_id=?", (binding.task_id,)
            ).fetchone()
            if old:
                if canonical(asdict(ResumeBinding(**json.loads(old[0])))) != encoded:
                    raise Conflict("Task already bound to another pause or policy")
                return False
            try:
                db.execute(
                    "INSERT INTO bindings VALUES (?,?,?,?,?)",
                    (
                        binding.task_id,
                        binding.workflow_id,
                        binding.run_id,
                        binding.context_id,
                        encoded,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("Pause already owned by another task") from exc
            return True

    def get(self, task_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT payload FROM bindings WHERE task_id=?", (task_id,)
            ).fetchone()
        if row is None:
            raise KeyError(task_id)
        return ResumeBinding(**json.loads(row[0]))

    def for_context(self, workflow_id, run_id, context_id):
        for value in (workflow_id, run_id, context_id):
            segment(value)
        with self.connect() as db:
            row = db.execute('SELECT task_id,payload FROM bindings WHERE workflow_id=? AND run_id=? AND context_id=?',
                             (workflow_id, run_id, context_id)).fetchone()
        if row is None:
            raise KeyError((workflow_id, run_id, context_id))
        binding = ResumeBinding(**json.loads(row[1]))
        if (binding.task_id != row[0] or binding.workflow_id != workflow_id
                or binding.run_id != run_id or binding.context_id != context_id):
            raise ValueError('Binding storage identity mismatch')
        return binding

    def page(self, *, after='', limit=50):
        """Read stable task-key pages without initializing missing storage."""
        if not isinstance(after, str):
            raise ValueError('Cursor must be a task identifier')
        if after:
            segment(after)
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError('Page limit must be 1..500')
        with self.connect() as db:
            rows = db.execute(
                'SELECT task_id,payload FROM bindings WHERE task_id>? ORDER BY task_id LIMIT ?',
                (after, limit),
            ).fetchall()
        bindings = []
        for task_id, payload in rows:
            binding = ResumeBinding(**json.loads(payload))
            if binding.task_id != task_id:
                raise ValueError('Binding storage identity mismatch')
            bindings.append(binding)
        return bindings
