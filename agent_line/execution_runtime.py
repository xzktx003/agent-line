"""Strict deployment-only parent-gateway factory. No capability or admission grant."""
from dataclasses import dataclass, field
from typing import Callable
from .role_revision import validate_role_revision

import json
import os
from pathlib import Path
import stat
from urllib.parse import urlsplit

import httpx

from .adapters.sim import host_role_shared_across_projects, segment
from .role_dispatch import RoleDispatchGateway, RoleDispatchLedger

REQUIRED = frozenset({'projectId', 'roleBindingId', 'roleAgentId', 'workspace', 'installationId',
                      'ledgerPath', 'baseURL', 'hostCredentialsFile', 'serviceTokenFile', 'memoryQuery'})
OPTIONAL = frozenset({'requestTimeoutSeconds', 'parentTimeoutSeconds'})


class ExecutionCompatibilityError(ValueError):
    """A remote descriptor failure, after strict local deployment validation."""

    REASONS = {
        'binding_incompatible': '执行插件接口与可信部署不兼容，已阻止新执行；历史记录仍可查询',
        'binding_unavailable': '执行插件接口暂不可用，已阻止新执行；请核对兼容性，历史记录仍可查询',
    }

    def __init__(self, code):
        if code not in self.REASONS:
            raise ValueError('Unsupported execution compatibility reason')
        self.code = code
        super().__init__(self.REASONS[code])


def _text(value):
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError('Deployment strings must be nonempty and trimmed')
    return value


def _path(value, *, directory=False, private=False):
    path = Path(_text(value))
    if not path.is_absolute(): raise ValueError('Deployment paths must be absolute')
    resolved = path.resolve(strict=True)
    if str(path) != str(resolved): raise ValueError('Deployment paths must be canonical without symlinks')
    info = path.stat()
    if directory:
        if not stat.S_ISDIR(info.st_mode): raise ValueError('Workspace must be a directory')
    elif not stat.S_ISREG(info.st_mode):
        raise ValueError('Deployment file must be regular')
    if private and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600):
        raise ValueError('Private deployment files must be owned by current user with mode 0600')
    return resolved


def _json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result: raise ValueError('Duplicate configuration key')
            result[key] = value
        return result
    def nonfinite(_value):
        raise ValueError('Non-finite configuration number')
    try:
        return json.loads(path.read_text(), object_pairs_hook=unique, parse_constant=nonfinite)
    except (json.JSONDecodeError, UnicodeError):
        raise ValueError('Invalid private JSON configuration') from None


def _origin(value):
    value = _text(value)
    parsed = urlsplit(value)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
            or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment
            or any(char.isspace() for char in value)):
        raise ValueError('QwenPaw baseURL must be a trusted HTTP origin')
    parsed.port  # Reject malformed/out-of-range ports before any request.
    return str(httpx.URL(value)).rstrip('/')


@dataclass(frozen=True)
class BoundRoleGateway:
    project_id: str
    role_binding_id: str
    role_agent_id: str
    workspace: str
    installation_id: str
    _gateway: RoleDispatchGateway
    _client: httpx.Client
    role_revision: str | None = None
    readiness_check: Callable[[], None] | None = field(default=None, repr=False, compare=False)
    executor_agent_id: str | None = field(default=None, compare=False)
    execution_controls: tuple[str, str] | None = field(default=None, repr=False, compare=False)

    @property
    def role(self): return self.role_agent_id

    def _check_role_revision(self, *, task_id, role_revision=None):
        try:
            self._gateway.ledger.get(task_id)
        except KeyError:
            if self.readiness_check is not None:
                self.readiness_check()
            if self.role_revision is None:
                raise ValueError('role_revision_required')
            if role_revision != self.role_revision:
                raise ValueError('role_revision_mismatch')

    def preflight(self, *, task_id, role_revision=None):
        self._check_role_revision(task_id=task_id, role_revision=role_revision)
        return self._gateway.preflight(task_id=task_id, role_revision=role_revision)

    def submit(self, **kwargs):
        self._check_role_revision(task_id=kwargs['task_id'], role_revision=kwargs.get('role_revision'))
        if kwargs.get('preflight_proof') is None:
            proof = self._gateway.preflight(task_id=kwargs['task_id'], role_revision=kwargs.get('role_revision'))
            if proof is not None:
                kwargs['preflight_proof'] = proof
        return self._gateway.submit(**kwargs)
    def observe(self, task_id): return self._gateway.observe(task_id)
    def memory_trace(self, task_id, expected_identity):
        return self._gateway.memory_trace(task_id, expected_identity)
    def cancel(self, task_id): return self._gateway.cancel(task_id)
    def execution_approvals(self, task_id): return self._gateway.execution_approvals(task_id)
    def decide_execution_approval(self, task_id, request_id, payload):
        return self._gateway.decide_execution_approval(task_id, request_id, payload)

    def close(self):
        # RoleDispatchGateway receives an injected client and does not own it.
        # The factory wrapper exclusively owns/cleans that client.
        self._client.close()


