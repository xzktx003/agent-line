"""Deterministic, in-memory expansion of one pinned child workflow."""
import copy
import hashlib
import re

def _authoring():
    # sim_authoring imports this module from its compiler; defer the reverse edge.
    from . import sim_authoring
    return sim_authoring


def _reference_module():
    from . import subworkflow_reference
    return subworkflow_reference


def _canonical(value):
    from .store import canonical
    return canonical(value)


def _canonical_equal(left, right):
    try:
        return _canonical(left) == _canonical(right)
    except (ValueError, TypeError, RecursionError):
        raise _authoring().AuthoringError('subworkflow_group_invalid') from None


def _digest(*parts):
    payload = '\0'.join(str(part) for part in parts).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def _mapped_id(call_id, child_id, kind='node'):
    # 3 + 60 = 63 characters, within the authoring identifier limit.
    return 'sf_' + _digest(call_id, kind, child_id)[:60]


def _mapped_name(call_id, child_id, original):
    # Names have a compact deterministic prefix and retain the child label.
    prefix = 'SF' + _digest(call_id, child_id)[:12] + '_'
    return (prefix + original)[:255]


def _identity(reference, parent_workflow):
    error = _authoring().AuthoringError
    try:
        checked = _reference_module().validate_reference(
            reference, reference['projectId'], parent_workflow)
    except (KeyError, TypeError, ValueError, AttributeError):
        raise error('subworkflow_reference_invalid') from None
    return checked


def _validate_input_from(value, parent_nodes):
    a = _authoring()
    a.keys(value, ('nodeId', 'field'))
    a.ident(value['nodeId'])
    node = parent_nodes.get(value['nodeId'])
    a.require(node is not None and
              ((node['type'] == 'start' and value['field'] == 'input') or
               (node['type'] in ('agent', 'join', 'subflow_output') and value['field'] == 'output')),
              'agent_source_invalid')
    return copy.deepcopy(value)


def _fragment(source, reference, call_id, input_from, *, input_mapping=None, output_mapping=None):
    """Return the child members and edges in their exact parent projection."""
    a = _authoring()
    from .subworkflow_mapping import validate_mapping
    try:
        input_mapping = validate_mapping(input_mapping) if input_mapping is not None else None
        output_mapping = validate_mapping(output_mapping) if output_mapping is not None else None
    except (ValueError, TypeError, RecursionError):
        raise a.AuthoringError('subworkflow_mapping_invalid') from None
    nodes = {node['id']: node for node in source['nodes']}
    starts = [node for node in source['nodes'] if node['type'] == 'start']
    ends = [node for node in source['nodes'] if node['type'] == 'end']
    a.require(len(starts) == 1, 'subworkflow_start_invalid')
    a.require(len(ends) == 1, 'subworkflow_end_invalid')
    allowed = {'start', 'end', 'agent', 'condition', 'human', 'loop', 'parallel', 'join'}
    a.require(all(node['type'] in allowed for node in source['nodes']), 'subworkflow_node_type_unsupported')
    start_id = starts[0]['id']
    mapping = {node_id: (call_id if node_id == ends[0]['id'] else
                         _mapped_id(call_id, node_id)) for node_id in nodes}

    def mapped_config(node):
        kind, config = node['type'], copy.deepcopy(node['config'])
        if kind == 'start':
            return {'reference': copy.deepcopy(reference),
                    'sourceDocument': copy.deepcopy(source),
                    'callId': call_id, 'inputFrom': copy.deepcopy(input_from),
                    **({'inputMapping': copy.deepcopy(input_mapping)} if input_mapping is not None else {}),
                    **({'outputMapping': copy.deepcopy(output_mapping)} if output_mapping is not None else {})}
        if kind == 'end':
            result = {'entryNodeId': mapping[start_id]}
            if 'resultFrom' in config:
                result['resultFrom'] = mapping[config['resultFrom']]
            else:
                result['output'] = config['output']
            if output_mapping is not None:
                result['outputMapping'] = copy.deepcopy(output_mapping)
            return result
        if kind == 'agent':
            config['inputFrom']['nodeId'] = mapping[config['inputFrom']['nodeId']]
        elif kind == 'condition':
            for rule in config['rules']:
                if 'source' in rule:
                    rule['source'] = mapping[rule['source']]
                else:
                    rule['source'] = mapping[start_id]
        elif kind == 'loop':
            config['body'] = mapping[config['body']]
        elif kind == 'parallel':
            config['branches'] = [mapping[item] for item in config['branches']]
            config['join'] = mapping[config['join']]
        elif kind == 'join':
            config['parallelId'] = mapping[config['parallelId']]
            config['requiredBranches'] = [mapping[item] for item in config['requiredBranches']]
        return config

    members = []
    for node in source['nodes']:
        child_id = node['id']
        members.append({
            'id': mapping[child_id],
            'type': 'subflow_input' if node['type'] == 'start' else
                    'subflow_output' if node['type'] == 'end' else node['type'],
            'name': _mapped_name(call_id, child_id, node['name']),
            'position': copy.deepcopy(node['position']),
            'config': mapped_config(node),
        })
    edges = []
    for edge in source['edges']:
        edges.append({
            'id': _mapped_id(call_id, edge['id'], 'edge'),
            'source': mapping[edge['source']],
            'target': mapping[edge['target']],
            **({'branch': edge['branch']} if 'branch' in edge else {}),
        })
    return members, edges, mapping[start_id]


