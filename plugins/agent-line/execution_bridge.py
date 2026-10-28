"""Version-pinned QwenPaw execution receipt bridge; never schedules graph nodes."""
import asyncio
import hashlib
import json
import os
import re
from pathlib import Path
import secrets
import sqlite3
import sys
import tomllib

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field


def unaccepted_rejection(status_code, detail):
    """Refuse before a parent or Codex task exists, and keep the reason explicit."""
    raise HTTPException(status_code, detail, headers={'X-Agent-Line-Submission': 'rejected_unaccepted'})


def revision_http_error(exc):
    """Refuse a new execution without replacing the configured model."""
    from .role_revision import model_configuration_failure
    headers = ({'X-Agent-Line-Configuration': 'model_unavailable'}
               if model_configuration_failure(exc) else None)
    raise HTTPException(409, '无法核对角色行为修订，拒绝新执行', headers=headers) from None


def execution_workspace_within_role(role_workspace, execution_workspace):
    """Allow the role workspace or a real directory inside it.

    The role workspace stays the revision input. A project subdirectory can
    be the container mount without moving that role. A symlink, including one
    that resolves back inside, is refused.
    """
    role_input = Path(role_workspace)
    execution_input = Path(execution_workspace)
    if role_input.is_symlink() or execution_input.is_symlink():
        return False
    role = role_input.resolve(strict=True)
    execution = execution_input.resolve(strict=True)
    if not role.is_dir() or not execution.is_dir():
        return False
    if execution != role and role not in execution.parents:
        return False
    cursor = role
    for part in execution.relative_to(role).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            return False
    return True


def codex_usage_snapshot(entries, identity, metadata):
    """Only explicit thread/turn-bound provider records, never token_count hints."""
    if metadata.get('forked_from_id'):
        return None
    events = [(i, entry.get('payload', {})) for i, entry in enumerate(entries)
              if entry.get('type') == 'event_msg']
    starts = [(i, event) for i, event in events if event.get('type') == 'task_started']
    ends = [(i, event) for i, event in events if event.get('type') in ('task_complete', 'turn_aborted')]
    if (len(starts) != 1 or len(ends) != 1 or starts[0][0] >= ends[0][0]
            or any(event.get('turn_id') != identity['codexTurnId'] for _, event in starts + ends)):
        return None
    keys = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens', 'output_tokens',
            'reasoning_output_tokens', 'total_tokens')

    def counters(value):
        if (not isinstance(value, dict) or set(value) != set(keys)
                or any(type(value.get(key)) is not int or not 0 <= value[key] <= 9007199254740991 for key in keys)
                or value['cached_input_tokens'] > value['input_tokens']
                or value['reasoning_output_tokens'] > value['output_tokens']
                or value['total_tokens'] != value['input_tokens'] + value['output_tokens']):
            return None
        return {key: value[key] for key in keys}

    totals = {key: 0 for key in keys}
    responses = {}
    attribution = None
    for index, entry in enumerate(entries):
        if entry.get('type') != 'token_usage_record':
            continue
        value = entry.get('payload')
        if (not starts[0][0] < index < ends[0][0] or not isinstance(value, dict)
                or value.get('thread_id') != identity['codexThreadId']
                or value.get('turn_id') != identity['codexTurnId']
                or any(not isinstance(value.get(key), str) or not 1 <= len(value[key]) <= 256
                       for key in ('response_id', 'session_id', 'root_turn_id'))):
            return None
        current_attribution = (value['session_id'], value['root_turn_id'])
        if attribution is not None and current_attribution != attribution:
            return None
        attribution = current_attribution
        usage = counters(value.get('usage'))
        cumulative = counters(value.get('turn_token_usage'))
        if usage is None or cumulative is None:
            return None
        response = value['response_id']
        if response in responses:
            if responses[response] != value:
                return None
            continue  # Identical persisted replay is not another response.
        totals = {key: totals[key] + usage[key] for key in keys}
        if totals != cumulative:
            return None  # Missing prefix, reordering or inherited counts.
        responses[response] = value
    if not responses:
        return None
    return {'schemaVersion': 1, 'source': 'codex-rollout',
            'coverage': 'reported-turn-records',
            'codexThreadId': identity['codexThreadId'], 'codexTurnId': identity['codexTurnId'],
            'tokens': totals, 'samplesObserved': len(responses)}


