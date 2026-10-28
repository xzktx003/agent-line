"""Synthetic proof tests bind policy intent to the actual turn context."""
import json
import unittest

from tests import test_execution_readiness as readiness_fixtures
from agent_line.store import canonical


class ReadonlyExecutionReadinessTests(unittest.TestCase):
    def setUp(self):
        self.fixture = readiness_fixtures.ReadinessTest('test_complete_synthetic_facts_confirm_and_read_only')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.controls = {'sandbox': 'read-only', 'approval_policy': 'never'}

    def readonly_fixture(self):
        f = self.fixture
        path = f.root / 'var/qwenpaw/config.json'
        config = json.loads(path.read_text())
        config['plugins']['agent-line']['execution_bridge']['readonly_role_ids'] = [f.role]
        f.write(path, config)
        f.native_payload['executionControls'] = self.controls.copy()
        f.native_record['executionControls'] = self.controls.copy()
        f.update_native()
        f.evidence.append({'kind': 'execution_policy', 'source': 'qwenpaw-plugin-task-binding',
                           'executionId': f.identity['codexTurnId'], 'controls': self.controls.copy()})
        f.update_task()
        with f.rollout.open('a') as stream:
            stream.write('\n' + canonical({'type': 'turn_context', 'payload': {
                'turn_id': f.identity['codexTurnId'], 'sandbox_policy': {'type': 'read-only'},
                'approval_policy': 'never'}}))
        f.write_proof()

    def test_current_readonly_native_receipt_and_turn_can_confirm(self):
        self.readonly_fixture()
        self.assertTrue(self.fixture.evaluate().confirmed)

    def test_configured_readonly_without_frozen_policy_cannot_confirm(self):
        f = self.fixture
        path = f.root / 'var/qwenpaw/config.json'
        config = json.loads(path.read_text())
        config['plugins']['agent-line']['execution_bridge']['readonly_role_ids'] = [f.role]
        f.write(path, config)
        f.assert_unknown()

    def test_receipt_readonly_claim_cannot_hide_writable_turn(self):
        self.readonly_fixture()
        f = self.fixture
        entries = [json.loads(line) for line in f.rollout.read_text().splitlines()]
        for entry in entries:
            if entry['type'] == 'turn_context':
                entry['payload']['sandbox_policy'] = {'type': 'danger-full-access'}
        f.rollout.write_text('\n'.join(canonical(e) for e in entries))
        f.assert_unknown()

    def test_receipt_policy_and_approval_cannot_be_changed(self):
        self.readonly_fixture()
        f = self.fixture
        f.native_record['executionControls'] = {'sandbox': 'read-only', 'approval_policy': 'on-request'}
        f.update_native()
        f.assert_unknown()

    def test_missing_task_policy_evidence_cannot_confirm_readonly(self):
        self.readonly_fixture()
        f = self.fixture
        f.evidence = [e for e in f.evidence if e['kind'] != 'execution_policy']
        f.update_task()
        f.assert_unknown()

    def test_old_readonly_receipt_cannot_confirm_a_new_writable_binding(self):
        self.readonly_fixture()
        f = self.fixture
        path = f.root / 'var/qwenpaw/config.json'
        config = json.loads(path.read_text())
        config['plugins']['agent-line']['execution_bridge'].pop('readonly_role_ids')
        f.write(path, config)
        f.assert_unknown()
