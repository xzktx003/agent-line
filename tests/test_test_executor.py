import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from agent_line.test_executor import DockerTestExecutor, _hash, _command, COLLECTOR


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.evidence = self.root/'evidence'; self.evidence.mkdir(mode=0o700)
        self.workspace = self.root/'workspace'; self.workspace.mkdir()
        self.executor = DockerTestExecutor(self.evidence, 'sha256:'+'a'*64)
        self.test = dict(id='unit', argv=['/usr/bin/python3', '-m', 'unittest'], timeoutSeconds=30, maxOutputBytes=128)
        self.policy = dict(schemaVersion=2, policyId='tests', kind='command_and_artifacts', requireTests=True,
                           artifacts=[dict(path='result',sha256='a'*64,exactSize=None,maxBytes=100)],
                           testEnvironmentId=self.executor.environment_id, tests=[self.test])
        self.task = dict(id='task-one', executionStatus='succeeded', resourcesReleased=True,
                         executionIdentity={'upstreamTaskId':'native-one','codexThreadId':'thread','codexTurnId':'turn'},
                         payload={'workspace':str(self.workspace)})
        self.calls=[]; self.container=None; self.start_error=False; self.create_error=False
        
        self.docker = patch('agent_line.test_executor._command', side_effect=self.command);self.docker.start();self.addCleanup(self.docker.stop)

    def command(self, argv, **kw):
        self.calls.append(argv)
        if argv[1]=='create':
            self.assertIn('--pull=never', argv)
            # Intent is durable and private before any remote write.
            files=list(self.evidence.glob('*.json')); self.assertEqual(len(files),1)
            record=json.loads(files[0].read_text());self.assertTrue(record['createIntent'])
            self.assertEqual(files[0].stat().st_mode&0o777,0o600)
            name=argv[argv.index('--name')+1]; b=record['binding']
            self.container=dict(Id='b'*64,Name='/'+name,Image=self.executor.image_id,
                Config=dict(Labels={'agent-line.test-binding':_hash(b),'agent-line.test-owner':_hash(b)[:32]},
                    Entrypoint=['/usr/bin/timeout'],Cmd=self.executor._argv(self.test),
                    Env=[k+'='+v for k,v in self.executor.descriptor['environment'].items()],
                    WorkingDir=str(self.workspace),User=f'{os.getuid()}:{os.getgid()}'),
                HostConfig=dict(NetworkMode='none',ReadonlyRootfs=True,Privileged=False,Init=True,PidsLimit=128,
                    Memory=2147483648,NanoCpus=2000000000,CapDrop=['ALL'],SecurityOpt=['no-new-privileges'],
                    LogConfig=self.executor.descriptor['logConfig'],
                    Tmpfs={'/tmp':'rw,nosuid,nodev,size=128m,mode=1777'}),
                Mounts=[dict(Type='bind',RW=False,Source=str(self.workspace),Destination=str(self.workspace))],
                State=dict(Status='created',Running=False,Pid=0,ExitCode=0,OOMKilled=False,
                    StartedAt='2026-10-22T01:00:00Z',FinishedAt='2026-10-22T01:00:01Z'))
            if self.create_error:raise TimeoutError('secret')
        elif argv[1]=='inspect':
            if self.container is None:return 1,b'',b'absent',False
            return 0,json.dumps([self.container]).encode(),b'',False
        elif argv[1]=='start':
            record=json.loads(next(self.evidence.glob('*.json')).read_text());self.assertTrue(record['startIntent'])
            if self.start_error:raise TimeoutError('secret')
            self.container['State'].update(Status='running',Running=True,Pid=42)
        elif argv[1]=='logs':return 0,b'ok\n',b'',False
        elif argv[1]=='rm':
            record=json.loads(next(self.evidence.glob('*.json')).read_text())
            self.assertTrue('check' in record or (record.get('cancelRequested') and not record.get('startIntent')))
            self.container=None
        return 0,b'',b'',False

    def check(self):return self.executor.check(self.task,self.policy,self.test)

    def finish(self,code=0):
        self.container['State'].update(Status='exited',Running=False,Pid=0,ExitCode=code)

    def test_single_execution_restart_and_positive_receipt(self):
        self.assertEqual(self.check()['status'],'waiting')
        self.assertEqual(self.check()['reason'],'test_running')
        self.finish(); result=self.check();self.assertEqual(result['check']['status'],'passed')
        self.executor=DockerTestExecutor(self.evidence,self.executor.image_id)
        before=len(self.calls);self.assertEqual(self.check(),result);self.assertEqual(len(self.calls),before)
        proof=result['check']['resourceExitReceipt'];self.assertEqual(proof['executionIdentity'],self.task['executionIdentity'])
        self.assertEqual(proof['containerId'],'b'*64)
        self.assertEqual(sum(c[1]=='create' for c in self.calls),1)

    def test_create_response_lost_only_inspects_existing_container(self):
        self.create_error=True;self.assertEqual(self.check()['status'],'waiting')
        self.assertEqual(sum(c[1]=='create' for c in self.calls),1)

    def test_start_response_lost_never_starts_again(self):
        self.start_error=True;self.check();self.assertEqual(self.check()['reason'],'container_start_unconfirmed')
        self.assertEqual(sum(c[1]=='start' for c in self.calls),1)

    def test_absent_after_intent_is_not_exit_and_never_recreated(self):
        self.check();self.container=None
        self.assertEqual(self.check()['reason'],'container_creation_unconfirmed')
        self.assertEqual(sum(c[1]=='create' for c in self.calls),1)

    def test_changed_identity_and_argv_do_not_execute(self):
        self.check();self.task['executionIdentity']['codexTurnId']='other'
        self.assertEqual(self.check()['status'],'blocked')
        self.assertEqual(sum(c[1]=='start' for c in self.calls),1)
        self.test['argv']=['different'];self.assertEqual(self.check()['status'],'blocked')

    def test_container_tamper_fails_closed(self):
        self.check();self.finish();self.container['Mounts'][0]['RW']=True
        self.assertEqual(self.check()['status'],'blocked')
        self.assertFalse(any(c[1]=='rm' for c in self.calls))

    def test_nonzero_and_oom_fail(self):
        self.check();self.finish(137);self.container['State']['OOMKilled']=True
        result=self.check()['check'];self.assertEqual(result['status'],'failed');self.assertEqual(result['reason'],'oom_killed')

    def test_keyboard_interrupt_leaves_intent_without_retry(self):
        with patch('agent_line.test_executor._command',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):self.check()
        self.assertEqual(self.check()['reason'],'container_creation_unconfirmed')
        self.assertFalse(any(c[1]=='create' for c in self.calls))

    def test_concurrent_same_job_only_starts_once(self):
        results=[]
        threads=[threading.Thread(target=lambda:results.append(self.check())) for _ in range(2)]
        for t in threads:t.start()
        for t in threads:t.join(3);self.assertFalse(t.is_alive())
        self.assertEqual(sum(c[1]=='create' for c in self.calls),1)
        self.assertEqual(sum(c[1]=='start' for c in self.calls),1)

    def test_bad_paths_environment_and_unfrozen_command(self):
        self.policy['testEnvironmentId']='wrong';self.assertEqual(self.check()['status'],'blocked');self.assertFalse(self.calls)
        self.policy['testEnvironmentId']=self.executor.environment_id;self.policy['tests']=[]
        self.assertEqual(self.check()['status'],'blocked');self.assertFalse(self.calls)
        self.evidence.chmod(0o755)
        with self.assertRaises(ValueError):DockerTestExecutor(self.evidence,self.executor.image_id)

    def test_symlink_receipt_and_lock_rejected(self):
        key=_hash({'taskId':self.task['id'],'testId':self.test['id']})
        (self.evidence/('test-'+key+'.lock')).symlink_to(self.root/'other')
        self.assertEqual(self.check()['status'],'blocked');self.assertFalse(self.calls)

    def test_logs_error_cannot_be_a_pass(self):
        self.check();self.finish()
        original=self.command
        def fail_logs(argv, **kw):
            if argv[1]=='logs':return 1,b'',b'sensitive failure',False
            return original(argv,**kw)
        with patch('agent_line.test_executor._command',side_effect=fail_logs):
            result=self.check()
        self.assertEqual(result,{'status':'blocked','reason':'test_logs_unavailable'})
        self.assertNotIn('check',json.loads(next(self.evidence.glob('*.json')).read_text()))

    def test_output_limit_fails_even_with_exit_zero(self):
        self.check();self.finish()
        original=self.command
        def logs(argv, **kw):
            if argv[1]=='logs':return -1,b'x'*128,b'',True
            return original(argv,**kw)
        with patch('agent_line.test_executor._command',side_effect=logs):
            result=self.check()['check']
        self.assertEqual(result['exitCode'],0)
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['reason'],'output_limit_exceeded')

    def test_created_replacement_and_changed_env_rejected(self):
        self.start_error=True;self.check();self.container['Id']='c'*64
        self.assertEqual(self.check()['status'],'blocked')
        self.container['Id']='b'*64;self.container['Config']['Env'].append('HOME=/other')
        self.assertEqual(self.check()['status'],'blocked')
        self.assertEqual(sum(c[1]=='start' for c in self.calls),1)

    def test_policy_valid_large_argument_can_create_and_inspect_container(self):
        self.test['argv'] = ['python3', '-c', 'x' * 9000]
        self.assertEqual(self.check()['status'], 'waiting')

    def test_corrupt_completed_receipt_rejected_without_docker(self):
        self.check();self.finish();self.check()
        p=next(self.evidence.glob('*.json'));record=json.loads(p.read_text())
        record['check']['resourceExitReceipt']['executionIdentity']={}
        p.write_text(json.dumps(record));before=len(self.calls)
        self.assertEqual(self.check()['status'],'blocked');self.assertEqual(len(self.calls),before)

    def test_remove_failure_retains_pending_cleanup_and_reopen_retries_only_remove(self):
        self.check();self.finish()
        original=self.command
        def fail_remove(argv, **kw):
            if argv[1]=='rm':return 1,b'',b'failed',False
            return original(argv,**kw)
        with patch('agent_line.test_executor._command',side_effect=fail_remove):
            self.assertNotEqual(self.check()['status'],'completed')
        self.assertIn('check',json.loads(next(self.evidence.glob('*.json')).read_text()))
        self.executor=DockerTestExecutor(self.evidence,self.executor.image_id)
        self.assertEqual(self.check()['status'],'completed')
        self.assertEqual(sum(c[1]=='create' for c in self.calls),1)
        self.assertEqual(sum(c[1]=='start' for c in self.calls),1)

    def test_cleanup_after_removed_but_persist_lost_needs_successful_daemon_absence(self):
        self.check();self.finish()
        original=self.command
        def remove_then_error(argv, **kw):
            result=original(argv,**kw)
            if argv[1]=='rm':raise TimeoutError('response lost')
            return result
        with patch('agent_line.test_executor._command',side_effect=remove_then_error):
            self.assertEqual(self.check()['status'],'blocked')
        def daemon_down(argv, **kw):
            if argv[1]=='ps':return 1,b'',b'down',False
            return original(argv,**kw)
        with patch('agent_line.test_executor._command',side_effect=daemon_down):
            self.assertEqual(self.check()['status'],'blocked')
        self.assertEqual(self.check()['status'],'completed')
        before=len(self.calls);self.check();self.assertEqual(len(self.calls),before)

    def test_collector_bounds_child_output_and_reports_nonzero(self):
        result=subprocess.run([sys.executable,'-c',COLLECTOR,'64',sys.executable,'-c',
                               'import os;os.write(1,b"x"*1000000)'],capture_output=True,timeout=5)
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(len(result.stdout)+len(result.stderr),65)

    def test_collector_preserves_separate_streams_and_exit(self):
        result=subprocess.run([sys.executable,'-c',COLLECTOR,'64',sys.executable,'-c',
                               'import os;os.write(1,b"ok");os.write(2,b"err");raise SystemExit(7)'],capture_output=True,timeout=5)
        self.assertEqual((result.returncode,result.stdout,result.stderr),(7,b'ok',b'err'))

    def test_collector_kills_inherited_pipe_descendant_on_overflow(self):
        child='import os,time;os.write(1,b"x"*10000);time.sleep(30)'
        parent='import subprocess,sys,time;subprocess.Popen([sys.executable,"-c",'+repr(child)+']);time.sleep(30)'
        result=subprocess.run([sys.executable,'-c',COLLECTOR,'64',sys.executable,'-c',parent],
                              capture_output=True,timeout=5)
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(len(result.stdout)+len(result.stderr),65)

    def test_cancel_before_create_is_durable_and_never_runs(self):
        result=self.executor.cancel(self.task,self.policy,self.test)
        self.assertEqual(result['cancellation']['disposition'],'not_started')
        self.assertFalse(self.calls)
        self.executor=DockerTestExecutor(self.evidence,self.executor.image_id)
        self.assertEqual(self.check()['status'],'blocked')
        self.assertEqual(self.executor.cancel(self.task,self.policy,self.test),result)
        self.assertFalse(self.calls)

    def test_cancel_running_waits_for_actual_exit_and_keeps_original_check(self):
        self.check()
        original=self.command
        def kill(argv, **kw):
            if argv[1]=='kill':
                self.calls.append(argv)
                self.assertTrue(json.loads(next(self.evidence.glob('*.json')).read_text())['cancelRequested'])
                return 0,b'ack',b'',False
            return original(argv,**kw)
        with patch('agent_line.test_executor._command',side_effect=kill):
            result=self.executor.cancel(self.task,self.policy,self.test)
        self.assertEqual(result['status'],'waiting')
        self.assertEqual(self.check()['reason'],'test_canceled')
        self.finish(137)
        result=self.executor.cancel(self.task,self.policy,self.test)
        self.assertEqual(result['cancellation']['disposition'],'exited')
        self.assertEqual(result['cancellation']['check']['exitCode'],137)
        before=len(self.calls)
        self.assertEqual(self.executor.cancel(self.task,self.policy,self.test),result)
        self.assertEqual(len(self.calls),before)
        self.assertEqual(sum(c[1]=='start' for c in self.calls),1)

    def test_cancel_unknown_kill_or_wrong_owner_never_releases(self):
        self.check()
        original=self.command
        def lost(argv, **kw):
            if argv[1]=='kill':raise TimeoutError('secret')
            return original(argv,**kw)
        with patch('agent_line.test_executor._command',side_effect=lost):
            self.assertEqual(self.executor.cancel(self.task,self.policy,self.test)['status'],'blocked')
        self.container['Config']['Labels']['agent-line.test-owner']='0'*32
        self.assertEqual(self.executor.cancel(self.task,self.policy,self.test)['status'],'blocked')
        self.assertFalse(any(c[1]=='kill' for c in self.calls))

    def test_cancel_completed_preserves_pass_fact(self):
        self.check();self.finish();check=self.check()['check'];before=len(self.calls)
        result=self.executor.cancel(self.task,self.policy,self.test)
        self.assertEqual(result['cancellation']['check'],check)
        self.assertEqual(check['status'],'passed')
        self.assertEqual(len(self.calls),before)

    def test_cancel_ambiguous_create_or_start_is_not_not_started(self):
        with patch('agent_line.test_executor._command',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):self.check()
        self.assertEqual(self.executor.cancel(self.task,self.policy,self.test)['status'],'blocked')
        self.assertEqual(self.check()['reason'],'test_canceled')
        self.assertFalse(any(c[1]=='create' for c in self.calls))

    def test_cancel_created_before_start_removes_without_start(self):
        original=self.command
        def after_create(argv, **kw):
            result=original(argv,**kw)
            if argv[1]=='create':raise KeyboardInterrupt()
            return result
        with patch('agent_line.test_executor._command',side_effect=after_create):
            with self.assertRaises(KeyboardInterrupt):self.check()
        result=self.executor.cancel(self.task,self.policy,self.test)
        self.assertEqual(result['cancellation']['disposition'],'not_started')
        self.assertFalse(any(c[1]=='start' for c in self.calls))
        self.assertEqual(self.check()['reason'],'test_canceled')

    def test_output_reader_bounded(self):
        self.docker.stop()
        code,out,err,cut=_command(['/usr/bin/python3','-c','import os;os.write(1,b"x"*1000000)'],limit=64)
        self.assertTrue(cut);self.assertLessEqual(len(out)+len(err),64)


if __name__=='__main__':unittest.main()
