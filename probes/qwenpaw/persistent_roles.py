#!/usr/bin/env python3
"""Create/recheck designated G0 roles only in the isolated authenticated instance."""
import argparse
import json
from pathlib import Path
from runtime_preflight import ROOT, call, state

ROLES = {'agent-line-g0-parent': 'qwenpaw', 'agent-line-g0-codex': 'codex'}
EVIDENCE = ROOT / 'var/qwenpaw/g0-role-persistence.json'
parser = argparse.ArgumentParser()
parser.add_argument('action', choices=['create', 'check'])
args = parser.parse_args()
status, listing = call('agents')
assert status == 200
known = {item['id']: item for item in listing['agents']}
for role, backend in ROLES.items():
    if role not in known:
        assert args.action == 'create', 'Existing role missing: ' + role
        status, created = call('agents', {'id':role,'name':'G0 持久协调角色' if backend=='qwenpaw' else 'G0 持久 Codex 执行角色','description':'Agent Line 隔离持久身份探针；尚未启用工作流执行','backend':backend,'language':'zh','skill_names':[]})
        assert status == 201, f'Create {role}: HTTP {status}'
status, listing = call('agents')
assert status == 200
known = {item['id']: item for item in listing['agents']}
result = {'service_pid':state['pid'],'roles':{role:{'id':known[role]['id'],'backend':known[role]['backend']} for role in ROLES}}
for role, backend in ROLES.items():
    assert result['roles'][role]['backend'] == backend
if args.action == 'create':
    if EVIDENCE.exists():
        old = json.loads(EVIDENCE.read_text())
        assert old['roles'] == result['roles']
    else:
        EVIDENCE.write_text(json.dumps(result,ensure_ascii=False,indent=2))
    result['created_or_reused'] = True
else:
    before = json.loads(EVIDENCE.read_text())
    assert before['roles'] == result['roles']
    assert before['service_pid'] != result['service_pid'], 'Service PID unchanged; restart not verified'
    result['restart_identity_preserved'] = True
    (EVIDENCE.parent / 'g0-role-persistence-after.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
print(json.dumps(result,ensure_ascii=False))