def load_execution_gateways(path, *, projects, bindings, client_factory=None):
    """Validate all entries first, then authenticate once per configured binding.

    Caller owns returned gateways and must stop its worker before closing them.
    No automatic refresh/relogin or retry of a failed login/write is performed.
    """
    raw = Path(path).absolute()
    config_path = _path(str(raw), private=True)
    config = _json(config_path)
    if (not isinstance(config, dict) or set(config) != {'schemaVersion', 'gateways'}
            or type(config['schemaVersion']) is not int or config['schemaVersion'] != 1
            or not isinstance(config['gateways'], list)):
        raise ValueError('Unsupported execution deployment configuration')
    if not isinstance(projects, dict) or not isinstance(bindings, dict):
        raise ValueError('Service projects/bindings must be registries')
    prepared = []; keys = set(); bridge_owners = {}; installation_owners = {}
    for entry in config['gateways']:
        if not isinstance(entry, dict) or not REQUIRED <= set(entry) or set(entry) - REQUIRED - OPTIONAL:
            raise ValueError('Execution binding has missing or unknown fields')
        values = dict(entry)
        for key in ('projectId', 'roleBindingId', 'roleAgentId', 'installationId'):
            segment(_text(values[key]))
        if len(values['roleAgentId']) > 128: raise ValueError('Role identifier too long')
        project, binding_id = values['projectId'], values['roleBindingId']
        if (project, binding_id) in keys: raise ValueError('Duplicate execution binding')
        keys.add((project, binding_id))
        binding = bindings.get(binding_id)
        registered = projects.get(project)
        if (not isinstance(binding, dict) or binding.get('projectId') != project
                or binding.get('roleAgentId') != values['roleAgentId'] or not isinstance(registered, dict)):
            raise ValueError('Execution binding differs from service project/role registry')
        values['roleRevision'] = validate_role_revision(binding['roleRevision']) if 'roleRevision' in binding else None
        workspace = _path(values['workspace'], directory=True)
        if workspace != _path(registered.get('workspace'), directory=True):
            raise ValueError('Execution workspace differs from registered project')
        if host_role_shared_across_projects(bindings, values['roleAgentId'], project):
            raise ValueError('A host role cannot share memory authority across projects')
        values['workspace'] = str(workspace)
        values['baseURL'] = _origin(values['baseURL'])
        # One host bridge and installation keep a single workspace. A second
        # project is allowed only for a different host role; the same role
        # across projects is still rejected above. Native ReMe remains per role.
        owner = (str(workspace), values['installationId'])
        previous = bridge_owners.setdefault(values['baseURL'], owner)
        if previous != owner:
            raise ValueError('A fixed bridge cannot serve different workspaces or installations')
        previous = installation_owners.setdefault(values['installationId'], str(workspace))
        if previous != str(workspace):
            raise ValueError('One fixed bridge installation cannot use another workspace')
        ledger_path = _path(values['ledgerPath'], private=True)
        values['ledger'] = RoleDispatchLedger(ledger_path)
        credentials = _json(_path(values['hostCredentialsFile'], private=True))
        if not isinstance(credentials, dict) or set(credentials) != {'username', 'password'}:
            raise ValueError('Host login file must contain only username and password')
        if any(not isinstance(v, str) or not v for v in credentials.values()):
            raise ValueError('Host login values must be nonempty strings')
        values['credentials'] = credentials
        service_token = _path(values['serviceTokenFile'], private=True).read_text().strip()
        if len(service_token) < 32 or any(char.isspace() for char in service_token):
            raise ValueError('Execution service token must contain at least 32 non-whitespace characters')
        values['serviceToken'] = service_token
        if not isinstance(values['memoryQuery'], str) or not values['memoryQuery'].strip() or len(values['memoryQuery']) > 4000:
            raise ValueError('Trusted memoryQuery must contain 1..4000 characters')
        for name, default, maximum in (('requestTimeoutSeconds', 30, 120), ('parentTimeoutSeconds', 180, 3600)):
            value = values.get(name, default)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError('Invalid execution timeout')
            values[name] = value
        prepared.append(values)
    factory = client_factory or httpx.Client
    built = {}; clients = []
    try:
        for values in prepared:
            client = factory(timeout=values['requestTimeoutSeconds'], follow_redirects=False, trust_env=False)
            clients.append(client)
            try:
                response = client.post(values['baseURL'] + '/api/auth/login', json=values['credentials'])
                if response.status_code != 200: raise ValueError('Host authentication failed')
                content = response.json()
                token = (content.get('token') or content.get('access_token')) if isinstance(content, dict) else None
                if not isinstance(token, str) or not token.strip(): raise ValueError('Host authentication response invalid')
            except (httpx.HTTPError, ValueError):
                # Do not expose response/request contents, credentials, or HTTP exception chains.
                raise ValueError('Execution host authentication failed; no task submitted') from None
            try:
                response = client.get(values['baseURL'] + '/api/agent-line/execution-binding',
                    headers={'Authorization':'Bearer ' + token,
                             'X-Agent-Line-Service-Token':values['serviceToken']})
                if response.status_code != 200:
                    raise ExecutionCompatibilityError('binding_unavailable')
                descriptor = response.json()
                if (not isinstance(descriptor, dict)
                        or type(descriptor.get('schemaVersion')) is not int
                        or descriptor.get('schemaVersion') != 1
                        or descriptor.get('ownedContainer') is not True
                        or descriptor.get('workspace') != values['workspace']
                        or not isinstance(descriptor.get('managedRoleIds'), list)
                        or values['roleAgentId'] not in descriptor['managedRoleIds']
                        or not isinstance(descriptor.get('targetAgentId'), str)
                        or not descriptor['targetAgentId']):
                    raise ValueError('Remote binding mismatch')
                if values['roleRevision'] is not None:
                    revisions = descriptor.get('roleRevisions')
                    try:
                        actual = validate_role_revision(revisions.get(values['roleAgentId']) if isinstance(revisions, dict) else None)
                    except ValueError:
                        raise ValueError('role_revision_unavailable') from None
                    if actual != values['roleRevision']:
                        raise ValueError('role_revision_mismatch')
            except (httpx.HTTPError, ValueError) as error:
                if isinstance(error, ExecutionCompatibilityError):
                    raise
                reason = str(error)
                if reason in {'role_revision_mismatch', 'role_revision_unavailable'}:
                    raise ValueError(reason) from None
                code = 'binding_unavailable' if isinstance(error, httpx.HTTPError) else 'binding_incompatible'
                raise ExecutionCompatibilityError(code) from None
            gateway = RoleDispatchGateway(values['baseURL'], token, values['serviceToken'],
                role_agent_id=values['roleAgentId'], deployment_id=values['installationId'], ledger=values['ledger'],
                memory_query=values['memoryQuery'], timeout_seconds=values['parentTimeoutSeconds'], client=client)
            # Display-only metadata from the descriptor already validated above.
            # Missing optional presentation fields never change execution admission.
            try:
                executor_id = segment(descriptor['targetAgentId'])
            except ValueError:
                executor_id = None
            controls = descriptor.get('roleExecutionControls')
            controls = controls.get(values['roleAgentId']) if isinstance(controls, dict) else None
            readonly = (isinstance(controls, dict) and set(controls) == {'sandbox', 'approval_policy'}
                        and controls['sandbox'] == 'read-only' and controls['approval_policy'] == 'never')
            built[(values['projectId'], values['roleBindingId'])] = BoundRoleGateway(
                values['projectId'], values['roleBindingId'], values['roleAgentId'], values['workspace'],
                values['installationId'], gateway, client, values['roleRevision'],
                executor_agent_id=executor_id, execution_controls=('read-only', 'never') if readonly else None)
        return built
    except BaseException:
        for client in clients:
            try: client.close()
            except Exception: pass
        raise
