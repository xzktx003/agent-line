"""Project-local team graph metadata stored in the existing Sim journal."""
import hashlib
import json
import re
import time

from .adapters.sim import segment
from .store import Conflict, canonical

CONTEXT = 'team-view'
MAX_EDGES = 200
MAX_ROLES = 100
MAX_TASKS = 500
IDENTITY_FIELDS = frozenset({'taskId', 'executionId', 'runId', 'nodeExecutionId',
                             'attemptId', 'executorId', 'processId', 'containerId',
                             'upstreamTaskId', 'codexThreadId', 'codexTurnId'})


class CorruptTeamJournal(RuntimeError):
    """The local team graph history cannot be trusted."""


def namespace(project):
    return 'team-' + hashlib.sha256(project.encode()).hexdigest()


def _edges(value):
    if not isinstance(value, list) or len(value) > MAX_EDGES:
        raise ValueError('Invalid team edges')
    result = []
    seen = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {'sourceAgentId', 'targetAgentId'}:
            raise ValueError('Invalid team edge')
        source, target = item['sourceAgentId'], item['targetAgentId']
        segment(source)
        segment(target)
        if source == target or (source, target) in seen:
            raise ValueError('Invalid duplicate or self edge')
        seen.add((source, target))
        result.append({'sourceAgentId': source, 'targetAgentId': target})
    return sorted(result, key=lambda edge: (edge['sourceAgentId'], edge['targetAgentId']))


