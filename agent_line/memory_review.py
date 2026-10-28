"""Immutable human memory-review audit metadata, never a memory content store.

Source metadata must come from trusted task identity fields at the API boundary.
No method writes to QwenPaw or changes task execution status.
"""
import base64
from datetime import datetime, timezone
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

from .adapters.sim import segment
from .store import Conflict


DEADLINE_PREFIX = '候选写入截止（UTC）：'


def content_deadline(content, *, now=None):
    """Inspect a user-declared write deadline, never native recall validity."""
    if not isinstance(content, str) or not content.strip():
        return {'state': 'unavailable', 'expiresAt': None}
    lines = content.strip().split('\n')
    marked = [index for index, line in enumerate(lines)
              if line.startswith(DEADLINE_PREFIX)]
    if not marked:
        return {'state': 'not_set', 'expiresAt': None}
    if marked != [0]:
        return {'state': 'invalid', 'expiresAt': None}
    if not '\n'.join(lines[1:]).strip():
        return {'state': 'invalid', 'expiresAt': None}
    value = lines[0].removesuffix('\r')[len(DEADLINE_PREFIX):]
    if re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z', value) is None:
        return {'state': 'invalid', 'expiresAt': None}
    try:
        deadline = datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp()
    except (ValueError, OverflowError):
        return {'state': 'invalid', 'expiresAt': None}
    checked = time.time() if now is None else now
    if type(checked) not in (int, float) or not math.isfinite(checked):
        return {'state': 'invalid', 'expiresAt': None}
    return {'state': 'expired' if checked >= deadline else 'active', 'expiresAt': value}


def require_unexpired_content(content):
    if content_deadline(content)['state'] in ('invalid', 'expired', 'unavailable'):
        raise ValueError('memory_candidate_deadline_invalid_or_expired')


def require_completed_content(content):
    """Reject unfinished review forms without interpreting native memories."""
    if (not isinstance(content, str) or not content.strip() or '\x00' in content
            or re.search(r'【请填写[^】]*】', content)):
        raise ValueError('memory_review_content_incomplete')


def _json(value):
    def validate(item):
        if item is None or type(item) in (str,bool,int):return
        if type(item) is float:
            if not math.isfinite(item):raise ValueError('Finite source numbers required')
            return
        if type(item) is list:
            for child in item:validate(child)
            return
        if type(item) is dict:
            for key,child in item.items():
                if type(key) is not str:raise ValueError('Source keys must be strings')
                validate(child)
            return
        raise ValueError('Source must contain only JSON values')
    if type(value) is not dict:raise ValueError('Source identity metadata must be a dictionary')
    try:
        validate(value)
        return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
    except (RecursionError,TypeError,OverflowError):
        raise ValueError('Invalid source identity metadata') from None


