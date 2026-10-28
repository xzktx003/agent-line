"""Pinned official memory actions. File deletion is not proof of index erasure.

indexSourceAbsent is claimed only from the live ReMe projections: the exact
digest path has no file node or chunk, and no live keyword posting belongs to
that path. A different live file may cite the task id as a supersession or
audit reference; that citation is not the deleted source. Top-k search and
graph_snapshot stay diagnostic. If those projections cannot be read, the
result stays deleting. A confirmed absence releases only that task's admission
block and keeps the content hash. The same body reappearing, or that deleted
path returning to the live index, blocks the role again. Other unconfirmed
deletions still block the role. The caller owns durable approval and retry
accounting. QwenPaw has no atomic hash-conditional delete against other native
writers.
"""
import asyncio
import hashlib
import os
from pathlib import Path
import re
import secrets
import stat
import sqlite3
from contextlib import closing

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from qwenpaw.app.agent_context import get_agent_for_request
from qwenpaw.app.routers.workspace import read_memory_file
from qwenpaw.config.config import load_agent_config

SEGMENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,159}\Z')
DIGEST = r'^[a-f0-9]{64}$'


def segment(value):
    if not isinstance(value, str) or not SEGMENT.fullmatch(value) or '..' in value:
        raise ValueError('Invalid fixed memory identity')
    return value


def private_state_path(value):
    path = Path(value)
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError('Canonical private governance state required')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
        raise ValueError('Governance state must be owner-held mode 0600')
    return path


def initialize_state(path):
    path = Path(path)
    if not path.is_absolute() or path.parent.resolve(strict=True) != path.parent:
        raise ValueError('Canonical governance state path required')
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('CREATE TABLE blocks(role TEXT NOT NULL, task TEXT NOT NULL, content_hash TEXT NOT NULL, intent TEXT NOT NULL CHECK(intent=\'delete\'), confirmed INTEGER NOT NULL DEFAULT 0 CHECK(confirmed IN (0,1)), PRIMARY KEY(role,task))')
        db.execute('PRAGMA user_version=2')
    return path


class DeleteRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    expectedContentSha256: str = Field(pattern=DIGEST)
    decisionId: str = Field(min_length=1, max_length=160)
    actor: str = Field(min_length=1, max_length=256)


