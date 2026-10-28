import tempfile
import json
import unittest
from dataclasses import replace
from pathlib import Path
from agent_line.completion_relay import ResumeBinding
from agent_line.pause_bindings import PauseBindings
from agent_line.store import Conflict


class PauseBindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "bindings.sqlite"
        self.store = PauseBindings.initialize(self.path)
        self.binding = ResumeBinding(
            task_id="task",
            workflow_id="workflow",
            run_id="run",
            block_id="wait",
            context_id="context",
            purpose="external_task_result",
        )

    def test_restart_preserves_original_context_and_idempotency(self):
        self.assertTrue(self.store.bind(self.binding))
        reopened = PauseBindings(self.path)
        self.assertEqual(reopened.get("task"), self.binding)
        self.assertFalse(reopened.bind(self.binding))

    def test_old_binding_defaults_to_no_failure_branch_without_conflict(self):
        self.store.bind(self.binding)
        with self.store.connect() as db:
            payload = json.loads(db.execute("SELECT payload FROM bindings").fetchone()[0])
            payload.pop("allow_failure_branch")
            db.execute("UPDATE bindings SET payload=?", (json.dumps(payload),))
        reopened = PauseBindings(self.path)
        self.assertFalse(reopened.get("task").allow_failure_branch)
        self.assertFalse(reopened.bind(self.binding))
        with self.assertRaises(Conflict):
            reopened.bind(replace(self.binding, allow_failure_branch=True))

    def test_cannot_retarget_existing_task_to_later_loop(self):
        self.store.bind(self.binding)
        with self.assertRaises(Conflict):
            self.store.bind(replace(self.binding, context_id="later"))
        self.assertEqual(self.store.get("task"), self.binding)

    def test_one_pause_cannot_accept_results_from_two_tasks(self):
        self.store.bind(self.binding)
        with self.assertRaises(Conflict):
            self.store.bind(replace(self.binding, task_id="other"))

    def test_human_approval_never_becomes_automatic_completion_binding(self):
        with self.assertRaises(ValueError):
            self.store.bind(replace(self.binding, purpose="human_approval"))

    def test_missing_storage_does_not_silently_recreate(self):
        self.path.unlink()
        with self.assertRaises(Exception):
            self.store.get("task")
        self.assertFalse(self.path.exists())

    def test_page_order_cursor_limit_and_reopen(self):
        for name in ('task-c', 'task-a', 'task-b'):
            self.store.bind(replace(self.binding, task_id=name, run_id=name))
        first=self.store.page(limit=2)
        self.assertEqual([b.task_id for b in first], ['task-a','task-b'])
        self.assertEqual([b.task_id for b in PauseBindings(self.path).page(after=first[-1].task_id,limit=2)], ['task-c'])
        self.assertEqual(self.store.page(after='task-z'), [])
        for cursor in (None, True, '../bad', 'a'*161):
            with self.assertRaises(ValueError):self.store.page(after=cursor)
        for limit in (True, 0, -1, 501, 1.5, '2'):
            with self.assertRaises(ValueError):self.store.page(limit=limit)

    def test_page_never_initializes_missing_database(self):
        self.path.unlink()
        with self.assertRaises(Exception):self.store.page()
        self.assertFalse(self.path.exists())
