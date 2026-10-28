import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from fastapi import HTTPException

spec = importlib.util.spec_from_file_location(
    "revocation_bridge",
    Path(__file__).resolve().parents[1] / "plugins/agent-line/execution_bridge.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class DelegationRevocationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "receipts.sqlite"
        module.ReceiptLedger.initialize(self.path)
        self.ledger = module.ReceiptLedger(self.path)
        self.ledger.bind("task", "role", "agent-line-task", 'qwenpaw-role-v1:' + 'a'*64)

    def test_cancel_before_claim_prevents_late_delegation_after_restart(self):
        binding, claimed = self.ledger.revoke_delegation("task")
        self.assertEqual(binding, ("role", "agent-line-task"))
        self.assertFalse(claimed)
        restarted = module.ReceiptLedger(self.path)
        with self.assertRaises(HTTPException):
            restarted.bind("task", "role", "agent-line-task", 'qwenpaw-role-v1:' + 'a'*64)
        with self.assertRaises(HTTPException) as error:
            restarted.claim("task", {"instruction": "late work"})
        self.assertEqual(error.exception.status_code, 409)

    def test_cancel_before_binding_blocks_late_parent_and_codex_claim(self):
        binding, claimed = self.ledger.revoke_delegation("not-bound")
        self.assertIsNone(binding)
        self.assertFalse(claimed)
        with self.assertRaises(HTTPException):
            self.ledger.bind("not-bound", "role", "agent-line-not-bound", 'qwenpaw-role-v1:' + 'a'*64)
        with self.assertRaises(HTTPException):
            self.ledger.claim("not-bound", {})

    def test_claim_before_cancel_is_preserved_without_second_submission(self):
        original, fresh = self.ledger.claim("task", {"instruction": "work"})
        self.assertTrue(fresh)
        _, claimed = self.ledger.revoke_delegation("task")
        self.assertTrue(claimed)
        repeated, fresh = self.ledger.claim("task", {"instruction": "work"})
        self.assertFalse(fresh)
        self.assertEqual(original, repeated)

    def test_race_has_one_ordered_outcome(self):
        def claim():
            try:
                return self.ledger.claim("task", {"instruction": "work"})[1]
            except HTTPException:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            claim_result = pool.submit(claim)
            revoke_result = pool.submit(self.ledger.revoke_delegation, "task")
        self.assertEqual(claim_result.result(), revoke_result.result()[1])
        with self.ledger.connection() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM revoked_delegations").fetchone()[0], 1
            )

    def test_explicit_migration_preserves_v2_binding(self):
        with sqlite3.connect(self.path) as db:
            db.execute("ALTER TABLE role_bindings DROP COLUMN role_revision")
            db.execute("DROP TABLE revoked_delegations")
            db.execute("PRAGMA user_version=2")
        with self.assertRaises(ValueError):
            module.ReceiptLedger(self.path)
        module.ReceiptLedger.migrate(self.path)
        reopened = module.ReceiptLedger(self.path)
        self.assertEqual(reopened.bound_task("role", "agent-line-task"), "task")
        reopened.revoke_delegation("task")
        module.ReceiptLedger.migrate(self.path)
        with self.assertRaises(HTTPException):
            reopened.claim("task", {})
