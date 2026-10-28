"""Discover only frozen, explicitly human pauses; never decides or resumes runs."""
from contextlib import contextmanager
from dataclasses import asdict
import json
import os
import re
from pathlib import Path
import sqlite3
import time
import uuid

from .adapters.sim import segment
from .approvals import ApprovalBinding
from .store import Conflict, canonical


def validate_loop_policies(loop_policies, agent_policies):
    if loop_policies is None:
        return {}
    if not isinstance(loop_policies, dict) or not loop_policies:
        raise ValueError('Explicit nonempty loop policy required')
    result = {}
    containers = set()
    for block, policy in loop_policies.items():
        segment(block)
        if (block not in agent_policies or not isinstance(agent_policies[block], dict) or not isinstance(policy, dict)
                or set(policy) != {'loopId', 'maxIterations'}
                or type(policy['maxIterations']) is not int or not 1 <= policy['maxIterations'] <= 3
                or agent_policies[block].get('allowFailureBranch') is not True):
            raise ValueError('Invalid bounded repair-loop policy')
        segment(policy['loopId'])
        if policy['loopId'] in containers or policy['loopId'] in agent_policies:
            raise ValueError('Each loop must have its own single body')
        containers.add(policy['loopId'])
        result[block] = dict(policy)
    return result


def validate_approval_ttls(value, blocks):
    if (type(value) is not dict or not value or not set(value) <= set(blocks)
            or any(type(seconds) is not int or not 1 <= seconds <= 604800
                   for seconds in value.values())):
        raise ValueError('Invalid per-node human approval lifetime')
    for block in value:
        segment(block)
    return dict(value)


def validate_approval_questions(value, blocks):
    if (type(value) is not dict or not value or not set(value) <= set(blocks)
            or any(not isinstance(text, str) or not 1 <= len(text) <= 2000
                   or text != text.strip() or '\0' in text for text in value.values())):
        raise ValueError('Invalid frozen human confirmation text')
    for block in value:
        segment(block)
    return dict(value)


def validate_human_policy(policy, *, revision=None, deployment=None, agent_policies=None):
    """An explicit human allowlist never grants access to machine waits."""
    fields = {'revision', 'deploymentVersionId', 'blockIds', 'ttlSeconds'}
    if (not isinstance(policy, dict) or not fields <= set(policy) <= fields | {'ttlByBlock', 'questionsByBlock'}
            or not isinstance(policy['revision'], str)
            or not re.fullmatch(r'[1-9][0-9]{0,14}', policy['revision'])
            or not isinstance(policy['blockIds'], list) or not policy['blockIds']
            or len(policy['blockIds']) > 200
            or type(policy['ttlSeconds']) is not int or not 1 <= policy['ttlSeconds'] <= 604800):
        raise ValueError('Invalid trusted human approval policy')
    segment(policy['deploymentVersionId'])
    for block in policy['blockIds']:
        segment(block)
    if (len(set(policy['blockIds'])) != len(policy['blockIds'])
            or (revision is not None and policy['revision'] != revision)
            or (deployment is not None and policy['deploymentVersionId'] != deployment)
            or set(policy['blockIds']) & set(agent_policies or {})):
        raise ValueError('Human policy publication and purpose must match')
    if 'ttlByBlock' in policy:
        validate_approval_ttls(policy['ttlByBlock'], policy['blockIds'])
    if 'questionsByBlock' in policy:
        validate_approval_questions(policy['questionsByBlock'], policy['blockIds'])
    return policy


