import json
from pathlib import Path
import tempfile
import unittest
import httpx

from agent_line.execution_runtime import load_execution_gateways
from agent_line.role_dispatch import RoleDispatchLedger


class ExecutionRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.workspace=self.root/'workspace';self.workspace.mkdir()
        self.ledger=self.root/'ledger.sqlite';RoleDispatchLedger.initialize(self.ledger)
        self.credentials=self.root/'login.json';self.credentials.write_text(json.dumps({'username':'user','password':'host-secret'}));self.credentials.chmod(0o600)
        self.token=self.root/'service-token';self.token.write_text('service-secret'*3);self.token.chmod(0o600)
        self.path=self.root/'execution.json'
        self.entry={'projectId':'project','roleBindingId':'binding','roleAgentId':'parent','workspace':str(self.workspace),
            'installationId':'installation','ledgerPath':str(self.ledger),'baseURL':'http://qwen.test',
            'hostCredentialsFile':str(self.credentials),'serviceTokenFile':str(self.token),'memoryQuery':'project rules'}
        self.projects={'project':{'workspace':str(self.workspace)}}
        self.bindings={'binding':{'projectId':'project','roleAgentId':'parent'}}
        self.clients=[];self.calls=[]
    def handler(self,request):
        self.calls.append(request)
        if request.url.path.endswith('/execution-binding'):
            return httpx.Response(200,json={'schemaVersion':1,'ownedContainer':True,'workspace':str(self.workspace),'managedRoleIds':['parent'],'targetAgentId':'executor'})
        return httpx.Response(200,json={'access_token':'opaque-host-token'})
    def factory(self,**kwargs):
        c=httpx.Client(transport=httpx.MockTransport(self.handler),**kwargs);self.clients.append(c);return c
    def load(self,entries=None):
        self.path.write_text(json.dumps({'schemaVersion':1,'gateways':entries if entries is not None else [self.entry]}));self.path.chmod(0o600)
        return load_execution_gateways(self.path,projects=self.projects,bindings=self.bindings,client_factory=self.factory)
    def test_load_authenticates_checks_remote_binding_and_exposes_fixed_binding(self):
        result=self.load();gateway=result[('project','binding')];self.addCleanup(gateway.close)
        self.assertEqual(gateway.role_agent_id,'parent');self.assertEqual(gateway.workspace,str(self.workspace))
        self.assertEqual(gateway.installation_id,'installation');self.assertEqual(len(self.calls),2)
        self.assertEqual(self.calls[1].url.path,'/api/agent-line/execution-binding')
        self.assertEqual(self.calls[0].url.path,'/api/auth/login')
        self.assertEqual(json.loads(self.calls[0].content),{'username':'user','password':'host-secret'})
        self.assertEqual(self.clients[0].timeout.read,30)
        gateway.close();self.assertTrue(self.clients[0].is_closed)
    def test_remote_workspace_or_role_mismatch_closes_transport_without_submission(self):
        original_handler = self.handler
        for change in ({'workspace':'/another-project'}, {'managedRoleIds':['other-role']}, {'ownedContainer':False}, {'schemaVersion':True}, {'schemaVersion':1.0}):
            def handler(request):
                response = original_handler(request)
                if request.url.path.endswith('/execution-binding'):
                    return httpx.Response(200,json={**response.json(),**change})
                return response
            self.handler=handler
            with self.assertRaises(ValueError):self.load()
            self.assertTrue(all(client.is_closed for client in self.clients))
            self.assertTrue(all(request.url.path in ('/api/auth/login','/api/agent-line/execution-binding') for request in self.calls))

    def test_unknown_capability_or_other_field_rejected_before_network(self):
        self.entry['controlledExecution']='confirmed'
        with self.assertRaises(ValueError):self.load()
        self.assertFalse(self.calls)
    def test_private_file_permissions_required(self):
        self.credentials.chmod(0o644)
        with self.assertRaises(ValueError):self.load()
        self.assertFalse(self.calls)
    def test_missing_ledger_never_initialized(self):
        self.entry['ledgerPath']=str(self.root/'missing.sqlite')
        with self.assertRaises((ValueError,FileNotFoundError)):self.load()
        self.assertFalse((self.root/'missing.sqlite').exists())
    def test_mismatched_service_role_or_workspace_rejected(self):
        self.bindings['binding']['roleAgentId']='other'
        with self.assertRaises(ValueError):self.load()
        self.bindings['binding']['roleAgentId']='parent'
        other=self.root/'other';other.mkdir();self.projects['project']['workspace']=str(other)
        with self.assertRaises(ValueError):self.load()
        self.assertFalse(self.calls)
    def test_shared_fixed_bridge_across_projects_rejected(self):
        other={**self.entry,'projectId':'second','roleBindingId':'second'}
        self.projects['second']={'workspace':str(self.workspace)};self.bindings['second']={'projectId':'second','roleAgentId':'parent'}
        with self.assertRaises(ValueError):self.load([self.entry,other])
        self.assertFalse(self.calls)
    def test_path_alias_and_untrusted_origin_rejected(self):
        alias=self.root/'alias';alias.symlink_to(self.workspace,target_is_directory=True);self.entry['workspace']=str(alias)
        with self.assertRaises(ValueError):self.load()
        self.entry['workspace']=str(self.workspace);self.entry['baseURL']='http://user:secret@qwen.test/path'
        with self.assertRaises(ValueError):self.load()
        self.assertFalse(self.calls)
    def test_same_host_role_across_projects_rejected_before_login(self):
        other_workspace=self.root/'second'; other_workspace.mkdir()
        other={**self.entry,'projectId':'second','roleBindingId':'second','workspace':str(other_workspace),
               'baseURL':'http://second.test','installationId':'second-install'}
        self.projects['second']={'workspace':str(other_workspace)}
        self.bindings['second']={'projectId':'second','roleAgentId':'parent'}
        with self.assertRaisesRegex(ValueError,'cannot share memory authority'):self.load([self.entry,other])
        self.assertFalse(self.calls)
        del self.bindings['second']
        self.bindings['alias']={'projectId':'project','roleAgentId':'parent'}
        result=self.load();self.addCleanup(result[('project','binding')].close)
        self.assertIn(('project','binding'), result)
    def test_failed_login_cleans_all_clients_and_never_retries(self):
        other={**self.entry,'projectId':'second','roleBindingId':'second','roleAgentId':'other-parent','baseURL':'http://second.test','installationId':'second-install'}
        self.projects['second']={'workspace':str(self.workspace)};self.bindings['second']={'projectId':'second','roleAgentId':'other-parent'}
        original_handler = self.handler
        def handler(request):
            if request.url.host=='second.test':
                self.calls.append(request)
                return httpx.Response(401,json={'detail':'private-secret'})
            return original_handler(request)
        self.handler=handler
        with self.assertRaises(ValueError) as caught:self.load([self.entry,other])
        self.assertNotIn('private-secret',str(caught.exception));self.assertTrue(all(c.is_closed for c in self.clients))
        self.assertEqual(len(self.calls),3)
    def test_timeout_or_boolean_timeout_is_not_accepted(self):
        self.entry['requestTimeoutSeconds']=True
        with self.assertRaises(ValueError):self.load()
        self.assertFalse(self.calls)
    def test_all_entries_validate_before_any_login(self):
        invalid={**self.entry,'roleBindingId':'missing'}
        with self.assertRaises(ValueError):self.load([self.entry,invalid])
        self.assertFalse(self.clients)
    def test_login_timeout_closes_client_without_retry(self):
        def handler(request):
            self.calls.append(request)
            raise httpx.ReadTimeout('private-sensitive-detail',request=request)
        self.handler=handler
        with self.assertRaises(ValueError) as caught:self.load()
        self.assertEqual(len(self.calls),1)
        self.assertTrue(self.clients[0].is_closed)
        self.assertNotIn('private-sensitive-detail',str(caught.exception))
    def test_same_installation_cannot_alias_other_project_via_another_url(self):
        other={**self.entry,'projectId':'second','roleBindingId':'second','baseURL':'http://alias.test'}
        self.projects['second']={'workspace':str(self.workspace)};self.bindings['second']={'projectId':'second','roleAgentId':'parent'}
        with self.assertRaises(ValueError):self.load([self.entry,other])
        self.assertFalse(self.calls)

    def test_same_bridge_accepts_another_project_for_a_different_role(self):
        other={**self.entry,'projectId':'second','roleBindingId':'second','roleAgentId':'reviewer'}
        self.projects['second']={'workspace':str(self.workspace)}
        self.bindings['second']={'projectId':'second','roleAgentId':'reviewer'}
        original=self.handler
        def handler(request):
            response=original(request)
            if request.url.path.endswith('/execution-binding'):
                body=response.json(); body['managedRoleIds']=['parent','reviewer']
                return httpx.Response(200, json=body)
            return response
        self.handler=handler
        result=self.load([self.entry, other])
        self.addCleanup(lambda: [gateway.close() for gateway in result.values()])
        self.assertEqual(set(result), {('project','binding'), ('second','second')})
        self.assertEqual(result[('second','second')].role_agent_id, 'reviewer')

    def test_same_bridge_rejects_a_different_workspace(self):
        other_workspace=self.root/'other-workspace'; other_workspace.mkdir()
        other={**self.entry,'projectId':'second','roleBindingId':'second','roleAgentId':'reviewer','workspace':str(other_workspace)}
        self.projects['second']={'workspace':str(other_workspace)}
        self.bindings['second']={'projectId':'second','roleAgentId':'reviewer'}
        with self.assertRaisesRegex(ValueError, 'different workspaces or installations'):
            self.load([self.entry, other])
        self.assertFalse(self.calls)
    def test_short_service_secret_rejected_before_login(self):
        self.token.write_text('short')
        with self.assertRaises(ValueError):self.load()
        self.assertFalse(self.calls)
    def test_empty_registry_permitted_without_network(self):
        self.assertEqual(self.load([]),{});self.assertFalse(self.calls)


if __name__=='__main__':unittest.main()
