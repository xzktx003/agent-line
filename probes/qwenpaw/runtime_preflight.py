#!/usr/bin/env python3
"""Sanitized isolated-instance inventory; never print credentials or account PII."""
import importlib.util
import json
import os
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('deployment', ROOT / 'deploy/qwenpaw/manage.py')
deployment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployment)
state = json.loads(deployment.STATE.read_text())
base = f"http://127.0.0.1:{state['port']}"
status, login = deployment.request(base + '/api/auth/login', json.loads(deployment.CREDENTIALS.read_text()))
assert status == 200
TOKEN = login.get('token') or login.get('access_token')
def call(path, body=None):
    return deployment.request(base + '/api/' + path, body, TOKEN)

if __name__ == '__main__':
    code, providers = call('models')
    assert code == 200
    result = {'providers': [{'id': p['id'], 'credential_present': bool(p.get('api_key')), 'model_count': len(p.get('models', [])) + len(p.get('extra_models', []))} for p in providers]}
    code, agents = call('agents')
    assert code == 200
    rows = agents.get('agents', []) if isinstance(agents, dict) else agents
    result['agents'] = [{k: a.get(k) for k in ('id', 'backend', 'active_model', 'backend_model')} for a in rows]
    environment = (Path('/proc') / str(state['pid']) / 'environ').read_bytes().split(b'\0')
    codex_home = next((p.split(b'=', 1)[1].decode() for p in environment if p.startswith(b'CODEX_HOME=')), None)
    result['codex_home_isolated'] = bool(codex_home and Path(codex_home).is_relative_to(ROOT))
    result['codex_home_explicit'] = bool(codex_home)
    # No harness/status here: its subprocess inherits default global Codex home.
    print(json.dumps(result, ensure_ascii=False, indent=2))