class WorkflowRunLedger:
    def __init__(self, path):
        self.path=Path(path).resolve(strict=True)
        if not self.path.is_file() or self.path.stat().st_mode & 0o077:
            raise ValueError('Workflow run ledger must be a private regular file')
        with self.connect() as db:
            if db.execute('PRAGMA user_version').fetchone()[0]!=1:
                raise ValueError('Unsupported workflow run ledger schema')

    @classmethod
    def initialize(cls,path):
        path=Path(path).resolve()
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.close(fd)
        db=sqlite3.connect(path)
        try:
            db.executescript('''CREATE TABLE runs(seq INTEGER PRIMARY KEY, workflow TEXT NOT NULL,
                run TEXT NOT NULL, snapshot TEXT NOT NULL, submission TEXT NOT NULL DEFAULT 'intent',
                complete INTEGER NOT NULL DEFAULT 0, registration_status TEXT NOT NULL DEFAULT 'awaiting_pause',
                registration_reason TEXT, UNIQUE(workflow,run));
                CREATE TABLE pauses(workflow TEXT NOT NULL, run TEXT NOT NULL, context TEXT NOT NULL,
                binding TEXT NOT NULL, state TEXT NOT NULL, PRIMARY KEY(workflow,run,context));
                PRAGMA user_version=1;''')
            db.commit()
        finally:db.close()
        return cls(path)

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path.as_uri()+'?mode=rw',uri=True,timeout=15);db.row_factory=sqlite3.Row
        try:yield db;db.commit()
        except BaseException:db.rollback();raise
        finally:db.close()

    def reserve(self,*,project_id,workflow_id,run_id,revision,human_block_ids,ttl_seconds,deployment_version_id,
                approval_ttls=None, approval_questions=None):
        for value in (project_id,workflow_id,run_id,revision,deployment_version_id):segment(value)
        if revision.lower() in ('latest','head','main','draft'):
            raise ValueError('Pinned published revision required')
        if not isinstance(human_block_ids,(list,tuple,frozenset)) or not human_block_ids:
            raise ValueError('Explicit human block allowlist required')
        for block in human_block_ids:segment(block)
        if len(set(human_block_ids))!=len(human_block_ids):raise ValueError('Duplicate human blocks')
        if type(ttl_seconds) is not int or not 1<=ttl_seconds<=31536000:
            raise ValueError('Finite approval TTL required')
        overrides = validate_approval_ttls(approval_ttls, human_block_ids) if approval_ttls is not None else None
        questions = validate_approval_questions(approval_questions, human_block_ids) if approval_questions is not None else None
        snapshot=canonical(dict(projectId=project_id,workflowId=workflow_id,runId=run_id,
                                revision=revision,deploymentVersionId=deployment_version_id,humanBlockIds=sorted(human_block_ids),ttlSeconds=ttl_seconds,
                                **({'approvalTtlByBlock': overrides} if overrides is not None else {}),
                                **({'approvalQuestions': questions} if questions is not None else {})))
        return self._reserve_snapshot(workflow_id,run_id,snapshot)

    def reserve_tasks(self, *, project_id, workflow_id, run_id, revision,
                      deployment_version_id, agent_policies, ttl_seconds=604800, loop_policies=None,
                      human_approval=None, static_parallel=False, run_budget=None,
                      subworkflow_depth_limit=None):
        """Freeze machine-wait policy separately from human approval purpose."""
        for value in (project_id,workflow_id,run_id,deployment_version_id):
            segment(value)
        if type(revision) is int:
            revision=str(revision)
        if not isinstance(revision,str) or not re.fullmatch(r'[1-9][0-9]{0,159}',revision):
            raise ValueError('Positive pinned publication revision required')
        if type(ttl_seconds) is not int or not 1<=ttl_seconds<=31536000:
            raise ValueError('Finite external task TTL required')
        if not isinstance(agent_policies,dict) or not 1<=len(agent_policies)<=200:
            raise ValueError('Explicit bounded agent policies required')
        fields={'roleBindingId','roleAgentId','instruction','memoryPolicyId',
                'outputSchemaId','requireMemoryCommit','allowFailureBranch'}
        policies={}
        for block,policy in agent_policies.items():
            segment(block)
            if not isinstance(policy,dict) or not fields <= set(policy) <= fields | {'verificationPolicy', 'roleRevision', 'limits', 'outputFields', 'batchItem'}:
                raise ValueError('Exact agent policy fields required')
            for field in ('roleBindingId','roleAgentId','memoryPolicyId','outputSchemaId'):
                segment(policy[field])
            instruction=policy['instruction']
            if not isinstance(instruction,str) or not instruction.strip() or len(instruction)>4000:
                raise ValueError('Bounded nonempty instruction required')
            for field in ('requireMemoryCommit','allowFailureBranch'):
                if type(policy[field]) is not bool:
                    raise ValueError('Explicit boolean task policy required')
            policies[block]=dict(policy)
            if 'batchItem' in policy:
                from .batch_item import validate_batch_item, batch_node_id
                from .sim_authoring import native_id
                item = validate_batch_item(policy['batchItem'])
                if block != native_id(workflow_id, batch_node_id(item)):
                    raise ValueError('batch_item_invalid')
                policies[block]['batchItem'] = item
            if policy['outputSchemaId'] in ('fields-result-v1', 'fields-result-v2') or 'outputFields' in policy:
                from .output_contract import validate_output_fields
                if policy['outputSchemaId'] not in ('fields-result-v1', 'fields-result-v2'):
                    raise ValueError('Output fields require the declared custom format')
                policies[block]['outputFields'] = validate_output_fields(policy.get('outputFields'), policy['outputSchemaId'])
            if 'limits' in policy:
                from .execution_limits import validate_limit_values
                policies[block]['limits'] = validate_limit_values(policy['limits'])
            if 'roleRevision' in policy:
                from .role_revision import validate_role_revision
                policies[block]['roleRevision'] = validate_role_revision(policy['roleRevision'])
            if 'verificationPolicy' in policy:
                from .verification_policy import validate_policy
                policies[block]['verificationPolicy'] = validate_policy(policy['verificationPolicy'])
        loops=validate_loop_policies(loop_policies, policies)
        if type(static_parallel) is not bool or (static_parallel and loops):
            raise ValueError('Static parallel capability must be explicit and separate from loops')
        approvals = {}
        if human_approval is not None:
            validate_human_policy(human_approval, revision=revision,
                deployment=deployment_version_id, agent_policies=policies)
            approvals = {'approvalBlockIds': sorted(human_approval['blockIds']),
                         'approvalTtlSeconds': human_approval['ttlSeconds']}
            if 'ttlByBlock' in human_approval:
                approvals['approvalTtlByBlock'] = validate_approval_ttls(human_approval['ttlByBlock'], human_approval['blockIds'])
            if 'questionsByBlock' in human_approval:
                approvals['approvalQuestions'] = validate_approval_questions(human_approval['questionsByBlock'], human_approval['blockIds'])
        from .run_budget import validate_run_budget
        budget = validate_run_budget(run_budget)
        if subworkflow_depth_limit is not None:
            if type(subworkflow_depth_limit) is not int or subworkflow_depth_limit != 1 or budget is None:
                raise ValueError('Subworkflow requires depth limit one and a finite run budget')
        snapshot=canonical(dict(projectId=project_id,workflowId=workflow_id,runId=run_id,
            revision=revision,deploymentVersionId=deployment_version_id,
            humanBlockIds=sorted(set(policies) | set(approvals.get('approvalBlockIds', []))),ttlSeconds=ttl_seconds,
            purpose='external_task_result',agentPolicies=policies,
            **approvals,
            **({'runBudget': budget} if budget is not None else {}),
            **({'subworkflowDepthLimit': 1} if subworkflow_depth_limit == 1 else {}),
            **({'staticParallel': True} if static_parallel else {}),
            **({'loopPolicies': loops} if loops else {})))
        return self._reserve_snapshot(workflow_id,run_id,snapshot)

    def _reserve_snapshot(self,workflow_id,run_id,snapshot):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT snapshot FROM runs WHERE workflow=? AND run=?',(workflow_id,run_id)).fetchone()
            if old:
                if old['snapshot']!=snapshot:raise Conflict('Workflow run snapshot is immutable')
                return False
            db.execute('INSERT INTO runs(workflow,run,snapshot) VALUES(?,?,?)',(workflow_id,run_id,snapshot))
            return True

    def record_submission(self,workflow_id,run_id,status):
        if status not in ('accepted','uncertain','rejected','intent'):raise ValueError('Invalid submission state')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT submission FROM runs WHERE workflow=? AND run=?',(workflow_id,run_id)).fetchone()
            if old is None:raise KeyError((workflow_id,run_id))
            if old['submission']==status:return False
            if old['submission'] in ('accepted','rejected'):raise Conflict('Confirmed submission is immutable')
            db.execute('UPDATE runs SET submission=? WHERE workflow=? AND run=?',(status,workflow_id,run_id))
            return True

    def get(self,workflow_id,run_id):
        with self.connect() as db:
            row=db.execute('SELECT * FROM runs WHERE workflow=? AND run=?',(workflow_id,run_id)).fetchone()
            if row is None:raise KeyError((workflow_id,run_id))
            return dict(json.loads(row['snapshot']),seq=row['seq'],
                        submissionStatus=row['submission'],complete=bool(row['complete']),
                        registrationStatus=row['registration_status'],registrationReason=row['registration_reason'])

    def page(self,*,after=0,limit=50):
        if type(after) is not int or after<0 or type(limit) is not int or not 1<=limit<=500:
            raise ValueError('Invalid run page')
        with self.connect() as db:
            return [dict(json.loads(row['snapshot']),seq=row['seq'],submissionStatus=row['submission'],
                         registrationStatus=row['registration_status'],registrationReason=row['registration_reason']) for row in db.execute(
                'SELECT * FROM runs WHERE seq>? AND complete=0 ORDER BY seq LIMIT ?',(after,limit))]

    def record_registration(self,run,status):
        reasons = {'bound': None, 'version_unverified': 'run_publication_not_verified',
                   'unknown': 'observation_unavailable', 'waiting': 'human_pause_not_observed',
                   'not_allowed_human_pause': 'pause_outside_frozen_human_policy',
                   'ambiguous_activation': 'context_activation_cannot_be_distinguished',
                   'terminal_observed': None,
                   'external_task_bound': None,
                   'external_task_input_unavailable': 'external_task_input_unavailable',
                   'external_task_role_unavailable': 'external_task_role_unavailable',
                   'external_task_admission_closed': 'external_task_admission_closed',
                   'external_task_conflict': 'external_task_identity_conflict',
                   'external_task_invalid': 'external_task_policy_invalid'}
        if status not in reasons:raise ValueError('Unsupported registration result')
        with self.connect() as db:
            db.execute('UPDATE runs SET registration_status=?,registration_reason=? WHERE workflow=? AND run=?',
                       (status,reasons[status],run['workflowId'],run['runId']))

    def observe_transition(self,run,active_context,*,terminal=False):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE pauses SET state='left' WHERE workflow=? AND run=? AND context!=? AND state='active'",
                       (run['workflowId'],run['runId'],active_context or ''))
            if terminal:
                db.execute("UPDATE runs SET complete=1 WHERE workflow=? AND run=? AND submission IN ('accepted','rejected')",
                           (run['workflowId'],run['runId']))

    def pause_binding(self,run,context,block,now):
        identity=(run['workflowId'],run['runId'],context)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT binding,state FROM pauses WHERE workflow=? AND run=? AND context=?',identity).fetchone()
            if row:
                binding=ApprovalBinding(**json.loads(row['binding']))
                if row['state']!='active' or binding.block_id!=block:return None
                return binding
            # The original run/context is the only supported activation identity.
            approval_id='approval-'+uuid.uuid5(uuid.NAMESPACE_URL,canonical(identity)).hex
            allowed = run['approvalBlockIds'] if run.get('purpose') == 'external_task_result' else run['humanBlockIds']
            overrides = validate_approval_ttls(run['approvalTtlByBlock'], allowed) if 'approvalTtlByBlock' in run else {}
            lifetime = overrides.get(block, run['approvalTtlSeconds'] if run.get('purpose') == 'external_task_result' else run['ttlSeconds'])
            binding=ApprovalBinding(approval_id,run['projectId'],run['workflowId'],run['runId'],
                block,context,run['revision'],run['runId'],'one','human_approval',
                now+lifetime)
            db.execute('INSERT INTO pauses VALUES(?,?,?,?,?)',(*identity,canonical(asdict(binding)),'active'))
            return binding


