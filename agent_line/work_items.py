"""Immutable business goals and atomic run links in the existing Sim journal.

This module stores metadata only. Sim owns graph execution; verification remains
an independent trusted policy, never generated from textual acceptance criteria.
"""
import hashlib
import json
import math
import re
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from .adapters.sim import segment
from .store import AdmissionClosed, Conflict, canonical

ITEM_CONTEXT = 'work-item'
LINK_CONTEXT = 'work-item-run'


def item_id(value):
    if not isinstance(value, str) or re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value) is None:
        raise ValueError('Invalid business work item identity')
    return value


def item_namespace(project):
    segment(project)
    return 'work-items-' + hashlib.sha256(project.encode()).hexdigest()


def link_namespace(workflow):
    segment(workflow)
    return 'work-item-runs-' + hashlib.sha256(workflow.encode()).hexdigest()


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def definition(project, identity, title, goal, criteria):
    segment(project)
    item_id(identity)
    if (not isinstance(title, str) or not title.strip() or len(title) > 160 or '\0' in title
            or not isinstance(goal, str) or not goal.strip() or len(goal) > 4000 or '\0' in goal
            or type(criteria) is not list or not 1 <= len(criteria) <= 8
            or any(not isinstance(value, str) or not value.strip() or len(value) > 500
                   or '\0' in value for value in criteria)
            or len(set(criteria)) != len(criteria)):
        raise ValueError('Invalid bounded goal or acceptance criteria')
    result = {'schemaVersion': 1, 'projectId': project, 'workItemId': identity,
              'title': title, 'goal': goal, 'acceptanceCriteria': list(criteria)}
    encoded = canonical(result)
    if len(encoded.encode('utf-8')) > 8000:
        raise ValueError('Business work item definition exceeds the context bound')
    # Reject unpaired surrogates rather than persist an unreadable goal.
    json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8')
    return result


def read_item(db, project, identity):
    row = db.execute('SELECT input,status,receipt,updated FROM operations '
                     'WHERE workflow=? AND run=? AND context=?',
                     (item_namespace(project), item_id(identity), ITEM_CONTEXT)).fetchone()
    if row is None:
        raise KeyError(identity)
    try:
        value = json.loads(row['input'])
        if (type(value) is not dict or set(value) != {'schemaVersion', 'projectId', 'workItemId',
                'title', 'goal', 'acceptanceCriteria'} or type(value['schemaVersion']) is not int
                or value['schemaVersion'] != 1 or value['projectId'] != project
                or value['workItemId'] != identity):
            raise ValueError
        clean = definition(project, identity, value['title'], value['goal'], value['acceptanceCriteria'])
        if (row['status'] != 'accepted' or row['input'] != canonical(clean)
                or row['receipt'] != row['input'] or type(row['updated']) not in (int, float)
                or not math.isfinite(row['updated']) or row['updated'] < 0):
            raise ValueError
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise Conflict('Business work item record cannot be verified') from None
    return {'source': 'local_work_item', **{key: value[key] for key in value if key != 'schemaVersion'},
            'definitionSha256': digest(row['input']), 'createdAt': row['updated']}


def task_context(record):
    return {key: record[key] for key in ('projectId', 'workItemId', 'title', 'goal',
                                         'acceptanceCriteria', 'definitionSha256')}


def _link(db, workflow, run):
    segment(run)
    row = db.execute('SELECT input,status,receipt FROM operations WHERE workflow=? AND run=? AND context=?',
                     (link_namespace(workflow), run, LINK_CONTEXT)).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row['input'])
        if (type(value) is not dict or set(value) != {'projectId', 'bindingId', 'workflowId', 'runId',
                'workItemId', 'definitionSha256', 'inputSha256'}
                or row['status'] != 'accepted' or row['receipt'] != row['input']
                or canonical(value) != row['input'] or value['workflowId'] != workflow or value['runId'] != run):
            raise ValueError
        for key in ('projectId', 'bindingId'):
            segment(value[key])
        item_id(value['workItemId'])
        for key in ('definitionSha256', 'inputSha256'):
            if not isinstance(value[key], str) or re.fullmatch(r'[0-9a-f]{64}', value[key]) is None:
                raise ValueError
    except (ValueError, TypeError, KeyError, RecursionError):
        raise Conflict('Business run association cannot be verified') from None
    return value


