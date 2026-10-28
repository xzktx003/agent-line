"""Optional trigger ingress uses the original service authentication and gateway."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from agent_line.runtime import load_app
from agent_line.sim_journal import SimJournal
from agent_line.store import TaskStore
from agent_line.trigger_runtime import load_trigger_api


class TriggerRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.projects = {'project': {'workspace': str(self.root)}}
        self.file = self.root/'triggers.json'
        self.trigger = {'schemaVersion':1,'triggers':{'daily':{
            'projectId':'project','bindingId':'published-fixed','type':'schedule'}}}
        self.write(self.file, self.trigger)
        TaskStore.initialize(self.root/'tasks.sqlite')
        self.config = {'schemaVersion':1,'database':'tasks.sqlite','tokens':{'secret':['project']},
            'projects':self.projects,'bindings':{}}
        self.service = self.root/'service.json'
        self.write(self.service,self.config)

    def write(self, path, value):
        path.write_text(json.dumps(value));path.chmod(0o600)

    def test_trigger_file_is_optional_and_no_routes_are_added_by_default(self):
        with TestClient(load_app(self.service)) as http:
            self.assertIsNone(http.app.state.trigger_api)
            self.assertEqual(http.post('/extension/v1/triggers/daily/schedule',json={}).status_code,404)

    def test_loader_preserves_fixed_binding_and_does_not_call_gateway(self):
        gateway=Mock()
        api=load_trigger_api(self.file,projects=self.projects,workflows=gateway)
        self.assertIs(api.workflows,gateway)
        self.assertEqual(api.bindings,self.trigger['triggers'])
        self.trigger['triggers']['daily']['bindingId']='changed'
        self.assertEqual(api.bindings['daily']['bindingId'],'published-fixed')
        gateway.assert_not_called()
        self.assertEqual(gateway.method_calls,[])

    def test_trigger_file_requires_sim_before_any_state_or_execution_initialization(self):
        self.config['triggerFile']='triggers.json';self.write(self.service,self.config)
        with self.assertRaisesRegex(ValueError,'requires Sim'):
            load_app(self.service)

    def test_private_regular_file_and_plain_schema_are_required(self):
        gateway=Mock()
        self.file.chmod(0o644)
        with self.assertRaises(ValueError):load_trigger_api(self.file,projects=self.projects,workflows=gateway)
        self.file.chmod(0o600)
        for value in ({'schemaVersion':True,'triggers':{}},{'schemaVersion':2,'triggers':{}},
                      {'schemaVersion':1,'triggers':{},'unknown':1},[],{'schemaVersion':1}):
            self.write(self.file,value)
            with self.assertRaises(ValueError):load_trigger_api(self.file,projects=self.projects,workflows=gateway)
        gateway.assert_not_called()

    def test_symlink_or_directory_config_is_rejected(self):
        linked=self.root/'linked.json';linked.symlink_to(self.file)
        for path in (linked,self.root):
            with self.assertRaises(ValueError):load_trigger_api(path,projects=self.projects,workflows=Mock())

    def prepare_sim(self):
        SimJournal.initialize(self.root/'sim.sqlite')
        self.write(self.root/'sim-auth.json',{'baseUrl':'http://unreachable.invalid','apiKey':'not-real'})
        self.config.update(triggerFile='triggers.json',sim={'authFile':'sim-auth.json',
            'journal':'sim.sqlite','bindings':{},'installationId':'trigger-runtime-test'})
        self.write(self.service,self.config)

    def test_real_factory_shares_gateway_and_service_authorization(self):
        self.prepare_sim();client=Mock()
        with patch('agent_line.adapters.sim.SimClient',return_value=client):
            app=load_app(self.service)
            with TestClient(app) as http:
                api=app.state.trigger_api
                self.assertIs(api.workflows.client,client)
                body={'slot':'2026-10-22T06:00:00Z','input':{}}
                path='/extension/v1/triggers/daily/schedule'
                self.assertEqual(http.post(path,json=body).status_code,401)
                self.assertEqual(http.post(path,json=body,headers={'Authorization':'Bearer wrong'}).status_code,401)
                self.assertEqual(http.post(path,json=body,headers={'Authorization':'Bearer secret'}).status_code,404)
                client.execute.assert_not_called()
            client.close.assert_called_once()

    def test_failed_trigger_load_closes_existing_sim_transport(self):
        self.prepare_sim();self.file.chmod(0o644);client=Mock()
        with patch('agent_line.adapters.sim.SimClient',return_value=client):
            with self.assertRaises(ValueError):load_app(self.service)
        client.close.assert_called_once()
        client.execute.assert_not_called()
