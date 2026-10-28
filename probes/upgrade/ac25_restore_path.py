#!/usr/bin/env python3
"""AC-25 isolated restore check on the Codex gpt-6-luna path.

Does not open 10.40.0.2, taskField, user namespace, or the parent role queue.
The over-budget tail task stays registered and is not submitted.
"""
import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'deploy/executor'))
from agent_line.backup import (
    create_backup, fingerprint_workspace, reconcile_restored_workspace, restore_backup,
)
from agent_line.store import AdmissionClosed, Conflict, TaskStore
from qwenpaw.harnesses.codex.app_server import CodexAppServerClient

DIR = Path(os.environ.get('AC25_EVIDENCE_DIR') or (ROOT / 'var/ac25-restore-20261027'))
if not DIR.is_absolute():
    DIR = ROOT / DIR
CONFIG = json.loads((ROOT / 'var/executor/config.json').read_text())
MOUNTED = Path(CONFIG['workspace']).resolve()
WORK = MOUNTED / 'ac25-restore-20261027'
SAMPLE = WORK / 'sample.py'
BOOTSTRAP = MOUNTED / 'main-bootstrap-45be61aebaa6d15b.py'
ORIGINAL = 'def add(left, right):\n    return left + right\n'
MODEL = 'gpt-6-luna'
TAIL = 'task-d61d558f568f49ecaf15d583005b2013'
WAIT_SECONDS = int(os.environ.get('AC25_WAIT_SECONDS') or '240')
if not 30 <= WAIT_SECONDS <= 1800:
    raise SystemExit('AC25_WAIT_SECONDS must be between 30 and 1800')
INSTRUCTION = os.environ.get('AC25_INSTRUCTION') or (
    'The file ac25-restore-20261027/sample.py is in the current working directory. '
    'Change only its return line to "return left + right  # ac25-code-changed". '
    'Do not modify main-bootstrap-45be61aebaa6d15b.py or any other file. '
    'Stop after that one edit.')