class MemoryReviewLedger:
    def __init__(self,path,*,clock=time.time):
        self.path=Path(path).absolute()
        self.clock=clock
        info=self._check_file()
        self._file_identity=(info.st_dev,info.st_ino)
        with self.connect() as db:
            if db.execute('PRAGMA user_version').fetchone()[0]!=1:
                raise ValueError('Unsupported memory review schema')
            db.execute('SELECT project_id,role_id,task_id,actor,decision_id,content_hash,source,created_at FROM memory_reviews LIMIT 0')

    def _check_file(self):
        if self.path.resolve(strict=True)!=self.path:
            raise ValueError('Memory review ledger must use a canonical path without symlinks')
        info=self.path.stat()
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode)!=0o600
                or info.st_uid!=os.getuid()):
            raise ValueError('Memory review ledger must be an owned 0600 regular file')
        if hasattr(self,'_file_identity') and self._file_identity!=(info.st_dev,info.st_ino):
            raise ValueError('Memory review ledger was replaced; reopen explicitly')
        return info

    @classmethod
    def initialize(cls,path,*,clock=time.time):
        path=Path(path).absolute()
        if path.parent.resolve(strict=True)!=path.parent:
            raise ValueError('Memory review parent must be canonical')
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.close(fd)
        db=sqlite3.connect(path)
        try:
            db.executescript('''CREATE TABLE memory_reviews(
                project_id TEXT NOT NULL, role_id TEXT NOT NULL, task_id TEXT NOT NULL,
                actor TEXT NOT NULL, decision_id TEXT NOT NULL UNIQUE,
                content_hash TEXT NOT NULL, source TEXT NOT NULL, created_at REAL NOT NULL,
                PRIMARY KEY(role_id,task_id));
                CREATE INDEX memory_reviews_project ON memory_reviews(project_id,role_id,task_id);
                PRAGMA user_version=1;''')
            db.commit()
        finally:db.close()
        return cls(path,clock=clock)

    @contextmanager
    def connect(self):
        self._check_file()
        db=sqlite3.connect(self.path.as_uri()+'?mode=rw',uri=True,timeout=15)
        db.row_factory=sqlite3.Row
        try:yield db;db.commit()
        except BaseException:db.rollback();raise
        finally:db.close()

    @staticmethod
    def _record(row):
        record=dict(row);record['source']=json.loads(record['source']);return record

    def reserve(self,*,project_id,role_id,task_id,actor,decision_id,content_sha256,source):
        for identifier in (project_id,role_id,task_id,decision_id):segment(identifier)
        if (not isinstance(actor,str) or not actor or actor!=actor.strip() or len(actor)>256
                or any(ord(char)<32 for char in actor)):
            raise ValueError('Nonempty trusted actor required')
        if not isinstance(content_sha256,str) or not re.fullmatch(r'[a-f0-9]{64}',content_sha256):
            raise ValueError('Lowercase SHA-256 digest required')
        encoded=_json(source)
        fields={'project_id':project_id,'role_id':role_id,'task_id':task_id,'actor':actor,
                'decision_id':decision_id,'content_hash':content_sha256,'source':encoded}
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM memory_reviews WHERE role_id=? AND task_id=?',(role_id,task_id)).fetchone()
            if old:
                if any(old[key]!=value for key,value in fields.items()):
                    raise Conflict('Memory review identity and approved metadata are immutable')
                return self._record(old)
            created=self.clock()
            if type(created) not in (int,float) or not math.isfinite(created) or created<0:
                raise ValueError('Finite audit timestamp required')
            try:
                db.execute('INSERT INTO memory_reviews VALUES(?,?,?,?,?,?,?,?)',(*fields.values(),created))
            except sqlite3.IntegrityError:
                raise Conflict('Decision identity already belongs to another memory review') from None
            return self._record(db.execute('SELECT * FROM memory_reviews WHERE role_id=? AND task_id=?',(role_id,task_id)).fetchone())

    def get(self,role_id,task_id):
        segment(role_id);segment(task_id)
        with self.connect() as db:
            row=db.execute('SELECT * FROM memory_reviews WHERE role_id=? AND task_id=?',(role_id,task_id)).fetchone()
            if row is None:raise KeyError((role_id,task_id))
            return self._record(row)

    @staticmethod
    def _cursor(role,task):
        return base64.urlsafe_b64encode(json.dumps([role,task],separators=(',',':')).encode()).decode().rstrip('=')

    def list(self,scopes,*,after=None,limit=50):
        if type(limit) is not int or not 1<=limit<=500:raise ValueError('Invalid review page limit')
        if not isinstance(scopes,(set,frozenset,list,tuple)):raise ValueError('Project scopes required')
        for scope in scopes:segment(scope)
        projects=sorted(set(scopes));role=task=''
        if after is not None:
            try:
                if not isinstance(after,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,1024}',after):raise ValueError()
                decoded=json.loads(base64.urlsafe_b64decode(after+'='*(-len(after)%4)))
                if type(decoded) is not list or len(decoded)!=2:raise ValueError()
                role,task=decoded;segment(role);segment(task)
                if self._cursor(role,task)!=after:raise ValueError()
            except (ValueError,TypeError,UnicodeError):raise ValueError('Invalid review cursor') from None
        if not projects:return {'reviews':[],'next_cursor':None}
        with self.connect() as db:
            rows=db.execute('SELECT * FROM memory_reviews WHERE project_id IN ('+
                ','.join('?' for _ in projects)+') AND (role_id>? OR (role_id=? AND task_id>?)) ORDER BY role_id,task_id LIMIT ?',
                [*projects,role,role,task,limit+1]).fetchall()
        page=rows[:limit]
        return {'reviews':[self._record(row) for row in page],
                'next_cursor':self._cursor(page[-1]['role_id'],page[-1]['task_id']) if len(rows)>limit else None}
