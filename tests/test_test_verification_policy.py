import copy
import unittest
from agent_line.verification_policy import validate_policy, policy_hash


def policy():
    return {'schemaVersion':2,'policyId':'tests','kind':'command_and_artifacts','requireTests':True,
            'artifacts':[],'testEnvironmentId':'docker-test-v1:'+'a'*64,
            'tests':[{'id':'unit','argv':['python','-m','unittest']}]}


class CommandPolicyTests(unittest.TestCase):
    def test_schema_two_normalizes_without_mutating_input(self):
        raw=policy();before=copy.deepcopy(raw);normalized=validate_policy(raw)
        self.assertEqual(normalized['tests'][0]['timeoutSeconds'],60)
        self.assertEqual(normalized['tests'][0]['maxOutputBytes'],65536)
        self.assertEqual(raw,before)
        self.assertEqual(policy_hash(raw),policy_hash(normalized))
        normalized['tests'][0]['argv'].append('changed')
        self.assertEqual(raw,before)

    def test_schema_one_hash_and_defaults_unchanged_and_tests_rejected(self):
        raw={'schemaVersion':1,'policyId':'bytes','kind':'artifact_sha256','requireTests':False,
             'artifacts':[{'path':'a.txt','sha256':'b'*64}]}
        import hashlib,json
        expected={**raw,'artifacts':[{'path':'a.txt','sha256':'b'*64,'exactSize':None,'maxBytes':10000000}]}
        self.assertEqual(validate_policy(raw),expected)
        self.assertEqual(policy_hash(raw),hashlib.sha256(json.dumps(expected,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest())
        raw['requireTests']=True
        with self.assertRaises(ValueError):validate_policy(raw)

    def test_invalid_fields_bounds_and_booleans(self):
        edits=[lambda p:p.update(schemaVersion=True),lambda p:p.update(requireTests=False),
               lambda p:p.update(testEnvironmentId='docker-test-v1:'+'A'*64),
               lambda p:p.update(tests=[]),lambda p:p.update(shell='injected'),
               lambda p:p['tests'][0].update(argv=['']),lambda p:p['tests'][0].update(argv=['a\0b']),
               lambda p:p['tests'][0].update(argv=['a'*16385]),lambda p:p['tests'][0].update(argv=['x']*65),
               lambda p:p['tests'][0].update(timeoutSeconds=True),lambda p:p['tests'][0].update(timeoutSeconds=301),
               lambda p:p['tests'][0].update(maxOutputBytes=False),lambda p:p['tests'][0].update(maxOutputBytes=1048577),
               lambda p:p['tests'][0].update(extra=1),lambda p:p['tests'].append(copy.deepcopy(p['tests'][0]))]
        for edit in edits:
            value=policy();edit(value)
            with self.subTest(value=value),self.assertRaises(ValueError):validate_policy(value)

    def test_cumulative_budgets(self):
        for timeout,bound in ((300,1),(1,1048576)):
            value=policy();value['tests']=[{'id':'test'+str(i),'argv':['x'],'timeoutSeconds':timeout,'maxOutputBytes':bound} for i in range(5)]
            with self.assertRaises(ValueError):validate_policy(value)


def receipt_check(value, task_id='task', identity=None, workspace='/work'):
    value=validate_policy(value);test=value['tests'][0];identity=identity or {'codexThreadId':'thread','codexTurnId':'turn'}
    return {'kind':'test_command','testId':test['id'],'argv':test['argv'],
            'timeoutSeconds':test['timeoutSeconds'],'maxOutputBytes':test['maxOutputBytes'],
            'environmentId':value['testEnvironmentId'],'exitCode':0,'status':'passed','reason':'command_exit_zero',
            'containerId':'c'*64,'stdout':'测试通过','stderr':'','outputTruncated':False,
            'resourceExitReceipt':{'source':'docker-inspect-before-remove','containerId':'c'*64,'taskId':task_id,
                'executionIdentity':identity,'policySha256':policy_hash(value),'testId':test['id'],
                'environmentId':value['testEnvironmentId'],'running':False,'pid':0,'status':'exited','exitCode':0,
                'owner':'d'*32,'workspace':workspace,'oomKilled':False,
                'startedAt':'2026-10-21T00:00:00Z','finishedAt':'2026-10-21T00:00:01Z'}}


class TestCheckTests(unittest.TestCase):
    def validate(self, check):
        from agent_line.verification_policy import validate_test_check
        p=validate_policy(policy())
        return validate_test_check(check,p['tests'][0],p,'task',{'codexThreadId':'thread','codexTurnId':'turn'})

    def test_pass_requires_real_bound_exit_not_model_claim(self):
        check=receipt_check(policy());self.assertIs(self.validate(check),check)
        changes=[lambda c:c.update(argv=['true']),lambda c:c.update(testId='other'),
                 lambda c:c.update(timeoutSeconds=True),lambda c:c.update(status='passed',exitCode=1),
                 lambda c:c.update(resourceExitReceipt=None),lambda c:c.update(containerId='short'),
                 lambda c:c['resourceExitReceipt'].update(taskId='other'),
                 lambda c:c['resourceExitReceipt'].update(policySha256='0'*64),
                 lambda c:c['resourceExitReceipt'].update(environmentId='docker-test-v1:'+'b'*64),
                 lambda c:c['resourceExitReceipt'].update(running=True),
                 lambda c:c['resourceExitReceipt'].update(pid=False),
                 lambda c:c['resourceExitReceipt'].update(status='running'),
                 lambda c:c['resourceExitReceipt'].update(owner='bad'),
                 lambda c:c['resourceExitReceipt'].update(workspace='relative'),
                 lambda c:c['resourceExitReceipt'].update(finishedAt='missing'),
                 lambda c:c.update(stdout='中'*65536)]
        for change in changes:
            candidate=copy.deepcopy(check);change(candidate)
            with self.subTest(change=change),self.assertRaises(ValueError):self.validate(candidate)

    def test_unknown_check_and_proof_fields_rejected(self):
        for where in ('check','proof'):
            check=receipt_check(policy())
            target=check if where=='check' else check['resourceExitReceipt']
            target['unboundedMetadata']='unexpected'
            with self.assertRaises(ValueError):self.validate(check)

    def test_workspace_must_be_canonical_string(self):
        for path in ('/work//sub','/work/./sub','/work/../sub','//work','/work/'):
            check=receipt_check(policy());check['resourceExitReceipt']['workspace']=path
            with self.subTest(path=path),self.assertRaises(ValueError):self.validate(check)

    def test_surrogate_argv_and_outputs_rejected(self):
        candidate=policy();candidate['tests'][0]['argv']=['\ud800']
        with self.assertRaises(ValueError):validate_policy(candidate)
        check=receipt_check(policy());check['stdout']='\ud800'
        with self.assertRaises(ValueError):self.validate(check)

    def test_nonzero_truncation_and_oom_are_failed_with_truthful_exit(self):
        for reason in ('command_exit_nonzero','output_limit_exceeded','oom_killed'):
            check=receipt_check(policy());check.update(status='failed',reason=reason)
            if reason=='command_exit_nonzero':check['exitCode']=7;check['resourceExitReceipt']['exitCode']=7
            if reason=='output_limit_exceeded':check['outputTruncated']=True
            if reason=='oom_killed':check['resourceExitReceipt']['oomKilled']=True
            self.validate(check)
            check['status']='passed'
            with self.assertRaises(ValueError):self.validate(check)


class CommandStoreTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        from agent_line.store import TaskStore
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.workspace=Path(self.temp.name)
        self.store=TaskStore.initialize(self.workspace/'tasks.sqlite');self.policy=validate_policy(policy())
        task,_=self.store.register('one',{'projectId':'project','roleAgentId':'role','workspace':str(self.workspace),
            'verificationPolicy':self.policy,'workflow':{'runId':'run','nodeExecutionId':'node','attemptId':'one'}})
        self.task=task['id'];self.identity={'codexThreadId':'thread','codexTurnId':'turn'}
        self.store.claim(self.task,self.workspace);self.store.accept(self.task,self.identity)
        self.store.finish(self.task,'succeeded',{'testsPassed':True},[{'kind':'execution_terminal','executionId':'turn','confirmed':True}])
        self.store.confirm_resources_released(self.task,'turn',{'source':'docker-inspect-before-remove','running':False,
            'pid':0,'status':'exited','owner':'a'*32,'containerId':'b'*64,'finishedAt':'time','workspace':str(self.workspace),
            'identities':[self.identity]})
        self.receipt={'kind':'verification','taskId':self.task,'executionIdentity':self.identity,'status':'passed',
            'policySha256':policy_hash(self.policy),'checks':[receipt_check(self.policy,self.task,self.identity,str(self.workspace))]}

    def test_good_receipt_releases_owned_lease_and_replay_is_read_only(self):
        self.assertTrue(self.store.record_verification(self.task,self.receipt))
        self.assertFalse(self.store.record_verification(self.task,self.receipt))
        self.assertEqual(self.store.get(self.task)['verificationStatus'],'passed')
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM workspace_leases').fetchone()[0],0)

    def test_missing_wrong_or_model_only_evidence_keeps_pending_lease(self):
        from agent_line.store import Conflict
        for change in (lambda c:c.update(kind='model_assertion'),lambda c:c['resourceExitReceipt'].update(workspace='/other'),
                       lambda c:c['resourceExitReceipt'].update(executionIdentity={'codexTurnId':'wrong'}),
                       lambda c:c.update(testId='other')):
            receipt=copy.deepcopy(self.receipt);change(receipt['checks'][0])
            with self.assertRaises(Conflict):self.store.record_verification(self.task,receipt)
            self.assertEqual(self.store.get(self.task)['verificationStatus'],'pending')
            self.assertEqual(self.store.verification_lease(self.task),str(self.workspace))

    def test_test_check_order_is_frozen_not_merely_set_equal(self):
        from agent_line.store import Conflict
        self.store.record_verification(self.task,self.receipt)
        p=policy();p['tests'].append({'id':'second','argv':['python','-V']});p=validate_policy(p)
        task,_=self.store.register('two',{'projectId':'project','roleAgentId':'role','workspace':str(self.workspace),
            'verificationPolicy':p,'workflow':{'runId':'run','nodeExecutionId':'second','attemptId':'one'}})
        tid=task['id'];self.store.claim(tid,self.workspace);self.store.accept(tid,self.identity)
        self.store.finish(tid,'succeeded',{},[{'kind':'execution_terminal','executionId':'turn','confirmed':True}])
        self.store.confirm_resources_released(tid,'turn',{'source':'docker-inspect-before-remove','running':False,
            'pid':0,'status':'exited','owner':'a'*32,'containerId':'b'*64,'finishedAt':'time','workspace':str(self.workspace),
            'identities':[self.identity]})
        first=receipt_check(p,tid,self.identity,str(self.workspace));second=copy.deepcopy(first)
        second.update(testId='second',argv=['python','-V']);second['resourceExitReceipt']['testId']='second'
        receipt={'kind':'verification','taskId':tid,'executionIdentity':self.identity,'status':'passed',
                 'policySha256':policy_hash(p),'checks':[second,first]}
        with self.assertRaises(Conflict):self.store.record_verification(tid,receipt)
        receipt['checks']=[first,second]
        self.assertTrue(self.store.record_verification(tid,receipt))
