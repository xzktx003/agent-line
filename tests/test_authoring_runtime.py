"""Private authoring configuration never grants machine credentials human power."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException
import httpx

from agent_line.adapters.sim import SimClient
from agent_line.authoring_runtime import load_authoring_api
from agent_line.sim_journal import SimJournal


class AuthoringRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root/'authoring.json'
        self.journal = SimJournal.initialize(self.root/'journal.sqlite')
        self.requests = []
        def network(request):
            self.requests.append(request)
            raise AssertionError('Factory must not contact Sim')
        self.http = httpx.Client(transport=httpx.MockTransport(network))
        self.addCleanup(self.http.close)
        self.client = SimClient('http://sim.invalid', 'k'*40, client=self.http)
        self.projects = {'one': {'workspace': str(self.root), 'enabled': True},
                         'two': {'workspace': str(self.root)}}
        self.config = {'schemaVersion': 1, 'workspaces': {'one': 'sim-workspace'},
            'credentials': [{'token': 'h'*40, 'subject': 'human-one', 'projectScopes': ['one'],
                             'capabilities': ['edit_workflows'], 'expiresAt': 4102444800}]}

    def write(self):
        self.path.write_text(json.dumps(self.config)); self.path.chmod(0o600)

    def load(self):
        self.write()
        return load_authoring_api(self.path, projects=self.projects, service_tokens=['s'*40],
                                  client=self.client, journal=self.journal)

    def test_real_api_binds_existing_transport_and_journal_without_io(self):
        with patch.object(SimJournal, 'initialize', side_effect=AssertionError('No DB creation')):
            api = self.load()
        self.assertIs(api.journal, self.journal)
        self.assertIs(api.clients['one'].sim, self.client)
        self.assertEqual(api.clients['one'].workspace_id, 'sim-workspace')
        principal = api.access.authenticate('Bearer '+'h'*40)
        self.assertEqual(principal.capabilities, frozenset({'edit_workflows'}))
        self.assertEqual(principal.project_scopes, frozenset({'one'}))
        self.assertEqual(self.requests, [])

    def test_publish_optional_and_shared_native_workspace_allowed(self):
        self.config['workspaces']['two'] = 'sim-workspace'
        entry = self.config['credentials'][0]
        entry['projectScopes'] = ['one', 'two']
        entry['capabilities'] = ['publish_workflows', 'edit_workflows']
        api = self.load()
        self.assertEqual(set(api.clients), {'one', 'two'})
        self.assertIn('publish_workflows', api.access.authenticate('Bearer '+'h'*40).capabilities)

    def test_strict_top_level_and_duplicate_json_keys(self):
        original = copy.deepcopy(self.config)
        for mutation in (lambda c:c.update(schemaVersion=True), lambda c:c.update(extra=True),
                         lambda c:c.update(credentials=[]), lambda c:c.update(workspaces={})):
            self.config = copy.deepcopy(original); mutation(self.config)
            with self.assertRaises(ValueError): self.load()
        self.path.write_text('{"schemaVersion":1,"schemaVersion":1}'); self.path.chmod(0o600)
        with self.assertRaises(ValueError):
            load_authoring_api(self.path, projects=self.projects, service_tokens=[], client=self.client, journal=self.journal)

    def test_private_file_mode_symlink_and_missing_file_refused(self):
        self.write(); self.path.chmod(0o644)
        def load(path):
            return load_authoring_api(path, projects=self.projects, service_tokens=[], client=self.client, journal=self.journal)
        with self.assertRaises(ValueError): load(self.path)
        self.path.chmod(0o600)
        link = self.root/'alias.json'; link.symlink_to(self.path)
        with self.assertRaises(ValueError): load(link)
        missing = self.root/'missing.json'
        with self.assertRaises(FileNotFoundError): load(missing)
        self.assertFalse(missing.exists())

    def test_scope_and_capability_validation(self):
        original = copy.deepcopy(self.config)
        for key, value in [('projectScopes', []), ('projectScopes', ['one', 'one']),
                           ('projectScopes', ['two']), ('projectScopes', [[]]),
                           ('capabilities', ['publish_workflows']), ('capabilities', ['edit_workflows']*2),
                           ('capabilities', ['approve']), ('capabilities', [None])]:
            self.config = copy.deepcopy(original); self.config['credentials'][0][key] = value
            with self.assertRaises(ValueError): self.load()
        self.assertEqual(self.requests, [])

    def test_workspaces_must_be_enabled_registered_and_segment_safe(self):
        for value in ({'missing': 'sim'}, {'one': '../sim'}, {'../one': 'sim'}, {'one': False}):
            self.config['workspaces'] = value
            with self.assertRaises(ValueError): self.load()
        self.config['workspaces'] = {'one': 'sim'}
        self.projects['one']['enabled'] = False
        with self.assertRaises(ValueError): self.load()

    def test_credentials_cannot_be_service_or_sim_key_and_cannot_rotate_into_them(self):
        for token in ('s'*40, 'k'*40, 'short', 'x'*40+' ', ''):
            self.config['credentials'][0]['token'] = token
            with self.assertRaises(ValueError): self.load()
        self.config['credentials'][0]['token'] = 'h'*40
        api = self.load(); principal = api.access.authenticate('Bearer '+'h'*40)
        for token in ('s'*40, 'k'*40):
            with self.assertRaises(ValueError): api.access.replace_credentials({token: principal})

    def test_duplicate_tokens_subjects_and_invalid_subjects_rejected(self):
        first = copy.deepcopy(self.config['credentials'][0])
        for second in (dict(first, subject='other'), dict(first, token='q'*40),
                       dict(first, token='q'*40, subject=''), dict(first, token='q'*40, subject='x'*257)):
            self.config['credentials'] = [first, second]
            with self.assertRaises(ValueError): self.load()

    def test_expiry_is_finite_positive_and_expired_credentials_never_authorize(self):
        for expiry in (True, 0, -1, float('nan'), float('inf'), '4102444800'):
            self.config['credentials'][0]['expiresAt'] = expiry
            with self.assertRaises(ValueError): self.load()
        self.config['credentials'][0]['expiresAt'] = 1
        api = self.load()
        with self.assertRaises(HTTPException) as caught: api.access.authenticate('Bearer '+'h'*40)
        self.assertEqual(caught.exception.status_code, 403)

    def test_existing_transport_and_journal_required(self):
        self.write()
        for client, journal in ((None, self.journal), (self.client, None)):
            with self.assertRaises(ValueError):
                load_authoring_api(self.path, projects=self.projects, service_tokens=[], client=client, journal=journal)
        self.assertEqual(self.requests, [])
