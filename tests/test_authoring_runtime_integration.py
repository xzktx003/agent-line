"""Runtime ownership of the optional editor routes and shared Sim transport."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from agent_line.adapters.sim import SimClient
from agent_line.runtime import load_app
from agent_line.sim_journal import SimJournal
from agent_line.store import TaskStore


class AuthoringRuntimeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.tasks = TaskStore.initialize(self.root / 'tasks.sqlite')
        SimJournal.initialize(self.root / 'sim.sqlite')
        self.editor_token = 'editor-' + 'x' * 32
        self.private('sim-auth.json', {'baseUrl': 'http://127.0.0.1:3190', 'apiKey': 'sim-key'})
        self.authoring = {'schemaVersion': 1, 'workspaces': {'project': 'native-workspace'},
                          'credentials': [{'token': self.editor_token, 'subject': 'owner',
                                           'projectScopes': ['project'],
                                           'capabilities': ['edit_workflows', 'publish_workflows'],
                                           'expiresAt': time.time() + 3600}]}
        self.private('authoring.json', self.authoring)
        self.settings = {'schemaVersion': 1, 'database': 'tasks.sqlite',
                         'tokens': {'service-key': ['project']},
                         'projects': {'project': {'workspace': str(self.root)}}, 'bindings': {},
                         'sim': {'authFile': 'sim-auth.json', 'journal': 'sim.sqlite',
                                 'installationId': 'test-installation', 'bindings': {}},
                         'authoringFile': 'authoring.json'}
        self.private('service.json', self.settings)
        self.requests = []
        def transport(request):
            self.requests.append(request)
            raise AssertionError('Configuration and project lookup must not contact Sim')
        self.http = httpx.Client(transport=httpx.MockTransport(transport))
        self.addCleanup(self.http.close)
        self.sim = SimClient('http://127.0.0.1:3190', 'sim-key', client=self.http)

    def private(self, name, value):
        path = self.root / name
        path.write_text(json.dumps(value)); path.chmod(0o600)

    def load(self):
        with patch('agent_line.adapters.sim.SimClient', return_value=self.sim):
            return load_app(self.root / 'service.json')

    def test_routes_share_existing_client_without_network_or_new_database(self):
        app = self.load()
        self.assertIs(app.state.authoring_api.clients['project'].sim, self.sim)
        with TestClient(app) as client:
            route = '/extension/v1/authoring/projects'
            self.assertEqual(client.get(route).status_code, 401)
            response = client.get(route, headers={'Authorization': 'Bearer ' + self.editor_token})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['projects'], [{'id': 'project', 'name': 'project'}])
            self.assertEqual(client.get(route, headers={'Authorization': 'Bearer service-key'}).status_code, 401)
        self.assertEqual(self.requests, [])

    def test_configured_editor_still_obeys_actual_task_maintenance_guard(self):
        owner = self.tasks.enter_maintenance()
        self.addCleanup(self.tasks.leave_maintenance, owner)
        with TestClient(self.load()) as client:
            response = client.post('/extension/v1/authoring/projects/project/workflows',
                                   headers={'Authorization': 'Bearer ' + self.editor_token},
                                   json={'operationId': 'attempt-1', 'name': '隔离流程'})
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.headers.get('X-Agent-Line-Admission'), 'closed')
        self.assertEqual(self.requests, [])
        with SimJournal(self.root / 'sim.sqlite').connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM operations').fetchone()[0], 0)

    def test_missing_sim_is_rejected_without_initializing_or_fetching(self):
        self.settings.pop('sim'); self.private('service.json', self.settings)
        with self.assertRaisesRegex(ValueError, 'requires Sim'):
            self.load()
        self.assertEqual(self.requests, [])

    def test_bad_optional_config_closes_shared_transport_before_raising(self):
        self.authoring['schemaVersion'] = 9; self.private('authoring.json', self.authoring)
        with patch.object(self.sim, 'close', wraps=self.sim.close) as close:
            with self.assertRaises(ValueError):
                self.load()
            close.assert_called_once_with()
        self.assertEqual(self.requests, [])