class MemoryGovernance:
    available = True

    def __init__(self, config):
        if not isinstance(config, dict) or set(config) != {'managed_role_ids', 'service_token_file', 'state_file'}:
            raise ValueError('Invalid memory governance configuration')
        roles = config['managed_role_ids']
        if not isinstance(roles, list) or not roles or len(set(roles)) != len(roles):
            raise ValueError('Fixed memory roles required')
        self.roles = frozenset(segment(role) for role in roles)
        path = Path(config['service_token_file'])
        if not path.is_absolute() or path.is_symlink():
            raise ValueError('Private absolute memory credential required')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
                raise ValueError('Memory credential must be owner-held mode 0600')
            self.token = os.read(fd, 4097).decode().strip()
            if not 32 <= len(self.token) <= 4096 or not self.token.isascii() or any(c.isspace() for c in self.token):
                raise ValueError('Invalid memory service credential')
        finally:
            os.close(fd)
        self.state_file = private_state_path(config['state_file'])
        with closing(self.connection()) as db:
            db.execute('SELECT role,task,content_hash,intent FROM blocks LIMIT 0')
        self.lock = asyncio.Lock()

    def connection(self):
        private_state_path(self.state_file)
        db = sqlite3.connect(self.state_file.as_uri() + '?mode=rw', uri=True)
        try:
            db.execute('BEGIN IMMEDIATE')
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version == 1:
                db.execute('ALTER TABLE blocks ADD COLUMN confirmed INTEGER NOT NULL DEFAULT 0 CHECK(confirmed IN (0,1))')
                db.execute('PRAGMA user_version=2')
            elif version != 2:
                raise ValueError('Unsupported governance state schema')
            db.execute('SELECT role,task,content_hash,intent,confirmed FROM blocks LIMIT 0')
            db.commit()
        except Exception:
            db.close()
            raise
        return db

    async def is_blocked(self, role):
        segment(role)
        try:
            with closing(self.connection()) as db:
                return db.execute(
                    'SELECT 1 FROM blocks WHERE role=? AND confirmed=0 LIMIT 1', (role,)).fetchone() is not None
        except Exception:
            return True

    def forbids_content(self, role, content):
        segment(role)
        if not isinstance(content, str) or not content.strip():
            return False
        digest = hashlib.sha256(content.strip().encode()).hexdigest()
        with closing(self.connection()) as db:
            return db.execute(
                'SELECT 1 FROM blocks WHERE role=? AND content_hash=? AND confirmed=1 LIMIT 1',
                (role, digest)).fetchone() is not None

    def _is_confirmed(self, role, task, expected):
        with closing(self.connection()) as db:
            row = db.execute(
                'SELECT content_hash, confirmed FROM blocks WHERE role=? AND task=?', (role, task)).fetchone()
        return bool(row and row[0] == expected and row[1] == 1)

    def _set_confirmed(self, role, task, expected, confirmed):
        with closing(self.connection()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute(
                'SELECT content_hash FROM blocks WHERE role=? AND task=?', (role, task)).fetchone()
            if not row or row[0] != expected:
                return False
            db.execute('UPDATE blocks SET confirmed=? WHERE role=? AND task=?', (int(confirmed), role, task))
            return True

    def block(self, role, task, expected):
        with closing(self.connection()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT content_hash FROM blocks WHERE role=? AND task=?', (role, task)).fetchone()
            if old:
                if old[0] != expected:
                    raise HTTPException(409, '删除意向已绑定其他正文摘要')
                return False
            db.execute("INSERT INTO blocks(role,task,content_hash,intent) VALUES(?,?,?,'delete')", (role, task, expected))
            return True

    def authenticate(self, x_agent_line_memory_token: str | None = Header(default=None)):
        if not x_agent_line_memory_token or not secrets.compare_digest(x_agent_line_memory_token.encode(), self.token.encode()):
            raise HTTPException(401, '记忆治理服务凭证无效')

    async def inspect(self, request, role, task, expected, *, delete=False):
        try:
            segment(role)
            segment(task)
        except ValueError:
            raise HTTPException(422, '记忆身份无效') from None
        if role not in self.roles:
            raise HTTPException(404, '记忆角色不在受管范围')
        path = f'digest/agent-line-task-{task}.md'
        result = {'roleAgentId': role, 'taskId': task, 'memoryPath': path,
                  'status': 'needs_review', 'reason': 'file_state_unverified',
                  'fileAbsent': False, 'indexSourceAbsent': False, 'contentHash': expected,
                  'roleBlocked': await self.is_blocked(role)}
        # Explicit agent_id takes priority over request headers and active role.
        workspace = await get_agent_for_request(request, agent_id=role)
        config = load_agent_config(role)
        if config.running.reme_light_memory_config.digest_dir != "digest":
            result["reason"] = "fixed_digest_root_mismatch"
            return result
        manager = workspace.memory_manager
        if manager is None or not callable(getattr(manager, 'list_actions', None)) or not callable(getattr(manager, 'run_action', None)):
            return result
        actions = await manager.list_actions()
        if not isinstance(actions, dict) or not {'read', 'delete', 'search'}.issubset(actions):
            result['reason'] = 'official_actions_unavailable'
            return result
        # Public route returns complete content; ReMe read alone may truncate.
        scope = dict(request.scope)
        scope['state'] = {**scope.get('state', {}), 'agent_id': role}
        scope['headers'] = [(k, v) for k, v in scope.get('headers', []) if k.lower() != b'x-agent-id']
        fixed_request = Request(scope)

        async def read_full():
            try:
                value = await read_memory_file(f'agent-line-task-{task}.md', fixed_request, section='digest')
            except HTTPException as exc:
                if exc.status_code == 404:
                    return 'missing', None
                return 'unknown', None
            content = getattr(value, 'content', None)
            return ('found', content) if isinstance(content, str) else ('unknown', None)

        async with self.lock:
            result['roleBlocked'] = await self.is_blocked(role)
            with closing(self.connection()) as db:
                prior = db.execute('SELECT content_hash FROM blocks WHERE role=? AND task=?', (role, task)).fetchone()
            if prior and prior[0] != expected:
                raise HTTPException(409, '删除意向已绑定其他正文摘要')
            state, content = await read_full()
            if state == 'unknown':
                return result
            if state == 'found':
                if hashlib.sha256(content.strip().encode()).hexdigest() != expected:
                    result['reason'] = 'content_hash_mismatch'
                    return result
                response = await manager.run_action('read', path=path)
                if response is None or response.success is not True or not isinstance(response.answer, str) or response.answer.strip() != content.strip():
                    result['reason'] = 'official_read_not_complete_or_mismatch'
                    return result
                if not delete:
                    if self._is_confirmed(role, task, expected):
                        self._set_confirmed(role, task, expected, False)
                        result.update(status='deleting', reason='deleted_content_revived', roleBlocked=True)
                        return result
                    result['reason'] = 'file_present'
                    return result
                # Recheck complete content immediately before the official delete.
                state, latest = await read_full()
                if state != 'found' or latest != content:
                    result['reason'] = 'content_changed_before_delete'
                    return result
                fresh = self.block(role, task, expected)
                result['roleBlocked'] = True
                if not fresh:
                    result.update(status='deleting', reason='prior_delete_intent_pending')
                    return result
                try:
                    await manager.run_action('delete', path=path)
                except Exception:
                    result.update(status='deleting', reason='native_delete_unknown')
                    return result
                state, content = await read_full()
            if state != 'missing':
                result['reason'] = 'file_absence_not_confirmed'
                return result
            if delete:
                # A missing file can still have searchable chunks. A trusted POST
                # records that uncertainty even when there is no file to remove.
                self.block(role, task, expected)
                result['roleBlocked'] = True
            result.update(status='deleting', reason='index_sync_pending', fileAbsent=True)
            # Real search is diagnostic only; top-k misses never prove erasure.
            response = await manager.run_action('search', query=path, limit=20, min_score=0.0)
            result['searchVerified'] = response is not None and response.success is True
            # Graph snapshots omit digest-root files, so they never prove absence.
            if 'graph_snapshot' in actions:
                response = await manager.run_action('graph_snapshot')
                result['graphVerified'] = response is not None and response.success is True
            if await self._live_index_source_absent(manager, path, task):
                if self._set_confirmed(role, task, expected, True):
                    result['roleBlocked'] = await self.is_blocked(role)
                result.update(status='deleted', reason='index_source_absent', indexSourceAbsent=True)
            elif self._is_confirmed(role, task, expected) and self._set_confirmed(role, task, expected, False):
                result['roleBlocked'] = await self.is_blocked(role)
                result['reason'] = 'index_source_still_present'
            return result


    async def _live_index_source_absent(self, manager, path, task):
        """True only when every live file and keyword projection lacks this source.

        Lazy-deleted keyword slots do not count. A projection that cannot be
        inspected fails closed instead of claiming erasure.
        """
        reme = getattr(manager, '_reme', None)
        components = getattr(getattr(reme, 'context', None), 'components', None)
        if not isinstance(components, dict):
            return False
        stores = components.get('file_store')
        keywords = components.get('keyword_index')
        if not isinstance(stores, dict) or not stores or not isinstance(keywords, dict) or not keywords:
            return False
        chunk_ids = set()
        chunk_paths = {}
        for store in stores.values():
            get_nodes = getattr(store, 'get_nodes', None)
            chunks = getattr(store, 'file_chunks', None)
            if not callable(get_nodes) or not isinstance(chunks, dict):
                return False
            nodes = await get_nodes([path])
            if not isinstance(nodes, list):
                return False
            if any(getattr(node, 'path', None) == path for node in nodes):
                return False
            for chunk in chunks.values():
                chunk_path = getattr(chunk, 'path', None)
                if chunk_path == path:
                    return False
                chunk_id = getattr(chunk, 'id', None)
                if isinstance(chunk_id, str):
                    chunk_ids.add(chunk_id)
                    if isinstance(chunk_path, str):
                        chunk_paths[chunk_id] = chunk_path
        for index in keywords.values():
            vocab = getattr(index, 'vocab', None)
            deleted = getattr(index, '_deleted', None)
            doc_ids = getattr(index, '_doc_ids', None)
            postings = getattr(index, '_posting_doc_idxs', None)
            live_map = getattr(index, '_doc_id_to_idx', None)
            if (not isinstance(vocab, dict) or deleted is None or not isinstance(doc_ids, list)
                    or not isinstance(postings, dict) or not isinstance(live_map, dict)):
                return False
            if chunk_ids and not vocab:
                return False
            # Hyphenated ids are stored as separate words. Only long words identify
            # the source; short pieces such as "task" also occur in unrelated files.
            identity_tokens = re.findall(r'(?u)\b\w{16,}\b', task) or [task]
            for identity in identity_tokens:
                token_id = vocab.get(identity)
                slots = None if token_id is None else postings.get(token_id)
                if slots is None:
                    continue
                for slot in list(slots):
                    slot = int(slot)
                    if slot < 0 or slot >= len(doc_ids) or slot >= len(deleted):
                        return False
                    if bool(deleted[slot]):
                        continue
                    doc_id = doc_ids[slot]
                    posted = chunk_paths.get(doc_id) if isinstance(doc_id, str) else None
                    if posted is None or posted == path:
                        return False
            for doc_id in live_map:
                if doc_id not in chunk_ids:
                    return False
        return True

    def router(self):
        router = APIRouter(dependencies=[Depends(self.authenticate)])

        @router.get('/memory/{role}/{task}/deletion')
        async def observe(role: str, task: str, request: Request,
                          expectedContentSha256: str = Query(pattern=DIGEST)):
            return await self.inspect(request, role, task, expectedContentSha256)

        @router.post('/memory/{role}/{task}/delete')
        async def delete(role: str, task: str, body: DeleteRequest, request: Request):
            try:
                segment(body.decisionId)
                if not body.actor.strip():
                    raise ValueError()
            except ValueError:
                raise HTTPException(422, '删除审核身份无效') from None
            return await self.inspect(request, role, task, body.expectedContentSha256, delete=True)

        return router
