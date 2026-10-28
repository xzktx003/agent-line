"""Durable approved-memory submission coordinator, separate from task execution.

This database belongs to one QwenPaw installation. Approval is a trusted service
operation, not a model tool or public HTTP endpoint. An unresolved intent only
permits reads, including after restart. A confirmed rejection requires an
explicit versioned retry before another submission opportunity.
"""
import fcntl
import hashlib
import json
import os
import re
import stat
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .adapters.qwenpaw_memory import MemoryApproval, content_hash
from .adapters.sim import segment
from .store import Conflict, canonical
from .memory_review import require_completed_content, require_unexpired_content

SCHEMA_VERSION = 2
SCHEMA = '''
CREATE TABLE installation (schema_version INTEGER NOT NULL);
CREATE TABLE memory_commits (
    role_id TEXT NOT NULL, task_id TEXT NOT NULL,
    content TEXT NOT NULL, content_hash TEXT NOT NULL, decision_id TEXT NOT NULL,
    status TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
    reason TEXT NOT NULL, created_at REAL NOT NULL,
    PRIMARY KEY(role_id,task_id)
);
CREATE TABLE memory_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    role_id TEXT NOT NULL, task_id TEXT NOT NULL, kind TEXT NOT NULL,
    created_at REAL NOT NULL
);
'''


DELETION_SCHEMA = """CREATE TABLE memory_deletions (
    role_id TEXT NOT NULL, task_id TEXT NOT NULL, actor TEXT NOT NULL,
    decision_id TEXT NOT NULL UNIQUE, expected_hash TEXT NOT NULL,
    status TEXT NOT NULL, receipt TEXT NOT NULL, created_at REAL NOT NULL,
    updated_at REAL NOT NULL, version INTEGER NOT NULL,
    PRIMARY KEY(role_id,task_id))"""