class TeamView:
    def __init__(self, journal, projects, role_bindings, task_store=None):
        self.journal = journal
        self.projects = projects
        self.role_bindings = role_bindings
        self.task_store = task_store

    def _configured_roles(self, project):
        grouped = {}
        for binding_id, role in self.role_bindings.items():
            if not isinstance(role, dict) or role.get('projectId') != project:
                continue
            role_id = role.get('roleAgentId')
            try:
                segment(role_id)
                segment(binding_id)
            except ValueError:
                continue
            item = grouped.setdefault(role_id, {'roleAgentId': role_id, 'bindingIds': [],
                                                  'enabled': False})
            item['bindingIds'].append(binding_id)
            item['enabled'] = item['enabled'] or role.get('enabled', True) is True
            if isinstance(role.get('roleRevision'), str):
                item['roleRevision'] = role['roleRevision']
        roles = [grouped[key] for key in sorted(grouped)]
        for role in roles:
            role['bindingIds'].sort()
        if len(roles) > MAX_ROLES:
            raise ValueError('Too many configured team roles')
        return roles

    def _history(self, project, db):
        rows = db.execute('SELECT workflow,run,context,input,status,receipt FROM operations '
                          'WHERE workflow=? AND context=?',
                          (namespace(project), CONTEXT)).fetchall()
        revision, edges, operations = 0, [], {}
        try:
            parsed = [(row, json.loads(row['receipt']) if row['receipt'] is not None else None)
                      for row in rows]
            if any(not isinstance(receipt, dict) or type(receipt.get('revision')) is not int
                   for _, receipt in parsed):
                raise ValueError
            parsed.sort(key=lambda item: item[1]['revision'])
        except (ValueError, TypeError, json.JSONDecodeError):
            raise CorruptTeamJournal('Invalid team graph history') from None
        for row, receipt in parsed:
            try:
                payload = json.loads(row['input'])
                if (row['status'] != 'accepted' or not isinstance(payload, dict)
                        or set(payload) != {'projectId', 'subject', 'request'}
                        or payload['projectId'] != project
                        or not isinstance(payload['subject'], str) or not payload['subject']
                        or not isinstance(payload['request'], dict)):
                    raise ValueError
                request = payload['request']
                if (set(request) != {'operationId', 'expectedRevision', 'edges'}
                        or request['operationId'] != row['run']
                        or type(request['expectedRevision']) is not int
                        or request['expectedRevision'] != revision):
                    raise ValueError
                clean_edges = _edges(request['edges'])
                if request['edges'] != clean_edges:
                    raise ValueError
                if (not isinstance(receipt, dict) or set(receipt) != {'projectId', 'operationId', 'revision', 'edges'}
                        or receipt['projectId'] != project or receipt['operationId'] != row['run']
                        or type(receipt['revision']) is not int or receipt['revision'] != revision + 1
                        or receipt['edges'] != clean_edges):
                    raise ValueError
                revision += 1
                edges = clean_edges
                operations[row['run']] = {'payload': payload, 'receipt': receipt}
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                raise CorruptTeamJournal('Invalid team graph history') from None
        return revision, edges, operations

    def get(self, project):
        roles = self._configured_roles(project)
        with self.journal.connect() as db:
            revision, edges, _ = self._history(project, db)
        tasks, truncated = self._active_tasks(project)
        known = {role['roleAgentId'] for role in roles}
        referenced = {agent for edge in edges for agent in (edge['sourceAgentId'], edge['targetAgentId'])}
        referenced.update(task['roleAgentId'] for task in tasks if task['roleAgentId'])
        return {'projectId': project, 'revision': revision, 'edges': edges, 'roles': roles,
                'orphanAgentIds': sorted(referenced - known), 'activeTasks': tasks,
                'tasksAvailable': self.task_store is not None, 'tasksTruncated': truncated}

    def operation(self, project, operation_id):
        segment(operation_id)
        with self.journal.connect() as db:
            _, _, operations = self._history(project, db)
        record = operations.get(operation_id)
        if record is None:
            return None
        return record['receipt']

    def replace(self, project, actor, operation_id, expected_revision, requested_edges, authorize):
        segment(operation_id)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError('Invalid team revision')
        clean_edges = _edges(requested_edges)
        request = {'operationId': operation_id, 'expectedRevision': expected_revision, 'edges': clean_edges}
        payload = {'projectId': project, 'subject': actor.subject, 'request': request}
        known = {role['roleAgentId'] for role in self._configured_roles(project)}
        with self.journal.mutation_scope(), self.journal.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            # Authorization and project scope are rechecked under the same write transaction.
            authorize()
            revision, edges, operations = self._history(project, db)
            previous = operations.get(operation_id)
            if previous is not None:
                if previous['payload'] != payload:
                    raise Conflict('Team operation identity reused')
                return previous['receipt']
            if expected_revision != revision:
                raise Conflict('Team graph revision changed')
            old_edges = { (edge['sourceAgentId'], edge['targetAgentId']) for edge in edges }
            for edge in clean_edges:
                pair = (edge['sourceAgentId'], edge['targetAgentId'])
                if edge['sourceAgentId'] not in known or edge['targetAgentId'] not in known:
                    if pair not in old_edges:
                        raise ValueError('New team edge references an unknown role')
            receipt = {'projectId': project, 'operationId': operation_id,
                       'revision': revision + 1, 'edges': clean_edges}
            db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?)',
                       (namespace(project), operation_id, CONTEXT, canonical(payload),
                        'accepted', canonical(receipt), time.time()))
            return receipt

    def _active_tasks(self, project):
        if self.task_store is None:
            return [], False
        with self.task_store.connect() as db:
            rows = db.execute(
                "SELECT id,payload,execution_identity,execution_status,verification_status,"
                "memory_status,resources_released FROM tasks WHERE "
                "json_extract(payload,'$.projectId')=? AND "
                "(execution_status NOT IN ('succeeded','failed','canceled') OR resources_released=0 "
                "OR EXISTS (SELECT 1 FROM workspace_leases lease WHERE lease.task_id=tasks.id)) "
                "ORDER BY id LIMIT ?", (project, MAX_TASKS + 1)).fetchall()
        truncated = len(rows) > MAX_TASKS
        tasks = []
        for row in rows[:MAX_TASKS]:
            try:
                segment(row['id'])
                payload = json.loads(row['payload'])
                identity = json.loads(row['execution_identity'])
                if not isinstance(payload, dict) or not isinstance(identity, dict):
                    raise ValueError
                if (not isinstance(row['execution_status'], str)
                        or not isinstance(row['verification_status'], str)
                        or not isinstance(row['memory_status'], str)):
                    raise ValueError
            except (ValueError, TypeError, json.JSONDecodeError):
                raise CorruptTeamJournal('Invalid active task projection') from None
            role_id = payload.get('roleAgentId')
            try:
                segment(role_id)
            except ValueError:
                role_id = None
            workflow = payload.get('workflow')
            run_id = workflow.get('runId') if isinstance(workflow, dict) else None
            try:
                segment(run_id)
            except ValueError:
                run_id = None
            safe_identity = {key: value for key, value in identity.items()
                             if key in IDENTITY_FIELDS and isinstance(value, str)
                             and re.fullmatch(r'[A-Za-z0-9_-]{1,160}', value)} if isinstance(identity, dict) else {}
            tasks.append({'taskId': row['id'], 'roleAgentId': role_id, 'workflowRunId': run_id,
                          'executionStatus': row['execution_status'],
                          'verificationStatus': row['verification_status'],
                          'memoryStatus': row['memory_status'],
                          'resourcesReleased': bool(row['resources_released']),
                          'executionIdentity': safe_identity})
        return tasks, truncated
