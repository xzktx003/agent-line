import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, AsyncMock, patch
from fastapi import HTTPException
from qwenpaw.providers.model_info import ModelInfo
from qwenpaw.providers.openai_provider import OpenAIProvider
from qwenpaw.providers.provider import agent_thinking_level
from qwenpaw.providers.thinking import ThinkingControl
from qwenpaw.providers.typesafe_provider import TypeSafeProvider

ROOT = Path(__file__).resolve().parents[1] / 'plugins/agent-line'


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


revision = load('role_revision_test', 'role_revision.py')
bridge_module = load('revision_bridge_test', 'execution_bridge.py')
plugin = load('revision_plugin_test', 'plugin.py')
PIN = 'qwenpaw-role-v1:'+'a'*64


class RevisionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.profile = {'id':'parent','name':'Name','description':'Label','backend':'qwenpaw',
                        'workspace_dir':str(self.root),'active_model':None,'tools':None,
                        'system_prompt_files':['AGENTS.md','SOUL.md'],'fallback_models':[]}
        self.global_config = {'tools':{'enabled':['memory_search']},'security':{},'mcp':{},
                              'agents':{'defaults':{},'running':{},'profiles':{}}}
        self.manager = Mock()
        self.manager.get_active_model.return_value = {'provider_id':'test','model':'one'}
        self.provider=OpenAIProvider(id='test',name='Fixture',enabled=True,api_key='secret',base_url='http://fixture',
                                    models=[ModelInfo(id='one',name='One'),ModelInfo(id='two',name='Two')])
        self.manager.get_provider.return_value=self.provider
        (self.root/'AGENTS.md').write_text('rules\n<!-- memory:start -->old<!-- memory:end -->')
        (self.root/'SOUL.md').write_text('role prompt')
        path = self.root/'ledger.sqlite';bridge_module.ReceiptLedger.initialize(path)
        self.bridge = bridge_module.ExecutionBridge.__new__(bridge_module.ExecutionBridge)
        self.bridge.ledger=bridge_module.ReceiptLedger(path);self.bridge.roles=frozenset({'parent'})
        self.bridge.current_revision=Mock(return_value=PIN)
        self.bridge.target='codex';self.bridge.workspace=self.root;self.bridge.target_config=Mock()
        self.bridge.governance=None
        self.bridge.ledger.bind('task','parent','agent-line-task',PIN)

    def fingerprint(self):
        data=revision.snapshot(self.profile,self.global_config,self.manager)
        return hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()

    def test_effective_model_permissions_prompt_and_global_drift(self):
        initial=self.fingerprint()
        self.manager.get_active_model.return_value['model']='two'
        self.assertNotEqual(initial,self.fingerprint())
        self.manager.get_active_model.return_value['model']='one'
        self.global_config['tools']['enabled'].append('shell')
        self.assertNotEqual(initial,self.fingerprint())
        self.global_config['tools']['enabled'].pop()
        (self.root/'SOUL.md').write_text('changed prompt')
        self.assertNotEqual(initial,self.fingerprint())

    def test_names_unrelated_roles_memory_and_credentials_do_not_change_revision(self):
        initial=self.fingerprint()
        self.profile['name']='renamed';self.profile['description']='another label'
        self.global_config['agents']['profiles']['unrelated']={'name':'another'}
        (self.root/'AGENTS.md').write_text('rules\n<!-- memory:start -->new<!-- memory:end -->')
        (self.root/'MEMORY.md').write_text('native memories')
        self.provider.api_key='rotated'
        self.assertEqual(initial,self.fingerprint())

    def test_selected_observation_timestamps_labels_prices_and_status_do_not_drift(self):
        initial=self.fingerprint()
        model=self.provider.models[0]
        model.discovered_at='2026-10-22T03:15:04+00:00'
        model.billing_checked_at='2026-10-22T03:15:04+00:00'
        model.availability_checked_at='2026-10-22T03:15:04+00:00'
        model.availability_status='available';model.availability_message='Observed available'
        model.availability_verification='live';model.availability_http_status=200
        model.name='A refreshed display label';model.pricing={'input':'0.01'}
        model.capability_provenance={'billing':{'updated_at':'2026-10-22T03:15:04+00:00'}}
        self.provider.models_last_synced_at='2026-10-22T03:15:04+00:00'
        self.provider.models_syncing=True;self.provider.model_sync_mode='startup'
        self.assertEqual(initial,self.fingerprint())

    def test_unselected_model_catalog_addition_removal_or_configuration_does_not_drift(self):
        initial=self.fingerprint()
        self.provider.models[1].generate_kwargs={'temperature':0.2}
        self.provider.models.append(ModelInfo(id='unrelated',name='Remote observation',max_input_length=65536))
        self.provider.discovered_models=[ModelInfo(id='unrelated-discovery',name='Another API observation')]
        self.provider.hidden_model_ids=['unrelated'];self.provider.removed_model_ids=['unrelated-discovery']
        self.assertEqual(initial,self.fingerprint())
        self.provider.models.pop();self.provider.discovered_models=[]
        self.assertEqual(initial,self.fingerprint())

    def test_selected_effective_execution_fields_and_provider_connection_do_drift(self):
        changes=[('generate_kwargs',{'temperature':0.2}),('relay_reasoning',False),
                 ('thinking_enabled',True),('thinking_budget',1024),('reasoning_effort','high'),
                 ('supports_tool_calling',False),('supports_image',False),('input_token_limit',32768),
                 ('max_output_length',4096)]
        self.provider.models[0].max_input_length=65536
        for key,value in changes:
            with self.subTest(field=key):
                initial=self.fingerprint();old=getattr(self.provider.models[0],key)
                setattr(self.provider.models[0],key,value)
                self.assertNotEqual(initial,self.fingerprint())
                setattr(self.provider.models[0],key,old)
        initial=self.fingerprint();self.provider.base_url='http://another-fixture'
        self.assertNotEqual(initial,self.fingerprint())

    def test_effective_generation_merge_ignores_overridden_provider_value(self):
        self.provider.models[0].generate_kwargs={'temperature':0.2}
        self.provider.generate_kwargs={'temperature':0.1}
        initial=self.fingerprint()
        self.provider.generate_kwargs['temperature']=0.9
        self.assertEqual(initial,self.fingerprint())
        self.provider.generate_kwargs['top_p']=0.8
        self.assertNotEqual(initial,self.fingerprint())

    def test_selected_context_and_thinking_controls_change_revision(self):
        model=self.provider.models[0]
        model.max_input_length_configured=True;model.max_input_length=65536
        initial=self.fingerprint();model.max_input_length=131072
        self.assertNotEqual(initial,self.fingerprint())
        initial=self.fingerprint()
        model.thinking_control=ThinkingControl(kind='effort',efforts=['low','high'],wire='compat_effort')
        self.assertNotEqual(initial,self.fingerprint())

    def test_actual_fallback_decision_and_subagent_models_are_frozen(self):
        for field in ('fallback_models','decision_model','subagent_model'):
            with self.subTest(field=field):
                slot={'provider_id':'test','model':'two'}
                self.profile[field]=[slot] if field=='fallback_models' else slot
                initial=self.fingerprint();self.provider.models[1].generate_kwargs={'temperature':0.4}
                self.assertNotEqual(initial,self.fingerprint())
                self.provider.models[1].generate_kwargs={}
                self.profile.pop(field)

    def test_ambient_thinking_context_is_not_part_of_effective_parameter_snapshot(self):
        self.provider.models[0].thinking_control=ThinkingControl(kind='effort',efforts=['low','high'],wire='compat_effort')
        original=OpenAIProvider.get_effective_generate_kwargs
        observed=[]
        def capture(provider,model):
            value=original(provider,model);observed.append(value);return value
        with patch.object(OpenAIProvider,'get_effective_generate_kwargs',new=capture):
            with agent_thinking_level('high'):high=self.fingerprint()
            with agent_thinking_level('low'):low=self.fingerprint()
        self.assertEqual(high,low)
        self.assertEqual(observed,[{},{}])

    def test_revision_http_error_marks_only_model_configuration(self):
        import importlib.util
        import sys
        import types
        package = "revision_header_probe"
        pkg = types.ModuleType(package)
        pkg.__path__ = [str(ROOT)]
        pkg.__package__ = package
        sys.modules[package] = pkg
        sys.modules[package + ".role_revision"] = revision
        spec = importlib.util.spec_from_file_location(package + ".execution_bridge", ROOT / "execution_bridge.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        for message in revision.MODEL_CONFIGURATION_ERRORS:
            with self.subTest(message=message):
                with self.assertRaises(HTTPException) as raised:
                    module.revision_http_error(ValueError(message))
                self.assertEqual(raised.exception.status_code, 409)
                self.assertEqual(raised.exception.detail, "无法核对角色行为修订，拒绝新执行")
                self.assertEqual(raised.exception.headers["X-Agent-Line-Configuration"], "model_unavailable")
        for exc in (ValueError("Registered role is disabled"), RuntimeError("Effective model selection unavailable"),
                    ValueError("Effective model selection unavailable ")):
            with self.subTest(exc=str(exc)):
                with self.assertRaises(HTTPException) as raised:
                    module.revision_http_error(exc)
                self.assertIsNone(raised.exception.headers)

    def test_selected_removed_remote_missing_or_paid_confirmation_refuses(self):
        for field in ('remote_missing','requires_paid_confirmation'):
            with self.subTest(field=field):
                setattr(self.provider.models[0],field,True)
                with self.assertRaises(ValueError):self.fingerprint()
                setattr(self.provider.models[0],field,False)
        self.provider.removed_model_ids=['one']
        with self.assertRaises(ValueError):self.fingerprint()
        self.provider.removed_model_ids=[]
        self.provider.discovered_models=[ModelInfo(id='one',name='One',remote_missing=True)]
        with self.assertRaises(ValueError):self.fingerprint()

    def test_selected_resolution_never_constructs_model_or_discovers_network_metadata(self):
        with patch.object(OpenAIProvider,'get_chat_model_instance',side_effect=AssertionError('No runtime')), \
             patch.object(OpenAIProvider,'fetch_models',side_effect=AssertionError('No discovery')), \
             patch.object(OpenAIProvider,'check_connection',side_effect=AssertionError('No network')):
            self.fingerprint()

    def test_native_typesafe_decision_slot_without_optional_media_limit(self):
        decision=TypeSafeProvider(id='typesafe',name='Native decision provider',base_url='http://decision-fixture',
            models=[ModelInfo(id='jev-fixture',name='Decision model',model_type='decision')])
        self.assertNotIn('max_inline_media_bytes',decision.model_dump())
        self.profile['decision_model']={'provider_id':'typesafe','model':'jev-fixture'}
        self.manager.get_provider.side_effect=lambda key:decision if key=='typesafe' else self.provider
        with patch.object(TypeSafeProvider,'evaluate',side_effect=AssertionError('No evaluation')), \
             patch.object(TypeSafeProvider,'fetch_models',side_effect=AssertionError('No discovery')):
            value=revision.snapshot(self.profile,self.global_config,self.manager)
        contract=value['models'][1]
        self.assertEqual(contract['model']['model_type'],'decision')
        self.assertEqual(contract['provider']['id'],'typesafe')
        self.assertNotIn('max_inline_media_bytes',contract['provider'])
        initial=self.fingerprint();decision.base_url='http://another-decision-fixture'
        self.assertNotEqual(initial,self.fingerprint())

    def test_strict_shared_projection_fields_exist_on_official_provider_and_model_bases(self):
        from qwenpaw.providers.provider import Provider
        provider_fields={'id','enabled','base_url','chat_model','auth_mode','custom_headers','is_local','require_api_key'}
        model_fields={'id','model_type','effective_max_input_length','input_token_limit','max_output_length',
            'supports_multimodal','supports_image','supports_audio','supports_video','supports_tool_calling',
            'relay_reasoning','thinking_enabled','thinking_budget','reasoning_effort','thinking_param_style',
            'reasoning_effort_options','thinking_budget_range'}
        self.assertFalse(provider_fields-set(Provider.model_fields))
        self.assertFalse(model_fields-set(ModelInfo.model_fields))

    def test_optional_media_limit_is_frozen_for_providers_that_support_it(self):
        self.assertIn('max_inline_media_bytes',self.provider.model_dump())
        initial=self.fingerprint();self.provider.max_inline_media_bytes+=1024
        self.assertNotEqual(initial,self.fingerprint())

    def test_unresolved_model_and_prompt_read_failure_refuse(self):
        self.manager.get_active_model.return_value=None
        with self.assertRaises(ValueError):self.fingerprint()
        self.manager.get_active_model.return_value={'provider_id':'test','model':'one'}
        self.profile['system_prompt_files']=['../outside.md']
        with self.assertRaises(ValueError):self.fingerprint()

    def test_reopen_pins_and_null_legacy_pin_cannot_be_upgraded_implicitly(self):
        self.bridge.ledger=bridge_module.ReceiptLedger(self.bridge.ledger.path)
        self.assertEqual(self.bridge.assert_role_revision('parent','agent-line-task'),PIN)
        self.bridge.current_revision.return_value='qwenpaw-role-v1:'+'b'*64
        with self.assertRaises(HTTPException):self.bridge.assert_role_revision('parent','agent-line-task')
        with self.bridge.ledger.connection() as db:db.execute('UPDATE role_bindings SET role_revision=NULL')
        with self.assertRaises(HTTPException):self.bridge.assert_role_revision('parent','agent-line-task')
        with self.assertRaises(HTTPException):self.bridge.ledger.bind('task','parent','agent-line-task',PIN)
        self.assertEqual(self.bridge.ledger.revoke_delegation('task'),(('parent','agent-line-task'),False))

    async def test_new_execution_drift_does_not_claim_or_call_native(self):
        self.bridge.current_revision.return_value='qwenpaw-role-v1:'+'b'*64
        body=bridge_module.ExecutionRequest(taskId='task',roleSessionId='agent-line-task',instruction='bounded')
        with self.assertRaises(HTTPException):await self.bridge.submit(body,None)
        with self.bridge.ledger.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM receipts').fetchone()[0],0)

    async def test_already_claimed_replay_observes_even_if_current_config_unavailable(self):
        body=bridge_module.ExecutionRequest(taskId='task',roleSessionId='agent-line-task',instruction='bounded')
        self.bridge.ledger.claim('task',{**body.model_dump(),'targetAgentId':'codex','workspace':str(self.root)})
        self.bridge.target_config.side_effect=RuntimeError('changed')
        self.bridge.current_revision.side_effect=RuntimeError('changed')
        self.bridge.observe=AsyncMock(return_value={'taskId':'task','status':'unknown'})
        self.assertEqual((await self.bridge.submit(body,None))['taskId'],'task')
        self.bridge.observe.assert_awaited_once_with('task')
        self.bridge.target_config.assert_not_called()

    async def test_all_inference_phases_and_mutating_tools_refuse_but_status_allowed(self):
        self.bridge.current_revision.return_value='qwenpaw-role-v1:'+'b'*64
        guard=plugin.ManagedRoleToolGuard(None,'parent',self.bridge)
        async def stream(**kwargs):
            self.fail('downstream inference called')
            yield None
        with patch.object(plugin,'get_current_session_id',return_value='agent-line-task'):
            for method in (guard.on_reply,guard.on_reasoning):
                with self.assertRaises(PermissionError):
                    async for _ in method(None,{},stream):pass
            model=AsyncMock()
            with self.assertRaises(PermissionError):await guard.on_model_call(None,{},model)
            model.assert_not_awaited()
            for name in ('memory_search','agent_line_delegate'):
                with self.assertRaises(PermissionError):
                    async for _ in guard.on_acting(None,{'tool_call':{'name':name}},stream):pass
            async def status():yield 'read-only'
            self.assertEqual([item async for item in guard.on_acting(None,{'tool_call':{'name':'agent_line_execution_status'}},status)],['read-only'])

    def test_explicit_schema3_migration_preserves_unpinned_old_row(self):
        path=self.root/'old.sqlite'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE receipts(task_id TEXT PRIMARY KEY,payload TEXT,record TEXT)')
            db.execute('CREATE TABLE role_bindings(task_id TEXT PRIMARY KEY,role_id TEXT,session_id TEXT UNIQUE)')
            db.execute('CREATE TABLE revoked_delegations(task_id TEXT PRIMARY KEY)')
            db.execute("INSERT INTO role_bindings VALUES('old','parent','old-session')")
            db.execute('PRAGMA user_version=3')
        with self.assertRaises(ValueError):bridge_module.ReceiptLedger(path)
        bridge_module.ReceiptLedger.migrate(path)
        ledger=bridge_module.ReceiptLedger(path)
        with ledger.connection() as db:self.assertIsNone(db.execute('SELECT role_revision FROM role_bindings').fetchone()[0])
        self.assertEqual(ledger.bound_task('parent','old-session'),'old')

    def test_prompt_symlink_fifo_and_limits_fail_closed(self):
        import os
        path=self.root/'SOUL.md';path.unlink();os.mkfifo(path)
        with self.assertRaises(ValueError):self.fingerprint()
        path.unlink();path.symlink_to(self.root/'AGENTS.md')
        with self.assertRaises(OSError):self.fingerprint()
        path.unlink();path.write_bytes(b'x'*(1_048_576+1))
        with self.assertRaises(ValueError):self.fingerprint()
        self.profile['system_prompt_files']=[]
        for index in range(5):
            name=str(index)+'.md';(self.root/name).write_bytes(b'x'*1_048_576)
            self.profile['system_prompt_files'].append(name)
        with self.assertRaises(ValueError):self.fingerprint()

    def test_codex_implicit_model_not_claimed_as_frozen(self):
        self.profile['backend']='codex';self.profile['backend_settings']={}
        with self.assertRaises(ValueError):self.fingerprint()
        self.profile['backend_settings']['model']='fixed-model'
        initial=self.fingerprint()
        self.profile['backend_settings']['approval_policy']='never'
        self.assertNotEqual(initial,self.fingerprint())

    async def test_unavailable_bridge_keeps_guards_and_status_is_explicit_503(self):
        bridge=plugin.UnavailableExecutionBridge()
        guard=plugin.ManagedRoleToolGuard(None,'parent',bridge)
        with self.assertRaises(PermissionError):await guard.on_model_call(None,{},AsyncMock())
        with self.assertRaises(HTTPException) as raised:bridge.bound_task('parent','session')
        self.assertEqual(raised.exception.status_code,503)

    async def test_real_agentscope_reasoning_chain_blocks_before_native_injection(self):
        from agentscope.agent import Agent
        from types import SimpleNamespace
        calls=[]
        async def native(**kwargs):
            calls.append('model and memory')
            yield None
        guard=plugin.ManagedRoleToolGuard(None,'parent',self.bridge)
        self.bridge.current_revision.return_value='qwenpaw-role-v1:'+'b'*64
        agent=SimpleNamespace(_reasoning_middlewares=[guard],_reasoning_impl=native)
        with patch.object(plugin,'get_current_session_id',return_value='agent-line-task'):
            with self.assertRaises(PermissionError):
                async for _ in Agent._reasoning(agent):pass
        self.assertEqual(calls,[])

    async def test_observation_and_cancel_keep_original_identity_after_config_drift(self):
        from starlette.requests import Request
        body=bridge_module.ExecutionRequest(taskId='task',roleSessionId='agent-line-task',instruction='bounded')
        self.bridge.ledger.claim('task',{**body.model_dump(),'targetAgentId':'codex','workspace':str(self.root)})
        self.bridge.target_config.side_effect=RuntimeError('changed config')
        self.assertEqual((await self.bridge.observe('task'))['status'],'unknown')
        request=Request({'type':'http','method':'POST','path':'/','headers':[],'state':{},'app':None})
        with patch('qwenpaw.app.routers.console.post_console_chat_stop',new=AsyncMock(return_value={'stopped':True})) as stop:
            observed=await self.bridge.cancel('task',request)
        stop.assert_awaited_once()
        self.assertTrue(observed['cancellationRequested'])
        self.assertFalse(observed['terminalConfirmed'])
        self.assertFalse(observed['resourcesReleased'])
        self.bridge.target_config.assert_not_called()



    async def test_memory_search_rejects_escaped_directories_and_absent_config_still_runs(self):
        guard = plugin.ManagedRoleToolGuard(None, 'parent')
        called = []
        async def downstream():
            called.append(True)
            yield 'recalled'
        args = (None, {'tool_call': {'name': 'memory_search'}}, downstream)
        with patch('qwenpaw.config.utils.load_config', return_value=SimpleNamespace(agents=SimpleNamespace(profiles={}))):
            self.assertEqual([item async for item in guard.on_acting(*args)], ['recalled'])
        self.assertEqual(called, [True])
        called.clear()
        defaults = dict(revision.MEMORY_DIRECTORY_DEFAULTS)
        (self.root / 'agent.json').write_text(json.dumps({'running': {'reme_light_memory_config': {**defaults, 'digest_dir': '../outside'}}}))
        config = SimpleNamespace(agents=SimpleNamespace(profiles={'parent': SimpleNamespace(workspace_dir=str(self.root))}))
        with patch('qwenpaw.config.utils.load_config', return_value=config):
            with self.assertRaises(PermissionError):
                async for _ in guard.on_acting(*args):
                    pass
        self.assertEqual(called, [])
        (self.root / 'agent.json').write_text(json.dumps({'running': {'reme_light_memory_config': {**defaults, 'daily_dir': 'memory/imports/proj'}}}))
        with patch('qwenpaw.config.utils.load_config', return_value=config):
            self.assertEqual([item async for item in guard.on_acting(*args)], ['recalled'])

    def test_memory_directories_reject_absolute_parent_and_non_strings_but_keep_nested_relative(self):
        defaults = dict(revision.MEMORY_DIRECTORY_DEFAULTS)
        workspace = self.root / 'role'
        workspace.mkdir()
        self.assertTrue(revision.memory_directories_within_workspace(workspace, {**defaults, 'daily_dir': 'memory/imports/proj'}))
        for escaped in ('/tmp/other', 'memory/../outside', '../outside', None, 1, ['memory']):
            self.assertFalse(revision.memory_directories_within_workspace(workspace, {**defaults, 'digest_dir': escaped}))
        self.assertFalse(revision.memory_directories_within_workspace('relative', defaults))
        outside = self.root / 'outside'
        outside.mkdir()
        (workspace / 'memory').symlink_to(outside)
        self.assertFalse(revision.memory_directories_within_workspace(workspace, defaults))
        with self.assertRaises(ValueError):
            revision.memory_directory_values(['digest'])


class EnabledRoleAdmissionTests(unittest.IsolatedAsyncioTestCase):
    """Use official profile references; all configuration and receipts are temporary."""
    def setUp(self):
        from qwenpaw.config.config import AgentProfileConfig, AgentProfileRef, Config
        from starlette.requests import Request
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.config=Config()
        self.profiles={}
        for identifier,backend in (('parent','qwenpaw'),('codex','codex')):
            workspace=self.root/identifier;workspace.mkdir()
            (workspace/'agent.json').write_text('{}')
            self.config.agents.profiles[identifier]=AgentProfileRef(id=identifier,workspace_dir=str(workspace))
            self.profiles[identifier]=AgentProfileConfig(id=identifier,name=identifier,backend=backend,
                workspace_dir=str(workspace),system_prompt_files=[],
                backend_settings={'model':'fixed-fixture-model'} if backend=='codex' else {})
        self.manager=Mock()
        self.manager.get_active_model.return_value={'provider_id':'fixture','model':'fixture-model'}
        self.manager.get_provider.return_value=OpenAIProvider(id='fixture',name='Fixture',enabled=True,
            base_url='http://fixture',models=[ModelInfo(id='fixture-model',name='Fixture model')])
        for patcher in (
            patch('qwenpaw.config.utils.load_config',return_value=self.config),
            patch('qwenpaw.config.utils.load_agent_config',side_effect=lambda key:self.profiles[key]),
            patch('qwenpaw.providers.provider_manager.ProviderManager.get_instance',return_value=self.manager)):
            patcher.start();self.addCleanup(patcher.stop)
        package=ModuleType('enabled_role_revision_fixture');package.__path__=[str(ROOT)]
        patcher=patch.dict(sys.modules,{'enabled_role_revision_fixture':package,
                                      'enabled_role_revision_fixture.role_revision':revision})
        patcher.start();self.addCleanup(patcher.stop)
        self.module=load('enabled_role_revision_fixture.execution_bridge','execution_bridge.py')
        ledger=self.root/'receipts.sqlite';self.module.ReceiptLedger.initialize(ledger)
        self.bridge=self.module.ExecutionBridge.__new__(self.module.ExecutionBridge)
        self.bridge.ledger=self.module.ReceiptLedger(ledger)
        self.bridge.roles=frozenset({'parent'});self.bridge.target='codex'
        self.bridge.workspace=self.root/'codex';self.bridge.container_launcher=None
        self.bridge.container_control=None
        self.bridge.governance=None;self.bridge.config={}
        self.request=Request({'type':'http','method':'POST','path':'/','headers':[],'state':{},'app':object()})
        self.pin=revision.role_revision('parent','codex')

    def bind(self,task='task'):
        body=self.module.RoleBindingRequest(taskId=task,roleAgentId='parent',roleRevision=self.pin,
                                            roleSessionId='agent-line-'+task)
        return self.bridge.bind_parent(body,self.request)

    def count(self,table):
        with self.bridge.ledger.connection() as db:
            return db.execute('SELECT count(*) FROM '+table).fetchone()[0]


    def test_escaped_memory_directory_blocks_preflight_without_hashing_bodies(self):
        reme = self.profiles['parent'].running.reme_light_memory_config
        original = reme.digest_dir
        reme.digest_dir = '/tmp/outside-memory'
        with self.assertRaises(ValueError):
            revision.role_revision('parent', 'codex')
        with self.assertRaises(HTTPException) as raised:
            self.bridge.binding_descriptor()
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(self.count('receipts'), 0)
        reme.digest_dir = 'memory/imports/proj'
        self.assertRegex(revision.role_revision('parent', 'codex'), revision.PATTERN)
        reme.digest_dir = original
        note = self.root / 'parent' / 'memory' / 'note.md'
        note.parent.mkdir()
        note.write_text('secret memory body that must not enter the revision')
        self.assertEqual(revision.role_revision('parent', 'codex'), self.pin)

    def test_disabled_parent_and_target_are_not_available_revisions(self):
        for identifier in ('parent','codex'):
            with self.subTest(identifier=identifier):
                self.config.agents.profiles[identifier].enabled=False
                with self.assertRaises(ValueError):revision.role_revision('parent','codex')
                self.config.agents.profiles[identifier].enabled=True

    def test_disabled_parent_and_target_block_descriptor_without_receipts(self):
        for identifier in ('parent','codex'):
            with self.subTest(identifier=identifier):
                self.config.agents.profiles[identifier].enabled=False
                with self.assertRaises(HTTPException) as raised:self.bridge.binding_descriptor()
                self.assertEqual(raised.exception.status_code,409)
                self.assertEqual(self.count('receipts'),0)
                self.config.agents.profiles[identifier].enabled=True

    def test_disabled_parent_and_target_block_new_parent_binding(self):
        for identifier in ('parent','codex'):
            with self.subTest(identifier=identifier):
                self.config.agents.profiles[identifier].enabled=False
                with self.assertRaises(HTTPException) as raised:self.bind('new-'+identifier)
                self.assertEqual(raised.exception.status_code,409)
                self.assertEqual(self.count('role_bindings'),0)
                self.assertEqual(self.count('receipts'),0)
                self.config.agents.profiles[identifier].enabled=True

    async def assert_disabled_delegation(self,identifier):
        self.bind()
        self.config.agents.profiles[identifier].enabled=False
        body=self.module.ExecutionRequest(taskId='task',roleSessionId='agent-line-task',instruction='bounded')
        with patch('qwenpaw.app.routers.console.post_console_chat_task',new=AsyncMock(
                return_value={'task_id':'must-not-start'})) as native:
            with self.assertRaises(HTTPException) as raised:await self.bridge.submit(body,self.request)
            self.assertEqual(raised.exception.status_code,409)
            native.assert_not_awaited()
        self.assertEqual(self.count('receipts'),0)

    async def test_disabled_parent_blocks_new_delegation_before_claim_and_native_call(self):
        await self.assert_disabled_delegation('parent')

    async def test_disabled_target_blocks_new_delegation_before_claim_and_native_call(self):
        await self.assert_disabled_delegation('codex')

    def test_reenable_restores_exact_original_revision_without_a_new_pin(self):
        for identifier in ('parent','codex'):
            self.config.agents.profiles[identifier].enabled=False
            with self.assertRaises(ValueError):revision.role_revision('parent','codex')
            self.config.agents.profiles[identifier].enabled=True
            self.assertEqual(revision.role_revision('parent','codex'),self.pin)

    def test_schema_two_hash_keeps_v1_envelope_and_does_not_accept_old_binding(self):
        value={'revisionSchema':2,'parent':revision.snapshot(self.profiles['parent'],self.config,self.manager),
               'executor':revision.snapshot(self.profiles['codex'],self.config,self.manager)}
        def digest(data):
            encoded=json.dumps(data,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
            return 'qwenpaw-role-v1:'+hashlib.sha256(encoded).hexdigest()
        self.assertEqual(self.pin,digest(value))
        self.assertRegex(self.pin,revision.PATTERN)
        value.pop('revisionSchema');old=digest(value)
        self.assertNotEqual(old,self.pin)
        self.bridge.ledger.bind('old','parent','agent-line-old',old)
        with self.assertRaises(HTTPException):self.bridge.assert_role_revision('parent','agent-line-old')
        self.assertEqual(self.bridge.ledger.bound_task('parent','agent-line-old'),'old')

    def test_deleted_parent_and_target_descriptor_refuses_before_fallback_loader(self):
        for identifier in ('parent','codex'):
            with self.subTest(identifier=identifier):
                reference=self.config.agents.profiles.pop(identifier)
                with patch('qwenpaw.config.utils.load_agent_config',side_effect=AssertionError('No fallback loader')) as loader:
                    with self.assertRaises(HTTPException) as raised:self.bridge.binding_descriptor()
                    self.assertEqual(raised.exception.status_code,409)
                    loader.assert_not_called()
                self.assertEqual(self.count('receipts'),0)
                self.config.agents.profiles[identifier]=reference

    def test_deleted_parent_and_target_new_bind_refuses_before_fallback_loader(self):
        for identifier in ('parent','codex'):
            with self.subTest(identifier=identifier):
                reference=self.config.agents.profiles.pop(identifier)
                with patch('qwenpaw.config.utils.load_agent_config',side_effect=AssertionError('No fallback loader')) as loader:
                    with self.assertRaises(HTTPException) as raised:self.bind('deleted-'+identifier)
                    self.assertEqual(raised.exception.status_code,409)
                    loader.assert_not_called()
                self.assertEqual(self.count('role_bindings'),0)
                self.assertEqual(self.count('receipts'),0)
                self.config.agents.profiles[identifier]=reference

    async def test_deleted_parent_and_target_new_delegate_refuses_before_loader_claim_or_native(self):
        for identifier in ('parent','codex'):
            with self.subTest(identifier=identifier):
                task='deleted-'+identifier;self.bind(task)
                reference=self.config.agents.profiles.pop(identifier)
                body=self.module.ExecutionRequest(taskId=task,roleSessionId='agent-line-'+task,instruction='bounded')
                with patch('qwenpaw.config.utils.load_agent_config',side_effect=AssertionError('No fallback loader')) as loader, \
                        patch('qwenpaw.app.routers.console.post_console_chat_task',new=AsyncMock()) as native:
                    with self.assertRaises(HTTPException) as raised:await self.bridge.submit(body,self.request)
                    self.assertEqual(raised.exception.status_code,409)
                    loader.assert_not_called();native.assert_not_awaited()
                self.assertEqual(self.count('receipts'),0)
                self.config.agents.profiles[identifier]=reference

    def test_missing_agent_file_descriptor_refuses_before_fallback_loader(self):
        for identifier in ('parent','codex'):
            with self.subTest(identifier=identifier):
                source=self.root/identifier/'agent.json';source.unlink()
                with patch('qwenpaw.config.utils.load_agent_config',side_effect=AssertionError('No fallback loader')) as loader:
                    with self.assertRaises(HTTPException) as raised:self.bridge.binding_descriptor()
                    self.assertEqual(raised.exception.status_code,409)
                    loader.assert_not_called()
                self.assertFalse(source.exists())
                source.write_text('{}')

    async def test_deleted_roles_keep_accepted_history_and_cancel_without_fallback_loader(self):
        self.bind()
        body=self.module.ExecutionRequest(taskId='task',roleSessionId='agent-line-task',instruction='bounded')
        payload={**body.model_dump(),'targetAgentId':'codex','workspace':str(self.bridge.workspace)}
        self.bridge.ledger.claim('task',payload)
        self.bridge.ledger.patch('task',status='accepted',identity={'upstreamTaskId':'original-native-task'})
        self.config.agents.profiles.clear()
        with patch('qwenpaw.config.utils.load_agent_config',side_effect=AssertionError('No fallback loader')) as loader, \
                patch('qwenpaw.app.routers.console.get_console_chat_task',
                      new=AsyncMock(return_value={'status':'running'})), \
                patch('qwenpaw.app.routers.console.post_console_chat_stop',
                      new=AsyncMock(return_value={'stopped':True})) as stop, \
                patch('qwenpaw.app.routers.console.post_console_chat_task',new=AsyncMock()) as native:
            replay=await self.bridge.submit(body,self.request)
            self.assertEqual(replay['identity'],{'upstreamTaskId':'original-native-task'})
            self.assertEqual((await self.bridge.observe('task'))['identity'],replay['identity'])
            canceled=await self.bridge.cancel('task',self.request)
            self.assertTrue(canceled['cancellationRequested'])
            self.assertEqual(canceled['identity'],replay['identity'])
            stop.assert_awaited_once()
            self.assertEqual(stop.call_args.kwargs['chat_id'],'agent-line-task')
            loader.assert_not_called();native.assert_not_awaited()
        self.assertEqual(self.count('receipts'),1)

    async def test_disabled_roles_keep_accepted_replay_observation_and_cancel_on_original_identity(self):
        self.bind()
        body=self.module.ExecutionRequest(taskId='task',roleSessionId='agent-line-task',instruction='bounded')
        payload={**body.model_dump(),'targetAgentId':'codex','workspace':str(self.bridge.workspace)}
        self.bridge.ledger.claim('task',payload)
        self.bridge.ledger.patch('task',status='accepted',identity={'upstreamTaskId':'original-native-task'})
        for reference in self.config.agents.profiles.values():reference.enabled=False
        with patch('qwenpaw.app.routers.console.get_console_chat_task',
                   new=AsyncMock(return_value={'status':'running'})) as read, \
             patch('qwenpaw.app.routers.console.post_console_chat_stop',
                   new=AsyncMock(return_value={'stopped':True})) as stop, \
             patch.object(self.bridge,'current_revision',side_effect=AssertionError('Historical identity only')), \
             patch('qwenpaw.app.routers.console.post_console_chat_task',new=AsyncMock()) as native:
            replay=await self.bridge.submit(body,self.request)
            self.assertEqual(replay['identity'],{'upstreamTaskId':'original-native-task'})
            observed=await self.bridge.observe('task')
            self.assertEqual(observed['identity'],replay['identity'])
            canceled=await self.bridge.cancel('task',self.request)
            self.assertTrue(canceled['cancellationRequested'])
            stop.assert_awaited_once()
            self.assertEqual(stop.call_args.kwargs['chat_id'],'agent-line-task')
            self.assertEqual(stop.call_args.args[0].state.agent_id,'codex')
            self.assertTrue(all(call.args==('original-native-task',) for call in read.call_args_list))
            native.assert_not_awaited()
        self.assertEqual(self.count('receipts'),1)