def _source_document(source_document, reference, parent_workflow):
    a = _authoring()
    if type(source_document) is not dict:
        raise a.AuthoringError('subworkflow_source_invalid')
    try:
        encoded = _canonical(source_document).encode('utf-8')
    except (ValueError, TypeError, RecursionError):
        raise a.AuthoringError('subworkflow_source_invalid') from None
    if len(encoded) > _reference_module().MAX_DOCUMENT_BYTES:
        raise a.AuthoringError('subworkflow_snapshot_too_large')
    # Validate the exact pinned identity and compile in the child namespace.
    try:
        checked = _reference_module().validate_reference(
            reference, reference['projectId'], parent_workflow)
    except (KeyError, TypeError, ValueError, AttributeError):
        raise a.AuthoringError('subworkflow_reference_invalid') from None
    if checked != reference:
        raise a.AuthoringError('subworkflow_reference_invalid')
    _reject_unsupported_source(source_document)
    compiled = a.compile_document(source_document, reference['workflowId'])
    digest = hashlib.sha256(_canonical(compiled).encode('utf-8')).hexdigest()
    if digest != reference['graphSha256']:
        raise a.AuthoringError('subworkflow_version_mismatch')
    return copy.deepcopy(source_document)


def _reject_unsupported_source(source_document):
    a = _authoring()
    nodes = source_document.get('nodes') if type(source_document) is dict else None
    if type(nodes) is not list:
        raise a.AuthoringError('subworkflow_source_invalid')
    if any(type(node) is dict and node.get('type') in ('subflow_input', 'subflow_output')
           for node in nodes):
        raise a.AuthoringError('subworkflow_nested_unsupported')
    allowed = {'start', 'end', 'agent', 'condition', 'human', 'loop', 'parallel', 'join'}
    if any(type(node) is dict and (not isinstance(node.get('type'), str)
                                   or node.get('type') not in allowed) for node in nodes):
        raise a.AuthoringError('subworkflow_node_type_unsupported')
    if any(type(node) is dict and node.get('type') == 'agent'
           and isinstance(node.get('config'), dict) and 'batchItem' in node['config']
           for node in nodes):
        raise a.AuthoringError('subworkflow_batch_identity_pending')