class ApprovalDiscovery:
    def __init__(self,run_ledger,approval_ledger,sim_client,*,publication_verifier,batch_size=50,clock=time.time):
        if type(batch_size) is not int or not 1<=batch_size<=500:raise ValueError('Invalid discovery batch size')
        if not callable(getattr(publication_verifier,"verify_run",None)):
            raise ValueError("Run publication verifier required")
        self.publication_verifier=publication_verifier
        self.runs,self.approvals,self.client=run_ledger,approval_ledger,sim_client
        self.batch_size,self.clock,self.cursor=batch_size,clock,0
        self.run_control = None

    def _discover(self,run):
        if self.run_control is not None:
            self.run_control.reconcile_stop(run['projectId'], run['workflowId'], run['runId'], self.client)
        allowed = run['approvalBlockIds'] if run.get('purpose') == 'external_task_result' else run['humanBlockIds']
        response=self.client.observe(run['workflowId'],run['runId'])
        envelope=response.get('response',{}) if isinstance(response,dict) else {}
        data=envelope.get('data',envelope) if isinstance(envelope,dict) else {}
        if (not isinstance(data,dict) or data.get('workflowId')!=run['workflowId']
                or data.get('runId')!=run['runId']):return {'status':'unknown'}
        status=data.get('status')
        if status in ('completed','failed','cancelled','canceled'):
            self.runs.observe_transition(run,None,terminal=True)
            return {'status':'terminal_observed'}
        if status!='paused':
            if status in ('running','queued','pending'):self.runs.observe_transition(run,None)
            return {'status':'waiting'}
        pause=data.get('paused')
        points=pause.get('pausePointCount') if isinstance(pause,dict) else None
        resumed=pause.get('resumedCount',0 if type(points) is int and points==1 else None) if isinstance(pause,dict) else None
        if (not isinstance(pause,dict) or pause.get('pauseKind')!='human'
                or type(points) is not int or type(resumed) is not int
                or points<1 or resumed<0 or points-resumed!=1):
            self.runs.observe_transition(run,None)
            return {'status':'not_allowed_human_pause'}
        if pause.get('blockedOnBlockId') not in allowed:
            self.runs.observe_transition(run,None)
            if pause.get('blockedOnBlockId') in run.get('agentPolicies', {}):
                return {'status': 'waiting', 'otherPurpose': 'external_task_result'}
            return {'status':'not_allowed_human_pause'}
        try:context=segment(pause.get('contextId'));block=segment(pause.get('blockedOnBlockId'))
        except ValueError:return {'status':'unknown'}
        try:
            if self.publication_verifier.verify_run(run) is not True:
                return {'status':'version_unverified'}
        except Exception:
            return {'status':'version_unverified'}
        self.runs.observe_transition(run,context)
        binding=self.runs.pause_binding(run,context,block,self.clock())
        if binding is None:return {'status':'ambiguous_activation'}
        try:
            existing=self.approvals.get(binding.approval_id)
            if existing['decision'] is not None:return {'status':'ambiguous_activation','approvalId':binding.approval_id}
        except KeyError:pass
        self.approvals.bind(binding)
        return {'status':'bound','approvalId':binding.approval_id}

    def tick(self):
        page=self.runs.page(after=self.cursor,limit=self.batch_size)
        if not page and self.cursor:
            self.cursor=0
            page=self.runs.page(limit=self.batch_size)
        results=[]
        for run in page:
            self.cursor=run['seq']
            if run.get('purpose')=='external_task_result' and not run.get('approvalBlockIds'):
                continue
            try:result=self._discover(run)
            except Exception:result={'status':'unknown'}
            if 'otherPurpose' not in result:
                self.runs.record_registration(run,result['status'])
            results.append({'workflowId':run['workflowId'],'runId':run['runId'],**result})
        if len(page)<self.batch_size:self.cursor=0
        return results

    def run(self,stop_event,*,interval=1.0,report=None):
        if interval<=0:raise ValueError('Positive discovery interval required')
        while not stop_event.is_set():
            try:result=self.tick()
            except Exception:result=[{'status':'unknown'}]
            if report is not None:report(result)
            stop_event.wait(interval)
