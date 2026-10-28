"""Real HTTP cancellation before binding; rejected work never reaches a model."""
import json
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parents[2]
state = json.loads((ROOT / 'var/qwenpaw/agent-line-process.json').read_text())
base = f'http://127.0.0.1:{state["port"]}/api'
task = 'revoke-before-bind-live-001'
with httpx.Client(timeout=30, follow_redirects=False) as client:
    response = client.post(base + '/auth/login', json=json.loads((ROOT / 'var/qwenpaw-secrets/local-login.json').read_text()))
    response.raise_for_status()
    login = response.json()
    client.headers['Authorization'] = 'Bearer ' + (login.get('token') or login['access_token'])
    client.headers['X-Agent-Line-Service-Token'] = (ROOT / 'var/qwenpaw-secrets/execution-service-token').read_text().strip()
    response = client.post(base + '/agent-line/delegations/' + task + '/cancel', json={})
    response.raise_for_status()
    canceled = response.json()
    assert canceled['delegationRevoked'] is True and canceled['executionClaimed'] is False
    bound = client.post(base + '/agent-line/delegations/bind', json={'taskId': task, 'roleAgentId': 'agent-line-g0-parent', 'roleSessionId': 'agent-line-' + task})
    assert bound.status_code == 409
    execution = client.post(base + '/agent-line/executions', json={'taskId': task, 'roleSessionId': 'agent-line-' + task, 'instruction': 'Canceled task must not execute.'})
    assert execution.status_code == 409
    evidence = {'taskId': task, 'delegationRevoked': True, 'executionClaimed': False, 'lateBindingStatus': bound.status_code, 'lateExecutionStatus': execution.status_code, 'verified': True}
    path = ROOT / 'var/cancellation/delegation-revocation.json'
    path.write_text(json.dumps(evidence, indent=2))
    path.chmod(0o600)
    print(json.dumps(evidence))
