import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock

from fastapi.testclient import TestClient
from agent_line.approvals import ApprovalLedger
from agent_line.approval_runtime import load_approval_api
from agent_line.runtime import load_app
from agent_line.sim_journal import SimJournal
from agent_line.store import TaskStore


class ApprovalRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.ledger = self.root / 'approvals.sqlite'
        ApprovalLedger.initialize(self.ledger)
        self.path = self.root / 'approval.json'
        self.token = 'human-secret-' + 'x' * 32
        self.data = {'schemaVersion': 1, 'ledgerPath': str(self.ledger), 'credentials': [{
            'token': self.token, 'subject': 'owner', 'projectScopes': ['project'],
            'capabilities': ['approve'], 'expiresAt': time.time() + 3600}]}
        self.client, self.journal = Mock(), Mock()

    def write(self):
        self.path.write_text(json.dumps(self.data))
        self.path.chmod(0o600)

    def load(self, service_tokens=()):
        self.write()
        return load_approval_api(self.path, projects={'project': {}},
                                 service_tokens=service_tokens, client=self.client, journal=self.journal)

    def test_load_does_not_contact_sim_or_create_new_storage(self):
        self.assertIsNotNone(self.load())
        self.assertEqual(self.client.mock_calls, [])
        self.assertEqual(self.journal.mock_calls, [])

    def test_rejects_service_token_reuse_and_duplicate_human_identities(self):
        with self.assertRaises(ValueError): self.load([self.token])
        self.data['credentials'].append(copy.deepcopy(self.data['credentials'][0]))
        with self.assertRaises(ValueError): self.load()
        self.data['credentials'][1]['token'] = 'other-human-' + 'z' * 32
        with self.assertRaises(ValueError): self.load()

    def test_rejects_extra_actor_fields_scope_expansion_and_invalid_expiry(self):
        original = copy.deepcopy(self.data)
        for field, value in [('projectScopes', ['other']), ('capabilities', ['admin']),
                             ('expiresAt', True), ('expiresAt', float('inf')),
                             ('actor', 'forged'), ('token', 'short')]:
            self.data = copy.deepcopy(original)
            self.data['credentials'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError): self.load()
        self.data = original
        self.data['schemaVersion'] = True
        with self.assertRaises(ValueError): self.load()

    def test_missing_private_ledger_is_not_reinitialized(self):
        self.ledger.unlink()
        with self.assertRaises(FileNotFoundError): self.load()
        self.assertFalse(self.ledger.exists())

    def test_private_permissions_and_symlink_aliases_rejected(self):
        self.ledger.chmod(0o644)
        with self.assertRaises(ValueError): self.load()
        self.ledger.chmod(0o600)
        alias = self.root / 'alias.sqlite'; alias.symlink_to(self.ledger)
        self.data['ledgerPath'] = str(alias)
        with self.assertRaises(ValueError): self.load()

    def test_runtime_requires_sim_and_mounts_independent_auth_route(self):
        self.write()
        TaskStore.initialize(self.root / 'tasks.sqlite')
        SimJournal.initialize(self.root / 'sim.sqlite')
        auth = self.root / 'sim-auth.json'
        auth.write_text(json.dumps({'baseUrl': 'http://127.0.0.1:1', 'apiKey': 'fixture'}))
        auth.chmod(0o600)
        service = self.root / 'service.json'
        config = {'schemaVersion': 1, 'database': 'tasks.sqlite', 'tokens': {'service-only': ['project']},
                  'projects': {'project': {'workspace': str(self.root)}}, 'bindings': {},
                  'approvalFile': 'approval.json'}
        def write():
            service.write_text(json.dumps(config)); service.chmod(0o600)
        write()
        with self.assertRaises(ValueError): load_app(service)
        config['sim'] = {'authFile': 'sim-auth.json', 'journal': 'sim.sqlite',
                         'installationId': 'fixture', 'bindings': {}}
        write()
        with TestClient(load_app(service)) as http:
            self.assertEqual(http.get('/extension/v1/approvals').status_code, 401)
            self.assertEqual(http.get('/extension/v1/approvals', headers={
                'Authorization': 'Bearer service-only'}).status_code, 401)
            response = http.get('/extension/v1/approvals', headers={'Authorization': 'Bearer ' + self.token})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn(self.token, response.text)
            self.assertNotIn(str(self.root), response.text)

    def test_optional_discovery_storage_is_existing_private_and_load_is_read_only(self):
        from agent_line.approval_discovery import WorkflowRunLedger
        runs = self.root / 'runs.sqlite'
        self.data['runLedgerPath'] = str(runs)
        with self.assertRaises(FileNotFoundError):
            self.load()
        self.assertFalse(runs.exists())
        WorkflowRunLedger.initialize(runs)
        api = self.load()
        self.assertEqual(api.discovery.runs.path, runs)
        self.assertIs(api.discovery.publication_verifier, api.publication_verifier)
        self.assertEqual(self.client.mock_calls, [])
        runs.chmod(0o644)
        with self.assertRaises(ValueError):
            self.load()

    def test_worker_reopens_persistent_runs_without_http_poll_to_register(self):
        from agent_line.approval_discovery import WorkflowRunLedger
        from agent_line.approval_runtime import ApprovalDiscoveryWorker
        import threading
        runs = self.root / 'runs.sqlite'
        ledger = WorkflowRunLedger.initialize(runs)
        ledger.reserve(project_id='project', workflow_id='workflow', run_id='run',
                       revision='1', deployment_version_id='deployment', human_block_ids=['human'],
                       ttl_seconds=300)
        ledger.record_submission('workflow', 'run', 'accepted')
        self.data['runLedgerPath'] = str(runs)
        self.client.observe.return_value = {'response': {'data': {
            'workflowId': 'workflow', 'runId': 'run', 'status': 'paused',
            'paused': {'pauseKind': 'human', 'pausePointCount': 1,
                       'blockedOnBlockId': 'human', 'contextId': 'context'}}}}
        identities = []
        for _ in range(2):
            api = self.load()
            api.discovery.publication_verifier = Mock()
            api.discovery.publication_verifier.verify_run.return_value = True
            ready = threading.Event()
            original = api.discovery.approvals.bind
            def bind(binding):
                result = original(binding)
                ready.set()
                return result
            api.discovery.approvals.bind = bind
            worker = ApprovalDiscoveryWorker(api.discovery, interval=0.01)
            worker.start()
            try:
                self.assertTrue(ready.wait(3))
            finally:
                worker.stop()
            self.assertFalse(worker.thread.is_alive())
            rows = api.coordinator.ledger.list_approvals({'project'})['approvals']
            self.assertEqual(len(rows), 1)
            identities.append(rows[0])
        self.assertEqual(identities[0], identities[1])
        self.client.resume.assert_not_called()
