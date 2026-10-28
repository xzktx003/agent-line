"""Explicit project-scoped human authoring authority; no startup mutations."""
import math
from pathlib import Path

from .adapters.sim import segment
from .approvals import TrustedPrincipal
from .execution_runtime import _json, _path


def load_authoring_api(path, *, projects, service_tokens, client, journal, role_bindings=None):
    config = _json(_path(str(Path(path).absolute()), private=True))
    if (not isinstance(config, dict)
            or set(config) != {'schemaVersion', 'credentials', 'workspaces'}
            or type(config['schemaVersion']) is not int or config['schemaVersion'] != 1
            or not isinstance(config['credentials'], list) or not config['credentials']
            or not isinstance(config['workspaces'], dict) or not config['workspaces']):
        raise ValueError('Invalid authoring deployment configuration')
    if client is None or journal is None:
        raise ValueError('Authoring requires an existing Sim client and journal')
    workspaces = config['workspaces']
    for project, workspace in workspaces.items():
        segment(project)
        segment(workspace)
        if project not in projects or projects[project].get('enabled', True) is not True:
            raise ValueError('Authoring workspace requires an enabled configured project')
    denied_tokens = frozenset(service_tokens) | {client._key}
    credentials, subjects = {}, set()
    for entry in config['credentials']:
        if not isinstance(entry, dict) or set(entry) != {
                'token', 'subject', 'projectScopes', 'capabilities', 'expiresAt'}:
            raise ValueError('Invalid authoring credential fields')
        token, subject = entry['token'], entry['subject']
        if (not isinstance(token, str) or len(token) < 32
                or any(c.isspace() for c in token) or token in credentials or token in denied_tokens):
            raise ValueError('Authoring credentials must be unique and independent')
        if (not isinstance(subject, str) or not subject.strip()
                or len(subject) > 256 or subject in subjects):
            raise ValueError('Authoring subjects must be nonempty and unique')
        scopes, capabilities = entry['projectScopes'], entry['capabilities']
        if (not isinstance(scopes, list) or not scopes
                or any(not isinstance(scope, str) or scope not in workspaces for scope in scopes)
                or len(scopes) != len(set(scopes))
                or not isinstance(capabilities, list)
                or any(not isinstance(cap, str) for cap in capabilities)
                or len(capabilities) != len(set(capabilities))
                or set(capabilities) not in ({'edit_workflows'}, {'edit_workflows', 'publish_workflows'})):
            raise ValueError('Invalid authoring scopes or capabilities')
        expiry = entry['expiresAt']
        if type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= 0:
            raise ValueError('Authoring credentials require finite positive expiration')
        credentials[token] = TrustedPrincipal(subject, frozenset(scopes), frozenset(capabilities), expiry)
        subjects.add(subject)
    from .authoring_api import AuthoringAPI, AuthoringAccess
    from .sim_authoring import SimAuthoringClient
    access = AuthoringAccess(credentials)
    # Retain the Sim key in the deny registry so future credential replacement
    # cannot accidentally turn a machine credential into human authority.
    access.reject_service_tokens(denied_tokens)
    clients = {project: SimAuthoringClient(client, workspace) for project, workspace in workspaces.items()}
    return AuthoringAPI(access, journal, clients, projects=projects, role_bindings=role_bindings)
