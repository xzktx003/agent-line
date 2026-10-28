"""Exact project-owned child-version references; no execution or graph scheduler."""
import copy
import hashlib
import re

from .adapters.sim import segment
from .sim_authoring import AuthoringError, compile_document, decode_document
from .store import canonical

MAX_DOCUMENT_BYTES = 256 * 1024
REFERENCE_KEYS = frozenset({
    'projectId', 'workflowId', 'revision', 'deploymentVersionId', 'graphSha256',
})


def validate_reference(value, project, parent_workflow):
    """Copy only a complete immutable version identity in the caller's project."""
    if type(value) is not dict or set(value) != REFERENCE_KEYS:
        raise AuthoringError('subworkflow_reference_invalid')
    for key in ('projectId', 'workflowId', 'deploymentVersionId'):
        segment(value[key])
    if (value['projectId'] != project or value['workflowId'] == parent_workflow
            or type(value['revision']) is not int
            or not 1 <= value['revision'] <= 9_007_199_254_740_991
            or not isinstance(value['graphSha256'], str)
            or re.fullmatch(r'[0-9a-f]{64}', value['graphSha256']) is None):
        raise AuthoringError('subworkflow_reference_invalid')
    return dict(value)


def read_reference(client, reference, *, project, parent_workflow):
    """Read and recompile the selected revision, never resolve the active version.

    The caller must prove ownership of both workflows before calling this helper.
    This validates a snapshot only; mapping, expansion and execution are separate.
    """
    reference = validate_reference(reference, project, parent_workflow)
    workflow = reference['workflowId']
    version = client.get_version(workflow, reference['revision'])
    if (type(version) is not dict or version.get('id') != reference['deploymentVersionId']
            or type(version.get('version')) is not int
            or version['version'] != reference['revision']
            or type(version.get('isActive')) is not bool):
        raise AuthoringError('subworkflow_version_mismatch')
    document = decode_document(version.get('state'))
    encoded = canonical(document).encode('utf-8')
    if len(encoded) > MAX_DOCUMENT_BYTES:
        raise AuthoringError('subworkflow_snapshot_too_large')
    compiled = compile_document(document, workflow)
    graph_sha256 = hashlib.sha256(canonical(compiled).encode('utf-8')).hexdigest()
    if graph_sha256 != reference['graphSha256']:
        raise AuthoringError('subworkflow_version_mismatch')
    nodes = {node['id']: node for node in document['nodes']}
    start = next(node for node in document['nodes'] if node['type'] == 'start')
    outputs = []
    for node in document['nodes']:
        if node['type'] != 'end':
            continue
        config = node['config']
        if 'resultFrom' not in config:
            outputs.append({'endNodeId': node['id'], 'mode': 'literal_object'})
            continue
        source = nodes[config['resultFrom']]
        port = {'endNodeId': node['id'], 'mode': 'result_reference',
                'sourceNodeId': source['id'], 'sourceType': source['type'],
                'responseField': 'agentLineResult'}
        if source['type'] == 'agent':
            port['outputSchemaId'] = source['config']['outputSchemaId']
            if 'outputFields' in source['config']:
                port['outputFields'] = copy.deepcopy(source['config']['outputFields'])
        else:
            port['joinPolicy'] = source['config']['policy']
        outputs.append(port)
    return {
        'source': 'verified_fixed_subworkflow_reference', 'readOnly': True,
        'executionSupported': False, 'reason': 'reference_read_only',
        'parentWorkflowId': parent_workflow, 'reference': reference,
        'isActive': version['isActive'],
        'input': {'type': 'object', 'startNodeId': start['id'], 'field': 'input'},
        'outputs': outputs,
        'nodeCount': len(document['nodes']), 'edgeCount': len(document['edges']),
        'documentSha256': hashlib.sha256(encoded).hexdigest(),
        'document': copy.deepcopy(document),
    }