class ReceiptLedger:
    @staticmethod
    def initialize(path):
        path = Path(path)
        if path.exists():
            raise FileExistsError('Receipt database already exists')
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE receipts(task_id TEXT PRIMARY KEY, payload TEXT NOT NULL, record TEXT NOT NULL)')
            db.execute('CREATE TABLE role_bindings(task_id TEXT PRIMARY KEY, role_id TEXT NOT NULL, session_id TEXT NOT NULL UNIQUE, role_revision TEXT)')
            db.execute('CREATE TABLE revoked_delegations(task_id TEXT PRIMARY KEY)')
            db.execute('PRAGMA user_version=4')

    @staticmethod
    def migrate(path):
        path = Path(path).resolve(strict=True)
        with sqlite3.connect(path.as_uri() + '?mode=rw', uri=True) as db:
            db.execute('BEGIN IMMEDIATE')
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version == 1:
                db.execute('CREATE TABLE role_bindings(task_id TEXT PRIMARY KEY, role_id TEXT NOT NULL, session_id TEXT NOT NULL UNIQUE)')
                version = 2
            if version == 2:
                db.execute('CREATE TABLE revoked_delegations(task_id TEXT PRIMARY KEY)')
                version = 3
            if version == 3:
                db.execute('ALTER TABLE role_bindings ADD COLUMN role_revision TEXT')
                db.execute('PRAGMA user_version=4')
            elif version != 4:
                raise ValueError('Unsupported receipt migration')

    def bind(self, task_id, role, session, revision):
        if not isinstance(revision, str) or not re.fullmatch(r'qwenpaw-role-v1:[0-9a-f]{64}', revision):
            unaccepted_rejection(409, '缺少有效角色修订')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM revoked_delegations WHERE task_id=?', (task_id,)).fetchone():
                unaccepted_rejection(409, '已撤销任务不能重新绑定协调角色')
            row = db.execute('SELECT role_id, session_id, role_revision FROM role_bindings WHERE task_id=?', (task_id,)).fetchone()
            if row:
                if row != (role, session, revision): unaccepted_rejection(409, '任务已绑定其他协调角色或会话')
                return False
            try: db.execute('INSERT INTO role_bindings VALUES(?,?,?,?)', (task_id, role, session, revision))
            except sqlite3.IntegrityError: unaccepted_rejection(409, '协调会话已绑定其他任务')
            return True

    def revoke_delegation(self, task_id):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            binding = db.execute('SELECT role_id,session_id FROM role_bindings WHERE task_id=?', (task_id,)).fetchone()
            db.execute('INSERT OR IGNORE INTO revoked_delegations VALUES(?)', (task_id,))
            claimed = db.execute('SELECT 1 FROM receipts WHERE task_id=?', (task_id,)).fetchone() is not None
            return binding, claimed

    def bound_task(self, role, session):
        with self.connection() as db:
            row = db.execute('SELECT task_id FROM role_bindings WHERE role_id=? AND session_id=?', (role, session)).fetchone()
        if row is None: raise HTTPException(403, '当前角色会话没有已登记任务绑定')
        return row[0]

    def __init__(self, path):
        self.path = Path(path).resolve(strict=True)
        with self.connection() as db:
            if db.execute('PRAGMA user_version').fetchone()[0] != 4:
                raise ValueError('Unsupported execution receipt schema')
            db.execute('SELECT task_id FROM receipts LIMIT 1')

    def connection(self):
        return sqlite3.connect(self.path.as_uri() + '?mode=rw', uri=True, timeout=10)

    def claim(self, task_id, payload):
        canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT payload,record FROM receipts WHERE task_id=?', (task_id,)).fetchone()
            if existing:
                if existing[0] != canonical:
                    raise HTTPException(409, '任务身份已绑定其他执行载荷')
                return json.loads(existing[1]), False
            if db.execute('SELECT 1 FROM revoked_delegations WHERE task_id=?', (task_id,)).fetchone():
                raise HTTPException(409, '任务已取消，不允许迟到委派')
            record = {'taskId':task_id, 'status':'unknown', 'identity':{}, 'output':None, 'terminalConfirmed':False, 'resourcesReleased':False}
            if 'executionControls' in payload:
                record['executionControls'] = dict(payload['executionControls'])
            db.execute('INSERT INTO receipts VALUES(?,?,?)', (task_id, canonical, json.dumps(record)))
            return record, True

    def get(self, task_id):
        with self.connection() as db:
            row = db.execute('SELECT payload,record FROM receipts WHERE task_id=?', (task_id,)).fetchone()
        if row is None:
            raise HTTPException(404, '无此执行登记；不得据此自动重新提交')
        return json.loads(row[0]), {'taskId':task_id, **json.loads(row[1])}

    def patch(self, task_id, **changes):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT record FROM receipts WHERE task_id=?', (task_id,)).fetchone()
            if row is None: raise KeyError(task_id)
            record = {'taskId':task_id, **json.loads(row[0])}
            if record.get('terminalConfirmed') is True:
                # An observer that read an older receipt cannot undo a terminal
                # observation. Resource readback/retirement remains updateable.
                for key in ('terminalConfirmed', 'status', 'nativeStatus', 'output',
                            'identity', 'codexUsage', 'codexUsageFrozen',
                            'executorInterrupted', 'reason'):
                    changes.pop(key, None)
            if record.get('codexUsageFrozen') is True:
                changes.pop('codexUsage', None)
                changes.pop('codexUsageFrozen', None)
            elif changes.get('terminalConfirmed') is True and 'codexUsage' in changes:
                # First terminal observation freezes even an unavailable value.
                # The receipt transaction serializes concurrent observers.
                changes['codexUsageFrozen'] = True
            record.update(changes)
            db.execute('UPDATE receipts SET record=? WHERE task_id=?', (json.dumps(record), task_id))
        return record


class ExecutionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    taskId: str = Field(min_length=1,max_length=128,pattern=r'^[a-zA-Z0-9_-]+$')
    roleSessionId: str = Field(min_length=1,max_length=256)
    instruction: str = Field(min_length=1,max_length=32000)


class RoleBindingRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    taskId: str = Field(min_length=1,max_length=128,pattern=r'^[a-zA-Z0-9_-]+$')
    roleAgentId: str = Field(min_length=1,max_length=128)
    roleRevision: str = Field(pattern=r'^qwenpaw-role-v1:[0-9a-f]{64}$')
    roleSessionId: str = Field(min_length=1,max_length=256)


class ExecutionBridge:
    def __init__(self, config):
        self.governance = None
        self.config = dict(config)
        self.ledger = ReceiptLedger(config['receipt_db'])
        self.token_path = Path(config['service_token_file']).resolve(strict=True)
        if self.token_path.stat().st_mode & 0o077:
            raise ValueError('Service token file must not be group/world readable')
        self.token = self.token_path.read_text().strip()
        if len(self.token) < 32: raise ValueError('Service token must have at least 32 characters')
        self.target = config['target_agent_id']
        self.roles = frozenset(config.get('managed_role_ids', []))
        readonly = config.get('readonly_role_ids', [])
        if (not isinstance(readonly, list)
                or any(not isinstance(role, str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,62}[A-Za-z0-9]', role) is None
                       for role in readonly)
                or len(set(readonly)) != len(readonly) or not set(readonly) <= self.roles):
            raise ValueError('readonly_role_ids 必须为受管角色内的唯一合法角色标识列表')
        self.readonly_roles = frozenset(readonly)
        self.application = None
        self._runtime_lock = asyncio.Lock()
        self.workspace = Path(config['workspace']).resolve(strict=True)
        self.codex_home = Path(config['codex_home']).resolve(strict=True)
        self.container_control = None
        self.container_launcher = None
        if config.get('container_control'):
            self.container_control = Path(config['container_control']).resolve(strict=True)
            self.container_launcher = Path(config['container_launcher']).resolve(strict=True)
            for path in (self.container_control, self.container_launcher):
                if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o022:
                    raise ValueError('Container control must be owned and not publicly writable')
        if not self.container_control and Path(os.environ.get('CODEX_HOME', '')).resolve() != self.codex_home:
            raise ValueError('QwenPaw process must use the configured isolated CODEX_HOME')

    def authorize(self, x_agent_line_service_token: str | None = Header(default=None)):
        if not secrets.compare_digest((x_agent_line_service_token or '').encode(), self.token.encode()):
            raise HTTPException(403, '需要执行服务凭证')

    def target_config(self):
        from qwenpaw.config.utils import load_agent_config
        config = load_agent_config(self.target)
        if config.backend != 'codex' or not execution_workspace_within_role(config.workspace_dir, self.workspace):
            raise HTTPException(409, '固定执行角色后端或工作目录与绑定不符')
        if self.container_launcher and Path(config.backend_settings.get('binary', '')).resolve() != self.container_launcher:
            raise HTTPException(409, '执行角色未绑定受控容器启动器')
        return config

    async def container_exit_receipt(self, record, action):
        """Read positive owned exit evidence; absence is never an exit receipt."""
        control = getattr(self, 'container_control', None)
        identity = record.get('identity', {})
        if not control or not all(isinstance(identity.get(key), str) and identity[key]
                for key in ('codexThreadId', 'codexTurnId')):
            return None
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable, str(control), action, '--thread', identity['codexThreadId'],
                '--turn', identity['codexTurnId'], '--workspace', str(self.workspace),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            output, _ = await asyncio.wait_for(process.communicate(), timeout=20)
            proof = json.loads(output) if process.returncode == 0 else {}
            candidate = proof.get('exitReceipt') or {}
            # Container exit may precede launcher pipe/process shutdown. Never
            # admit the next task until trusted control proves both exits.
            import math
            launcher = proof.get('launcherExitProof')
            if (not isinstance(launcher, dict) or set(launcher) != {
                    'schemaVersion', 'source', 'owner', 'containerId', 'pid',
                    'startTicks', 'uid', 'state', 'observedAt'}
                    or type(launcher.get('schemaVersion')) is not int or launcher['schemaVersion'] != 1
                    or launcher.get('source') != 'procfs-launcher-exit'
                    or launcher.get('owner') != proof.get('owner') or launcher.get('owner') != candidate.get('owner')
                    or launcher.get('containerId') != proof.get('containerId')
                    or launcher.get('containerId') != candidate.get('containerId')
                    or type(launcher.get('pid')) is not int or launcher['pid'] <= 0
                    or type(launcher.get('uid')) is not int or launcher['uid'] != os.getuid()
                    or not isinstance(launcher.get('startTicks'), str)
                    or re.fullmatch(r'[0-9]+', launcher['startTicks']) is None
                    or launcher.get('state') not in ('absent', 'pid_reused')
                    or type(launcher.get('observedAt')) not in (int, float)
                    or not math.isfinite(launcher['observedAt']) or launcher['observedAt'] <= 0):
                return None
            identities = candidate.get('identities', [])
            matching = [item for item in identities if isinstance(item, dict) and
                item.get('codexThreadId') == identity['codexThreadId'] and
                item.get('codexTurnId') == identity['codexTurnId']]
            valid = (type(proof.get('schemaVersion')) is int and proof['schemaVersion'] == 1 and
                type(candidate.get('schemaVersion')) is int and candidate['schemaVersion'] == 1 and
                proof.get('resourcesReleased') is True and isinstance(identities, list) and
                all(isinstance(item, dict) for item in identities) and len(matching) == 1 and
                proof.get('codexThreadId') == identity['codexThreadId'] and
                proof.get('codexTurnId') == identity['codexTurnId'] and
                re.fullmatch(r'[0-9a-f]{32}', proof.get('owner', '')) is not None and
                proof['owner'] == candidate.get('owner') and
                re.fullmatch(r'[0-9a-f]{64}', proof.get('containerId', '')) is not None and
                proof['containerId'] == candidate.get('containerId') and
                candidate.get('workspace') == str(self.workspace) and
                candidate.get('running') is False and type(candidate.get('pid')) is int and
                candidate['pid'] == 0 and candidate.get('status') in ('exited', 'dead') and
                isinstance(candidate.get('finishedAt'), str) and bool(candidate['finishedAt']) and
                candidate.get('source') == 'docker-inspect-before-remove')
            return candidate if valid else None
        except (OSError, ValueError, TypeError, AttributeError, asyncio.TimeoutError):
            if process is not None and process.returncode is None:
                process.kill()
                await process.communicate()
            return None

    async def retire_runtime(self, task_id, record, receipt, *, interrupted=False):
        """Discard an idle Codex adapter through QwenPaw's public runtime API.

        A departed launcher alone does not retire its asynchronous stdout reader.
        Keep the original task's lease until the next session can use a fresh
        adapter. Persist this fact so observing an old task cannot stop a newer
        session. This target is dedicated to the bridge's serialized workspace.
        """
        identity = record.get('identity', {})
        if (not isinstance(identity, dict)
                or set(identity) != {'upstreamTaskId', 'codexThreadId', 'codexTurnId'}
                or not all(isinstance(value, str) and value for value in identity.values())):
            return False
        if interrupted is not False:
            if (interrupted is not True or record.get('terminalConfirmed') is not False
                    or record.get('nativeStatus') != 'unknown' or not isinstance(receipt, dict)
                    or type(receipt.get('schemaVersion')) is not int or receipt['schemaVersion'] != 1
                    or receipt.get('source') != 'docker-inspect-before-remove'
                    or receipt.get('workspace') != str(self.workspace) or receipt.get('running') is not False
                    or type(receipt.get('pid')) is not int or receipt['pid'] != 0
                    or receipt.get('status') not in ('exited', 'dead')
                    or not isinstance(receipt.get('finishedAt'), str) or not receipt['finishedAt']
                    or not isinstance(receipt.get('owner'), str) or re.fullmatch(r'[0-9a-f]{32}', receipt['owner']) is None
                    or not isinstance(receipt.get('containerId'), str) or re.fullmatch(r'[0-9a-f]{64}', receipt['containerId']) is None
                    or not isinstance(receipt.get('identities'), list)
                    or not all(isinstance(item, dict) for item in receipt['identities'])):
                return False
            matches = [item for item in receipt['identities'] if
                       item.get('codexThreadId') == identity['codexThreadId']
                       and item.get('codexTurnId') == identity['codexTurnId']]
            if len(matches) != 1 or matches[0].get('nativeTerminal') is not False:
                return False
        expected = {'schemaVersion': 1, 'source': 'qwenpaw-harness-runtime-stop',
                    'taskId': task_id, 'targetAgentId': self.target,
                    'workspace': str(self.workspace), 'identity': identity,
                    'owner': receipt['owner'], 'containerId': receipt['containerId']}
        if not hasattr(self, '_runtime_lock'):
            self._runtime_lock = asyncio.Lock()
        async with self._runtime_lock:
            payload, latest = self.ledger.get(task_id)
            if interrupted and (latest.get('identity') != identity
                    or latest.get('terminalConfirmed') is not False or latest.get('nativeStatus') != 'unknown'
                    or payload.get('targetAgentId') != self.target
                    or payload.get('workspace') != str(self.workspace)):
                return False
            retired = latest.get('runtimeRetirement')
            if retired is not None:
                return (isinstance(retired, dict) and type(retired.get('schemaVersion')) is int
                        and retired in ({**expected, 'state': 'stopped'}, {**expected, 'state': 'unloaded'}))
            try:
                manager = self.application.state.multi_agent_manager
                workspace = manager.get_loaded_agent(self.target)
                if workspace is None:
                    state = 'unloaded'  # Read only: never lazy-load a completed target.
                else:
                    self.target_config()
                    if (workspace.agent_id != self.target
                            or not execution_workspace_within_role(workspace.workspace_dir, self.workspace)
                            or await workspace.task_tracker.has_active_tasks()):
                        return False
                    from qwenpaw.app.routers.console import get_console_chat_task
                    try:
                        native = await get_console_chat_task(identity['upstreamTaskId'])
                        if native.get('status') != 'finished':
                            return False
                    except HTTPException as error:
                        # Official background task handles are ephemeral across
                        # restart. A verified native terminal or the strictly
                        # checked nonterminal exit can cover that missing handle.
                        if error.status_code != 404 or not (record.get('terminalConfirmed') is True or interrupted):
                            return False
                    with self.ledger.connection() as db:
                        others = db.execute('SELECT task_id,payload,record FROM receipts WHERE task_id != ?',
                                            (task_id,)).fetchall()
                    for _, payload, other in others:
                        payload, other = json.loads(payload), json.loads(other)
                        if (payload.get('targetAgentId') == self.target
                                and payload.get('workspace') == str(self.workspace)
                                and other.get('resourcesReleased') is not True):
                            return False
                    await workspace.harness_runtime.stop()
                    if (manager.get_loaded_agent(self.target) is not workspace
                            or await workspace.task_tracker.has_active_tasks()):
                        return False
                    state = 'stopped'
                self.ledger.patch(task_id, runtimeRetirement={**expected, 'state': state})
                return True
            except Exception:
                # Keep admission closed on unavailable APIs, unknown stop or lost
                # persistence. Do not reach into the host's private client state.
                return False

    async def release_resources(self, task_id, record):
        # Only a native terminal permits active container teardown here.
        receipt = await self.container_exit_receipt(record, 'stop') if record.get('terminalConfirmed') else None
        if receipt is not None and not await self.retire_runtime(task_id, record, receipt):
            receipt = None
        return self.ledger.patch(task_id, resourcesReleased=receipt is not None,
                                 resourceExitReceipt=receipt)

    async def observe_interruption(self, task_id, record):
        # Do not stop a live executor to manufacture evidence of interruption.
        receipt = await self.container_exit_receipt(record, 'observe')
        if receipt is None:
            return self.ledger.patch(task_id, executorInterrupted=False,
                resourcesReleased=False, resourceExitReceipt=None, nativeStatus='unknown', reason=None)
        # The executor can finish normally while control.observe is awaited.
        # Re-read the complete mapping/metadata/turn-bound rollout after proven
        # exit; never decide interruption from the pre-exit event snapshot.
        return await self.observe(task_id, _observed_exit=receipt)

    def execution_controls(self, role):
        if role in getattr(self, 'readonly_roles', frozenset()):
            return {'sandbox': 'read-only', 'approval_policy': 'never'}
        return None

    def assert_readonly_mcp_absent(self):
        """A file sandbox cannot constrain remote MCP side effects."""
        from qwenpaw.config.utils import load_agent_config, load_config
        from qwenpaw.drivers.storage import list_card_paths, load_card
        target = load_agent_config(self.target)
        for mcp in (load_config().mcp, target.mcp):
            if mcp is not None and any(client.enabled is not False for client in mcp.clients.values()):
                raise ValueError('Readonly execution does not allow MCP servers')
        for path in list_card_paths(self.workspace / 'drivers'):
            card = load_card(path)
            if card.protocol == 'mcp' and card.enabled:
                raise ValueError('Readonly execution does not allow MCP drivers')
        def has_mcp(value):
            return isinstance(value, dict) and ('mcp_servers' in value or any(has_mcp(item) for item in value.values()))
        for path in (self.codex_home / 'config.toml', self.workspace / '.codex/config.toml'):
            if path.exists() or path.is_symlink():
                if path.is_symlink() or not path.is_file():
                    raise ValueError('Readonly Codex configuration unavailable')
                if has_mcp(tomllib.loads(path.read_text(encoding='utf-8'))):
                    raise ValueError('Readonly execution does not allow Codex MCP configuration')

    def current_revision(self, role):
        try:
            from .role_revision import role_revision
            revision = role_revision(role, self.target)
            controls = self.execution_controls(role)
            if controls is None:
                return revision
            self.assert_readonly_mcp_absent()
            value = {'roleRevision': revision, 'executionControls': controls}
            data = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
            return 'qwenpaw-role-v1:' + hashlib.sha256(data.encode()).hexdigest()
        except Exception as exc:
            revision_http_error(exc)

    def assert_role_revision(self, role, session):
        if role not in self.roles or not session:
            raise HTTPException(403, '缺少受管角色任务会话')
        with self.ledger.connection() as db:
            row = db.execute('SELECT task_id,role_revision FROM role_bindings WHERE role_id=? AND session_id=?',
                             (role, session)).fetchone()
            if row is None or row[1] is None:
                raise HTTPException(409, '旧任务未冻结角色修订，禁止新推理或委派')
            if db.execute('SELECT 1 FROM revoked_delegations WHERE task_id=?', (row[0],)).fetchone():
                raise HTTPException(409, '任务已经撤销')
        if row[1] != self.current_revision(role):
            raise HTTPException(409, '角色行为已变更，旧任务不能使用新修订')
        return row[1]

    def binding_descriptor(self):
        revisions = {role:self.current_revision(role) for role in sorted(self.roles)}
        self.target_config()
        from qwenpaw.config.utils import load_agent_config
        if any(load_agent_config(role).backend != 'qwenpaw' for role in self.roles):
            raise HTTPException(409, '受管协调角色后端与部署契约不符')
        descriptor = {'schemaVersion':1, 'targetAgentId':self.target,
                'managedRoleIds':sorted(self.roles), 'roleRevisions':revisions, 'workspace':str(self.workspace),
                'ownedContainer':bool(self.container_control and self.container_launcher)}
        controls = {role: self.execution_controls(role) for role in sorted(self.roles) if self.execution_controls(role) is not None}
        if controls:
            descriptor['roleExecutionControls'] = controls
        return descriptor

    def bind_parent(self, body, request):
        if body.roleAgentId not in self.roles:
            raise HTTPException(403, '协调角色不在服务端受管列表')
        if body.roleSessionId != 'agent-line-' + body.taskId:
            unaccepted_rejection(422, '协调会话必须由登记任务身份派生')
        if body.roleRevision != self.current_revision(body.roleAgentId):
            unaccepted_rejection(409, '角色修订与冻结任务不符')
        from qwenpaw.config.utils import load_agent_config
        if load_agent_config(body.roleAgentId).backend != 'qwenpaw':
            unaccepted_rejection(409, '协调角色必须使用原生 QwenPaw 后端')
        fresh = self.ledger.bind(body.taskId, body.roleAgentId, body.roleSessionId, body.roleRevision)
        # Admission is refreshed by the trusted service before running a parent.
        # No global application singleton or private host state is consulted.
        self.application = request.app
        return {'taskId':body.taskId, 'roleAgentId':body.roleAgentId,
                'roleSessionId':body.roleSessionId, 'roleRevision':body.roleRevision, 'newBinding':fresh}

    async def cancel_parent(self, task_id, request):
        binding, claimed = self.ledger.revoke_delegation(task_id)
        if binding is None:
            return {'taskId':task_id, 'delegationRevoked':True,
                    'parentStopAcknowledged':False, 'executionClaimed':claimed}
        role, session = binding
        if role not in self.roles:
            raise HTTPException(409, '已撤销委派；原协调角色不在当前服务配置内')
        from qwenpaw.app.routers.console import post_console_chat_stop
        from qwenpaw.tool_calls import CancelReason
        scoped = dict(request.scope)
        scoped['state'] = {**request.scope.get('state', {}), 'agent_id':role}
        acknowledged = False
        try:
            result = await post_console_chat_stop(Request(scoped, request.receive),
                chat_id=session, cancel_reason=CancelReason.USER,
                initiator='agent-line-service')
            acknowledged = result.get('stopped') is True
        except Exception:
            pass  # Revocation is durable even if native stop's response is lost.
        return {'taskId':task_id, 'delegationRevoked':True,
                'parentStopAcknowledged':acknowledged, 'executionClaimed':claimed}

    async def submit_from_parent(self, role, session, instruction):
        task_id = self.ledger.bound_task(role, session)
        if self.application is None:
            raise HTTPException(503, '重启后需由服务重新核对任务绑定，尚未启动执行')
        body = ExecutionRequest(taskId=task_id,roleSessionId=session,instruction=instruction)
        request = Request({'type':'http','method':'POST','path':'/api/agent-line/executions',
            'headers':[], 'state':{}, 'app':self.application})
        return await self.submit(body, request)

    async def submit(self, body, request):
        if not hasattr(self, '_runtime_lock'):
            self._runtime_lock = asyncio.Lock()
        async with self._runtime_lock:
            record, fresh = await self._submit(body, request)
        return record if fresh else await self.observe(body.taskId)

    async def _submit(self, body, request):
        payload = {**body.model_dump(), 'targetAgentId':self.target, 'workspace':str(self.workspace)}
        try:
            previous, record = self.ledger.get(body.taskId)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
        else:
            # Historical payloads retain the controls frozen before the native
            # call, even when the role's current policy has since changed.
            if 'executionControls' in previous:
                payload['executionControls'] = previous['executionControls']
            if previous != payload:
                raise HTTPException(409, '任务身份已绑定其他执行载荷')
            return record, False
        with self.ledger.connection() as db:
            parent = db.execute('SELECT role_id,session_id FROM role_bindings WHERE task_id=?', (body.taskId,)).fetchone()
        if parent is None or parent[1] != body.roleSessionId:
            raise HTTPException(409, '新执行缺少可信父角色绑定')
        self.assert_role_revision(parent[0], parent[1])
        self.target_config()
        if body.roleSessionId != 'agent-line-' + body.taskId:
            raise HTTPException(422, '执行会话必须由已登记任务身份派生')
        if self.governance is not None:
            with self.ledger.connection() as db:
                binding = db.execute('SELECT role_id,session_id FROM role_bindings WHERE task_id=?', (body.taskId,)).fetchone()
            if binding is None or binding[1] != body.roleSessionId or binding[0] not in self.roles:
                raise HTTPException(403, '缺少受管父角色的持久任务绑定')
            if await self.governance.is_blocked(binding[0]):
                raise HTTPException(409, '父角色记忆索引删除尚未核验，禁止新执行')
        payload = {**body.model_dump(), 'targetAgentId':self.target, 'workspace':str(self.workspace)}
        controls = self.execution_controls(parent[0])
        if controls is not None:
            payload['executionControls'] = controls
        record, fresh = self.ledger.claim(body.taskId, payload)
        if not fresh:
            return record, False
        # The durable claim precedes any native call. Exceptions and cancellation
        # leave unknown; a later POST cannot start a second native turn.
        from qwenpaw.app.routers.console import post_console_chat_task
        scoped = dict(request.scope)
        scoped['state'] = {**request.scope.get('state', {}), 'agent_id':self.target}
        native_request = Request(scoped, request.receive)
        native_payload = {
            'session_id':body.roleSessionId,
            'user_id':'agent-line-service',
            'input':[{'role':'user','content':[{'type':'text','text':body.instruction}]}],
            'timeout':self.config.get('timeout_seconds', 1800),
        }
        if controls is not None:
            native_payload['request_context'] = {'backend_controls': dict(controls)}
        try:
            receipt = await post_console_chat_task(native_payload, native_request)
            record = self.ledger.patch(body.taskId, status='accepted', identity={'upstreamTaskId':receipt['task_id']})
        except Exception:
            # The native call can start before its response is lost. Unknown is
            # deliberately retained and reconciled through the native session.
            return record, True
        return record, True

    async def observe(self, task_id, *, _observed_exit=None):
        payload, record = self.ledger.get(task_id)
        if record.get('resourcesReleased') or record.get('resourceExitReceipt') or record.get('executorInterrupted'):
            record = self.ledger.patch(task_id, resourcesReleased=False, resourceExitReceipt=None,
                                       executorInterrupted=False, reason=None)
        if payload['targetAgentId'] != self.target or payload['workspace'] != str(self.workspace):
            return record
        if record['terminalConfirmed']:
            return await self.release_resources(task_id, record)
        identity = dict(record['identity'])
        upstream_finished = False
        if identity.get('upstreamTaskId'):
            from qwenpaw.app.routers.console import get_console_chat_task
            try:
                upstream = await get_console_chat_task(identity['upstreamTaskId'])
                upstream_finished = upstream.get('status') == 'finished'
            except HTTPException:
                pass
        # Version-pinned persistent mapping, read only. Public adapter.history()
        # discards thread/turn IDs, so it cannot establish execution identity.
        mapping_path = self.workspace / 'harnesses/codex_sessions.json'
        try:
            mapping = json.loads(mapping_path.read_text())
            thread_id = mapping.get(payload['roleSessionId'])
        except (FileNotFoundError, json.JSONDecodeError):
            return record
        if not thread_id: return record
        if identity.get('codexThreadId') not in (None, thread_id):
            return self.ledger.patch(task_id, status='unknown', terminalConfirmed=False, resourcesReleased=False)
        identity['codexThreadId'] = thread_id
        # A second app-server may report a still-running persisted turn as
        # interrupted. Observe append-only native events without creating or
        # resuming any Codex process; this also works after QwenPaw restart.
        files = list((self.codex_home / 'sessions').rglob('*' + thread_id + '.jsonl'))
        if len(files) != 1 or not files[0].resolve().is_relative_to(self.codex_home):
            return self.ledger.patch(task_id, identity=identity)
        try:
            lines = files[0].read_text().splitlines()
            entries = []
            for index, line in enumerate(lines):
                try: entries.append(json.loads(line))
                except json.JSONDecodeError:
                    if index != len(lines) - 1: raise
        except (OSError, json.JSONDecodeError):
            return self.ledger.patch(task_id, identity=identity)
        metadata = [entry.get('payload', {}) for entry in entries if entry.get('type') == 'session_meta']
        if len(metadata) != 1 or metadata[0].get('id') != thread_id or Path(metadata[0].get('cwd', '')).resolve() != self.workspace:
            return self.ledger.patch(task_id, status='unknown', identity=identity, terminalConfirmed=False, resourcesReleased=False)
        events = [entry.get('payload', {}) for entry in entries if entry.get('type') == 'event_msg']
        starts = [event for event in events if event.get('type') == 'task_started']
        # A later task_started in this rollout is a native same-thread retry.
        # Keep the first turn; do not open a task, thread, or execution identity.
        turn_id = starts[0].get('turn_id') if starts else None
        if not isinstance(turn_id, str) or not turn_id:
            return self.ledger.patch(task_id, status='unknown', identity=identity, terminalConfirmed=False, resourcesReleased=False)
        if identity.get('codexTurnId') not in (None, turn_id):
            return self.ledger.patch(task_id, status='unknown', terminalConfirmed=False, resourcesReleased=False)
        identity['codexTurnId'] = turn_id
        matching = [event for event in events if event.get('turn_id') == turn_id]
        terminals = [event for event in matching if event.get('type') in {'task_complete', 'turn_aborted'}]
        terminal = bool(terminals)
        last = terminals[-1] if terminals else {}
        status = {'task_complete':'succeeded','turn_aborted':'canceled'}.get(last.get('type'), 'accepted')
        evidence = []
        for event in matching:
            item = event.get('item', {})
            if event.get('type') == 'item_completed' and isinstance(item, dict) and item.get('type') not in {'userMessage','agentMessage','reasoning'}:
                evidence.append({key:item.get(key) for key in ('id','type','status','exit_code')})
        record = self.ledger.patch(task_id, identity=identity, status=status,
            output={'text':last.get('last_agent_message',''), 'nativeStatus':last.get('type') or 'unknown', 'toolEvidence':evidence},
            codexUsage=codex_usage_snapshot(entries, identity, metadata[0]) if terminal else None,
            terminalConfirmed=terminal, resourcesReleased=False, executorInterrupted=False,
            nativeStatus=last.get('type') or 'unknown', reason=None)
        if terminal:
            return await self.release_resources(task_id, record)
        if _observed_exit is not None:
            matching = [item for item in _observed_exit['identities'] if
                item.get('codexThreadId') == identity['codexThreadId'] and
                item.get('codexTurnId') == identity['codexTurnId']]
            # A native-terminal hint contradicts interruption; only the actual
            # native event can classify that outcome. Missing hints fail closed.
            interrupted = len(matching) == 1 and matching[0].get('nativeTerminal') is False
            if interrupted:
                interrupted = await self.retire_runtime(task_id, record, _observed_exit, interrupted=True)
            return self.ledger.patch(task_id, executorInterrupted=interrupted,
                resourcesReleased=interrupted,
                resourceExitReceipt=_observed_exit if interrupted else None,
                nativeStatus='unknown', reason='executor_exit_without_native_terminal' if interrupted else None)
        return await self.observe_interruption(task_id, record)

    async def cancel(self, task_id, request):
        payload, record = self.ledger.get(task_id)
        if payload['targetAgentId'] != self.target or payload['workspace'] != str(self.workspace):
            raise HTTPException(409, '取消目标与当前执行绑定不符')
        record = await self.observe(task_id)
        if record['terminalConfirmed']:
            return record
        record = self.ledger.patch(task_id, cancellationRequested=True)
        if record.get('executorInterrupted') is True and record.get('resourcesReleased') is True:
            return record
        # Stop the immutable session for this task only. An acknowledged stop
        # is never used as terminal/resource-release evidence.
        from qwenpaw.app.routers.console import post_console_chat_stop
        from qwenpaw.tool_calls import CancelReason
        scoped = dict(request.scope)
        scoped['state'] = {**request.scope.get('state', {}), 'agent_id': self.target}
        native_request = Request(scoped, request.receive)
        try:
            result = await post_console_chat_stop(native_request,
                chat_id=payload['roleSessionId'], cancel_reason=CancelReason.USER,
                initiator='agent-line-service')
            self.ledger.patch(task_id, cancelAcknowledged=result.get('stopped') is True)
        except Exception:
            # Repeated stop targets the same immutable task session; it cannot
            # create another execution. Persist intent when the ack is lost.
            pass
        return await self.observe(task_id)

    def router(self):
        from .execution_approvals import ExecutionApprovals, DecisionRequest
        approvals = ExecutionApprovals(self)
        router = APIRouter(dependencies=[Depends(self.authorize)])
        @router.get('/execution-binding')
        def descriptor():
            return self.binding_descriptor()
        @router.post('/delegations/bind')
        def bind(body: RoleBindingRequest, request: Request):
            return self.bind_parent(body, request)
        @router.post('/delegations/{task_id}/cancel', status_code=202)
        async def cancel_parent(task_id: str, request: Request):
            return await self.cancel_parent(task_id, request)
        @router.post('/executions', status_code=202)
        async def submit(body: ExecutionRequest, request: Request):
            return await self.submit(body, request)
        @router.post('/executions/{task_id}/cancel', status_code=202)
        async def cancel(task_id: str, request: Request):
            return {'taskId': task_id, **await self.cancel(task_id, request)}
        @router.get('/executions/{task_id}')
        async def observe(task_id: str, request: Request):
            self.application = request.app
            return await self.observe(task_id)
        @router.get('/executions/{task_id}/approvals')
        async def approval_list(task_id: str):
            return await approvals.list(task_id)
        @router.post('/executions/{task_id}/approvals/{request_id}/decision')
        async def approval_decide(task_id: str, request_id: str, body: DecisionRequest):
            return await approvals.decide(task_id, request_id, body)
        return router