class MemoryCommitStore:
    def __init__(self,path):
        self.path=Path(path).resolve()
        if not self.path.is_file():
            raise ValueError('Memory database missing; explicit initialization required')
        try:
            with self.connect() as db:
                rows=db.execute('SELECT schema_version FROM installation').fetchall()
                if len(rows)!=1 or rows[0][0] not in (1,2):
                    raise ValueError('Unsupported memory database schema')
                self.schema_version=rows[0][0]
                if self.schema_version==2:db.execute('SELECT role_id,task_id,status FROM memory_deletions LIMIT 0')
                db.execute('SELECT role_id,task_id,content_hash,decision_id,status,version FROM memory_commits LIMIT 0')
                db.execute('SELECT seq FROM memory_events LIMIT 0')
        except sqlite3.DatabaseError as exc:
            raise ValueError('Invalid memory database') from exc

    @classmethod
    def initialize(cls,path):
        path=Path(path).resolve()
        with path.open('xb'):
            pass
        path.chmod(0o600)
        with sqlite3.connect(path) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript(SCHEMA)
            db.execute(DELETION_SCHEMA)
            db.execute('INSERT INTO installation VALUES (?)',(SCHEMA_VERSION,))
        return cls(path)

    @classmethod
    def upgrade(cls,path):
        store=cls(path)
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            version=db.execute('SELECT schema_version FROM installation').fetchone()[0]
            if version==1:
                db.execute(DELETION_SCHEMA)
                db.execute('UPDATE installation SET schema_version=2')
        return cls(path)

    @contextmanager
    def memory_scope(self,role,task,*,exclusive=False):
        segment(role);segment(task)
        digest=hashlib.sha256(canonical([role]).encode()).hexdigest()
        path=self.path.with_name(self.path.name+'.memory-'+digest+'.lock')
        fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o600:
                raise ValueError('Private owned memory lock required')
            fcntl.flock(fd,fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:yield
            finally:fcntl.flock(fd,fcntl.LOCK_UN)
        finally:os.close(fd)

    @staticmethod
    def _deletion_schema(db):
        if db.execute('SELECT schema_version FROM installation').fetchone()[0]!=2:
            raise ValueError('Explicit schema 2 upgrade required for deletion')

    @staticmethod
    def _deletion(row):
        result=dict(row);result['receipt']=json.loads(result['receipt']);return result

    def get_deletion(self,role,task):
        segment(role);segment(task)
        with self.connect() as db:
            self._deletion_schema(db)
            row=db.execute('SELECT * FROM memory_deletions WHERE role_id=? AND task_id=?',(role,task)).fetchone()
            if row is None:raise KeyError((role,task))
            return self._deletion(row)

    def reserve_delete(self,role,task,actor,decision_id,expected_hash):
        segment(role);segment(task);segment(decision_id)
        if not isinstance(actor,str) or not actor.strip() or actor!=actor.strip() or len(actor)>256:
            raise ValueError('Trusted deletion actor required')
        if not isinstance(expected_hash,str) or not re.fullmatch(r'[a-f0-9]{64}',expected_hash):
            raise ValueError('Expected content hash required')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');self._deletion_schema(db)
            old=db.execute('SELECT * FROM memory_deletions WHERE role_id=? AND task_id=?',(role,task)).fetchone()
            if old:
                if any(old[k]!=v for k,v in {'actor':actor,'decision_id':decision_id,'expected_hash':expected_hash}.items()):
                    raise Conflict('Deletion approval is immutable')
                return self._deletion(old),False
            row=self._row(db,role,task)
            if row['status']!='committed' or row['content_hash']!=expected_hash:
                raise Conflict('Only matching committed memory can be deleted')
            now=time.time()
            try:db.execute('INSERT INTO memory_deletions VALUES(?,?,?,?,?,?,?,?,?,?)',(role,task,actor,decision_id,expected_hash,'intent','{}',now,now,1))
            except sqlite3.IntegrityError:raise Conflict('Deletion decision already used') from None
            db.execute("UPDATE memory_commits SET content='',status='deleting',version=version+1,reason='approved_deletion_intent' WHERE role_id=? AND task_id=?",(role,task))
            self._event(db,role,task,'deletion_intent')
            result=db.execute('SELECT * FROM memory_deletions WHERE role_id=? AND task_id=?',(role,task)).fetchone()
            return self._deletion(result),True

    def record_deletion(self,role,task,receipt):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE');self._deletion_schema(db)
            row=db.execute('SELECT * FROM memory_deletions WHERE role_id=? AND task_id=?',(role,task)).fetchone()
            if row is None:raise KeyError((role,task))
            if row['status']=='deleted':return self._deletion(row)
            matching=(isinstance(receipt,dict) and receipt.get('roleAgentId')==role
                and receipt.get('taskId')==task and receipt.get('contentHash')==row['expected_hash'])
            deleted=(matching and receipt.get('status')=='deleted' and receipt.get('fileAbsent') is True
                     and receipt.get('indexSourceAbsent') is True)
            status='deleted' if deleted else ('needs_review' if matching and receipt.get('status')=='needs_review' else 'deleting')
            # Never persist arbitrary upstream data or a model-supplied memory body.
            metadata={'roleAgentId':role,'taskId':task,'contentHash':row['expected_hash'],
                      'status':status,'fileAbsent':bool(matching and receipt.get('fileAbsent') is True),
                      'indexSourceAbsent':bool(matching and receipt.get('indexSourceAbsent') is True),
                      'roleBlocked':receipt.get('roleBlocked') if matching and type(receipt.get('roleBlocked')) is bool else None}
            db.execute('UPDATE memory_deletions SET status=?,receipt=?,updated_at=?,version=version+1 WHERE role_id=? AND task_id=?',
                       (status,canonical(metadata),time.time(),role,task))
            db.execute("UPDATE memory_commits SET content='',status=?,reason=?,version=version+1 WHERE role_id=? AND task_id=?",
                       (status,'deletion_'+status,role,task))
            self._event(db,role,task,'deletion_'+status)
            return self._deletion(db.execute('SELECT * FROM memory_deletions WHERE role_id=? AND task_id=?',(role,task)).fetchone())

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path.as_uri()+'?mode=rw',uri=True,timeout=15)
        db.row_factory=sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _row(db,role,task):
        row=db.execute('SELECT * FROM memory_commits WHERE role_id=? AND task_id=?',(role,task)).fetchone()
        if row is None:raise KeyError((role,task))
        return dict(row)

    @staticmethod
    def _event(db,role,task,kind):
        db.execute('INSERT INTO memory_events(role_id,task_id,kind,created_at) VALUES (?,?,?,?)',
                   (role,task,kind,time.time()))

    @staticmethod
    def _hash_deleted_elsewhere(db,role,task,digest):
        if db.execute('SELECT schema_version FROM installation').fetchone()[0]!=2:
            return False
        # Preserve the original deletion's own state; its body is already empty.
        if db.execute('SELECT 1 FROM memory_deletions WHERE role_id=? AND task_id=?',(role,task)).fetchone():
            return False
        return db.execute('SELECT 1 FROM memory_deletions WHERE role_id=? AND expected_hash=? LIMIT 1',
                          (role,digest)).fetchone() is not None

    def _block_deleted_hash(self,db,role,task,row):
        if (row['status']!='needs_review' or row['content']!=''
                or row['reason']!='deleted_content_hash_blocked'):
            db.execute("UPDATE memory_commits SET content='',status='needs_review',reason='deleted_content_hash_blocked',version=version+1 WHERE role_id=? AND task_id=?",(role,task))
            self._event(db,role,task,'deleted_content_hash_blocked')
        return self._row(db,role,task)

    def approve(self,role,task,content,approval):
        segment(role);segment(task)
        digest=content_hash(content)
        if (not isinstance(approval,MemoryApproval) or approval.task_id!=task
                or approval.content_sha256!=digest or not isinstance(approval.decision_id,str)
                or not approval.decision_id.strip()):
            return {'status':'needs_review','reason':'matching_approval_required'}
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM memory_commits WHERE role_id=? AND task_id=?',(role,task)).fetchone()
            if old:
                if old['content_hash']!=digest or old['decision_id']!=approval.decision_id:
                    self._event(db,role,task,'approval_conflict')
                    return {'status':'needs_review','reason':'immutable_approval_conflict'}
                if self._hash_deleted_elsewhere(db,role,task,digest):
                    return self._block_deleted_hash(db,role,task,old)
                return dict(old)
            blocked=self._hash_deleted_elsewhere(db,role,task,digest)
            status='needs_review' if blocked else 'approved'
            reason='deleted_content_hash_blocked' if blocked else 'approved_content_registered'
            db.execute('INSERT INTO memory_commits VALUES (?,?,?,?,?,?,?,?,?)',
                       (role,task,'' if blocked else content.strip(),digest,approval.decision_id,status,1,reason,time.time()))
            self._event(db,role,task,'deleted_content_hash_blocked' if blocked else 'approved')
            return self._row(db,role,task)

    def get(self,role,task):
        with self.connect() as db:return self._row(db,role,task)

    def claim(self,role,task):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=self._row(db,role,task)
            if self._hash_deleted_elsewhere(db,role,task,row['content_hash']):
                return 'none',self._block_deleted_hash(db,role,task,row)
            if row['status']=='approved':
                require_completed_content(row['content'])
                require_unexpired_content(row['content'])
                db.execute("UPDATE memory_commits SET status='intent',version=version+1,reason='submission_intent' WHERE role_id=? AND task_id=?",(role,task))
                self._event(db,role,task,'submission_intent')
                return 'write',self._row(db,role,task)
            if row['status'] in ('intent','pending'):
                return 'reconcile',row
            return 'none',row

    def record(self,role,task,version,result):
        status=result.get('status')
        if status not in ('pending','committed','needs_review','failed'):
            raise ValueError('Invalid memory outcome')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=self._row(db,role,task)
            if row['version']!=version or row['status'] not in ('intent','pending','failed'):
                return False
            if row['status']=='failed' and status not in ('committed','needs_review'):
                return False
            if status=='failed' and (row['status']!='intent' or result.get('reason')!='official_write_rejected'):
                return False
            db.execute('UPDATE memory_commits SET status=?,reason=?,version=version+1 WHERE role_id=? AND task_id=?',
                       (status,str(result.get('reason',''))[:256],role,task))
            self._event(db,role,task,status)
            return True

    def retry_claim(self,role,task,expected_version):
        if type(expected_version) is not int or expected_version<1:
            raise ValueError('Positive memory commit version required')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=self._row(db,role,task)
            if (row['status']!='failed' or row['reason']!='official_write_rejected'
                    or row['version']!=expected_version):
                raise Conflict('Memory retry requires the original failed commit version')
            require_completed_content(row['content'])
            require_unexpired_content(row['content'])
            if (not row['decision_id'] or content_hash(row['content'])!=row['content_hash']
                    or self._hash_deleted_elsewhere(db,role,task,row['content_hash'])):
                raise Conflict('Original approved memory is unavailable for retry')
            if self.schema_version>=2 and db.execute(
                    'SELECT 1 FROM memory_deletions WHERE role_id=? AND task_id=?',(role,task)).fetchone():
                raise Conflict('Memory deletion prevents retry')
            db.execute("UPDATE memory_commits SET status='intent',version=version+1,reason='explicit_retry_intent' WHERE role_id=? AND task_id=?",(role,task))
            self._event(db,role,task,'explicit_retry_intent')
            return self._row(db,role,task)


