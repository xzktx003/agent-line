"""Create an isolated local probe identity through Sim's own HTTP APIs."""
import json
import secrets
from pathlib import Path
import httpx
from workflow_probe import ROOT, write_private


def main():
    account_path = ROOT / 'var/sim/probe-account.json'
    auth_path = ROOT / 'var/sim/probe-auth.json'
    base = 'http://127.0.0.1:3190'
    if auth_path.exists():
        print('Probe credentials already exist; unchanged')
        return
    if account_path.exists():
        account = json.loads(account_path.read_text())
    else:
        account = {'email': 'agent-line-probe-' + secrets.token_hex(5) + '@example.com',
                   'password': secrets.token_urlsafe(32), 'name': 'Agent Line G0 Probe'}
        write_private(account_path, account)
    env = dict(line.split('=', 1) for line in (ROOT / 'var/sim/sim.env').read_text().splitlines()
               if '=' in line and not line.startswith('#'))
    origin = env.get('SIM_PUBLIC_URL', base).strip('"\'')
    with httpx.Client(base_url=base, headers={'Origin': origin}, timeout=60) as client:
        if not account.get('registered'):
            r = client.post('/api/auth/sign-up/email', json={k: account[k] for k in ['email','password','name']})
            if r.status_code >= 400:
                print('Signup refused', r.status_code, r.json().get('code', 'unknown'))
                raise SystemExit(1)
            account['registered'] = True
            write_private(account_path, account)
        r = client.post('/api/auth/sign-in/email', json={k: account[k] for k in ['email','password']})
        if r.status_code >= 400:
            print('Sign-in refused', r.status_code)
            raise SystemExit(1)
        if 'workspaceId' not in account:
            r = client.post('/api/workspaces', json={'name': 'Agent Line G0 Isolated', 'skipDefaultWorkflow': True})
            if r.status_code >= 400:
                print('Workspace creation refused', r.status_code)
                raise SystemExit(1)
            account['workspaceId'] = r.json()['workspace']['id']
            write_private(account_path, account)
        r = client.post('/api/users/me/api-keys', json={'name': 'agent-line-g0-probe'})
        if r.status_code >= 400:
            print('Key creation refused', r.status_code)
            raise SystemExit(1)
        # Preserve response privately before parsing to avoid losing a one-time secret.
        write_private(ROOT / 'var/sim/probe-key-response.json', r.json())
        key = r.json()['key']
        if isinstance(key, dict):
            key = key.get('key') or key.get('apiKey')
        if not isinstance(key, str):
            print('Key response schema requires inspection; private response saved')
            raise SystemExit(1)
        write_private(auth_path, {'baseUrl': base, 'apiKey': key, 'workspaceId': account['workspaceId']})
    print('Isolated account, workspace, API credential created; secrets saved privately')


if __name__ == '__main__':
    main()