def build_preview(parent_document, reference, source_document, *, parent_workflow,
                  call_id, edge_id, input_from, input_mapping=None, output_mapping=None):
    """Expand the chosen edge with a pinned, single-end child graph."""
    a = _authoring()
    # Each graph is validated in its own native namespace before any copying.
    a.compile_document(parent_document, parent_workflow)
    reference_value = _identity(reference, parent_workflow)
    source = _source_document(source_document, reference_value, parent_workflow)
    a.ident(call_id)
    a.ident(edge_id)
    parent_nodes = {node['id']: node for node in parent_document['nodes']}
    a.require(call_id not in parent_nodes, 'subworkflow_call_id_conflict')
    input_from = _validate_input_from(input_from, parent_nodes)
    edges = copy.deepcopy(parent_document['edges'])
    selected = [edge for edge in edges if edge['id'] == edge_id]
    a.require(len(selected) == 1, 'subworkflow_edge_invalid')
    original_target = selected[0]['target']
    members, child_edges, entry_id = _fragment(source, reference_value, call_id, input_from,
                                             input_mapping=input_mapping, output_mapping=output_mapping)
    selected[0]['target'] = entry_id
    exit_edge = {'id': _mapped_id(call_id, 'exit', 'exit'), 'source': call_id,
                 'target': original_target}
    expanded = copy.deepcopy(parent_document)
    expanded['nodes'].extend(members)
    expanded['edges'] = edges + child_edges + [exit_edge]
    # This catches parent-side duplicate labels, invalid mapped references, and
    # dominance rules using the actual compiler's same checks.
    a.compile_document(expanded, parent_workflow)
    return expanded


