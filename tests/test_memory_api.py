import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from agent_line.adapters.qwenpaw_memory import MemoryApproval, QwenPawMemoryClient, content_hash
from agent_line.approval_api import ApprovalAccess
from agent_line.approvals import TrustedPrincipal
from agent_line.memory import MemoryCommitStore, MemoryCoordinator
from agent_line.memory_api import MemoryAPI, MemoryReviewLedger
from agent_line.service import ServiceConfig, create_app
from agent_line.store import AdmissionClosed, TaskStore


class MemoryAPITests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.tasks = TaskStore.initialize(self.root / 'tasks.sqlite')
        self.commits = MemoryCommitStore.initialize(self.root / 'memory.sqlite')
        self.reviews = MemoryReviewLedger.initialize(self.root / 'reviews.sqlite')
        self.principal = TrustedPrincipal('reviewer', frozenset({'project'}),
                                          frozenset({'approve', 'review_memory'}), 2000)
        self.access = ApprovalAccess({
            'human-token': self.principal,
            'other-token': replace(self.principal, subject='other', project_scopes=frozenset({'other'})),
            'approval-only': replace(self.principal, subject='approver', capabilities=frozenset({'approve'})),
            'expired-token': replace(self.principal, subject='expired', expires_at=900),
        }, clock=lambda: 1000)
        self.remote = {}
        self.puts = []
        self.reads = []
        self.lose_write = False
        self.rejection_status = None
        self.guarded = False
        self.before_put = lambda: None

        def transport(request):
            key = (request.headers['X-Agent-Id'], request.url.path)
            if request.method == 'PUT':
                self.before_put()
                self.puts.append(request)
                if self.rejection_status is not None:
                    return httpx.Response(self.rejection_status, json={'detail': 'fixture rejection'})
                if self.lose_write:
                    raise httpx.ReadTimeout('private-host-token/private/path')
                self.remote[key] = json.loads(request.content)['content']
                return httpx.Response(200, json={'written': True})
            self.reads.append(request)
            return (httpx.Response(200, json={'content': self.remote[key]})
                    if key in self.remote else httpx.Response(404))

        self.http = httpx.Client(transport=httpx.MockTransport(transport))
        self.addCleanup(self.http.close)
        native = QwenPawMemoryClient('http://127.0.0.1:3194', 'private-host-token',
                                    role_agent_id='role', client=self.http,
                                    native_write_rejections=True)
        self.coordinator = MemoryCoordinator(self.commits, native)
        self.bindings = {'binding': {'projectId': 'project', 'roleBindingId': 'role-binding', 'roleAgentId': 'role'}}

        @contextmanager
        def admission():
            if self.guarded:
                raise AdmissionClosed('maintenance')
            yield

        self.api = MemoryAPI(self.access, {'binding': self.coordinator}, self.reviews,
                             self.tasks, bindings=self.bindings, admission=admission)
        self.config = ServiceConfig(tokens={'service-token': {'project'}},
                                    projects={'project': {'workspace': str(self.root)}}, bindings={})
        self.client = TestClient(create_app(self.tasks, self.config, memories=self.api))
        self.addCleanup(self.client.close)
        self.task = self.make_task('first')

    def make_task(self, key, *, project='project', role_binding='role-binding', role='role', terminal=True, released=True,
                  output=None, extra_evidence=None):
        workspace = str(self.root / key)
        task, _ = self.tasks.register(key, {
            'projectId': project, 'roleBindingId': role_binding, 'roleAgentId': role,
            'workspace': workspace, 'workflow': {'id': 'workflow', 'runId': key, 'nodeExecutionId': 'node', 'attemptId': 'one'}, 'input': {}})
        identity = {'upstreamTaskId': key, 'codexThreadId': 'thread-' + key, 'codexTurnId': 'turn-' + key}
        self.tasks.claim(task['id'], workspace)
        self.tasks.accept(task['id'], identity)
        if terminal:
            self.tasks.finish(task['id'], 'succeeded', output if output is not None else {'text': 'Verified output'}, [
                {'kind': 'execution_terminal', 'executionId': identity['codexTurnId'], 'confirmed': True},
                *(extra_evidence or [])])
            if released:
                self.tasks.confirm_resources_released(task['id'], identity['codexTurnId'], {
                    'source': 'docker-inspect-before-remove', 'workspace': workspace,
                    'owner': 'a' * 32, 'containerId': 'b' * 64, 'running': False,
                    'pid': 0, 'status': 'exited', 'finishedAt': '2026-10-21T00:00:00Z', 'identities': [identity]})
        return self.tasks.get(task['id'])

    def release_task(self, task):
        identity = task['executionIdentity']
        turn_id = identity['codexTurnId']
        if task['executionStatus'] not in ('succeeded', 'failed', 'canceled'):
            self.tasks.finish(task['id'], 'succeeded', task['output'], [
                {'kind': 'execution_terminal', 'executionId': turn_id, 'confirmed': True}])
        if not task['resourcesReleased']:
            workspace = task['payload']['workspace']
            self.tasks.confirm_resources_released(task['id'], turn_id, {
                'source': 'docker-inspect-before-remove', 'workspace': workspace,
                'owner': 'a' * 32, 'containerId': 'b' * 64, 'running': False,
                'pid': 0, 'status': 'exited', 'finishedAt': '2026-10-21T00:00:00Z',
                'identities': [identity]})

    def request(self, method='get', path='', *, token='human-token', **kwargs):
        return self.client.request(method, '/extension/v1/memories' + path,
                                   headers={'Authorization': 'Bearer ' + token}, **kwargs)

    def review(self, *, task=None, body=None, token='human-token'):
        task = task or self.task
        return self.request('post', '/binding/' + task['id'] + '/review', token=token,
                            json=body or {'decisionId': 'decision', 'content': '经人工核实的项目事实'})

    def audit_count(self):
        with sqlite3.connect(self.root / 'reviews.sqlite') as db:
            return db.execute('SELECT count(*) FROM memory_reviews').fetchone()[0]

    def test_independent_credentials_capability_and_expiry(self):
        for token, code in [('service-token', 401), ('invalid', 401), ('approval-only', 403), ('expired-token', 403)]:
            self.assertEqual(self.review(token=token).status_code, code)
        self.assertEqual(self.puts, [])

    def test_scope_and_all_task_role_identity_fields_are_enforced(self):
        self.assertEqual(self.review(token='other-token').status_code, 404)
        for fields in ({'project': 'other'}, {'role_binding': 'other'}, {'role': 'other'}):
            task = self.make_task('mismatch-' + next(iter(fields)), **fields)
            self.assertEqual(self.review(task=task).status_code, 404)
        self.assertEqual(self.puts, [])

    def test_nonterminal_and_unreleased_reject_without_audit(self):
        before = self.audit_count()
        for key, options in [('running', {'terminal': False}), ('unreleased', {'released': False})]:
            task = self.make_task(key, **options)
            self.assertEqual(self.review(task=task).status_code, 409)
            self.release_task(task)
        self.assertEqual(self.audit_count(), before)
        self.assertEqual(self.puts, [])

    def test_review_audit_precedes_real_adapter_put_and_retry_is_idempotent(self):
        before = self.audit_count()
        self.before_put = lambda: self.assertGreater(self.audit_count(), before)
        first = self.review()
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(first.json()['actor'], 'reviewer')
        self.assertEqual(first.json()['projectId'], 'project')
        self.assertEqual(first.json()['taskId'], self.task['id'])
        self.assertEqual(first.json()['source']['workflow'], self.task['payload']['workflow'])
        self.assertEqual(first.json()['source']['executionStatus'], 'succeeded')
        self.assertEqual(self.commits.get('role', self.task['id'])['status'], 'committed')
        current = self.tasks.get(self.task['id'])
        self.assertEqual(current['executionStatus'], self.task['executionStatus'])
        self.assertEqual(current['evidence'], self.task['evidence'])
        self.assertEqual(current['memoryStatus'], 'committed')
        self.assertEqual(self.review().status_code, 202)
        self.assertEqual(len(self.puts), 1)
        self.assertEqual(self.puts[0].headers['X-Agent-Id'], 'role')
        self.assertEqual(self.puts[0].url.params['section'], 'digest')

    def test_actor_source_and_project_injection_are_forbidden(self):
        for field in ('actor', 'projectId', 'roleAgentId', 'sourceTaskId'):
            body = {'decisionId': 'decision', 'content': '事实', field: 'forged'}
            self.assertEqual(self.review(body=body).status_code, 422)
        self.assertEqual(self.puts, [])

    def test_maintenance_makes_no_audit_and_no_upstream_call(self):
        before = self.audit_count()
        self.guarded = True
        self.assertEqual(self.review().status_code, 503)
        self.assertEqual(self.audit_count(), before)
        self.assertEqual(self.puts + self.reads, [])

    def test_content_or_decision_replacement_is_conflict(self):
        self.assertEqual(self.review().status_code, 202)
        for body in ({'decisionId': 'new', 'content': '经人工核实的项目事实'},
                     {'decisionId': 'decision', 'content': '替换内容'}):
            self.assertEqual(self.review(body=body).status_code, 409)
        self.assertEqual(len(self.puts), 1)

    def test_uncertain_write_only_reconciles_on_repeat(self):
        self.lose_write = True
        response = self.review()
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(self.commits.get('role', self.task['id'])['status'], 'pending')
        self.assertEqual(self.review().status_code, 202)
        self.assertEqual(len(self.puts), 1)
        self.assertNotIn('private-host-token', response.text)

    def test_service_token_overlap_rejected_at_app_start(self):
        with self.assertRaises(ValueError):
            create_app(self.tasks, replace(self.config, tokens={'human-token': {'project'}}), memories=self.api)

    def test_detail_and_list_hide_cross_scope_and_private_material(self):
        self.assertEqual(self.review().status_code, 202)
        detail = self.request(path='/binding/' + self.task['id'])
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(self.request(path='/binding/' + self.task['id'], token='other-token').status_code, 404)
        listing = self.request(params={'limit': 1})
        self.assertEqual(listing.status_code, 200, listing.text)
        for response in (detail, listing):
            self.assertNotIn('private-host-token', response.text)
            self.assertNotIn(str(self.root), response.text)
        self.assertEqual(self.request(params={'limit': 0}).status_code, 422)

    def test_scoped_list_pagination_covers_each_review_once(self):
        second = self.make_task('second')
        self.assertEqual(self.review().status_code, 202)
        self.assertEqual(self.review(task=second, body={'decisionId': 'second-decision', 'content': '另一条事实'}).status_code, 202)
        first_page = self.request(params={'limit': 1}).json()
        self.assertEqual(len(first_page['memories']), 1)
        self.assertIsInstance(first_page['nextCursor'], str)
        second_page = self.request(params={'limit': 1, 'after': first_page['nextCursor']}).json()
        self.assertEqual(len(second_page['memories']), 1)
        self.assertIsNone(second_page['nextCursor'])
        self.assertNotEqual(first_page['memories'], second_page['memories'])
        self.assertEqual(self.request(token='other-token').json()['memories'], [])

    def test_revocation_after_initial_auth_prevents_review_audit_and_put(self):
        @contextmanager
        def revoked_admission():
            self.access.revoke('human-token')
            yield
        self.api.admission = revoked_admission
        before = self.audit_count()
        self.assertEqual(self.review().status_code, 403)
        self.assertEqual(self.audit_count(), before)
        self.assertEqual(self.puts + self.reads, [])

    def test_late_pending_snapshot_cannot_regress_persisted_commit(self):
        original = self.coordinator.commit

        def stale_result(task_id):
            self.assertEqual(original(task_id)['status'], 'committed')
            return {'status': 'pending'}

        self.coordinator.commit = stale_result
        response = self.review()
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()['status'], 'committed')
        task = self.tasks.get(self.task['id'])
        self.assertEqual(task['memoryStatus'], 'committed')
        self.assertEqual(task['executionStatus'], self.task['executionStatus'])
        self.assertEqual(task['evidence'], self.task['evidence'])
        self.assertEqual(len(self.puts), 1)

    def reconcile(self, *, token='human-token', task=None, body=None):
        task=task or self.task
        return self.request('post','/binding/'+task['id']+'/reconcile',token=token,json={} if body is None else body)

    def retry(self, *, token='human-token', task=None, expected_version=None, body=None):
        task=task or self.task
        if body is None:
            if expected_version is None:
                expected_version=self.commits.get('role',task['id'])['version']
            body={'expectedVersion':expected_version}
        return self.request('post','/binding/'+task['id']+'/retry',token=token,json=body)

    def seed_failed_submission(self, task, *, decision_id='seeded-decision'):
        content='人工批准内容 '+task['id'];digest=content_hash(content)
        self.reviews.reserve(project_id='project',role_id='role',task_id=task['id'],actor='reviewer',
            decision_id=decision_id,content_sha256=digest,source=self.api._source(task))
        self.coordinator.approve(task['id'],content,MemoryApproval(task['id'],digest,decision_id))
        mode,row=self.commits.claim('role',task['id'])
        self.assertEqual(mode,'write')
        self.assertTrue(self.commits.record('role',task['id'],row['version'],
            {'status':'failed','reason':'official_write_rejected'}))
        return self.commits.get('role',task['id'])

    def register_review_without_submission(self):
        content='经人工核实的项目事实';digest=content_hash(content)
        record=self.reviews.reserve(project_id='project',role_id='role',task_id=self.task['id'],actor='reviewer',
            decision_id='decision',content_sha256=digest,source=self.api._source(self.task))
        self.coordinator.approve(self.task['id'],content,MemoryApproval(self.task['id'],digest,'decision'))
        return record

    def test_readonly_reconcile_pending_uses_real_adapter_get_and_preserves_execution_evidence(self):
        self.lose_write=True;self.assertEqual(self.review().status_code,202)
        before=self.tasks.get(self.task['id']);audits=self.audit_count();puts=len(self.puts);reads=len(self.reads)
        self.remote[('role',self.reads[-1].url.path)]='经人工核实的项目事实'
        result=self.reconcile()
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['status'],'committed')
        self.assertEqual(result.json(),self.request(path='/binding/'+self.task['id']).json())
        self.assertEqual(len(self.reads),reads+1);self.assertEqual(len(self.puts),puts)
        self.assertEqual(self.reads[-1].method,'GET');self.assertEqual(self.reads[-1].headers['X-Agent-Id'],'role')
        self.assertEqual(self.reads[-1].url.params['section'],'digest')
        after=self.tasks.get(self.task['id'])
        for key in ('executionIdentity','executionStatus','verificationStatus','evidence','output'):
            self.assertEqual(after[key],before[key])
        self.assertEqual(after['memoryStatus'],'committed');self.assertEqual(self.audit_count(),audits)

    def test_explicit_retry_after_definite_rejection_preserves_task_and_rejects_replay(self):
        self.rejection_status=403
        rejected=self.review()
        self.assertEqual(rejected.status_code,202,rejected.text)
        self.assertEqual(rejected.json()['status'],'failed')
        self.assertEqual(rejected.json()['reason'],'official_write_rejected')
        version=rejected.json()['commitVersion']
        self.assertEqual(self.tasks.get(self.task['id'])['memoryStatus'],'failed')
        before=self.tasks.get(self.task['id'])
        self.rejection_status=None
        retried=self.retry(expected_version=version)
        self.assertEqual(retried.status_code,200,retried.text)
        self.assertEqual(retried.json()['status'],'committed')
        after=self.tasks.get(self.task['id'])
        for key in ('executionIdentity','executionStatus','verificationStatus','evidence','output'):
            self.assertEqual(after[key],before[key])
        self.assertEqual(after['memoryStatus'],'committed')
        self.assertEqual(len(self.puts),2)
        replay=self.retry(expected_version=version)
        self.assertEqual(replay.status_code,409)
        self.assertEqual(len(self.puts),2)

    def test_retry_rejects_unknown_stale_unauthorized_and_guarded_attempts_without_put(self):
        self.lose_write=True
        self.assertEqual(self.review().json()['status'],'pending')
        self.assertEqual(self.retry().status_code,409)
        self.assertEqual(len(self.puts),1)

        self.rejection_status=403
        second=self.make_task('retry-guarded')
        self.assertEqual(self.review(task=second,body={
            'decisionId':'retry-guarded-decision','content':'经人工核实的项目事实'}).json()['status'],'failed')
        version=self.commits.get('role',second['id'])['version']
        for token,code in [('approval-only',403),('other-token',404)]:
            self.assertEqual(self.retry(task=second,token=token,expected_version=version).status_code,code)
        self.guarded=True
        self.assertEqual(self.retry(task=second,expected_version=version).status_code,503)
        self.guarded=False
        self.assertEqual(self.retry(task=second,expected_version=version-1).status_code,409)
        self.assertEqual(len(self.puts),2)

    def test_retry_requires_current_terminal_released_task_and_frozen_review_source(self):
        for key,options in [('retry-running',{'terminal':False}),('retry-unreleased',{'released':False})]:
            task=self.make_task(key,**options)
            row=self.seed_failed_submission(task,decision_id=key+'-decision')
            self.assertEqual(self.retry(task=task,expected_version=row['version']).status_code,409)
            self.release_task(task)
        task=self.make_task('retry-source-tampered')
        row=self.seed_failed_submission(task,decision_id='retry-source-decision')
        with self.reviews.connect() as db:
            db.execute('UPDATE memory_reviews SET source=?',
                       (json.dumps({'workflow':{'id':'forged'},'executionStatus':'succeeded',
                                    'verificationStatus':'pending','evidenceSha256':'0'*64}),))
        self.assertEqual(self.retry(task=task,expected_version=row['version']).status_code,409)
        self.assertEqual(self.puts+self.reads,[])

    def test_retry_rechecks_revocation_after_waiting_for_role_lock(self):
        row=self.seed_failed_submission(self.task)
        original=self.commits.memory_scope
        @contextmanager
        def revoke_on_lock(*args,**kwargs):
            with original(*args,**kwargs):
                self.access.revoke('human-token')
                yield
        with patch.object(self.commits,'memory_scope',new=revoke_on_lock):
            result=self.retry(expected_version=row['version'])
        self.assertEqual(result.status_code,403)
        self.assertEqual(self.puts+self.reads,[])

    def test_readonly_reconcile_after_app_rebuild_and_repeat_does_not_add_versions(self):
        self.register_review_without_submission();self.commits.claim('role',self.task['id'])
        path=self.coordinator.client.base_url+self.coordinator.client._path(self.task['id'])
        self.remote[('role',httpx.URL(path).path)]='经人工核实的项目事实'
        rebuilt=MemoryAPI(self.access,{'binding':MemoryCoordinator(MemoryCommitStore(self.commits.path),self.coordinator.client)},
            MemoryReviewLedger(self.reviews.path),self.tasks,bindings=self.bindings,admission=self.api.admission)
        with TestClient(create_app(self.tasks,self.config,memories=rebuilt)) as client:
            target='/extension/v1/memories/binding/'+self.task['id']+'/reconcile'
            first=client.post(target,headers={'Authorization':'Bearer human-token'},json={})
            self.assertEqual(first.status_code,200,first.text)
            memory=self.commits.get('role',self.task['id']);task=self.tasks.get(self.task['id'])
            repeated=client.post(target,headers={'Authorization':'Bearer human-token'},json={})
        self.assertEqual(first.json(),repeated.json())
        self.assertEqual(self.commits.get('role',self.task['id']),memory)
        self.assertEqual(self.tasks.get(self.task['id']),task)
        self.assertEqual(self.puts,[])

    def test_readonly_reconcile_approved_never_does_first_put_or_creates_review(self):
        self.register_review_without_submission();before=self.commits.get('role',self.task['id']);count=self.audit_count()
        response=self.reconcile();self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['status'],'approved')
        self.assertEqual(self.commits.get('role',self.task['id']),before)
        self.assertEqual(self.puts+self.reads,[]);self.assertEqual(self.audit_count(),count)

    def test_readonly_reconcile_requires_existing_review_and_commit(self):
        self.assertEqual(self.reconcile().status_code,404)
        self.register_review_without_submission()
        with self.commits.connect() as db:db.execute('DELETE FROM memory_commits')
        self.assertEqual(self.reconcile().status_code,404)
        self.assertEqual(self.puts+self.reads,[])

    def test_readonly_reconcile_rejects_body_injection_and_checks_authorization(self):
        self.register_review_without_submission()
        for body in ({'content':'replacement'},{'decisionId':'another'},{'actor':'other'}, {'roleAgentId':'other'}, {'approve':True},[]):
            self.assertEqual(self.reconcile(body=body).status_code,422)
        for token,code in [('service-token',401),('invalid',401),('approval-only',403),('expired-token',403),('other-token',404)]:
            self.assertEqual(self.reconcile(token=token).status_code,code)
        self.assertEqual(self.puts+self.reads,[])

    def test_readonly_reconcile_maintenance_and_revocation_prevent_native_get(self):
        self.register_review_without_submission();self.commits.claim('role',self.task['id'])
        self.guarded=True;self.assertEqual(self.reconcile().status_code,503)
        self.guarded=False
        @contextmanager
        def revoked():
            self.access.revoke('human-token');yield
        self.api.admission=revoked
        self.assertEqual(self.reconcile().status_code,403)
        self.assertEqual(self.puts+self.reads,[])

    def test_readonly_reconcile_conflicting_stored_review_or_content_never_reads_upstream(self):
        self.register_review_without_submission();self.commits.claim('role',self.task['id'])
        with self.commits.connect() as db:before=dict(db.execute('SELECT * FROM memory_commits').fetchone())
        for column,value in [('content','tampered'),('content_hash','0'*64),('decision_id','other')]:
            with self.commits.connect() as db:db.execute('UPDATE memory_commits SET '+column+'=?',(value,))
            self.assertEqual(self.reconcile().status_code,409)
            with self.commits.connect() as db:db.execute('UPDATE memory_commits SET '+column+'=?',(before[column],))
        with self.reviews.connect() as db:db.execute('UPDATE memory_reviews SET source=?',(json.dumps({'workflow':{}}),))
        self.assertEqual(self.reconcile().status_code,409)
        self.assertEqual(self.puts+self.reads,[])

    def test_readonly_reconcile_remote_conflict_is_visible_without_overwrite(self):
        self.register_review_without_submission();self.commits.claim('role',self.task['id'])
        path=self.coordinator.client.base_url+self.coordinator.client._path(self.task['id'])
        self.remote[('role',httpx.URL(path).path)]='other native memory'
        result=self.reconcile();self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['status'],'needs_review');self.assertEqual(self.puts,[])

    def test_readonly_reconcile_deletion_tombstone_never_reads_or_restores_body(self):
        self.assertEqual(self.review().status_code,202)
        row=self.commits.get('role',self.task['id'])
        self.commits.reserve_delete('role',self.task['id'],'reviewer','delete',row['content_hash'])
        reads=len(self.reads);puts=len(self.puts)
        result=self.reconcile();self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['status'],'deleting');self.assertEqual(result.json()['content'],'')
        self.assertEqual(len(self.reads),reads);self.assertEqual(len(self.puts),puts)
        self.assertEqual(self.tasks.get(self.task['id'])['memoryStatus'],'deleting')

    def test_readonly_reconcile_uses_latest_ledger_for_task_projection(self):
        self.register_review_without_submission();self.commits.claim('role',self.task['id'])
        original=self.coordinator.reconcile
        path=self.coordinator.client.base_url+self.coordinator.client._path(self.task['id'])
        self.remote[('role',httpx.URL(path).path)]='经人工核实的项目事实'
        def stale(task):
            self.assertEqual(original(task)['status'],'committed')
            return {'status':'pending'}
        self.coordinator.reconcile=stale
        result=self.reconcile();self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['status'],'committed')
        self.assertEqual(self.tasks.get(self.task['id'])['memoryStatus'],'committed')

    def test_readonly_reconcile_keeps_original_review_after_later_task_verification(self):
        record=self.register_review_without_submission();self.commits.claim('role',self.task['id'])
        latest=self.tasks.get(self.task['id'])
        self.tasks.record_verification(self.task['id'],{'kind':'verification','taskId':self.task['id'],
            'executionIdentity':latest['executionIdentity'],'status':'passed','checks':[{'status':'passed'}]})
        path=self.coordinator.client.base_url+self.coordinator.client._path(self.task['id'])
        self.remote[('role',httpx.URL(path).path)]='经人工核实的项目事实'
        result=self.reconcile();self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['status'],'committed')
        self.assertEqual(result.json()['source'],record['source'])
        self.assertEqual(self.tasks.get(self.task['id'])['verificationStatus'],'passed')
        self.assertEqual(self.reviews.get('role',self.task['id']),record)

    def test_readonly_reconcile_scope_mismatches_cannot_observe_another_task(self):
        for fields in ({'project':'other'},{'role_binding':'other'},{'role':'other'}):
            task=self.make_task('reconcile-mismatch-'+next(iter(fields)),**fields)
            self.assertEqual(self.reconcile(task=task).status_code,404)
        self.assertEqual(self.puts+self.reads,[])

    def test_readonly_reconcile_rechecks_authority_after_role_lock_wait(self):
        self.register_review_without_submission();self.commits.claim('role',self.task['id'])
        original=self.commits.memory_scope
        @contextmanager
        def revoke_on_lock(*args,**kwargs):
            with original(*args,**kwargs):
                self.access.revoke('human-token')
                yield
        with patch.object(self.commits,'memory_scope',new=revoke_on_lock):
            self.assertEqual(self.reconcile().status_code,403)
        self.assertEqual(self.puts+self.reads,[])

    def test_readonly_reconcile_completed_deletion_and_blocked_duplicate_never_resurrect(self):
        self.assertEqual(self.review().status_code,202)
        original=self.commits.get('role',self.task['id'])
        second=self.make_task('duplicate-pending');content='经人工核实的项目事实'
        self.reviews.reserve(project_id='project',role_id='role',task_id=second['id'],actor='reviewer',
            decision_id='duplicate-review',content_sha256=original['content_hash'],source=self.api._source(second))
        self.coordinator.approve(second['id'],content,MemoryApproval(second['id'],original['content_hash'],'duplicate-review'))
        self.commits.claim('role',second['id'])
        self.commits.reserve_delete('role',self.task['id'],'reviewer','delete',original['content_hash'])
        self.commits.record_deletion('role',self.task['id'],{'roleAgentId':'role','taskId':self.task['id'],
            'contentHash':original['content_hash'],'status':'deleted','fileAbsent':True,'indexSourceAbsent':True})
        reads=len(self.reads);puts=len(self.puts)
        deleted=self.reconcile();self.assertEqual(deleted.status_code,200,deleted.text)
        self.assertEqual(deleted.json()['status'],'deleted');self.assertEqual(deleted.json()['content'],'')
        blocked=self.reconcile(task=second);self.assertEqual(blocked.status_code,200,blocked.text)
        self.assertEqual(blocked.json()['status'],'needs_review');self.assertEqual(blocked.json()['content'],'')
        self.assertEqual(len(self.reads),reads);self.assertEqual(len(self.puts),puts)

    def test_repeated_committed_review_does_not_churn_task_version_or_events(self):
        self.assertEqual(self.review().status_code, 202)
        before = self.tasks.get(self.task['id'])
        before_events = self.tasks.events(self.task['id'])
        with self.commits.connect() as db:
            commit_events = list(db.execute('SELECT * FROM memory_events ORDER BY seq'))
        for _ in range(3):
            response = self.review()
            self.assertEqual(response.status_code, 202, response.text)
            self.assertEqual(response.json()['status'], 'committed')
        self.assertEqual(self.tasks.get(self.task['id']), before)
        self.assertEqual(self.tasks.events(self.task['id']), before_events)
        with self.commits.connect() as db:
            self.assertEqual(list(db.execute('SELECT * FROM memory_events ORDER BY seq')), commit_events)
        self.assertEqual(len(self.puts), 1)

    def test_legacy_conflicting_commit_is_needs_review_without_new_put(self):
        legacy_content = '历史审核的不同内容'
        self.coordinator.approve(self.task['id'], legacy_content, MemoryApproval(
            self.task['id'], content_hash(legacy_content), 'legacy-decision'))
        self.assertEqual(self.coordinator.commit(self.task['id'])['status'], 'committed')
        puts_before = len(self.puts)
        response = self.review()
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()['status'], 'needs_review')
        self.assertEqual(self.tasks.get(self.task['id'])['memoryStatus'], 'needs_review')
        detail = self.request(path='/binding/' + self.task['id'])
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()['status'], 'needs_review')
        self.assertNotEqual(detail.json().get('content'), legacy_content)
        self.assertEqual(self.request().json()['memories'][0]['status'], 'needs_review')
        self.assertEqual(self.commits.get('role', self.task['id'])['content'], legacy_content)
        self.assertEqual(len(self.puts), puts_before)
        self.assertEqual(self.tasks.get(self.task['id'])['evidence'], self.task['evidence'])

    def test_binding_catalog_only_exposes_authorized_identity_fields(self):
        self.api.bindings['binding'].update({
            'baseURL': 'http://private-host', 'hostCredentialsFile': '/private/credentials',
            'commitLedgerPath': '/private/memory.sqlite'})
        response = self.request(path='/bindings')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {'bindings': [{
            'bindingId': 'binding', 'projectId': 'project',
            'roleBindingId': 'role-binding', 'roleAgentId': 'role',
            'memoryDeletion': {'configured': False, 'canRequest': False}}]})
        self.assertEqual(self.request(path='/bindings', token='other-token').json(), {'bindings': []})
        self.assertNotIn('private', response.text)

    def test_source_pages_filter_project_and_both_role_fields(self):
        self.make_task('source-other-project', project='other')
        self.make_task('source-other-binding', role_binding='other')
        self.make_task('source-other-role', role='other')
        active = self.make_task('source-active', terminal=False)
        cursor, seen_cursors, rows = '', set(), []
        for _ in range(10):
            response = self.request(path='/sources', params={
                'bindingId': 'binding', 'after': cursor, 'limit': 1})
            self.assertEqual(response.status_code, 200, response.text)
            page = response.json()
            self.assertLessEqual(len(page['tasks']), 1)
            rows.extend(page['tasks'])
            self.assertNotIn(str(self.root), response.text)
            self.assertNotIn('Verified output', response.text)
            for row in page['tasks']:
                self.assertEqual(set(row), {'taskId', 'executionStatus', 'verificationStatus',
                                           'resourcesReleased', 'memoryStatus', 'workflowRunId'})
            cursor = page['nextCursor']
            if cursor is None:
                break
            self.assertNotIn(cursor, seen_cursors)
            seen_cursors.add(cursor)
        else:
            self.fail('Source pagination did not terminate')
        self.assertEqual({row['taskId'] for row in rows}, {self.task['id'], active['id']})
        self.assertEqual(len(rows), 2)
        statuses = {row['taskId']: row['executionStatus'] for row in rows}
        self.assertEqual(statuses[self.task['id']], 'succeeded')
        self.assertEqual(statuses[active['id']], 'accepted')

    def test_source_scope_errors_and_revocation_do_not_query_tasks(self):
        with patch.object(self.tasks, 'list_tasks', side_effect=AssertionError('unauthorized query')) as listing:
            for binding, token in [('binding', 'other-token'), ('missing', 'human-token')]:
                response = self.request(path='/sources', token=token, params={'bindingId': binding})
                self.assertEqual(response.status_code, 404)
            self.access.revoke('human-token')
            for path, params in [('/bindings', {}), ('/sources', {'bindingId': 'binding'})]:
                self.assertEqual(self.request(path=path, params=params).status_code, 401)
            listing.assert_not_called()

    def test_source_pagination_validation_does_not_write(self):
        for params in ({}, {'bindingId': 'binding', 'limit': 0},
                       {'bindingId': 'binding', 'limit': 101}):
            self.assertEqual(self.request(path='/sources', params=params).status_code, 422)
        self.assertEqual(self.puts + self.reads, [])

    def test_source_detail_reads_actual_evidence_without_mutation_or_host_path(self):
        def databases():
            snapshots = []
            for name in ('tasks.sqlite', 'memory.sqlite', 'reviews.sqlite'):
                with sqlite3.connect(self.root / name) as db:
                    snapshots.append(tuple(db.iterdump()))
            return snapshots
        before = databases()
        response = self.request(path='/sources/binding/' + self.task['id'])
        self.assertEqual(response.status_code, 200, response.text)
        detail = response.json()
        self.assertEqual(json.loads(detail['outputPreview']), self.task['output'])
        expected = json.loads(json.dumps(self.task['evidence']))
        for item in expected:
            if item.get('kind') == 'resource_exit':
                item['receipt'].pop('workspace')
        self.assertEqual(json.loads(detail['evidencePreview']), expected)
        self.assertFalse(detail['outputTruncated'])
        self.assertFalse(detail['evidenceTruncated'])
        self.assertTrue(detail['resourcesReleased'])
        self.assertNotIn(str(self.root), response.text)
        self.assertNotIn('private-host-token', response.text)
        self.assertNotIn('"workspace"', detail['evidencePreview'])
        self.assertEqual(databases(), before)
        self.assertEqual(self.puts + self.reads, [])

    def test_source_detail_bounds_large_output_and_evidence(self):
        task = self.make_task('large-source', output={'text': '中文结果' * 5000},
                              extra_evidence=[{'kind': 'artifact', 'summary': '核验结果' * 20000}])
        before = self.tasks.get(task['id'])
        response = self.request(path='/sources/binding/' + task['id'])
        self.assertEqual(response.status_code, 200, response.text[:300])
        detail = response.json()
        self.assertEqual(len(detail['outputPreview']), 16000)
        self.assertEqual(len(detail['evidencePreview']), 64000)
        self.assertTrue(detail['outputTruncated'])
        self.assertTrue(detail['evidenceTruncated'])
        self.assertTrue(detail['outputPreview'].startswith(json.dumps(task['output'], ensure_ascii=False, indent=2)[:100]))
        self.assertIn('execution_terminal', detail['evidencePreview'])
        self.assertNotIn(str(self.root), response.text)
        self.assertEqual(self.tasks.get(task['id']), before)
        self.assertEqual(self.puts + self.reads, [])

    def test_source_detail_hides_scope_and_task_role_mismatches(self):
        self.assertEqual(self.request(path='/sources/binding/' + self.task['id'], token='other-token').status_code, 404)
        for field in ('project', 'role_binding', 'role'):
            task = self.make_task('detail-' + field, **{field: 'other'})
            response = self.request(path='/sources/binding/' + task['id'])
            self.assertEqual(response.status_code, 404)
            self.assertNotIn('Verified output', response.text)
        self.assertEqual(self.puts + self.reads, [])


if __name__ == '__main__':
    unittest.main()
