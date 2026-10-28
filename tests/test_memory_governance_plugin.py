"""Official memory boundary, with mock native actions; no real service or deletion."""
import asyncio
import hashlib
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

PATH = Path(__file__).resolve().parents[1] / 'plugins/agent-line/memory_governance.py'
spec = importlib.util.spec_from_file_location('memory_governance_test_module', PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class GovernanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        secret = Path(self.temp.name) / 'token'
        secret.write_text('m' * 48)
        secret.chmod(0o600)
        state_file = Path(self.temp.name) / 'state.sqlite'
        module.initialize_state(state_file)
        self.config = {'managed_role_ids': ['role1'], 'service_token_file': str(secret), 'state_file': str(state_file)}
        self.bridge = module.MemoryGovernance(self.config)
        self.content = 'Approved content\n'
        self.digest = hashlib.sha256(self.content.strip().encode()).hexdigest()
        self.actions = []
        self.manager = NS(list_actions=AsyncMock(return_value={name: {} for name in ['read', 'delete', 'search', 'graph_snapshot']}), run_action=self.action)
        self.workspace = NS(memory_manager=self.manager)
        self.lookup = AsyncMock(return_value=self.workspace)
        self.full_read = AsyncMock(side_effect=self.read)
        self.addCleanup(patch.stopall)
        patch.object(module, 'get_agent_for_request', self.lookup).start()
        patch.object(module, 'read_memory_file', self.full_read).start()
        patch.object(module, 'load_agent_config', return_value=NS(running=NS(reme_light_memory_config=NS(digest_dir='digest')))).start()
        app = FastAPI()
        def identity(request: Request):
            if request.headers.get('authorization') != 'Bearer host':
                raise HTTPException(401)
        app.include_router(self.bridge.router(), dependencies=[Depends(identity)])
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.headers = {'Authorization': 'Bearer host', 'X-Agent-Line-Memory-Token': 'm' * 48, 'X-Agent-Id': 'attacker-role'}
        self.url = '/memory/role1/task1'

    async def read(self, path, request, section):
        self.assertEqual(path, 'agent-line-task-task1.md')
        self.assertEqual(section, 'digest')
        self.assertEqual(request.state.agent_id, 'role1')
        self.assertIsNone(request.headers.get('x-agent-id'))
        if self.content is None:
            raise HTTPException(404)
        return NS(content=self.content)

    async def action(self, name, **kwargs):
        self.actions.append((name, kwargs))
        if name == 'read':
            return NS(success=True, answer=self.content)
        if name == 'delete':
            self.content = None
            return NS(success=True, answer='Deleted', metadata={'deleted': True})
        return NS(success=True, answer=[])

    def post(self, **extra):
        body = {'expectedContentSha256': self.digest, 'decisionId': 'decision1', 'actor': 'reviewer'}
        body.update(extra)
        return self.client.post(self.url + '/delete', headers=self.headers, json=body)

    def test_delete_uses_fixed_role_path_and_never_claims_index_erasure(self):
        response = self.post()
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['contentHash'], self.digest)
        self.assertEqual(data['status'], 'deleting')
        self.assertTrue(data['fileAbsent'])
        self.assertFalse(data['indexSourceAbsent'])
        self.assertEqual(data['reason'], 'index_sync_pending')
        self.assertEqual([name for name, _ in self.actions], ['read', 'delete', 'search', 'graph_snapshot'])
        self.assertEqual(self.actions[1][1], {'path': 'digest/agent-line-task-task1.md'})
        self.assertEqual(self.lookup.call_args.kwargs['agent_id'], 'role1')

    def _index_manager(self, *, nodes=(), chunks=(), vocab=None, doc_ids=None, deleted=None, postings=None, live=None):
        class _Node:
            def __init__(self, path):
                self.path = path
        class _Chunk:
            def __init__(self, chunk_id, path):
                self.id = chunk_id
                self.path = path
        class _Store:
            def __init__(self, nodes, chunks):
                self._nodes = nodes
                self.file_chunks = {chunk.id: chunk for chunk in chunks}
            async def get_nodes(self, paths):
                return [node for node in self._nodes if node.path in paths]
        class _Index:
            def __init__(self):
                self.vocab = {} if vocab is None else vocab
                self._doc_ids = [] if doc_ids is None else doc_ids
                self._deleted = [] if deleted is None else deleted
                self._posting_doc_idxs = {} if postings is None else postings
                self._doc_id_to_idx = {} if live is None else live
        store = _Store([_Node(path) for path in nodes], [_Chunk(cid, path) for cid, path in chunks])
        index = _Index()
        self.manager._reme = type('ReMe', (), {'context': type('Ctx', (), {'components': {
            'file_store': {'default': store}, 'keyword_index': {'default': index}}})()})()
        return store, index

    def test_exact_live_index_absence_confirms_source_removed(self):
        self.content = None
        self._index_manager(
            chunks=(('other', 'digest/other.md'),),
            vocab={'task1': 7}, doc_ids=['old', 'other'], deleted=[True, False],
            postings={7: [0]}, live={'other': 1})
        data = self.post().json()
        self.assertEqual(data['status'], 'deleted')
        self.assertTrue(data['fileAbsent'])
        self.assertTrue(data['indexSourceAbsent'])
        self.assertEqual(data['reason'], 'index_source_absent')

    def test_live_keyword_posting_keeps_deletion_pending(self):
        self.content = None
        self._index_manager(
            chunks=(('mine', 'digest/agent-line-task-task1.md'), ('other', 'digest/other.md')),
            vocab={'task1': 7}, doc_ids=['mine', 'other'], deleted=[False, False],
            postings={7: [0]}, live={'mine': 0, 'other': 1})
        data = self.post().json()
        self.assertEqual(data['status'], 'deleting')
        self.assertFalse(data['indexSourceAbsent'])

    def test_supersession_citation_is_not_the_deleted_source(self):
        self.content = None
        self._index_manager(
            chunks=(('other', 'digest/other.md'),),
            vocab={'task1': 7}, doc_ids=['other'], deleted=[False],
            postings={7: [0]}, live={'other': 0})
        data = self.post().json()
        self.assertEqual(data['status'], 'deleted')
        self.assertTrue(data['indexSourceAbsent'])
        self.assertEqual(data['reason'], 'index_source_absent')

    def test_unreadable_index_never_claims_absence(self):
        self.content = None
        self.manager._reme = type('ReMe', (), {'context': type('Ctx', (), {'components': {
            'file_store': {}}})()})()
        data = self.post().json()
        self.assertEqual(data['status'], 'deleting')
        self.assertFalse(data['indexSourceAbsent'])
        self.assertEqual(data['reason'], 'index_sync_pending')

    def test_hyphenated_task_uses_long_token_not_the_whole_string(self):
        task = 'task-' + ('ab' * 16)
        self._index_manager(
            chunks=(('other', 'digest/other.md'),),
            vocab={task[5:]: 3}, doc_ids=['stale', 'other'], deleted=[True, False],
            postings={3: [0]}, live={'other': 1})
        self.assertTrue(asyncio.run(self.bridge._live_index_source_absent(
            self.manager, 'digest/agent-line-task-' + task + '.md', task)))
        deleted_path = 'digest/agent-line-task-' + task + '.md'
        self._index_manager(
            chunks=(('mine', deleted_path), ('other', 'digest/other.md')),
            vocab={task[5:]: 3}, doc_ids=['mine', 'other'], deleted=[False, False],
            postings={3: [0]}, live={'mine': 0, 'other': 1})
        self.assertFalse(asyncio.run(self.bridge._live_index_source_absent(
            self.manager, deleted_path, task)))
        self._index_manager(
            chunks=(('other', 'digest/other.md'),),
            vocab={task[5:]: 3}, doc_ids=['other'], deleted=[False],
            postings={3: [0]}, live={'other': 0})
        self.assertTrue(asyncio.run(self.bridge._live_index_source_absent(
            self.manager, deleted_path, task)))

    def test_get_is_read_only_and_missing_does_not_repeat_delete(self):
        response = self.client.get(self.url + '/deletion', headers=self.headers, params={'expectedContentSha256': self.digest})
        self.assertEqual(response.json()['reason'], 'file_present')
        self.assertNotIn('delete', [x[0] for x in self.actions])
        self.content = None
        self.assertEqual(self.post().json()['status'], 'deleting')
        self.assertNotIn('delete', [x[0] for x in self.actions])

    def test_wrong_hash_and_truncated_read_never_delete(self):
        self.assertEqual(self.post(expectedContentSha256='0' * 64).json()['reason'], 'content_hash_mismatch')
        self.assertEqual(self.actions, [])
        self.manager.run_action = AsyncMock(return_value=NS(success=True, answer='Approved'))
        self.assertEqual(self.post().json()['reason'], 'official_read_not_complete_or_mismatch')
        self.assertEqual(self.manager.run_action.await_count, 1)

    def test_content_change_before_delete_is_rejected(self):
        self.full_read.side_effect = [NS(content=self.content), NS(content='changed')]
        self.assertEqual(self.post().json()['reason'], 'content_changed_before_delete')
        self.assertNotIn('delete', [x[0] for x in self.actions])

    def test_auth_schema_and_scope_fail_closed(self):
        self.assertEqual(self.client.post(self.url+'/delete',json={}).status_code,401)
        self.assertEqual(self.client.get(self.url+'/deletion',headers={'Authorization':'Bearer host'},params={'expectedContentSha256':self.digest}).status_code,401)
        self.assertEqual(self.post(path='elsewhere').status_code,422)
        self.assertEqual(self.post(actor=' ').status_code,422)
        self.assertEqual(self.post(decisionId='../bad').status_code,422)
        self.url='/memory/other/task1'
        self.assertEqual(self.post().status_code,404)
        self.assertEqual(self.lookup.await_count,0)

    def test_permission_failure_is_not_absence(self):
        self.full_read.side_effect = HTTPException(500)
        result=self.post().json()
        self.assertFalse(result['fileAbsent'])
        self.assertEqual(result['status'],'needs_review')
        self.assertEqual(self.actions,[])

    def test_private_token_required(self):
        Path(self.config['service_token_file']).chmod(0o644)
        with self.assertRaises(ValueError):module.MemoryGovernance(self.config)

    def test_custom_digest_root_refused(self):
        with patch.object(module,'load_agent_config',return_value=NS(running=NS(reme_light_memory_config=NS(digest_dir='digest/wiki')))):
            self.assertEqual(self.post().json()['reason'],'fixed_digest_root_mismatch')
        self.assertEqual(self.full_read.await_count,0)

    def test_failed_native_delete_cannot_claim_absence(self):
        original = self.action
        async def action(name, **kwargs):
            if name == 'delete':
                return NS(success=False, answer='permission denied')
            return await original(name, **kwargs)
        self.manager.run_action = action
        result = self.post().json()
        self.assertEqual(result['status'], 'needs_review')
        self.assertFalse(result['fileAbsent'])
        self.assertEqual(result['reason'], 'file_absence_not_confirmed')

    def test_bad_task_and_service_token_do_not_touch_native(self):
        self.url = '/memory/role1/bad..task'
        self.assertEqual(self.post().status_code, 422)
        self.headers['X-Agent-Line-Memory-Token'] = 'wrong'
        self.assertEqual(self.post().status_code, 401)
        self.assertEqual(self.lookup.await_count, 0)

    def test_absent_with_search_or_graph_unavailable_stays_pending(self):
        self.content = None
        self.manager.run_action = AsyncMock(return_value=NS(success=False, answer='unavailable'))
        result = self.post().json()
        self.assertEqual(result['status'], 'deleting')
        self.assertFalse(result['indexSourceAbsent'])
        self.assertFalse(result['searchVerified'])
        self.assertFalse(result['graphVerified'])

    def test_delete_block_commits_before_action_and_survives_reload(self):
        import asyncio
        original=self.action
        async def action(name, **kwargs):
            if name=='delete':
                reloaded=module.MemoryGovernance(self.config)
                self.assertTrue(await reloaded.is_blocked('role1'))
                self.assertFalse(await reloaded.is_blocked('other-role'))
            return await original(name,**kwargs)
        self.manager.run_action=action
        result=self.post().json()
        self.assertTrue(result['roleBlocked'])
        self.assertTrue(asyncio.run(module.MemoryGovernance(self.config).is_blocked('role1')))
        self.assertEqual(self.post(expectedContentSha256='0'*64).status_code,409)
        self.assertTrue(self.post().json()['roleBlocked'])
        self.assertEqual(sum(name=='delete' for name,_ in self.actions),1)

    def test_unknown_delete_retains_block_and_never_retries_action(self):
        import asyncio
        original=self.action
        async def action(name, **kwargs):
            if name=='delete':raise RuntimeError('native connection lost')
            return await original(name,**kwargs)
        self.manager.run_action=action
        response=self.post()
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['reason'],'native_delete_unknown')
        self.assertNotIn('native connection lost',response.text)
        self.assertTrue(asyncio.run(module.MemoryGovernance(self.config).is_blocked('role1')))
        self.assertEqual(self.post().json()['reason'],'prior_delete_intent_pending')

    def test_read_error_and_conflict_do_not_block(self):
        import asyncio
        self.assertEqual(self.post(expectedContentSha256='0'*64).json()['reason'],'content_hash_mismatch')
        self.full_read.side_effect=HTTPException(500)
        self.post()
        self.assertFalse(asyncio.run(self.bridge.is_blocked('role1')))

    def test_state_is_explicit_private_and_never_recreated(self):
        path=Path(self.config['state_file'])
        with self.assertRaises(FileExistsError):module.initialize_state(path)
        path.chmod(0o644)
        with self.assertRaises(ValueError):module.MemoryGovernance(self.config)
        path.unlink()
        with self.assertRaises(FileNotFoundError):module.MemoryGovernance(self.config)
        self.assertFalse(path.exists())

    def test_tool_and_direct_delegation_guards_preserve_status_queries(self):
        import asyncio
        from agentscope.message import ToolResultState
        spec=importlib.util.spec_from_file_location('governance_plugin_guard_test',PATH.with_name('plugin.py'))
        plugin=importlib.util.module_from_spec(spec);spec.loader.exec_module(plugin)
        self.bridge.block('role1','task1',self.digest)
        async def check():
            invoked=[]
            async def downstream():
                invoked.append(True)
                yield 'ok'
            for role in ('role1',None):
                guard=plugin.ManagedRoleToolGuard(self.bridge,role)
                for name in ('memory_search','agent_line_delegate'):
                    with self.assertRaises(PermissionError):
                        async for _ in guard.on_acting(None,{'tool_call':{'name':name}},downstream):pass
                self.assertEqual(invoked,[])
            for role,name in [('role1','agent_line_execution_status'),('other-role','memory_search')]:
                guard=plugin.ManagedRoleToolGuard(self.bridge,role)
                self.assertEqual([x async for x in guard.on_acting(None,{'tool_call':{'name':name}},downstream)],['ok'])
            contract=plugin.DelegationContract({'managed_role_ids':['role1','other-role'],'codex_target_agent_id':'codex'})
            contract.governance=self.bridge
            contract.bridge=NS(submit_from_parent=AsyncMock(return_value={'status':'accepted'}))
            with patch.object(plugin,'get_current_agent_id',return_value='role1'),patch.object(plugin,'get_current_session_id',return_value='session'):
                self.assertEqual((await contract.submit('work')).state,ToolResultState.DENIED)
            contract.bridge.submit_from_parent.assert_not_awaited()
            with patch.object(plugin,'get_current_agent_id',return_value='other-role'),patch.object(plugin,'get_current_session_id',return_value='session'):
                self.assertEqual((await contract.submit('work')).state,ToolResultState.SUCCESS)
        asyncio.run(check())

    def test_missing_file_post_blocks_but_get_remains_read_only(self):
        import asyncio
        self.content=None
        response=self.client.get(self.url+'/deletion',headers=self.headers,params={'expectedContentSha256':self.digest})
        self.assertFalse(response.json()['roleBlocked'])
        self.assertFalse(asyncio.run(self.bridge.is_blocked('role1')))
        result=self.post().json()
        self.assertTrue(result['fileAbsent'])
        self.assertTrue(result['roleBlocked'])
        self.assertEqual(result['status'],'deleting')
        self.assertTrue(asyncio.run(module.MemoryGovernance(self.config).is_blocked('role1')))
        self.assertNotIn('delete',[name for name,_ in self.actions])
        self.assertEqual(self.post(expectedContentSha256='0'*64).status_code,409)

    def test_reasoning_gate_precedes_native_model_chain_and_provider_gate(self):
        import asyncio
        from agentscope.agent import Agent
        spec=importlib.util.spec_from_file_location('governance_model_guard_test',PATH.with_name('plugin.py'))
        plugin=importlib.util.module_from_spec(spec);spec.loader.exec_module(plugin)
        self.bridge.block('role1','task1',self.digest)
        async def check():
            guard=plugin.ManagedRoleToolGuard(self.bridge,'role1')
            native_calls=[]
            async def reasoning_impl(**kwargs):
                native_calls.append('native memory/model chain')
                yield 'unsafe'
            # Use the real AgentScope reasoning lifecycle dispatcher, not a
            # fabricated invocation order. Native injection is inside impl.
            agent=NS(_reasoning_middlewares=[guard],_reasoning_impl=reasoning_impl)
            with self.assertRaises(PermissionError):
                async for _ in Agent._reasoning(agent):pass
            self.assertEqual(native_calls,[])
            provider=AsyncMock()
            with self.assertRaises(PermissionError):await guard.on_model_call(agent,{},provider)
            provider.assert_not_awaited()
        asyncio.run(check())

    def test_direct_bridge_submit_uses_persistent_parent_binding(self):
        import asyncio
        from unittest.mock import Mock
        spec=importlib.util.spec_from_file_location('governance_execution_bridge_test',PATH.with_name('execution_bridge.py'))
        bridge_module=importlib.util.module_from_spec(spec);spec.loader.exec_module(bridge_module)
        ledger_path=Path(self.temp.name)/'receipts.sqlite'
        bridge_module.ReceiptLedger.initialize(ledger_path)
        ledger=bridge_module.ReceiptLedger(ledger_path)
        ledger.bind('task1','role1','agent-line-task1', 'qwenpaw-role-v1:' + 'a'*64)
        bridge=bridge_module.ExecutionBridge.__new__(bridge_module.ExecutionBridge)
        bridge.governance=self.bridge;bridge.ledger=ledger;bridge.roles=frozenset({'role1'})
        bridge.target_config=Mock()
        bridge.target='fixed';bridge.workspace=Path(self.temp.name)
        bridge.current_revision=lambda role:'qwenpaw-role-v1:'+'a'*64
        self.bridge.block('role1','task1',self.digest)
        async def check():
            for task in ('task1','unbound'):
                body=bridge_module.ExecutionRequest(taskId=task,roleSessionId='agent-line-'+task,instruction='bounded')
                with self.assertRaises(HTTPException) as exc:await bridge.submit(body,None)
                self.assertEqual(exc.exception.status_code,409)
            with ledger.connection() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0],0)
        asyncio.run(check())

    def test_confirmed_absence_releases_only_that_task_and_revival_reblocks(self):
        import sqlite3
        self.content = None
        self._index_manager(
            chunks=(('other', 'digest/other.md'),),
            vocab={'task1': 7}, doc_ids=['stale', 'other'], deleted=[True, False],
            postings={7: [0]}, live={'other': 1})
        data = self.post().json()
        self.assertEqual(data['status'], 'deleted')
        self.assertFalse(data['roleBlocked'])
        self.assertFalse(asyncio.run(self.bridge.is_blocked('role1')))
        self.assertTrue(self.bridge.forbids_content('role1', 'Approved content'))
        self.bridge.block('role1', 'task2', 'b' * 64)
        self.assertTrue(asyncio.run(self.bridge.is_blocked('role1')))
        with sqlite3.connect(self.config['state_file']) as db:
            db.execute('DELETE FROM blocks WHERE task=?', ('task2',))
        self.assertFalse(asyncio.run(self.bridge.is_blocked('role1')))
        self.content = 'Approved content\n'
        self.actions.clear()
        revived = self.client.get(self.url + '/deletion', headers=self.headers,
                                   params={'expectedContentSha256': self.digest}).json()
        self.assertEqual(revived['reason'], 'deleted_content_revived')
        self.assertEqual(revived['status'], 'deleting')
        self.assertTrue(revived['roleBlocked'])
        self.assertFalse(revived['fileAbsent'])
        self.assertTrue(asyncio.run(self.bridge.is_blocked('role1')))
        self.assertNotIn('delete', [name for name, _ in self.actions])

    def test_returned_deleted_path_reblocks_confirmed_absence(self):
        self.content = None
        self._index_manager(
            chunks=(('other', 'digest/other.md'),),
            vocab={'task1': 7}, doc_ids=['stale', 'other'], deleted=[True, False],
            postings={7: [0]}, live={'other': 1})
        self.assertEqual(self.post().json()['status'], 'deleted')
        self.assertFalse(asyncio.run(self.bridge.is_blocked('role1')))
        self._index_manager(
            chunks=(('mine', 'digest/agent-line-task-task1.md'),),
            vocab={'task1': 7}, doc_ids=['mine'], deleted=[False],
            postings={7: [0]}, live={'mine': 0})
        revived = self.client.get(self.url + '/deletion', headers=self.headers,
                                   params={'expectedContentSha256': self.digest}).json()
        self.assertEqual(revived['reason'], 'index_source_still_present')
        self.assertEqual(revived['status'], 'deleting')
        self.assertFalse(revived['indexSourceAbsent'])
        self.assertTrue(revived['fileAbsent'])
        self.assertTrue(revived['roleBlocked'])
        self.assertTrue(asyncio.run(self.bridge.is_blocked('role1')))
        self.assertNotIn('delete', [name for name, _ in self.actions])

    def test_confirmed_exact_body_leaves_model_call_and_reply_context(self):
        import asyncio
        spec = importlib.util.spec_from_file_location('governance_exact_body_test', PATH.with_name('plugin.py'))
        plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plugin)
        self.content = None
        self._index_manager(chunks=(('other', 'digest/other.md'),), vocab={'task1': 7},
                            doc_ids=['stale', 'other'], deleted=[True, False], postings={7: [0]}, live={'other': 1})
        self.assertEqual(self.post().json()['status'], 'deleted')
        exact = 'Approved content'
        fragment = 'prefix Approved content suffix'
        other = 'unrelated note'

        class Exact:
            def get_text_content(self):
                return exact

        async def check():
            guard = plugin.ManagedRoleToolGuard(self.bridge, 'role1')
            seen = {}

            async def provider(**kwargs):
                seen['messages'] = list(kwargs['messages'])
                return 'called'

            original = [exact, Exact(), fragment, other, '   ', NS(content=[{'text': exact}])]
            result = await guard.on_model_call(None, {'messages': original}, provider)
            self.assertEqual(result, 'called')
            self.assertEqual([item if isinstance(item, str) else type(item).__name__ for item in seen['messages']],
                             [fragment, other, '   '])
            self.assertEqual(len(original), 6)

            agent = NS(state=NS(context=[Exact(), NS(content=fragment), NS(content=[{'text': other}]), exact]))

            async def downstream(**kwargs):
                yield 'replied'

            self.assertEqual([item async for item in guard.on_reply(agent, {}, downstream)], ['replied'])
            self.assertEqual(agent.state.context[0].content, fragment)
            self.assertEqual(agent.state.context[1].content, [{'text': other}])
            self.assertEqual(len(agent.state.context), 2)

            open_guard = plugin.ManagedRoleToolGuard(None, 'role1')
            async def keep(**kwargs):
                seen['open'] = list(kwargs['messages'])
                return 'kept'
            await open_guard.on_model_call(None, {'messages': [exact, other]}, keep)
            self.assertEqual(seen['open'], [exact, other])

        asyncio.run(check())

    def test_version_one_block_stays_unconfirmed_after_migration(self):
        import sqlite3
        legacy = Path(self.temp.name) / 'legacy.sqlite'
        fd = __import__('os').open(legacy, __import__('os').O_CREAT | __import__('os').O_EXCL | __import__('os').O_WRONLY, 0o600)
        __import__('os').close(fd)
        with sqlite3.connect(legacy) as db, db:
            db.execute("CREATE TABLE blocks(role TEXT NOT NULL, task TEXT NOT NULL, content_hash TEXT NOT NULL, intent TEXT NOT NULL CHECK(intent='delete'), PRIMARY KEY(role,task))")
            db.execute("INSERT INTO blocks VALUES('role1','task1',?,'delete')", (self.digest,))
            db.execute('PRAGMA user_version=1')
        legacy_config = dict(self.config, state_file=str(legacy))
        bridge = module.MemoryGovernance(legacy_config)
        self.assertTrue(asyncio.run(bridge.is_blocked('role1')))
        self.assertFalse(bridge.forbids_content('role1', 'Approved content'))
        with sqlite3.connect(legacy) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT confirmed FROM blocks').fetchone()[0], 0)

    def test_real_loader_keeps_guards_when_governance_state_unavailable(self):
        import os
        import json
        import subprocess
        import sys
        code = r'''
import asyncio, importlib, json, sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from qwenpaw.plugins.loader import PluginLoader
from qwenpaw.plugins.architecture import PluginManifest
from agentscope.message import ToolResultState

async def main():
    directory=Path(sys.argv[1]); config=json.loads(sys.argv[2])
    app=FastAPI()
    @app.middleware('http')
    async def identity(request, call_next):
        request.state.user='authenticated-test-user'
        return await call_next(request)
    loader=PluginLoader([directory.parent]);loader.registry.set_plugin_http_app(app)
    manifest=PluginManifest(**json.loads((directory/'plugin.json').read_text()))
    record=await loader.load_plugin(manifest,directory,config=config)
    assert record.enabled
    registration=next(r for r in loader.registry.get_middleware_factories() if r.plugin_id=='agent-line')
    contract=registration.factory.__self__
    assert not contract.governance.available
    assert await contract.governance.is_blocked('role1')
    contract.bridge=NS(submit_from_parent=AsyncMock(),ledger=NS(bound_task=Mock(return_value='existing')),observe=AsyncMock(return_value={'taskId':'existing'}))
    module=sys.modules[contract.__class__.__module__]
    with patch.object(module,'get_current_agent_id',return_value='role1'),patch.object(module,'get_current_session_id',return_value='session'):
        assert (await contract.submit('work')).state==ToolResultState.DENIED
        assert (await contract.execution_status()).state==ToolResultState.SUCCESS
    contract.bridge.submit_from_parent.assert_not_awaited()
    for role in ('role1','memory-only',None):
        guard=registration.factory(NS(agent_id=role),None)
        assert guard is not None
        provider=AsyncMock()
        try: await guard.on_model_call(None,{},provider)
        except PermissionError: pass
        else: raise AssertionError('unavailable governance admitted model call')
        provider.assert_not_awaited()
    with TestClient(app) as client:
        health=client.get('/api/agent-line/health').json()
        assert health['status']=='degraded' and health['scope']=='plugin-registration-only'
        assert health['memoryGovernance']=='unavailable'
        assert client.get('/api/agent-line/memory/role1/task/deletion').status_code==503
        assert client.post('/api/agent-line/memory/role1/task/delete',json={}).status_code==503
asyncio.run(main())
'''
        config={'managed_role_ids':['role1'],'codex_target_agent_id':'codex',
                'memory_governance':{**self.config,'managed_role_ids':['role1','memory-only']}}
        state=Path(self.config['state_file'])
        for condition in ('missing','permissions','schema'):
            if state.exists():state.unlink()
            if condition!='missing':
                module.initialize_state(state)
                if condition=='permissions':state.chmod(0o644)
                else:
                    import sqlite3
                    with sqlite3.connect(state) as db:db.execute('PRAGMA user_version=77')
            environment={**os.environ,'QWENPAW_WORKING_DIR':str(Path(self.temp.name)/('host-'+condition)),
                         'PYTHONDONTWRITEBYTECODE':'1'}
            result=subprocess.run([sys.executable,'-B','-c',code,str(PATH.parent),json.dumps(config)],
                                  env=environment,capture_output=True,text=True,timeout=45)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            if condition=='missing':self.assertFalse(state.exists())
