"""Memory authority deployment validates locally before authenticated HTTP discovery."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import httpx

from agent_line.memory import MemoryCommitStore
from agent_line.memory_review import MemoryReviewLedger
from agent_line.memory_runtime import load_memory_api


class MemoryRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.review = self.root / 'review.sqlite'; MemoryReviewLedger.initialize(self.review)
        self.commit = self.root / 'commit.sqlite'; MemoryCommitStore.initialize(self.commit)
        self.login = self.root / 'login.json'; self.write(self.login, {'username': 'probe', 'password': 'not-a-real-password'})
        self.path = self.root / 'memory.json'
        self.projects = {'project': {'workspace': str(self.root)}}
        self.bindings = {'role-binding': {'projectId': 'project', 'roleAgentId': 'parent', 'enabled': True}}
        self.config = {'schemaVersion': 1, 'reviewLedgerPath': str(self.review),
            'credentials': [{'token': 'h' * 32, 'subject': 'human', 'projectScopes': ['project'],
                             'capabilities': ['review_memory'], 'expiresAt': 4102444800}],
            'bindings': {'memory': {'projectId': 'project', 'roleBindingId': 'role-binding',
                'roleAgentId': 'parent', 'baseURL': 'http://qwen.invalid:3190',
                'hostCredentialsFile': str(self.login), 'commitLedgerPath': str(self.commit)}}}
        self.clients = []; self.calls = []; self.tasks = Mock()
        self.real_client = httpx.Client
        self.handler = self.good_response

    def write(self, path, body):
        path.write_text(json.dumps(body)); path.chmod(0o600)

    def good_response(self, request):
        self.calls.append((request.method, request.url.path))
        if request.url.path == '/api/auth/login': return httpx.Response(200, json={'token': 'host-token'})
        self.assertEqual(request.headers['Authorization'], 'Bearer host-token')
        return httpx.Response(200, json={'agents': [{'id': 'parent', 'backend': 'qwenpaw', 'enabled': True}]})

    def factory(self, **kwargs):
        self.assertFalse(kwargs['follow_redirects']); self.assertFalse(kwargs['trust_env'])
        client = self.real_client(transport=httpx.MockTransport(self.handler), **kwargs)
        self.clients.append(client); return client

    def load(self, service_tokens=('s' * 32,)):
        self.write(self.path, self.config)
        with patch('agent_line.memory_runtime.httpx.Client', side_effect=self.factory):
            return load_memory_api(self.path, projects=self.projects, bindings=self.bindings,
                                   service_tokens=service_tokens, tasks=self.tasks)

    def test_bound_role_and_independent_authority_and_transport_ownership(self):
        api = self.load()
        principal = api.access.authenticate('Bearer ' + 'h' * 32)
        self.assertEqual(principal.capabilities, frozenset({'approve', 'review_memory'}))
        self.assertEqual(api.coordinators['memory'].role, 'parent')
        self.assertEqual(api.coordinators['memory'].client.headers['X-Agent-Id'], 'parent')
        self.assertNotIn('login', api.bindings['memory'])
        self.assertEqual(self.calls, [('POST', '/api/auth/login'), ('GET', '/api/agents')])
        api.close(); self.assertTrue(self.clients[0].is_closed)

    def test_all_local_bindings_are_checked_before_http(self):
        self.config['bindings']['bad'] = {**self.config['bindings']['memory'], 'roleBindingId': 'missing'}
        with self.assertRaises(ValueError): self.load()
        self.assertEqual(self.clients, [])

    def test_schema_tokens_scopes_capabilities_expiration_are_strict(self):
        original = copy.deepcopy(self.config)
        mutations = [lambda c:c.update(schemaVersion=True), lambda c:c.update(extra=True),
            lambda c:c['credentials'][0].update(token='short'),
            lambda c:c['credentials'][0].update(token='s' * 32),
            lambda c:c['credentials'][0].update(capabilities=['approve']),
            lambda c:c['credentials'][0].update(projectScopes=['unknown']),
            lambda c:c['credentials'][0].update(expiresAt=True),
            lambda c:c['credentials'][0].update(expiresAt=float('inf')),
            lambda c:c['credentials'].append(dict(c['credentials'][0]))]
        for change in mutations:
            self.config = copy.deepcopy(original); change(self.config)
            with self.subTest(config=self.config), self.assertRaises(ValueError): self.load()
            self.assertEqual(self.clients, [])

    def test_disabled_or_mismatched_role_and_cross_project_host_role_are_refused(self):
        self.bindings['role-binding']['enabled'] = False
        with self.assertRaises(ValueError): self.load()
        self.bindings['role-binding']['enabled'] = True
        self.projects['other'] = {'workspace': str(self.root)}
        self.bindings['other'] = {'projectId': 'other', 'roleAgentId': 'parent'}
        self.config['bindings']['other'] = {**self.config['bindings']['memory'], 'projectId': 'other', 'roleBindingId': 'other'}
        with self.assertRaises(ValueError): self.load()
        self.assertEqual(self.clients, [])

    def test_missing_or_nonprivate_existing_ledgers_do_not_initialize(self):
        self.commit.unlink()
        with self.assertRaises(FileNotFoundError): self.load()
        self.assertFalse(self.commit.exists()); self.assertEqual(self.clients, [])
        MemoryCommitStore.initialize(self.commit); self.commit.chmod(0o644)
        with self.assertRaises(ValueError): self.load()
        self.assertEqual(self.clients, [])

    def test_remote_role_failure_closes_client_and_redacts_exception(self):
        self.handler = lambda request: httpx.Response(200, json={'token': 'host-token'} if request.method == 'POST'
            else {'agents': [{'id': 'parent', 'backend': 'codex'}]})
        with self.assertRaisesRegex(ValueError, 'no memory written'): self.load()
        self.assertTrue(all(client.is_closed for client in self.clients))

    def test_later_login_failure_closes_all_created_transports(self):
        self.bindings['second-role'] = {'projectId': 'project', 'roleAgentId': 'second-parent'}
        self.config['bindings']['second'] = {**self.config['bindings']['memory'],
            'roleBindingId': 'second-role', 'roleAgentId': 'second-parent'}
        count = 0
        def handler(request):
            nonlocal count
            if request.method == 'POST':
                count += 1
                if count == 2: raise httpx.ConnectError('secret diagnostic', request=request)
            return self.good_response(request)
        self.handler = handler
        with self.assertRaisesRegex(ValueError, '^Memory host authentication or role verification failed; no memory written$'):
            self.load()
        self.assertEqual(len(self.clients), 2)
        self.assertTrue(all(client.is_closed for client in self.clients))

    def test_actual_memory_api_constructor_is_compatible(self):
        api = self.load()
        self.assertEqual(type(api).__name__, 'MemoryAPI')
        self.assertEqual(api.by_role, {'parent': 'memory'})
        api.close(); self.assertTrue(self.clients[0].is_closed)

    def test_duplicate_role_and_cross_project_shared_ledger_fail_before_http(self):
        self.config['bindings']['duplicate'] = dict(self.config['bindings']['memory'])
        with self.assertRaises(ValueError): self.load()
        del self.config['bindings']['duplicate']
        self.projects['other'] = {'workspace': str(self.root)}
        self.bindings['other'] = {'projectId': 'other', 'roleAgentId': 'different-role'}
        self.config['bindings']['other'] = {**self.config['bindings']['memory'],
            'projectId': 'other', 'roleBindingId': 'other', 'roleAgentId': 'different-role'}
        with self.assertRaises(ValueError): self.load()
        self.assertEqual(self.clients, [])

    def test_load_app_mounts_memory_auth_and_lifespan_closes_owned_transport(self):
        from fastapi.testclient import TestClient
        from agent_line.runtime import load_app
        from agent_line.store import TaskStore
        TaskStore.initialize(self.root / 'tasks.sqlite')
        self.write(self.path, self.config)
        service = self.root / 'service.json'
        self.write(service, {'schemaVersion': 1, 'database': 'tasks.sqlite',
            'tokens': {'s' * 32: ['project']}, 'projects': self.projects,
            'bindings': self.bindings, 'memoryFile': 'memory.json'})
        with patch('agent_line.memory_runtime.httpx.Client', side_effect=self.factory):
            app = load_app(service)
        self.assertEqual(len(self.clients), 1)
        with TestClient(app) as browser:
            route = '/extension/v1/memories'
            self.assertEqual(browser.get(route).status_code, 401)
            self.assertEqual(browser.get(route, headers={'Authorization': 'Bearer ' + 's' * 32}).status_code, 401)
            response = browser.get(route, headers={'Authorization': 'Bearer ' + 'h' * 32})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json(), {'memories': [], 'nextCursor': None})
            self.assertFalse(self.clients[0].is_closed)
        self.assertTrue(self.clients[0].is_closed)
        self.assertEqual(self.calls, [('POST', '/api/auth/login'), ('GET', '/api/agents')])

    def test_different_project_roles_and_ledgers_can_share_host(self):
        other_workspace = self.root / 'other-workspace'; other_workspace.mkdir()
        other_ledger = self.root / 'other-commit.sqlite'; MemoryCommitStore.initialize(other_ledger)
        self.projects['other'] = {'workspace': str(other_workspace)}
        self.bindings['other-role'] = {'projectId': 'other', 'roleAgentId': 'other-parent', 'enabled': True}
        self.config['bindings']['other-memory'] = {**self.config['bindings']['memory'],
            'projectId': 'other', 'roleBindingId': 'other-role', 'roleAgentId': 'other-parent',
            'commitLedgerPath': str(other_ledger)}
        self.config['credentials'][0]['projectScopes'].append('other')
        def response(request):
            self.calls.append((request.method, request.url.path))
            if request.method == 'POST': return httpx.Response(200, json={'token': 'host-token'})
            return httpx.Response(200, json={'agents': [
                {'id': 'parent', 'backend': 'qwenpaw', 'enabled': True},
                {'id': 'other-parent', 'backend': 'qwenpaw', 'enabled': True}]})
        self.handler = response
        api = self.load()
        self.assertEqual(set(api.coordinators), {'memory', 'other-memory'})
        self.assertEqual(api.coordinators['other-memory'].role, 'other-parent')
        self.assertNotEqual(api.coordinators['memory'].store.path, api.coordinators['other-memory'].store.path)
        api.close()
        self.assertTrue(all(client.is_closed for client in self.clients))

    def enable_deletion(self, token=None):
        token = 'd' * 48 if token is None else token
        path = self.root / 'deletion-token'
        path.write_text(token); path.chmod(0o600)
        self.config['bindings']['memory']['deletionServiceTokenFile'] = str(path)
        self.config['credentials'][0]['capabilities'] = ['review_memory', 'delete_memory']
        return path

    def test_deletion_is_opt_in_and_grant_alone_does_not_enable_native(self):
        self.config['credentials'][0]['capabilities'].append('delete_memory')
        api = self.load(); self.addCleanup(api.close)
        self.assertEqual(api.deletions, {})
        self.assertIn('delete_memory', api.access.authenticate('Bearer ' + 'h'*32).capabilities)
        self.assertEqual(self.calls, [('POST', '/api/auth/login'), ('GET', '/api/agents')])

    def test_enabled_native_transport_fixed_identity_headers_and_payload(self):
        self.enable_deletion()
        digest = 'b' * 64
        def handler(request):
            if '/api/agent-line/memory/' not in request.url.path:
                return self.good_response(request)
            self.assertEqual(request.url.path, '/api/agent-line/memory/parent/task1/delete')
            self.assertEqual(request.headers['Authorization'], 'Bearer host-token')
            self.assertEqual(request.headers['X-Agent-Line-Memory-Token'], 'd' * 48)
            self.assertEqual(json.loads(request.content), {'expectedContentSha256':digest,'decisionId':'decision1','actor':'human'})
            self.calls.append((request.method, request.url.path))
            return httpx.Response(200,json={'roleAgentId':'parent','taskId':'task1','contentHash':digest,
                'status':'deleting','fileAbsent':True,'indexSourceAbsent':False})
        self.handler = handler
        api = self.load(); self.addCleanup(api.close)
        self.assertIs(api.deletions['memory'].store, api.coordinators['memory'].store)
        self.assertIs(api.deletions['memory'].client, api.coordinators['memory'].client)
        self.assertEqual(self.calls, [('POST','/api/auth/login'),('GET','/api/agents')])
        result = api.deletions['memory'].client.delete_checked('task1',digest,'decision1','human')
        self.assertEqual(result['status'],'deleting')
        self.assertEqual(len(self.calls),3)

    def test_deletion_tokens_paths_permissions_and_capabilities_fail_before_network(self):
        for token in ('short','h'*32,'s'*32,'中'*32,'d'*31+' ','d'*4097,'d'*32+' x'):
            with self.subTest(token_length=len(token)):
                self.enable_deletion(token)
                with self.assertRaises(ValueError): self.load()
                self.assertEqual(self.clients,[])
        path=self.enable_deletion();path.chmod(0o644)
        with self.assertRaises(ValueError):self.load()
        path.chmod(0o600)
        link=self.root/'link';link.symlink_to(path)
        self.config['bindings']['memory']['deletionServiceTokenFile']=str(link)
        with self.assertRaises(ValueError):self.load()
        self.enable_deletion()
        for caps in (['delete_memory'],['review_memory','review_memory'],['review_memory','delete_memory','other'],['review_memory',{}]):
            self.config['credentials'][0]['capabilities']=caps
            with self.assertRaises(ValueError):self.load()
        self.assertEqual(self.clients,[])

    def test_schema_one_deletion_requires_explicit_upgrade_without_network(self):
        import sqlite3
        self.enable_deletion()
        with sqlite3.connect(self.commit) as db:
            db.execute('UPDATE installation SET schema_version=1')
            db.execute('DROP TABLE memory_deletions')
        with self.assertRaisesRegex(ValueError,'explicit.*upgrade'):self.load()
        self.assertEqual(self.clients,[])
        self.assertEqual(MemoryCommitStore(self.commit).schema_version,1)

    def test_native_token_must_not_equal_host_session_token_and_closes_client(self):
        self.enable_deletion()
        self.handler=lambda request:httpx.Response(200,json={'token':'d'*48})
        with self.assertRaisesRegex(ValueError,'no memory written'):self.load()
        self.assertTrue(all(client.is_closed for client in self.clients))
