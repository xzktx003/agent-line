import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
from agent_line.store import AdmissionClosed, TaskStore, Conflict
from agent_line.backup import (
    create_backup, fingerprint_workspace, reconcile_restored_workspace, restore_backup,
)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "original.sqlite"
        self.store = TaskStore.initialize(self.db)
        self.payload = {
            "projectId": "p",
            "roleAgentId": "r",
            "workspace": str(self.root / "workspace"),
            "workflow": {
                "runId": "run",
                "nodeExecutionId": "node",
                "attemptId": "attempt",
            },
            "input": {},
        }

    def test_snapshot_restores_record_and_blocks_submission_until_reconciled(self):
        task, _ = self.store.register("key", self.payload)
        bundle = self.root / "backup"
        create_backup(self.db, bundle)
        restored = self.root / "restored.sqlite"
        restore_backup(bundle, restored)
        store = TaskStore(restored)
        self.assertEqual(store.get(task["id"])["payload"], self.payload)
        with self.assertRaises(Conflict):
            store.claim(task["id"], self.payload["workspace"])
        # Original is neither quarantined nor replaced.
        self.assertTrue(self.store.claim(task["id"], self.payload["workspace"]))

    def test_restore_never_overwrites_existing_database(self):
        create_backup(self.db, self.root / "backup")
        with self.assertRaises(FileExistsError):
            restore_backup(self.root / "backup", self.db)
        self.assertEqual(TaskStore(self.db).path, self.db)

    def test_corrupt_snapshot_is_rejected_before_target_creation(self):
        bundle = self.root / "backup"
        create_backup(self.db, bundle)
        with (bundle / "tasks.sqlite").open("ab") as stream:
            stream.write(b"corruption")
        target = self.root / "restored.sqlite"
        with self.assertRaises(ValueError):
            restore_backup(bundle, target)
        self.assertFalse(target.exists())

    def test_active_task_and_workspace_lease_remain_in_snapshot(self):
        task, _ = self.store.register("key", self.payload)
        self.store.claim(task["id"], self.payload["workspace"])
        create_backup(self.db, self.root / "backup")
        restore_backup(self.root / "backup", self.root / "restored.sqlite")
        store = TaskStore(self.root / "restored.sqlite")
        self.assertEqual(store.get(task["id"])["executionStatus"], "submitting")
        with self.assertRaises(Conflict):
            store.release_workspace(self.payload["workspace"], task["id"])


    def test_restore_reconciliation_reports_code_diff_without_overwrite_or_resubmit(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        source = workspace / "sample.py"
        source.write_text("def add(left, right):\n    return left + right\n")
        task, _ = self.store.register("key", self.payload)
        before = fingerprint_workspace(workspace, ["sample.py"])
        bundle = self.root / "backup"
        create_backup(self.db, bundle)
        source.write_text("def add(left, right):\n    return left + right  # changed\n")
        changed = source.read_text()
        restored = self.root / "restored.sqlite"
        restore_backup(bundle, restored)
        report = reconcile_restored_workspace(
            restored, backup_fingerprint=before,
            current_fingerprint=fingerprint_workspace(workspace, ["sample.py"]),
            execution_observation="unknown", native_triggers="unknown")
        self.assertEqual(report["workspaceDiff"], ["sample.py"])
        self.assertTrue(report["unknownBlocksWrites"])
        self.assertTrue(report["admissionRemainsClosed"])
        self.assertFalse(report["continuationAttempted"])
        self.assertFalse(report["codeOverwritten"])
        self.assertEqual(source.read_text(), changed)
        store = TaskStore(restored)
        with self.assertRaises(Conflict):
            store.claim(task["id"], self.payload["workspace"])
        with self.assertRaises(AdmissionClosed):
            store.register("after-restore", self.payload)
        self.assertEqual(store.get(task["id"])["upstreamSubmissionStatus"], "pending")

    def test_clean_restore_still_does_not_reopen_admission(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        (workspace / "sample.py").write_text("x = 1\n")
        fingerprint = fingerprint_workspace(workspace, ["sample.py"])
        create_backup(self.db, self.root / "backup")
        restore_backup(self.root / "backup", self.root / "restored.sqlite")
        report = reconcile_restored_workspace(
            self.root / "restored.sqlite", backup_fingerprint=fingerprint,
            current_fingerprint=fingerprint, execution_observation="absent",
            native_triggers="stopped")
        self.assertEqual(report["workspaceDiff"], [])
        self.assertFalse(report["unknownBlocksWrites"])
        self.assertFalse(report["newWritesBlocked"])
        self.assertTrue(report["admissionRemainsClosed"])
        with self.assertRaises(AdmissionClosed):
            TaskStore(self.root / "restored.sqlite").register("later", self.payload)
