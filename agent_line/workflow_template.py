"""Portable, bounded workflow templates over the existing logical graph schema."""
import copy

from .adapters.sim import host_role_shared_across_projects, segment
from .sim_authoring import AuthoringError, compile_document
from .store import canonical


FORMAT = 'agent-line-workflow-template'
SCHEMA_VERSION = 1
MAX_TEMPLATE_BYTES = 256 * 1024
VALIDATION_NAMESPACE = 'agent-line-template-validation'


def _invalid():
    raise AuthoringError('workflow_template_invalid')


def _bounded(value):
    try:
        encoded = canonical(value).encode('utf-8')
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _invalid()
    if len(encoded) > MAX_TEMPLATE_BYTES:
        raise AuthoringError('workflow_template_too_large')


def _compile(document):
    _bounded(document)
    if type(document) is not dict:
        _invalid()
    compile_document(document, VALIDATION_NAMESPACE)
    for node in document['nodes']:
        if any(abs(value) > 10000 for value in node['position'].values()):
            raise AuthoringError('workflow_template_position_invalid')
        if node['type'] == 'agent' and node['config'].get('memoryPolicyId') != 'native-role':
            raise AuthoringError('workflow_template_memory_unsupported')


def _agent_nodes(document):
    return [node for node in document['nodes'] if node['type'] == 'agent']


def _slot_ids(agent_nodes):
    ordered = []
    for node in agent_nodes:
        slot = node['config']['roleBindingId']
        if type(slot) is not str:
            _invalid()
        segment(slot)
        if slot not in ordered:
            ordered.append(slot)
    return ordered


def export_template(document):
    """Replace role bindings with first-use slots after bounded local validation."""
    _compile(document)
    portable = copy.deepcopy(document)
    bindings = {}
    slots = []
    for node in _agent_nodes(portable):
        binding_id = node['config']['roleBindingId']
        if binding_id not in bindings:
            slot_id = f'slot_{len(bindings) + 1}'
            bindings[binding_id] = slot_id
            slots.append({'id': slot_id, 'name': f'角色 {len(slots) + 1}'})
        node['config']['roleBindingId'] = bindings[binding_id]
    _compile(portable)
    template = {'format': FORMAT, 'schemaVersion': SCHEMA_VERSION,
                'document': portable, 'roleSlots': slots}
    _bounded(template)
    return template


def _validate_template(template):
    _bounded(template)
    if (type(template) is not dict or set(template) != {'format', 'schemaVersion', 'document', 'roleSlots'}
            or template['format'] != FORMAT or type(template['schemaVersion']) is not int
            or template['schemaVersion'] != SCHEMA_VERSION or type(template['document']) is not dict
            or type(template['roleSlots']) is not list or len(template['roleSlots']) > 200):
        _invalid()
    slot_ids = []
    for index, slot in enumerate(template['roleSlots'], 1):
        if (type(slot) is not dict or set(slot) != {'id', 'name'}
                or slot['id'] != f'slot_{index}' or slot['name'] != f'角色 {index}'):
            _invalid()
        slot_ids.append(slot['id'])
    document = template['document']
    _compile(document)
    used_slots = _slot_ids(_agent_nodes(document))
    if used_slots != slot_ids:
        _invalid()
    return document, slot_ids


def validate_template(template):
    """Validate an entire portable envelope without binding roles or saving it."""
    _validate_template(template)
    return copy.deepcopy(template)


def import_template(template, role_bindings, *, project, configured_role_bindings):
    """Validate a portable template and materialize only caller-selected existing roles."""
    document, slot_ids = _validate_template(template)
    if type(role_bindings) is not dict or len(role_bindings) != len(slot_ids) or set(role_bindings) != set(slot_ids):
        _invalid()
    configured = configured_role_bindings
    resolved = {}
    for slot_id in slot_ids:
        binding_id = role_bindings[slot_id]
        try:
            segment(binding_id)
        except (TypeError, ValueError):
            _invalid()
        role = configured.get(binding_id) if type(binding_id) is str else None
        if (not isinstance(role, dict) or role.get('projectId') != project
                or role.get('enabled', True) is not True
                or type(role.get('roleAgentId')) is not str):
            raise AuthoringError('workflow_template_role_unavailable')
        try:
            segment(role['roleAgentId'])
        except (TypeError, ValueError):
            raise AuthoringError('workflow_template_role_unavailable') from None
        if host_role_shared_across_projects(configured, role['roleAgentId'], project):
            raise AuthoringError('workflow_template_role_unavailable')
        resolved[slot_id] = binding_id

    materialized = copy.deepcopy(document)
    for node in _agent_nodes(materialized):
        node['config']['roleBindingId'] = resolved[node['config']['roleBindingId']]
    _compile(materialized)
    return {'source': 'validated_workflow_template', 'projectId': project, 'document': materialized}
