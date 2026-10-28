"""Authoring shares the durable mutation boundary, never remote retry semantics."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import Mock

from agent_line.sim_journal import SimJournal
from agent_line.store import AdmissionClosed, Conflict


class AuthoringJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'sim.sqlite'
        self.journal = SimJournal.initialize(self.path)
        self.payload = {'project': 'one', 'actor': 'owner', 'name': 'flow'}
        self.operation = Mock(return_value={'workflowId': 'remote-id'})

    def mutate(self, **changes):
        arguments = dict(namespace='authoring-project', operation_id='op-1',
                         kind='create', payload=self.payload, operation=self.operation)
        arguments.update(changes)
        return self.journal.mutate(**arguments)

    def count(self):
        with self.journal.connect() as db:
            return db.execute('SELECT COUNT(*) FROM operations').fetchone()[0]

    def test_concurrent_distinct_journal_instances_claim_only_once(self):
        gate = threading.Barrier(8)
        def invoke(_):
            journal = SimJournal(self.path)
            gate.wait(timeout=10)
            return journal.mutate('authoring-project', 'op-1', 'create', self.payload, self.operation)
        with ThreadPoolExecutor(max_workers=8) as pool:
            result = list(pool.map(invoke, range(8)))
        self.operation.assert_called_once_with()
        self.assertEqual(self.count(), 1)
        self.assertTrue(all(row['status'] in ('intent', 'accepted') for row in result))
        self.assertEqual(self.mutate()['receipt'], {'workflowId': 'remote-id'})

    def test_payload_identity_and_canonical_order(self):
        first = self.mutate()
        self.assertEqual(self.mutate(payload=dict(reversed(list(self.payload.items())))), first)
        with self.assertRaises(Conflict): self.mutate(payload={**self.payload, 'actor': 'someone-else'})
        self.operation.assert_called_once_with()

    def test_timeout_reopen_is_uncertain_and_redacted(self):
        self.operation.side_effect = TimeoutError('PRIVATE remote credential message')
        result = self.mutate()
        self.assertEqual(result['status'], 'uncertain')
        self.assertIsNone(result['receipt'])
        self.assertNotIn('PRIVATE', str(result))
        reopened = SimJournal(self.path)
        never = Mock(side_effect=AssertionError('must not replay'))
        self.assertEqual(reopened.mutate('authoring-project', 'op-1', 'create', self.payload, never), result)
        never.assert_not_called()

    def test_keyboard_interrupt_keeps_intent(self):
        self.operation.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt): self.mutate()
        self.journal = SimJournal(self.path)
        result = self.mutate()
        self.assertEqual(result['status'], 'intent')
        self.assertFalse(result['safeToResubmit'])
        self.operation.assert_called_once_with()

    def test_maintenance_denies_new_intent_but_allows_existing_read(self):
        first = self.mutate()
        @contextmanager
        def denied():
            raise AdmissionClosed('maintenance')
            yield
        self.journal.mutation_guard = denied
        self.assertEqual(self.mutate(), first)
        with self.assertRaises(AdmissionClosed): self.mutate(operation_id='new')
        self.assertEqual(self.count(), 1)
        self.operation.assert_called_once_with()

    def test_callback_has_committed_intent_and_no_open_write_transaction(self):
        def remote():
            # A WAL reader alone would not detect a leaked writer transaction.
            with sqlite3.connect(self.path, timeout=0) as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT status FROM operations').fetchone()
                self.assertEqual(row[0], 'intent')
            return {'ok': True}
        self.assertEqual(self.mutate(operation=remote)['status'], 'accepted')

    def test_kinds_have_independent_contexts_in_same_schema(self):
        for kind in ('create', 'save', 'publish'):
            result = self.mutate(kind=kind)
            self.assertEqual(result['contextId'], 'author-'+kind)
        self.assertEqual(self.operation.call_count, 3)
        with self.journal.connect() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)

    def test_invalid_kind_or_identity_has_no_intent(self):
        for changes in ({'kind': 'execute'}, {'operation_id': '../other'}, {'namespace': ''}):
            with self.assertRaises(ValueError): self.mutate(**changes)
        self.assertEqual(self.count(), 0)
        self.operation.assert_not_called()

    def test_invalid_remote_receipt_persists_uncertainty(self):
        for suffix, receipt in enumerate((None, [], {'notJson': object()})):
            self.operation.return_value = receipt
            result = self.mutate(operation_id='invalid-'+str(suffix))
            self.assertEqual(result['status'], 'uncertain')
            self.assertIsNone(result['receipt'])

    def test_shared_outer_mutation_scope_not_reentered(self):
        entered = []
        @contextmanager
        def guard():
            entered.append(True)
            yield
        self.journal.mutation_guard = guard
        with self.journal.mutation_scope():
            self.mutate()
        self.assertEqual(len(entered), 1)