def bind_run(db, workflow, run, encoded_input, reference, *, existing):
    """Called inside the root-intent transaction; no network or separate commit."""
    link = _link(db, workflow, run)
    if reference is None:
        if link is not None:
            raise Conflict('Associated run requires its original business task')
        return
    if type(reference) is not dict or set(reference) != {'projectId', 'bindingId', 'workItemId', 'definitionSha256'}:
        raise ValueError('Explicit trusted business task reference required')
    segment(reference['bindingId'])
    record = read_item(db, reference['projectId'], reference['workItemId'])
    if record['definitionSha256'] != reference['definitionSha256']:
        raise Conflict('Business task definition differs from its original record')
    expected = {**reference, 'workflowId': workflow, 'runId': run, 'inputSha256': digest(encoded_input)}
    if existing:
        if link != expected:
            raise Conflict('A historical run cannot acquire or change a business task')
        return
    if link is not None:
        raise Conflict('Business run association has no matching root intent')
    encoded = canonical(expected)
    db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?)',
               (link_namespace(workflow), run, LINK_CONTEXT, encoded, 'accepted', encoded, time.time()))


def read_run(db, project, workflow, run, *, binding_id=None):
    link = _link(db, workflow, run)
    if link is None:
        return None
    if link['projectId'] != project or (binding_id is not None and link['bindingId'] != binding_id):
        raise KeyError(run)
    root = db.execute("SELECT input,status,receipt,updated FROM operations WHERE workflow=? AND run=? AND context=''",
                      (workflow, run)).fetchone()
    if (root is None or not isinstance(root['input'], str)
            or digest(root['input']) != link['inputSha256']):
        raise Conflict('Business association has no matching submission input')
    try:
        if (root['status'] not in ('intent', 'accepted', 'rejected', 'uncertain')
                or type(root['updated']) not in (int, float)
                or not math.isfinite(root['updated']) or root['updated'] < 0):
            raise ValueError
        # Legacy transports may return no receipt; preserve that contract.
        if root['receipt'] is not None and canonical(json.loads(root['receipt'])) != root['receipt']:
            raise ValueError
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise Conflict('Business submission record cannot be verified') from None
    record = read_item(db, project, link['workItemId'])
    if record['definitionSha256'] != link['definitionSha256']:
        raise Conflict('Business association definition is unavailable')
    return record


def run_item(journal, project, workflow, run, *, binding_id=None):
    with journal.connect() as db:
        db.execute('BEGIN')
        return read_run(db, project, workflow, run, binding_id=binding_id)


class CreateItemRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    workItemId: str = Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')
    title: str = Field(min_length=1, max_length=160)
    goal: str = Field(min_length=1, max_length=4000)
    acceptanceCriteria: list[str] = Field(min_length=1, max_length=8)