def save(name, value):
    DIR.mkdir(mode=0o700, exist_ok=True)
    target = DIR / name
    temporary = target.with_suffix(target.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    os.chmod(temporary, 0o600)
    temporary.replace(target)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tail_state():
    import sqlite3
    db = sqlite3.connect(ROOT / 'var/adapter/tasks.sqlite')
    db.row_factory = sqlite3.Row
    row = db.execute(
        'select execution_status, submission_status, version from tasks where id=?',
        (TAIL,)).fetchone()
    events = db.execute(
        'select count(*) from events where task_id=? and kind=?',
        (TAIL, 'submission_intent')).fetchone()[0]
    return {
        'executionStatus': row['execution_status'],
        'submissionStatus': row['submission_status'],
        'version': row['version'],
        'submissionIntent': events,
    }


async def edit_once(state):
    client = CodexAppServerClient(binary=str(ROOT / 'deploy/executor/launcher.py'))
    await asyncio.wait_for(client.start(), 30)
    try:
        start_params = {
            'cwd': str(MOUNTED),
            'approvalPolicy': 'never',
            'sandbox': 'danger-full-access',
            'model': MODEL,
        }
        disabled = [item for item in os.environ.get('AC25_DISABLE_PLUGINS', '').split(',') if item]
        if disabled:
            start_params['disabledPluginIds'] = disabled
            state['disabledPluginIds'] = disabled
        opened = await asyncio.wait_for(client.request('thread/start', start_params), 30)
        state['threadId'] = opened['thread']['id']
        state['modelEcho'] = opened.get('model')
        if state['modelEcho'] != MODEL:
            raise RuntimeError('model_mismatch')
        queue = client.subscribe()
        started = await asyncio.wait_for(client.request('turn/start', {
            'threadId': state['threadId'],
            'cwd': str(MOUNTED),
            'approvalPolicy': 'never',
            'sandboxPolicy': {'type': 'dangerFullAccess'},
            'input': [{'type': 'text', 'text': INSTRUCTION}],
        }), 30)
        state['turnId'] = started['turn']['id']
        state['submissions'] = 1
        deadline = time.monotonic() + WAIT_SECONDS
        while time.monotonic() < deadline:
            try:
                event = await asyncio.wait_for(queue.get(), min(15, max(0.01, deadline - time.monotonic())))
            except asyncio.TimeoutError:
                continue
            state['eventCount'] = state.get('eventCount', 0) + 1
            state['lastEvent'] = event.get('method')
            save('heartbeat.json', {
                'eventCount': state['eventCount'],
                'lastEvent': state['lastEvent'],
                'waitSeconds': WAIT_SECONDS,
            })
            if event.get('method') == 'turn/completed':
                state['nativeTerminal'] = event['params']['turn']['status']
                break
        else:
            state['nativeTerminal'] = 'timeout'
    finally:
        await client.stop()


def main():
    before_tail = tail_state()
    if before_tail['executionStatus'] != 'registered' or before_tail['submissionIntent'] != 0:
        raise SystemExit('tail task is not in the required registered state')
    DIR.mkdir(mode=0o700, exist_ok=True)
    WORK.mkdir(mode=0o700, exist_ok=True)
    state_path = DIR / 'state.json'
    if state_path.exists():
        print(state_path.read_text())
        return
    if not WORK.is_relative_to(MOUNTED):
        raise SystemExit('sample workspace is outside the mounted Codex workspace')
    bootstrap_before = BOOTSTRAP.read_bytes() if BOOTSTRAP.is_file() else None
    SAMPLE.write_text(ORIGINAL)
    os.chmod(SAMPLE, 0o600)
    database = DIR / 'original.sqlite'
    store = TaskStore.initialize(database)
    payload = {
        'projectId': 'ac25-isolated',
        'roleAgentId': 'ac25-codex-luna',
        'workspace': str(WORK),
        'workflow': {'runId': 'ac25', 'nodeExecutionId': 'edit', 'attemptId': '1'},
        'input': {'path': 'sample.py'},
    }
    task, _ = store.register('ac25-before-backup', payload)
    fingerprint = fingerprint_workspace(WORK, ['sample.py'])
    create_backup(database, DIR / 'backup')
    state = {
        'openedPath': 'codex-launcher/gpt-6-luna',
        'rejectedPaths': ['10.40.0.2', 'taskField', 'user-namespace', 'kilo-auto/free-as-executor'],
        'sandbox': 'danger-full-access',
        'approval': 'never',
        'modelRequested': MODEL,
        'waitSeconds': WAIT_SECONDS,
        'taskId': task['id'],
        'backupFingerprint': fingerprint,
        'submissions': 0,
    }
    save('state.json', state)
    asyncio.run(edit_once(state))
    after = SAMPLE.read_text()
    if bootstrap_before is not None and BOOTSTRAP.read_bytes() != bootstrap_before:
        BOOTSTRAP.write_bytes(bootstrap_before)
        state['bootstrapRestored'] = True
    else:
        state['bootstrapRestored'] = False
    state['bootstrapUnchanged'] = (
        bootstrap_before is None or BOOTSTRAP.read_bytes() == bootstrap_before)
    state['codeChanged'] = (
        after != ORIGINAL and '# ac25-code-changed' in after and state['bootstrapUnchanged'])
    state['sampleSha256'] = sha(SAMPLE)
    restored = DIR / 'restored.sqlite'
    restore_backup(DIR / 'backup', restored)
    state['unknownReport'] = reconcile_restored_workspace(
        restored, backup_fingerprint=fingerprint,
        current_fingerprint=fingerprint_workspace(WORK, ['sample.py']),
        execution_observation='unknown', native_triggers='unknown')
    restored_store = TaskStore(restored)
    blocked_claim = False
    try:
        restored_store.claim(task['id'], str(WORK))
    except Conflict:
        blocked_claim = True
    blocked_register = False
    try:
        restored_store.register('after-restore', payload)
    except AdmissionClosed:
        blocked_register = True
    state['claimBlocked'] = blocked_claim
    state['registerBlocked'] = blocked_register
    state['codePreserved'] = SAMPLE.read_text() == after
    state['restoredTaskStillPending'] = (
        restored_store.get(task['id'])['upstreamSubmissionStatus'] == 'pending')
    state['tailAfter'] = tail_state()
    state['tailUnchanged'] = state['tailAfter'] == before_tail
    state['passed'] = bool(
        state['modelEcho'] == MODEL and state['nativeTerminal'] == 'completed'
        and state['codeChanged'] and state['bootstrapUnchanged'] and state['submissions'] == 1
        and state['unknownReport']['unknownBlocksWrites']
        and state['unknownReport']['workspaceDiff'] == ['sample.py']
        and state['unknownReport']['continuationAttempted'] is False
        and state['unknownReport']['codeOverwritten'] is False
        and blocked_claim and blocked_register and state['codePreserved']
        and state['restoredTaskStillPending'] and state['tailUnchanged'])
    save('state.json', state)
    save('report.json', state)
    print(json.dumps({key: state[key] for key in (
        'passed', 'modelEcho', 'nativeTerminal', 'codeChanged', 'submissions',
        'claimBlocked', 'registerBlocked', 'tailUnchanged')}, ensure_ascii=False))
    if os.environ.get('AC25_CLEAN_SAMPLE') == '1' and state['passed'] and WORK.exists():
        SAMPLE.unlink(missing_ok=True)
        WORK.rmdir()
    if not state['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