class MemoryCoordinator:
    def __init__(self,store,client):
        self.store=store
        self.client=client
        self.role=segment(client.role)

    def approve(self,task_id,content,approval):
        """Called only after authenticated review; no inference or auto-approval."""
        with self.store.memory_scope(self.role,task_id):
            return self.store.approve(self.role,task_id,content,approval)

    def commit(self,task_id):
        segment(task_id)
        with self.store.memory_scope(self.role,task_id):
            return self._commit(task_id)

    def reconcile(self,task_id):
        """Observe an existing submission; never use an approval's first PUT."""
        segment(task_id)
        with self.store.memory_scope(self.role,task_id):
            row=self.store.get(self.role,task_id)
            if row['status']=='failed':
                return self._complete('reconcile',row)
            if row['status'] not in ('intent','pending'):
                return row
            return self._commit(task_id)

    def retry(self,task_id,*,expected_version):
        segment(task_id)
        # The role scope excludes deletion's exclusive lock; SQLite versions
        # admit only one request for this failed attempt across all processes.
        with self.store.memory_scope(self.role,task_id):
            row=self.store.retry_claim(self.role,task_id,expected_version)
            return self._complete('write',row)

    def _commit(self,task_id):
        mode,row=self.store.claim(self.role,task_id)
        if mode=='none':return row
        return self._complete(mode,row)

    def _complete(self,mode,row):
        task_id=row['task_id']
        try:
            if mode=='write':
                result=self.client.write_approved(task_id,row['content'],approval=MemoryApproval(
                    task_id,row['content_hash'],row['decision_id']))
            else:
                result=self.client.reconcile(task_id,row['content'])
            if (not isinstance(result,dict) or result.get('taskId')!=task_id
                    or result.get('roleAgentId')!=self.role
                    or result.get('contentSha256')!=row['content_hash']
                    or result.get('status') not in ('pending','committed','needs_review','failed')
                    or (result.get('status')=='failed' and
                        (mode!='write' or result.get('reason')!='official_write_rejected'))):
                result={'status':'pending','reason':'invalid_adapter_receipt'}
        except Exception:
            # Intent was already committed before the HTTP call; even an arbitrary
            # adapter failure permits only read reconciliation on the next call.
            result={'status':'pending','reason':'adapter_outcome_unknown'}
        if row['status']=='failed' and result['status']=='pending':
            return self.store.get(self.role,task_id)
        self.store.record(self.role,task_id,row['version'],result)
        return self.store.get(self.role,task_id)


class MemoryDeletionCoordinator:
    def __init__(self,store,client):
        self.store,self.client=store,client
        self.role=segment(client.role)

    def request_delete(self,task_id,*,actor,decision_id,expected_hash):
        with self.store.memory_scope(self.role,task_id,exclusive=True):
            row,fresh=self.store.reserve_delete(self.role,task_id,actor,decision_id,expected_hash)
            try:
                receipt=(self.client.delete_checked(task_id,expected_hash,decision_id,actor) if fresh
                         else self.client.observe_deletion(task_id,expected_hash))
            except Exception:receipt={}
            return self.store.record_deletion(self.role,task_id,receipt)

    def reconcile_delete(self,task_id):
        with self.store.memory_scope(self.role,task_id,exclusive=True):
            row=self.store.get_deletion(self.role,task_id)
            try:receipt=self.client.observe_deletion(task_id,row['expected_hash'])
            except Exception:receipt={}
            return self.store.record_deletion(self.role,task_id,receipt)
