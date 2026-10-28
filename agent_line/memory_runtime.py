"""Private deployment factory for reviewed memory writes; never initializes storage."""
import math
from pathlib import Path

import httpx

from .adapters.qwenpaw_memory import QwenPawMemoryClient
from .adapters.sim import segment
from .approval_api import ApprovalAccess
from .approvals import TrustedPrincipal
from .execution_runtime import _json, _origin, _path, _text
from .memory import MemoryCommitStore, MemoryCoordinator, MemoryDeletionCoordinator
from .memory_review import MemoryReviewLedger


def load_memory_api(path, *, projects, bindings, service_tokens, tasks):
    config = _json(_path(str(Path(path).absolute()), private=True))
    if (not isinstance(config, dict) or set(config) != {
            'schemaVersion', 'reviewLedgerPath', 'credentials', 'bindings'}
            or type(config['schemaVersion']) is not int or config['schemaVersion'] != 1
            or not isinstance(config['credentials'], list) or not config['credentials']
            or not isinstance(config['bindings'], dict) or not config['bindings']
            or not isinstance(projects, dict) or not isinstance(bindings, dict)
            or not callable(getattr(tasks, 'workflow_admission', None))):
        raise ValueError('Invalid memory deployment configuration')
    credentials = {}; subjects = set()
    for entry in config['credentials']:
        if not isinstance(entry, dict) or set(entry) != {'token', 'subject', 'projectScopes', 'capabilities', 'expiresAt'}:
            raise ValueError('Invalid memory credential fields')
        token, subject = entry['token'], entry['subject']
        if (not isinstance(token, str) or len(token) < 32 or any(c.isspace() for c in token)
                or token in credentials or token in service_tokens):
            raise ValueError('Memory review tokens must be unique and separate from service tokens')
        if not isinstance(subject, str) or not subject.strip() or len(subject) > 256 or subject in subjects:
            raise ValueError('Memory reviewer subjects must be nonempty and unique')
        scopes = entry['projectScopes']; expiry = entry['expiresAt']; caps = entry['capabilities']
        if (not isinstance(scopes, list) or not scopes
                or any(not isinstance(item, str) or item not in projects for item in scopes)
                or len(set(scopes)) != len(scopes)
                or not isinstance(caps, list) or any(not isinstance(cap, str) for cap in caps)
                or len(set(caps)) != len(caps) or set(caps) not in ({'review_memory'}, {'review_memory', 'delete_memory'})
                or type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= 0):
            raise ValueError('Invalid memory credential scope, capability or expiry')
        credentials[token] = TrustedPrincipal(subject, frozenset(scopes), frozenset({'approve', *caps}), expiry)
        subjects.add(subject)
    access = ApprovalAccess(credentials)
    access.reject_service_tokens(service_tokens)
    reviews = MemoryReviewLedger(_path(config['reviewLedgerPath'], private=True))
    prepared = {}; role_owners = {}; ledger_owners = {}; roles = set()
    for memory_id, entry in config['bindings'].items():
        segment(_text(memory_id))
        if not isinstance(entry, dict) or set(entry) - {'deletionServiceTokenFile'} != {
                'projectId', 'roleBindingId', 'roleAgentId', 'baseURL', 'hostCredentialsFile', 'commitLedgerPath'}:
            raise ValueError('Invalid memory binding fields')
        values = dict(entry)
        for key in ('projectId', 'roleBindingId', 'roleAgentId'): segment(_text(values[key]))
        if len(values['roleAgentId']) > 128: raise ValueError('Memory role identifier too long')
        project, role = values['projectId'], values['roleAgentId']
        if role in roles: raise ValueError('A memory role requires one canonical binding')
        roles.add(role)
        registered = projects.get(project); binding = bindings.get(values['roleBindingId'])
        if (not isinstance(registered, dict) or registered.get('enabled', True) is not True
                or not isinstance(binding, dict) or binding.get('enabled', True) is not True
                or binding.get('projectId') != project or binding.get('roleAgentId') != role):
            raise ValueError('Memory binding differs from enabled service project/role registry')
        values['baseURL'] = _origin(values['baseURL'])
        ledger_path = _path(values['commitLedgerPath'], private=True)
        if role_owners.setdefault((values['baseURL'], role), (project, ledger_path)) != (project, ledger_path):
            raise ValueError('A host role cannot share memory authority across projects or ledgers')
        if ledger_owners.setdefault(ledger_path, project) != project:
            raise ValueError('Memory commit ledger cannot be shared across projects')
        values['store'] = MemoryCommitStore(ledger_path)
        values['deletion_token'] = None
        if 'deletionServiceTokenFile' in values:
            if values['store'].schema_version != 2:
                raise ValueError('Memory deletion requires explicit commit ledger upgrade to schema 2')
            deletion_token = _path(values['deletionServiceTokenFile'], private=True).read_text().strip()
            if (not 32 <= len(deletion_token) <= 4096 or not deletion_token.isascii()
                    or any(c.isspace() for c in deletion_token)
                    or deletion_token in credentials or deletion_token in service_tokens):
                raise ValueError('Memory deletion service token must be independent and valid')
            values['deletion_token'] = deletion_token
        login = _json(_path(values['hostCredentialsFile'], private=True))
        if (not isinstance(login, dict) or set(login) != {'username', 'password'}
                or any(not isinstance(value, str) or not value for value in login.values())):
            raise ValueError('Host login file must contain only nonempty username and password')
        values['login'] = login
        prepared[memory_id] = values
    # No network until every binding, authority and existing ledger is validated.
    clients = []; coordinators = {}; deletions = {}
    deletion_tokens = {value['deletion_token'] for value in prepared.values() if value['deletion_token'] is not None}
    try:
        for memory_id, values in prepared.items():
            client = httpx.Client(timeout=30, follow_redirects=False, trust_env=False)
            clients.append(client)
            try:
                response = client.post(values['baseURL'] + '/api/auth/login', json=values['login'])
                if response.status_code != 200: raise ValueError('Host authentication failed')
                body = response.json()
                token = (body.get('token') or body.get('access_token')) if isinstance(body, dict) else None
                if not isinstance(token, str) or not token.strip(): raise ValueError('Invalid host login response')
                if token in deletion_tokens: raise ValueError('Host and memory deletion tokens must differ')
                response = client.get(values['baseURL'] + '/api/agents', headers={'Authorization': 'Bearer ' + token})
                if response.status_code != 200: raise ValueError('Host role lookup failed')
                body = response.json(); roles = body.get('agents') if isinstance(body, dict) else None
                matches = [item for item in roles if isinstance(item, dict) and item.get('id') == values['roleAgentId']] if isinstance(roles, list) else []
                if len(matches) != 1 or matches[0].get('backend') != 'qwenpaw' or matches[0].get('enabled', True) is not True:
                    raise ValueError('Host role is not an enabled QwenPaw coordinator')
            except (httpx.HTTPError, ValueError):
                raise ValueError('Memory host authentication or role verification failed; no memory written') from None
            adapter = QwenPawMemoryClient(values['baseURL'], token, role_agent_id=values['roleAgentId'], client=client, deletion_service_token=values['deletion_token'], native_write_rejections=True)
            adapter.owns_client = True  # Transfer this factory-created transport to API.close().
            coordinators[memory_id] = MemoryCoordinator(values['store'], adapter)
            if values['deletion_token'] is not None:
                deletions[memory_id] = MemoryDeletionCoordinator(values['store'], adapter)
        from .memory_api import MemoryAPI
        public_bindings = {key: {name: values[name] for name in ('projectId', 'roleBindingId', 'roleAgentId')}
                           for key, values in prepared.items()}
        return MemoryAPI(access, coordinators, reviews, tasks, bindings=public_bindings, admission=tasks.workflow_admission, deletions=deletions)
    except BaseException:
        for client in clients:
            try: client.close()
            except Exception: pass
        raise