class WorkItems:
    def __init__(self, journal, projects):
        self.journal, self.projects = journal, dict(projects)

    def get(self, project, identity):
        with self.journal.connect() as db:
            db.execute('BEGIN')
            return read_item(db, project, identity)

    def create(self, project, body):
        value = definition(project, body.workItemId, body.title, body.goal, body.acceptanceCriteria)
        encoded = canonical(value)
        try:
            record = self.get(project, body.workItemId)
        except KeyError:
            pass
        else:
            if record['definitionSha256'] != digest(encoded):
                raise Conflict('Business identity reused with another definition')
            return {'workItem': record, 'reused': True}
        with self.journal.mutation_scope(), self.journal.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                record = read_item(db, project, body.workItemId)
            except KeyError:
                db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?)',
                           (item_namespace(project), body.workItemId, ITEM_CONTEXT,
                            encoded, 'accepted', encoded, time.time()))
                return {'workItem': read_item(db, project, body.workItemId), 'reused': False}
            if record['definitionSha256'] != digest(encoded):
                raise Conflict('Business identity reused with another definition')
            return {'workItem': record, 'reused': True}

    def list(self, project, after=None, limit=10):
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError('Invalid business task page size')
        with self.journal.connect() as db:
            db.execute('BEGIN')
            params = [item_namespace(project), ITEM_CONTEXT]
            where = 'workflow=? AND context=?'
            if after is not None:
                cursor = db.execute('SELECT rowid FROM operations WHERE ' + where + ' AND run=?',
                                    (*params, item_id(after))).fetchone()
                if cursor is None:
                    raise KeyError(after)
                where += ' AND rowid<?'
                params.append(cursor['rowid'])
            rows = db.execute('SELECT run FROM operations WHERE ' + where + ' ORDER BY rowid DESC LIMIT ?',
                              (*params, limit + 1)).fetchall()
            items = [read_item(db, project, row['run']) for row in rows[:limit]]
            return {'source': 'local_work_item_directory', 'projectId': project, 'items': items,
                    'nextCursor': items[-1]['workItemId'] if len(rows) > limit else None}

    def runs(self, project, identity, after=None, limit=10):
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError('Invalid business run page size')
        with self.journal.connect() as db:
            db.execute('BEGIN')
            read_item(db, project, identity)
            if db.execute('SELECT 1 FROM operations WHERE context=? AND NOT json_valid(input) LIMIT 1',
                          (LINK_CONTEXT,)).fetchone() is not None:
                raise Conflict('Business run directory contains an unreadable association')
            where = "l.context=? AND json_extract(l.input,'$.projectId')=? AND json_extract(l.input,'$.workItemId')=?"
            params = [LINK_CONTEXT, project, identity]
            if after is not None:
                segment(after)
                cursor = db.execute('SELECT l.rowid FROM operations l WHERE ' + where + ' AND l.run=?',
                                    (*params, after)).fetchone()
                if cursor is None:
                    raise KeyError(after)
                where += ' AND l.rowid<?'
                params.append(cursor['rowid'])
            rows = db.execute('SELECT l.* FROM operations l WHERE ' + where + ' ORDER BY l.rowid DESC LIMIT ?',
                              (*params, limit + 1)).fetchall()
            runs = []
            for row in rows[:limit]:
                try:
                    value = json.loads(row['input'])
                    workflow, run = value['workflowId'], value['runId']
                    if row['run'] != run:
                        raise ValueError
                except (ValueError, TypeError, KeyError):
                    raise Conflict('Business run directory record cannot be verified') from None
                record = read_run(db, project, workflow, run)
                if record is None or record['workItemId'] != identity or row['workflow'] != link_namespace(workflow):
                    raise Conflict('Business run directory identity mismatch')
                root = db.execute("SELECT status,updated FROM operations WHERE workflow=? AND run=? AND context=''",
                                  (workflow, run)).fetchone()
                if (root is None or root['status'] not in ('intent', 'accepted', 'rejected', 'uncertain')
                        or type(root['updated']) not in (int, float)
                        or not math.isfinite(root['updated']) or root['updated'] < 0):
                    raise Conflict('Business submission state cannot be verified')
                runs.append({'bindingId': value['bindingId'], 'workflowId': workflow, 'runId': run,
                             'submissionStatus': root['status'], 'recordedAt': root['updated']})
            return {'source': 'local_work_item_runs', 'projectId': project, 'workItemId': identity,
                    'executionStateIncluded': False, 'runs': runs,
                    'nextCursor': runs[-1]['runId'] if len(rows) > limit else None}

    def router(self, authorize):
        router = APIRouter(prefix='/extension/v1/work-items')

        def scoped(project, scopes):
            if (project not in scopes or project not in self.projects
                    or self.projects[project].get('enabled', True) is not True):
                raise HTTPException(404, '业务任务项目不可用')

        def call(function, *args):
            try:
                return function(*args)
            except KeyError:
                raise HTTPException(404, '业务任务或分页记录不可用') from None
            except AdmissionClosed:
                raise HTTPException(503, '维护或恢复核对中，暂不登记新业务任务') from None
            except Conflict:
                raise HTTPException(409, '业务任务身份或关联记录不一致，请保留原标识核对') from None
            except (ValueError, UnicodeError, RecursionError):
                raise HTTPException(422, '目标或验收条件无效或过长，请缩短后核对') from None

        @router.get('/projects')
        def projects(scopes=Depends(authorize)):
            return {'source': 'configured_projects',
                    'projects': [{'projectId': key, 'name': self.projects[key].get('name')
                                  if isinstance(self.projects[key].get('name'), str)
                                  and 0 < len(self.projects[key]['name']) <= 256 else key}
                                 for key in sorted(set(scopes) & self.projects.keys())
                                 if self.projects[key].get('enabled', True) is True]}

        @router.get('/projects/{project}/items')
        def listing(project: str, after: str | None = Query(default=None, max_length=128),
                    limit: int = Query(default=10, ge=1, le=50), scopes=Depends(authorize)):
            scoped(project, scopes)
            return call(self.list, project, after, limit)

        @router.post('/projects/{project}/items')
        def create(project: str, body: CreateItemRequest, scopes=Depends(authorize)):
            scoped(project, scopes)
            return call(self.create, project, body)

        @router.get('/projects/{project}/items/{identity}')
        def detail(project: str, identity: str, scopes=Depends(authorize)):
            scoped(project, scopes)
            return call(self.get, project, identity)

        @router.get('/projects/{project}/items/{identity}/runs')
        def runs(project: str, identity: str, after: str | None = Query(default=None, max_length=160),
                 limit: int = Query(default=10, ge=1, le=50), scopes=Depends(authorize)):
            scoped(project, scopes)
            return call(self.runs, project, identity, after, limit)

        return router
