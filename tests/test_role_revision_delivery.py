"""Pinned role identity rejects new work without blocking historical observation."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import httpx
from tests import test_role_dispatch as role_fixtures
from tests import test_execution_runtime as runtime_fixtures
from agent_line.role_dispatch import RoleDispatchLedger
from agent_line.role_revision import validate_role_revision
from agent_line.store import Conflict

PIN = 'qwenpaw-role-v1:' + 'a'*64
OTHER = 'qwenpaw-role-v1:' + 'b'*64


class RevisionValidationTests(unittest.TestCase):
    def test_exact_format(self):
        self.assertEqual(validate_role_revision(PIN), PIN)
        for value in (None, True, 'a'*64, PIN.upper(), PIN+'\n', PIN[:-1], {}, ''):
            with self.subTest(value=value), self.assertRaises(ValueError): validate_role_revision(value)


class RevisionDeliveryTests(unittest.TestCase):
    setUp = role_fixtures.RoleDispatchTests.setUp
    handler = role_fixtures.RoleDispatchTests.handler
    gateway = role_fixtures.RoleDispatchTests.gateway
    submit = role_fixtures.RoleDispatchTests.submit

    def test_proof_consumed_once_without_second_descriptor_get(self):
        gate=self.gateway()
        proof=gate.preflight(task_id='task-one',role_revision=PIN)
        gate.submit(task_id='task-one',role_session_id='agent-line-task-one',instruction='x',role_revision=PIN,preflight_proof=proof)
        self.assertEqual(self.calls.count(('GET','/api/agent-line/execution-binding')),1)
        with self.assertRaisesRegex(ValueError,'role_revision_proof_invalid'):
            gate.submit(task_id='task-one',role_session_id='agent-line-task-one',instruction='x',role_revision=PIN,preflight_proof=proof)
        self.assertEqual(self.calls.count(('POST','/api/console/chat/task')),1)

    def test_proof_cannot_be_json_boolean_other_gateway_task_or_pin(self):
        gate=self.gateway()
        for fake in (True, {}, 'token'):
            with self.assertRaisesRegex(ValueError,'role_revision_proof_invalid'):
                gate.submit(task_id='task-one',role_session_id='agent-line-task-one',instruction='x',role_revision=PIN,preflight_proof=fake)
        for target,task,pin in ((self.gateway(),'task-one',PIN),(gate,'task-two',PIN),(gate,'task-one',OTHER)):
            proof=gate.preflight(task_id='task-one',role_revision=PIN)
            with self.assertRaisesRegex(ValueError,'role_revision_proof_invalid'):
                target.submit(task_id=task,role_session_id='agent-line-'+task,instruction='x',role_revision=pin,preflight_proof=proof)
        self.assertFalse(any(method=='POST' for method,path in self.calls))

    def test_descriptor_changed_after_preflight_does_not_strand_missing_ledger(self):
        from agent_line.store import TaskStore
        from agent_line.dispatcher import Dispatcher
        store=TaskStore.initialize(Path(self.temp.name)/'tasks.sqlite')
        task,_=store.register('proof-race',{'projectId':'project','roleAgentId':'fixed-parent','workspace':self.temp.name,
            'workflow':{'runId':'run','nodeExecutionId':'node','attemptId':'one'},'input':{},'roleRevision':PIN})
        reads=[]
        def handler(request):
            if request.url.path.endswith('/execution-binding'):
                reads.append(1)
                if len(reads)>1:return httpx.Response(503,json={})
            if request.url.path.endswith('/delegations/bind'):
                self.calls.append(('POST',request.url.path))
                return httpx.Response(409,json={'detail':'revision changed'})
            return self.handler(request)
        gate=self.gateway(handler)
        dispatcher=Dispatcher(store,{'project':gate})
        result=dispatcher.submit(task['id'])
        self.assertEqual(len(reads),1)
        self.assertEqual(self.ledger.get(task['id'])['submissionStatus'],'intent')
        dispatcher.reconcile(task['id'])
        self.assertEqual(self.calls.count(('POST','/api/agent-line/delegations/bind')),1)
        self.assertFalse(result['resourcesReleased'])

    def test_missing_pin_never_claims_or_sends(self):
        with self.assertRaisesRegex(ValueError, 'role_revision_required'):
            self.gateway().submit(task_id='task-one',role_session_id='agent-line-task-one',instruction='x')
        self.assertEqual(self.calls, [])
        with self.assertRaises(KeyError): self.ledger.get('task-one')

    def test_mismatch_before_intent_and_binding(self):
        with self.assertRaisesRegex(ValueError, 'role_revision_mismatch'):
            self.gateway().submit(task_id='task-one',role_session_id='agent-line-task-one',instruction='x',role_revision=OTHER)
        self.assertEqual(self.calls, [('GET','/api/agent-line/execution-binding')])
        with self.assertRaises(KeyError): self.ledger.get('task-one')

    def test_unavailable_descriptor_never_claims(self):
        def unavailable(request): return httpx.Response(503,json={})
        with self.assertRaisesRegex(ValueError,'role_revision_unavailable'): self.submit(self.gateway(unavailable))
        with self.assertRaises(KeyError): self.ledger.get('task-one')

    def test_pin_is_persisted_and_echoed_by_binding(self):
        seen=[]
        def handler(request):
            if request.url.path.endswith('/delegations/bind'): seen.append(json.loads(request.content))
            return self.handler(request)
        self.submit(self.gateway(handler))
        self.assertEqual(seen[0]['roleRevision'],PIN)
        self.assertEqual(self.ledger.get('task-one')['payload']['roleRevision'],PIN)

    def test_reopen_replay_ignores_current_role_but_cannot_replace_pin(self):
        self.submit(self.gateway());self.calls.clear()
        def unavailable(request):
            if request.url.path.endswith('/execution-binding'): self.fail('Replay must not fetch current role')
            return self.handler(request)
        gate=self.gateway(unavailable,ledger=RoleDispatchLedger(self.path))
        self.submit(gate)
        self.assertFalse(any(method=='POST' for method,path in self.calls))
        with self.assertRaises(Conflict):
            gate.submit(task_id='task-one',role_session_id='agent-line-task-one',instruction='write assigned artifact',role_revision=OTHER)

    def test_wrong_binding_revision_receipt_does_not_start_parent(self):
        def handler(request):
            if request.url.path.endswith('/delegations/bind'):
                self.calls.append(('POST',request.url.path))
                return httpx.Response(200,json={**json.loads(request.content),'roleRevision':OTHER})
            return self.handler(request)
        gate=self.gateway(handler)
        self.submit(gate)
        self.submit(gate)
        self.assertEqual(self.calls.count(('POST','/api/agent-line/delegations/bind')),1)
        self.assertNotIn(('POST','/api/console/chat/task'),self.calls)

    def test_legacy_intent_remains_observable_and_replay_never_binds(self):
        gate=self.gateway()
        self.ledger.claim('task-one',{'roleAgentId':'fixed-parent','roleSessionId':'agent-line-task-one',
            'deploymentId':'installation-test','instruction':'legacy','memoryQuery':gate.memory_query,'timeoutSeconds':gate.timeout})
        gate.submit(task_id='task-one',role_session_id='agent-line-task-one',instruction='legacy')
        gate.observe('task-one')
        self.assertFalse(any(m=='POST' or p.endswith('/execution-binding') for m,p in self.calls))


class RevisionFactoryTests(unittest.TestCase):
    setUp = runtime_fixtures.ExecutionRuntimeTests.setUp
    handler = runtime_fixtures.ExecutionRuntimeTests.handler
    factory = runtime_fixtures.ExecutionRuntimeTests.factory
    load = runtime_fixtures.ExecutionRuntimeTests.load

    def test_unpinned_factory_observe_only_not_remote_auto_adoption(self):
        result=self.load();gate=result[('project','binding')];self.addCleanup(gate.close)
        with self.assertRaisesRegex(ValueError,'role_revision_required'):
            gate.submit(task_id='task-one',role_session_id='agent-line-task-one',instruction='x',role_revision=PIN)
        self.assertEqual(len(self.calls),2)

    def test_bound_gateway_transmits_proof_without_duplicate_get(self):
        self.bindings['binding']['roleRevision']=PIN
        original=self.handler
        def handler(request):
            response=original(request)
            if request.url.path.endswith('/execution-binding'):
                return httpx.Response(200,json={**response.json(),'roleRevisions':{'parent':PIN}})
            return response
        self.handler=handler
        gate=self.load()[('project','binding')];self.addCleanup(gate.close)
        proof=gate.preflight(task_id='task-one',role_revision=PIN)
        gate.submit(task_id='task-one',role_session_id='agent-line-task-one',instruction='x',role_revision=PIN,preflight_proof=proof)
        self.assertEqual(sum(r.url.path.endswith('/execution-binding') for r in self.calls),2)  # factory + preflight
        self.assertEqual(RoleDispatchLedger(self.ledger).get('task-one')['payload']['roleRevision'],PIN)

    def test_trusted_pin_must_match_live_descriptor(self):
        self.bindings['binding']['roleRevision']=PIN
        original=self.handler
        def handler(request):
            result=original(request)
            if request.url.path.endswith('/execution-binding'):
                return httpx.Response(200,json={**result.json(),'roleRevisions':{'parent':PIN}})
            return result
        self.handler=handler
        loaded=self.load();self.addCleanup(loaded[('project','binding')].close)
        self.assertEqual(loaded[('project','binding')].role_revision,PIN)
        self.bindings['binding']['roleRevision']=OTHER
        with self.assertRaises(ValueError): self.load()
        self.assertTrue(self.clients[-1].is_closed)

class DispatcherRevisionTests(unittest.TestCase):
    def test_blocked_revision_holds_no_lease_then_delivers_frozen_pin(self):
        import tempfile
        from pathlib import Path
        from agent_line.store import TaskStore
        from agent_line.dispatcher import Dispatcher
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);store=TaskStore.initialize(root/'tasks.sqlite')
            task,_=store.register('revision-task',{'projectId':'project','roleAgentId':'role','workspace':str(root),
                'workflow':{'runId':'run','nodeExecutionId':'node','attemptId':'one'},
                'input':{'roleRevision':OTHER},'roleRevision':PIN})
            class Gateway:
                blocked=True
                calls=[]
                def preflight(self,*,task_id,role_revision):
                    if self.blocked: raise ValueError('role_revision_mismatch')
                    if role_revision!=PIN: raise AssertionError('Frozen pin lost')
                def submit(self,**kwargs):
                    self.calls.append(kwargs)
                    return {'status':'accepted','identity':{'upstreamTaskId':'native'}}
            gate=Gateway();dispatcher=Dispatcher(store,{'project':gate})
            for _ in range(2):
                blocked=dispatcher.submit(task['id'])
                self.assertEqual(blocked['executionStatus'],'registered')
                self.assertEqual(blocked['submissionBlock'],'role_revision_mismatch')
            with store.connect() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM workspace_leases').fetchone()[0],0)
            self.assertEqual(gate.calls,[])
            gate.blocked=False
            delivered=dispatcher.submit(task['id'])
            self.assertEqual(delivered['executionStatus'],'accepted')
            self.assertNotIn('submissionBlock',delivered)
            self.assertEqual([e['kind'] for e in store.events(task['id'])].count('role_revision_mismatch'),1)
            self.assertEqual(gate.calls[0]['role_revision'],PIN)
            self.assertEqual(gate.calls[0]['instruction'],'{"roleRevision":"'+OTHER+'"}')
