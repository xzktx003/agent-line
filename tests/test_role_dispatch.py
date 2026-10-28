import json
from pathlib import Path
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
import httpx

from agent_line.role_dispatch import RoleDispatchLedger, RoleDispatchGateway
from agent_line.store import Conflict


class RoleDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'roles.sqlite';self.ledger=RoleDispatchLedger.initialize(self.path)
        self.calls=[];self.native=None;self.parent_finished=False
    def handler(self,request):
        self.calls.append((request.method,request.url.path))
        if request.url.path.endswith('/execution-binding'):
            return httpx.Response(200,json={'roleRevisions':{'fixed-parent':'qwenpaw-role-v1:'+'a'*64}})
        if request.url.path.endswith('/delegations/bind'):
            return httpx.Response(200,json={**json.loads(request.content),'newBinding':True})
        if request.url.path=='/api/console/chat/task':
            self.parent_body=json.loads(request.content)
            self.parent_headers=request.headers
            return httpx.Response(200,json={'task_id':'parent-native-one'})
        if request.url.path=='/api/console/chat/task/parent-native-one':
            return httpx.Response(200,json={'status':'finished' if self.parent_finished else 'running','result':{'status':'completed','output':[]}})
        if request.url.path=='/api/agent-line/delegations/task-one/cancel':return httpx.Response(200,json={'taskId':'task-one','delegationRevoked':True,'parentStopAcknowledged':True,'executionClaimed':False})
        return httpx.Response(200,json=self.native) if self.native else httpx.Response(404,json={'detail':'missing'})
    def gateway(self,handler=None,ledger=None,role='fixed-parent',memory_query='项目规则和相关经验'):
        client=httpx.Client(transport=httpx.MockTransport(handler or self.handler));self.addCleanup(client.close)
        return RoleDispatchGateway('http://qwen.test','host-secret','service-secret',role_agent_id=role,
            deployment_id='installation-test',ledger=ledger or self.ledger,memory_query=memory_query,client=client)
    def submit(self,gateway,instruction='write assigned artifact'):
        return gateway.submit(task_id='task-one',role_session_id='agent-line-task-one',instruction=instruction,role_revision='qwenpaw-role-v1:'+'a'*64)
    def test_explicit_initialization_missing_database_never_created(self):
        missing=Path(self.temp.name)/'missing.sqlite'
        with self.assertRaises(FileNotFoundError):RoleDispatchLedger(missing)
        self.assertFalse(missing.exists())
    def test_role_bound_session_prompt_and_parent_not_native_success(self):
        result=self.submit(self.gateway())
        self.assertEqual(result['status'],'unknown');self.assertFalse(result['terminalConfirmed'])
        self.assertEqual(self.parent_headers['X-Agent-Id'],'fixed-parent')
        self.assertEqual(self.parent_headers['Authorization'],'Bearer host-secret')
        self.assertEqual(self.parent_body['session_id'],'agent-line-task-one')
        prompt=self.parent_body['input'][0]['content'][0]['text']
        self.assertIn('memory_search',prompt);self.assertIn('agent_line_delegate',prompt)
        self.assertIn('不得改写任务数据里的精确文件内容',prompt)
        self.assertNotIn('加入实际召回且相关的经验事实',prompt)
        self.assertIn('没有给出精确文件内容',prompt)
        self.assertIn('正式约束原样放进唯一一次 Codex instruction',prompt)
        self.assertNotIn('不要把经验标记写进文件或交给 Codex 的 instruction',prompt)
        self.assertIn('仅约束协调角色本人，不得复制给 Codex',prompt)
        self.assertIn('Codex 执行角色应在固定受控工作区实际完成任务要求的文件操作和测试',prompt)
        self.assertEqual(prompt.split('【交给 Codex 的实际任务数据】\n',1)[1],'write assigned artifact')
        self.assertEqual(prompt[:50].strip(),'项目规则和相关经验')
        self.assertEqual(self.ledger.get('task-one')['parentTaskId'],'parent-native-one')
    def test_native_auto_recall_window_is_only_the_controlled_query(self):
        query='原生经验闭环-fce27c9a126f3581'
        self.submit(self.gateway(memory_query=query))
        prompt=self.parent_body['input'][0]['content'][0]['text']
        self.assertEqual(prompt[:50].strip(),query)
        self.assertIn('【协调角色使用的检索词】\n'+query,prompt)
        self.assertEqual(prompt.split('【交给 Codex 的实际任务数据】\n',1)[1],'write assigned artifact')
    def test_finished_parent_without_delegation_remains_unknown(self):
        gateway=self.gateway();self.submit(gateway);self.parent_finished=True
        self.assertEqual(gateway.observe('task-one')['status'],'unknown')
        self.assertFalse(gateway.observe('task-one')['resourcesReleased'])
    def test_parent_workspace_is_not_forwarded_as_executor_cwd(self):
        instruction='Only modify src/example.py within the explicitly authorized project scope.'
        self.submit(self.gateway(), instruction)
        prompt=self.parent_body['input'][0]['content'][0]['text']
        self.assertIn('协调角色的 workspace 不是 Codex 的 cwd',prompt)
        self.assertIn('不得猜测执行目录、把协调角色自己的绝对路径当成 Codex 的 cwd，或添加 cd',prompt)
        self.assertIn('绝对读取路径必须原样放进 instruction',prompt)
        self.assertIn('不能据此扩大写入、挂载或工作区',prompt)
        self.assertIn('相对文件路径',prompt)
        self.assertIn('服务端绑定的 cwd',prompt)
        self.assertIn('保留用户明确授权的项目范围',prompt)
        self.assertEqual(prompt.split('【交给 Codex 的实际任务数据】\n',1)[1],instruction)
    def test_terminal_task_is_reported_without_disguised_redelegation(self):
        self.submit(self.gateway())
        prompt=self.parent_body['input'][0]['content'][0]['text']
        self.assertIn('同一任务已到原生终态时只汇报',prompt)
        self.assertIn('不得重复调用 agent_line_delegate 假装业务重试',prompt)
        self.assertIn('不能绕过持久幂等身份',prompt)
    def test_concurrent_submit_sends_one_parent_task(self):
        gate=self.gateway()
        with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(lambda _:self.submit(gate),range(4)))
        self.assertEqual(self.calls.count(('POST','/api/console/chat/task')),1)
    def test_timeout_parent_receipt_after_delivery_never_reposted_on_restart(self):
        def handler(request):
            if request.method=='POST' and request.url.path=='/api/console/chat/task':
                self.calls.append(('POST',request.url.path));raise httpx.ReadTimeout('lost',request=request)
            return self.handler(request)
        self.submit(self.gateway(handler));reopened=RoleDispatchLedger(self.path)
        self.submit(self.gateway(ledger=reopened))
        self.assertEqual(self.calls.count(('POST','/api/console/chat/task')),1)
        self.assertIsNone(reopened.get('task-one')['parentTaskId'])
    def test_crash_after_intent_never_restarts_parent(self):
        def handler(request):
            if request.method=='POST':raise KeyboardInterrupt()
            return self.handler(request)
        with self.assertRaises(KeyboardInterrupt):self.submit(self.gateway(handler))
        self.submit(self.gateway(ledger=RoleDispatchLedger(self.path)))
        self.assertNotIn(('POST','/api/console/chat/task'),self.calls)
    def test_payload_and_role_conflicts_fail_before_http(self):
        gateway=self.gateway();self.submit(gateway);old=len(self.calls)
        with self.assertRaises(Conflict):self.submit(gateway,'different')
        with self.assertRaises(Conflict):self.submit(self.gateway(role='other-parent'))
        self.assertEqual(old,len(self.calls))
    def test_arbitrary_session_rejected(self):
        with self.assertRaises(ValueError):self.gateway().submit(task_id='task-one',role_session_id='attacker-session',instruction='x')
        self.assertEqual(self.calls,[])
    def test_native_identity_preserved_and_parent_metadata_separate(self):
        gateway=self.gateway();self.submit(gateway)
        self.native={'taskId':'task-one','status':'succeeded','identity':{'upstreamTaskId':'codex-native','codexThreadId':'thread','codexTurnId':'turn'},'terminalConfirmed':True,'resourcesReleased':True,'output':'actual'}
        result=gateway.observe('task-one')
        self.assertEqual(result['identity'],self.native['identity']);self.assertEqual(result['output'],'actual')
        self.assertEqual(result['parent']['parentTaskId'],'parent-native-one')

    def test_explicit_bind_rejection_is_not_replayed_until_corrected(self):
        mode = {'reject': True}
        def handler(request):
            if request.url.path.endswith('/execution-binding'):
                return self.handler(request)
            if request.url.path.endswith('/delegations/bind'):
                self.calls.append((request.method, request.url.path))
                if mode['reject']:
                    return httpx.Response(409, headers={'X-Agent-Line-Submission': 'rejected_unaccepted'})
                return httpx.Response(200, json={**json.loads(request.content), 'newBinding': True})
            return self.handler(request)
        gateway = self.gateway(handler)
        first = self.submit(gateway)
        self.assertEqual(first['status'], 'rejected')
        self.assertEqual(first['rejectionReason'], 'submission_rejected_unaccepted')
        self.assertIs(first['accepted'], False)
        self.assertEqual(first['identity'], {})
        self.assertFalse(first['terminalConfirmed'])
        self.assertEqual(self.ledger.get('task-one')['submissionStatus'], 'rejected_unaccepted')
        self.assertIsNone(self.ledger.get('task-one')['parentTaskId'])
        second = self.submit(gateway)
        self.assertEqual(second['status'], 'rejected')
        self.assertEqual(self.calls.count(('POST', '/api/agent-line/delegations/bind')), 1)
        self.assertNotIn(('POST', '/api/console/chat/task'), self.calls)
        self.assertIsNone(gateway.allow_corrected_resubmit('missing-task'))
        proof = gateway.allow_corrected_resubmit('task-one')
        self.assertIsNotNone(proof)
        mode['reject'] = False
        # The same handler object is already bound; flipping mode allows the corrected bind.
        third = gateway.submit(task_id='task-one', role_session_id='agent-line-task-one',
                               instruction='write assigned artifact',
                               role_revision='qwenpaw-role-v1:' + 'a' * 64,
                               resubmit_proof=proof)
        self.assertEqual(self.calls.count(('POST', '/api/agent-line/delegations/bind')), 2)
        self.assertEqual(self.calls.count(('POST', '/api/console/chat/task')), 1)
        self.assertNotEqual(third['status'], 'rejected')
        self.assertEqual(self.ledger.get('task-one')['parentTaskId'], 'parent-native-one')

    def test_bind_conflict_without_rejection_marker_stays_unknown(self):
        def handler(request):
            if request.url.path.endswith('/execution-binding'):
                return self.handler(request)
            if request.url.path.endswith('/delegations/bind'):
                self.calls.append(('POST', request.url.path))
                return httpx.Response(409, json={'detail': 'conflict'})
            return self.handler(request)
        result = self.submit(self.gateway(handler))
        self.assertEqual(result['status'], 'unknown')
        self.assertNotIn('rejectionReason', result)
        self.assertEqual(self.ledger.get('task-one')['submissionStatus'], 'intent')
        self.submit(self.gateway(handler, ledger=self.ledger))
        self.assertEqual(self.calls.count(('POST', '/api/agent-line/delegations/bind')), 1)

    def test_bind_failure_does_not_start_parent_or_retry_binding(self):
        calls=[]
        def handler(request):
            if request.url.path.endswith('/execution-binding'):return self.handler(request)
            calls.append(request.method);return httpx.Response(503,json={})
        gateway=self.gateway(handler);self.submit(gateway);self.submit(gateway)
        self.assertEqual(calls.count('POST'),1)
    def test_cancel_parent_and_bridge_but_missing_native_stays_unknown(self):
        gateway=self.gateway();self.submit(gateway);result=gateway.cancel('task-one')
        self.assertIn(('POST','/api/agent-line/delegations/task-one/cancel'),self.calls)
        self.assertIn(('POST','/api/agent-line/executions/task-one/cancel'),self.calls)
        self.assertFalse(result['resourcesReleased']);self.assertFalse(result['terminalConfirmed'])
        self.assertTrue(self.ledger.get('task-one')['cancelRequested'])
    def test_revocation_timeout_never_releases_even_if_codex_reports_exit(self):
        gateway=self.gateway();self.submit(gateway)
        self.native={'taskId':'task-one','status':'canceled','identity':{'upstreamTaskId':'native'},'terminalConfirmed':True,'resourcesReleased':True}
        def handler(request):
            if '/delegations/task-one/cancel' in request.url.path:raise httpx.ReadTimeout('lost',request=request)
            return self.handler(request)
        result=self.gateway(handler).cancel('task-one')
        self.assertFalse(result['resourcesReleased'])
        self.assertEqual(result['identity']['upstreamTaskId'],'native')
    def test_revoked_cancel_ack_is_not_container_exit(self):
        gateway=self.gateway();self.submit(gateway)
        self.native={'taskId':'task-one','status':'canceled','identity':{'upstreamTaskId':'native'},'terminalConfirmed':True,'resourcesReleased':True}
        result=gateway.cancel('task-one')
        self.assertTrue(result['parent']['delegationRevoked'])
        self.assertFalse(result['resourcesReleased'])
        self.assertFalse(result['terminalConfirmed'])
        observed=gateway.observe('task-one')
        self.assertTrue(observed['resourcesReleased'])
        self.assertTrue(observed['terminalConfirmed'])
        self.assertTrue(observed['parent']['delegationRevoked'])

    def test_observe_cancel_reconciles_revocation_and_native_exit_after_timeout(self):
        available=False
        def handler(request):
            if '/delegations/task-one/cancel' in request.url.path and not available:
                raise httpx.ReadTimeout('lost',request=request)
            return self.handler(request)
        gateway=self.gateway(handler);self.submit(gateway)
        self.native={'taskId':'task-one','status':'canceled','identity':{'upstreamTaskId':'native'},'terminalConfirmed':True,'resourcesReleased':True}
        self.assertFalse(gateway.cancel('task-one')['resourcesReleased'])
        available=True
        result=gateway.observe('task-one')
        self.assertTrue(result['resourcesReleased'])
        self.assertTrue(result['terminalConfirmed'])
        self.assertTrue(result['parent']['delegationRevoked'])
    def test_cancel_during_parent_post_is_rechecked_after_receipt(self):
        def handler(request):
            if request.method=='POST' and request.url.path=='/api/console/chat/task':
                self.ledger.request_cancel('task-one')
            return self.handler(request)
        result=self.submit(self.gateway(handler))
        self.assertIn(('POST','/api/agent-line/delegations/task-one/cancel'),self.calls)
        self.assertFalse(result['resourcesReleased'])
        self.assertEqual(self.calls.count(('POST','/api/console/chat/task')),1)
    def test_native_can_be_recovered_without_parent_receipt(self):
        def handler(request):
            if request.method=='POST' and request.url.path=='/api/console/chat/task':
                self.native={'taskId':'task-one','status':'accepted','identity':{'upstreamTaskId':'already-delegated'},'terminalConfirmed':False,'resourcesReleased':False}
                raise httpx.ReadTimeout('lost',request=request)
            return self.handler(request)
        result=self.submit(self.gateway(handler))
        self.assertEqual(result['identity']['upstreamTaskId'],'already-delegated')
        self.assertIsNone(result['parent']['parentTaskId'])
    def test_cancel_before_submit_prevents_future_parent_submission(self):
        gate=self.gateway();gate.cancel('task-one')
        result=self.submit(gate)
        self.assertFalse(result['resourcesReleased'])
        self.assertNotIn(('POST','/api/console/chat/task'),self.calls)

    def test_unstarted_cancel_fresh_proof_survives_ledger_restart(self):
        from agent_line.role_dispatch import UnstartedCancellationProof
        gate=self.gateway();self.submit(gate);self.parent_finished=True
        first=gate.cancel('task-one')
        proof=first['unstartedCancellationProof']
        self.assertIsInstance(proof,UnstartedCancellationProof)
        self.assertEqual(proof.as_dict()['parentTaskId'],'parent-native-one')
        restarted=self.gateway(ledger=RoleDispatchLedger(self.path))
        self.assertEqual(restarted.observe('task-one')['unstartedCancellationProof'],proof)
        self.assertEqual(self.calls.count(('POST','/api/console/chat/task')),1)

    def test_cached_finished_parent_never_replaces_live_lookup(self):
        gate=self.gateway();self.submit(gate);self.parent_finished=True;gate.observe('task-one')
        for status in (404,503):
            def handler(request):
                if request.method=='GET' and '/console/chat/task/' in request.url.path:
                    return httpx.Response(status,json={'status':'finished'})
                return self.handler(request)
            self.assertNotIn('unstartedCancellationProof',self.gateway(handler).cancel('task-one'))

    def test_claimed_or_native_identity_blocks_absence_proof(self):
        gate=self.gateway();self.submit(gate);self.parent_finished=True
        def handler(request):
            if '/delegations/task-one/cancel' in request.url.path:
                return httpx.Response(200,json={'taskId':'task-one','delegationRevoked':True,'executionClaimed':True})
            return self.handler(request)
        self.assertNotIn('unstartedCancellationProof',self.gateway(handler).cancel('task-one'))
        self.native={'taskId':'task-one','identity':{'upstreamTaskId':'native'},'status':'accepted'}
        self.assertNotIn('unstartedCancellationProof',gate.cancel('task-one'))

    def test_parent_failure_cannot_replace_configuration_block(self):
        mode = {"value": "authentication"}
        def handler(request):
            if request.url.path == "/api/agent-line/executions/task-one":
                if mode["value"] == "authentication":
                    return httpx.Response(401)
                return httpx.Response(409, headers={"X-Agent-Line-Configuration": "model_unavailable"})
            if request.url.path == "/api/console/chat/task/parent-native-one":
                return httpx.Response(429, headers={"Retry-After": "9"})
            return self.handler(request)
        gateway = self.gateway(handler)
        self.submit(gateway)
        authenticated = gateway.observe("task-one")
        self.assertEqual(authenticated["pollHint"], "authentication")
        self.assertNotIn("pollAfterSeconds", authenticated)
        mode["value"] = "model"
        missing = gateway.observe("task-one")
        self.assertEqual(missing["pollHint"], "model_missing")
        self.assertNotIn("pollAfterSeconds", missing)

    def test_live_parent_timeout_keeps_absence_unknown(self):
        gate=self.gateway();self.submit(gate);self.parent_finished=True
        def handler(request):
            if request.method=='GET' and '/console/chat/task/' in request.url.path:
                raise httpx.ReadTimeout('lost',request=request)
            return self.handler(request)
        self.assertNotIn('unstartedCancellationProof',self.gateway(handler).cancel('task-one'))

    def test_mismatched_native_identity_cannot_be_normalized_into_absence(self):
        gate=self.gateway();self.submit(gate);self.parent_finished=True
        for identity in ({'unexpected':'id'}, 'malformed-id'):
            self.native={'taskId':'wrong-task','identity':identity}
            self.assertNotIn('unstartedCancellationProof',gate.cancel('task-one'))

    def test_revocation_claim_missing_or_non_boolean_never_proves_absence(self):
        gate=self.gateway();self.submit(gate);self.parent_finished=True
        for claimed in (None,0,'false'):
            def handler(request):
                if '/delegations/task-one/cancel' in request.url.path:
                    return httpx.Response(200,json={'taskId':'task-one','delegationRevoked':True,'executionClaimed':claimed})
                return self.handler(request)
            self.assertNotIn('unstartedCancellationProof',self.gateway(handler).cancel('task-one'))

    def test_parent_identity_mismatch_and_native_timeout_keep_lease(self):
        gate=self.gateway();self.submit(gate);self.parent_finished=True
        for failure in ('parent-mismatch','native-timeout'):
            def handler(request):
                if failure=='parent-mismatch' and request.method=='GET' and '/console/chat/task/' in request.url.path:
                    return httpx.Response(200,json={'task_id':'wrong-parent','status':'finished'})
                if failure=='native-timeout' and '/executions/task-one/cancel' in request.url.path:
                    raise httpx.ReadTimeout('lost',request=request)
                return self.handler(request)
            self.assertNotIn('unstartedCancellationProof',self.gateway(handler).cancel('task-one'))


if __name__=='__main__':unittest.main()
