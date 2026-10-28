"""Real local ledgers and fake upstream observation; never executes a model."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from agent_line.approval_discovery import WorkflowRunLedger
from agent_line.pause_bindings import PauseBindings
from agent_line.store import TaskStore
from agent_line.task_discovery import ExternalTaskDiscovery
from agent_line.adapters.sim_wait import WaitInputUnavailable


class FakeSim:
    def __init__(self):
        self.calls=[]; self.block='wait-one';self.points=1;self.resumed=0
        self.execute=Mock();self.resume=Mock()
    def observe(self,workflow,run):
        self.calls.append((workflow,run))
        return {'response':{'data':{'workflowId':workflow,'runId':run,'status':'paused','paused':{'blockedOnBlockId':self.block,'contextId':self.block,'pauseKind':'human','pausePointCount':self.points,'resumedCount':self.resumed}}}}


class FakeWait:
    def __init__(self,sim):self.sim=sim;self.calls=[];self.error=None;self.override={}
    def read_input(self,workflow,run,block):
        self.calls.append((workflow,run,block))
        if self.error:raise self.error
        return {'input':{'instruction':'untrusted instruction','roleAgentId':'attacker','workspace':'/attacker','value':7},'run':{'workflowId':workflow,'runId':run,'status':'paused','blockId':block,'contextId':block,'pauseKind':'human','pausePointCount':self.sim.points,'resumedCount':self.sim.resumed,**self.override}}


class TaskDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.workspace=self.root/'workspace';self.workspace.mkdir()
        self.tasks=TaskStore.initialize(self.root/'tasks.sqlite')
        self.runs=WorkflowRunLedger.initialize(self.root/'runs.sqlite')
        self.pauses=PauseBindings.initialize(self.root/'pauses.sqlite')
        self.sim=FakeSim();self.wait=FakeWait(self.sim);self.verifier=Mock();self.verifier.verify_run.return_value=True
        self.projects={'project':{'workspace':str(self.workspace),'enabled':True}}
        self.roles={'binding':{'projectId':'project','roleAgentId':'existing-role','enabled':True}}
        self.policy={'roleBindingId':'binding','roleAgentId':'existing-role','instruction':'trusted instruction','memoryPolicyId':'native','outputSchemaId':'artifact','requireMemoryCommit':False,'allowFailureBranch':False}
        self.reserve()
        self.discovery=self.reopen()

    def reserve(self,run='run'):
        self.runs.reserve_tasks(project_id='project',workflow_id='workflow',run_id=run,revision='1',deployment_version_id='deployment',agent_policies={'wait-one':dict(self.policy),'wait-two':dict(self.policy)})

    def reopen(self,**changes):
        args=dict(publication_verifier=self.verifier,projects=self.projects,role_bindings=self.roles,allow_submissions=True,batch_size=50);args.update(changes)
        return ExternalTaskDiscovery(WorkflowRunLedger(self.runs.path),TaskStore(self.tasks.path),PauseBindings(self.pauses.path),self.sim,self.wait,**args)

    def count(self):
        with self.tasks.connect() as db:return db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0]

    def test_real_pause_registers_once_fixed_server_identity_and_never_executes(self):
        first=self.discovery.tick()[0];second=self.discovery.tick()[0];third=self.reopen().tick()[0]
        self.assertEqual(first['status'],'external_task_bound');self.assertEqual(first['taskId'],second['taskId']);self.assertEqual(first['taskId'],third['taskId']);self.assertEqual(self.count(),1)
        self.assertTrue(second['reusedExistingTask']);self.assertTrue(third['reusedExistingTask'])
        task=self.tasks.get(first['taskId']);payload=task['payload'];binding=self.pauses.get(first['taskId'])
        self.assertEqual(task['executionStatus'],'registered');self.assertFalse(task['executionIdentity'])
        self.assertEqual(payload['projectId'],'project');self.assertEqual(payload['roleAgentId'],'existing-role');self.assertEqual(payload['roleBindingId'],'binding');self.assertEqual(payload['workspace'],str(self.workspace.resolve()))
        self.assertEqual(payload['input']['instruction'],'trusted instruction')
        self.assertEqual(payload['input']['taskInput']['roleAgentId'],'attacker')
        self.assertEqual(binding.task_id,task['id']);self.assertEqual(binding.block_id,payload['workflow']['nodeId']);self.assertEqual(binding.context_id,'wait-one');self.assertEqual(binding.run_id,'run')
        self.sim.execute.assert_not_called();self.sim.resume.assert_not_called()

    def test_disabled_changed_role_and_project_are_not_admitted(self):
        for target,field,value in [(self.roles['binding'],'roleAgentId','changed'),(self.roles['binding'],'enabled',False),(self.roles['binding'],'projectId','wrong'),(self.projects['project'],'enabled',False)]:
            old=target[field];target[field]=value
            with self.subTest(field=field):
                self.assertEqual(self.discovery.tick()[0]['status'],'external_task_role_unavailable');self.assertEqual(self.count(),0)
            target[field]=old

    def test_same_host_role_registered_to_another_project_is_not_admitted(self):
        self.roles['foreign']={'projectId':'other','roleAgentId':'existing-role','enabled':True}
        self.assertEqual(self.discovery.tick()[0]['status'],'external_task_role_unavailable')
        self.assertEqual(self.count(),0)
        del self.roles['foreign']
        self.roles['alias']={'projectId':'project','roleAgentId':'existing-role','enabled':True}
        self.assertEqual(self.discovery.tick()[0]['status'],'external_task_bound')

    def test_unproven_publication_does_not_read_input_or_register(self):
        self.verifier.verify_run.return_value=False
        self.assertEqual(self.discovery.tick()[0]['status'],'version_unverified');self.assertEqual(self.count(),0);self.assertEqual(self.wait.calls,[])
        self.verifier.verify_run.side_effect=RuntimeError('secret publication failure')
        result=self.discovery.tick();self.assertNotIn('secret',str(result));self.assertEqual(self.count(),0)

    def test_hidden_input_or_wrong_identity_never_registers(self):
        self.wait.error=WaitInputUnavailable('input_projection_unavailable')
        self.assertEqual(self.discovery.tick()[0]['status'],'external_task_input_unavailable')
        self.wait.error=None;self.wait.override={'contextId':'wrong'}
        self.assertEqual(self.discovery.tick()[0]['status'],'external_task_input_unavailable');self.assertEqual(self.count(),0)

    def test_guard_and_unknown_capability_prevent_creation(self):
        self.discovery.allow_submissions=False
        self.assertEqual(self.discovery.tick()[0]['status'],'external_task_admission_closed');self.assertEqual(self.count(),0)
        self.discovery.allow_submissions=True;owner=self.tasks.enter_maintenance()
        try:self.assertEqual(self.discovery.tick()[0]['status'],'external_task_admission_closed');self.assertEqual(self.count(),0)
        finally:self.tasks.leave_maintenance(owner)

    def test_crash_after_register_before_bind_recovers_same_task(self):
        original=self.discovery.pauses.bind
        self.discovery.pauses.bind=Mock(side_effect=RuntimeError('crash'))
        self.assertEqual(self.discovery.tick()[0]['status'],'unknown');self.assertEqual(self.count(),1)
        with self.tasks.connect() as db:tid=db.execute('SELECT id FROM tasks').fetchone()[0]
        self.discovery.pauses.bind=original
        result=self.reopen().tick()[0]
        self.assertEqual(result['taskId'],tid);self.assertTrue(result['reusedExistingTask']);self.assertEqual(self.pauses.get(tid).context_id,'wait-one');self.assertEqual(self.count(),1)

    def test_malformed_snapshot_does_not_starve_next_page(self):
        self.reserve('second')
        with self.runs.connect() as db:
            row=json.loads(db.execute("SELECT snapshot FROM runs WHERE run='run'").fetchone()[0]);del row['agentPolicies']['wait-one']['instruction']
            db.execute("UPDATE runs SET snapshot=? WHERE run='run'",(json.dumps(row),))
        self.discovery.batch_size=1
        self.assertEqual(self.discovery.tick()[0]['status'],'external_task_invalid');self.assertEqual(self.count(),0)
        self.assertEqual(self.discovery.tick()[0]['status'],'external_task_bound');self.assertEqual(self.count(),1)

    def test_human_snapshot_is_never_observed_by_machine_discovery(self):
        self.runs.reserve(project_id='project',workflow_id='workflow',run_id='human-run',revision='1',deployment_version_id='deployment',human_block_ids=['human'],ttl_seconds=100)
        self.discovery.batch_size=1
        self.discovery.tick();self.assertEqual(self.discovery.tick(),[])
        self.assertNotIn(('workflow','human-run'),self.sim.calls)
        self.assertEqual(self.runs.get('workflow','human-run')['registrationStatus'],'awaiting_pause')

    def test_second_sequential_machine_wait_has_distinct_task_and_binding(self):
        first=self.discovery.tick()[0]
        self.sim.block='wait-two';self.sim.points=2;self.sim.resumed=1
        second=self.discovery.tick()[0]
        self.assertEqual(second['status'],'external_task_bound');self.assertNotEqual(first['taskId'],second['taskId']);self.assertEqual(self.count(),2)
        self.assertEqual(self.pauses.get(first['taskId']).block_id,'wait-one');self.assertEqual(self.pauses.get(second['taskId']).block_id,'wait-two')
        self.assertEqual(self.tasks.get(second['taskId'])['payload']['workflow']['attemptId'],'1')
        self.assertEqual(self.reopen().tick()[0]['taskId'],second['taskId'])

    def test_changed_resolved_input_conflicts_without_new_task(self):
        first=self.discovery.tick()[0]
        original=self.wait.read_input
        def changed(*args):
            result=copy.deepcopy(original(*args));result['input']['value']=8;return result
        self.wait.read_input=changed
        self.assertEqual(self.discovery.tick()[0]['status'],'external_task_conflict');self.assertEqual(self.count(),1)
        self.assertEqual(self.tasks.get(first['taskId'])['payload']['input']['taskInput']['value'],7)