def validate_groups(document):
    """Validate every persisted wrapper as a closed deterministic fragment."""
    a = _authoring()
    nodes = document.get('nodes', []) if type(document) is dict else []
    edges = document.get('edges', []) if type(document) is dict else []
    entries = [node for node in nodes if node.get('type') == 'subflow_input']
    outputs = [node for node in nodes if node.get('type') == 'subflow_output']
    if not entries and not outputs:
        return None
    ids = {node.get('id') for node in nodes if type(node) is dict}
    claimed = set()
    groups = []
    for entry in entries:
        try:
            a.keys(entry, ('id', 'type', 'name', 'position', 'config'))
            a.ident(entry['id'])
            a.require(isinstance(entry['config'], dict)
                      and {'reference','sourceDocument','callId','inputFrom'} <= set(entry['config'])
                      <= {'reference','sourceDocument','callId','inputFrom','inputMapping','outputMapping'},
                      'subworkflow_group_invalid')
            call_id = a.ident(entry['config']['callId'])
            reference = entry['config']['reference']
            a.keys(reference, _reference_module().REFERENCE_KEYS)
            for key in ('projectId', 'workflowId', 'deploymentVersionId'):
                from .adapters.sim import segment
                segment(reference[key])
            if (type(reference['revision']) is not int or reference['revision'] < 1
                    or reference['revision'] > 9_007_199_254_740_991
                    or not isinstance(reference['graphSha256'], str)
                    or re.fullmatch(r'[0-9a-f]{64}', reference['graphSha256']) is None):
                raise a.AuthoringError('subworkflow_reference_invalid')
        except (KeyError, TypeError, ValueError, AttributeError):
            raise a.AuthoringError('subworkflow_group_invalid') from None
        source = entry['config']['sourceDocument']
        if type(source) is not dict:
            raise a.AuthoringError('subworkflow_source_invalid')
        try:
            if len(_canonical(source).encode('utf-8')) > _reference_module().MAX_DOCUMENT_BYTES:
                raise a.AuthoringError('subworkflow_snapshot_too_large')
            _reject_unsupported_source(source)
            compiled = a.compile_document(source, reference['workflowId'])
            if hashlib.sha256(_canonical(compiled).encode('utf-8')).hexdigest() != reference['graphSha256']:
                raise a.AuthoringError('subworkflow_version_mismatch')
        except a.AuthoringError:
            raise
        except (ValueError, TypeError, KeyError, RecursionError):
            raise a.AuthoringError('subworkflow_source_invalid') from None
        input_from = entry['config']['inputFrom']
        a.keys(input_from, ('nodeId', 'field'))
        a.ident(input_from['nodeId'])
        parent_source = next((node for node in nodes if node.get('id') == input_from['nodeId']), None)
        if parent_source is None or not (
                (parent_source['type'] == 'start' and input_from['field'] == 'input') or
                (parent_source['type'] in ('agent', 'join', 'subflow_output') and input_from['field'] == 'output')):
            raise a.AuthoringError('agent_source_invalid')
        members, child_edges, entry_id = _fragment(source, reference, call_id, input_from,
                                                 input_mapping=entry['config'].get('inputMapping'),
                                                 output_mapping=entry['config'].get('outputMapping'))
        if entry_id != entry['id']:
            raise a.AuthoringError('subworkflow_group_invalid')
        expected_by_id = {member['id']: member for member in members}
        member_ids = set(expected_by_id)
        if claimed & member_ids or not member_ids <= ids:
            raise a.AuthoringError('subworkflow_group_overlap')
        claimed.update(member_ids)
        actual_by_id = {node['id']: node for node in nodes if node.get('id') in member_ids}
        if set(actual_by_id) != member_ids:
            raise a.AuthoringError('subworkflow_group_invalid')
        for member_id, expected in expected_by_id.items():
            actual = actual_by_id[member_id]
            if (actual.get('type') != expected['type'] or actual.get('name') != expected['name']
                    or not _canonical_equal(actual.get('config'), expected['config'])
                    or set(actual) != {'id', 'type', 'name', 'position', 'config'}):
                raise a.AuthoringError('subworkflow_group_invalid')
        expected_child_edges = child_edges
        actual_child_edges = [edge for edge in edges
                              if edge.get('source') in member_ids and edge.get('target') in member_ids]
        if _sort_json(actual_child_edges) != _sort_json(expected_child_edges):
            raise a.AuthoringError('subworkflow_group_edges_invalid')
        incoming = [edge for edge in edges if edge.get('target') in member_ids
                    and edge.get('source') not in member_ids]
        outgoing = [edge for edge in edges if edge.get('source') in member_ids
                    and edge.get('target') not in member_ids]
        if (len(incoming) != 1 or incoming[0].get('target') != entry['id']
                or len(outgoing) != 1 or outgoing[0].get('source') != call_id):
            raise a.AuthoringError('subworkflow_group_boundary_invalid')
        expected_exit = {'id': _mapped_id(call_id, 'exit', 'exit'), 'source': call_id,
                         'target': outgoing[0].get('target')}
        if outgoing[0] != expected_exit:
            raise a.AuthoringError('subworkflow_group_boundary_invalid')
        groups.append((member_ids, entry['id'], call_id))
    if len({group[2] for group in groups}) != len(groups):
        raise a.AuthoringError('subworkflow_group_overlap')
    output_ids = [group[2] for group in groups]
    if len(outputs) != len(groups) or set(output_ids) != {node['id'] for node in outputs}:
        raise a.AuthoringError('subworkflow_group_output_invalid')
    # No edge may cross a group's boundary except its one entry/exit edge.
    for member_ids, entry_id, call_id in groups:
        for edge in edges:
            source_inside = edge.get('source') in member_ids
            target_inside = edge.get('target') in member_ids
            if source_inside and target_inside:
                continue
            if target_inside and edge.get('target') != entry_id:
                raise a.AuthoringError('subworkflow_group_boundary_invalid')
            if source_inside and edge.get('source') != call_id:
                raise a.AuthoringError('subworkflow_group_boundary_invalid')
        # Outside consumers may read only the declared return port. Requiring
        # closed control edges alone would still allow data references to bypass
        # outputMapping and expose an internal Agent's unprojected result.
        for node in nodes:
            if node['id'] in member_ids:
                continue
            for source in _data_sources(node):
                if source in member_ids and source != call_id:
                    raise a.AuthoringError('subworkflow_result_boundary_invalid')
    return None


def _data_sources(node):
    config = node.get('config')
    if not isinstance(config, dict):
        return []  # The main compiler rejects malformed node configuration.
    if node['type'] in ('agent', 'subflow_input'):
        value = config.get('inputFrom')
        return [value.get('nodeId')] if isinstance(value, dict) else []
    if node['type'] in ('end', 'subflow_output') and 'resultFrom' in config:
        return [config['resultFrom']]
    if node['type'] == 'condition' and isinstance(config.get('rules'), list):
        return [rule['source'] for rule in config['rules']
                if isinstance(rule, dict) and 'source' in rule]
    return []


def _sort_json(value):
    return sorted(value, key=lambda item: item['id'])
