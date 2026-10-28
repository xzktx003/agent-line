#!/usr/bin/env python3
"""Live AC-43: same agent id, prompt drift blocks the old frozen session.

Does not change the model, tool permissions, sandbox, taskField, or role id.
SOUL.md is restored to its original bytes and timestamps before exit.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import types

ROOT = Path(__file__).resolve().parents[2]
ROLE = 'agent-line-native-loop-parent'
SESSION = 'agent-line-task-6031a0c08aec4de0b8a29004f4bc5601'
DATA = ROOT / 'var/ac43-config-drift-20261027'
MARKER = '\n<!-- ac43-drift-probe: do not keep -->\n'


def load_plugin():
    root = ROOT / 'plugins/agent-line'
    package = types.ModuleType('liveplugin')
    package.__path__ = [str(root)]
    package.__package__ = 'liveplugin'
    sys.modules['liveplugin'] = package
    for name in ('role_revision', 'execution_bridge'):
        spec = importlib.util.spec_from_file_location('liveplugin.' + name, root / (name + '.py'))
        module = importlib.util.module_from_spec(spec)
        module.__package__ = 'liveplugin'
        sys.modules['liveplugin.' + name] = module
        spec.loader.exec_module(module)
    return sys.modules['liveplugin.execution_bridge']


def binding_count(path):
    db = sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True)
    try:
        return db.execute('SELECT count(*) FROM role_bindings').fetchone()[0]
    finally:
        db.close()


def main():
    os.environ.setdefault('QWENPAW_WORKING_DIR', str(ROOT / 'var/qwenpaw'))
    os.environ.setdefault('QWENPAW_SECRET_DIR', str(ROOT / 'var/qwenpaw-secrets'))
    os.environ.setdefault('QWENPAW_AUTH_ENABLED', 'true')
    DATA.mkdir(mode=0o700, exist_ok=True)
    config = json.loads((ROOT / 'var/qwenpaw/config.json').read_text())
    plugin = config['plugins']['agent-line']
    bridge_module = load_plugin()
    bridge = bridge_module.ExecutionBridge({**plugin['execution_bridge'], 'managed_role_ids': plugin['managed_role_ids']})
    soul = ROOT / 'var/qwenpaw/workspaces' / ROLE / 'SOUL.md'
    agent_json = soul.parent / 'agent.json'
    original = soul.read_bytes()
    original_stat = soul.stat()
    agent_hash = hashlib.sha256(agent_json.read_bytes()).hexdigest()
    before = binding_count(bridge.ledger.path if hasattr(bridge.ledger, 'path') else plugin['execution_bridge']['receipt_db'])
    receipt_db = Path(plugin['execution_bridge']['receipt_db'])
    before = binding_count(receipt_db)
    current = bridge.current_revision(ROLE)
    frozen = bridge.assert_role_revision(ROLE, SESSION)
    if frozen != current:
        raise SystemExit('frozen revision already differs; refusing to continue')
    changed = None
    try:
        soul.write_bytes(original + MARKER.encode())
        changed = bridge.current_revision(ROLE)
        if changed == current:
            raise SystemExit('prompt change did not change the role revision')
        refused = None
        try:
            bridge.assert_role_revision(ROLE, SESSION)
        except bridge_module.HTTPException as exc:
            refused = {'status': exc.status_code, 'detail': exc.detail}
        if not refused or refused['status'] != 409:
            raise SystemExit('old session was not refused after prompt drift')
        body = bridge_module.RoleBindingRequest(
            taskId='ac43-must-not-bind', roleAgentId=ROLE, roleRevision=current,
            roleSessionId='agent-line-ac43-must-not-bind')
        bind_refused = None
        try:
            bridge.bind_parent(body, None)
        except bridge_module.HTTPException as exc:
            bind_refused = {'status': exc.status_code, 'detail': exc.detail}
        if not bind_refused or bind_refused['status'] != 409:
            raise SystemExit('new bind of the old revision was not refused')
        during = binding_count(receipt_db)
        if during != before:
            raise SystemExit('role binding count changed during the drift')
    finally:
        soul.write_bytes(original)
        os.utime(soul, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    restored = soul.read_bytes()
    if restored != original or hashlib.sha256(agent_json.read_bytes()).hexdigest() != agent_hash:
        raise SystemExit('role files were not restored')
    restored_revision = bridge.current_revision(ROLE)
    restored_assert = bridge.assert_role_revision(ROLE, SESSION)
    after = binding_count(receipt_db)
    if restored_revision != current or restored_assert != frozen or after != before:
        raise SystemExit('old session did not return to the original frozen revision')
    report = {
        'status': 'passed_live',
        'acceptance': 'AC-43',
        'roleAgentId': ROLE,
        'oldSession': SESSION,
        'frozenRevision': frozen,
        'driftedRevision': changed,
        'restoredRevision': restored_revision,
        'oldSessionRefused': refused,
        'oldRevisionBindRefused': bind_refused,
        'bindingCount': before,
        'agentJsonSha256': agent_hash,
        'soulRestored': True,
        'openedPath': 'temporary parent SOUL.md prompt byte used only to recompute the existing role revision',
        'restrictionsLeftClosed': [
            'taskField', 'codex sandbox', 'apparmor', 'user namespace', 'reme',
            'role id', 'role model', 'tool permissions', 'activation identity check',
        ],
        'limitations': [
            'Prompt drift only; model and tool permissions were not rewritten',
            'The old session was refused before any new model or delegation call',
            'No new role binding was written',
        ],
    }
    (DATA / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'status': 'passed_live', 'bindingCount': before, 'soulRestored': True}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
