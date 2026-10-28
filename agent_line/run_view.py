"""Read-only fixed-publication graph and bounded local node-task projection.

This is an observation surface, not another graph executor. Edges and native
nodes remain unobserved; a registered task is never proof of a traversed edge.
"""
import math
import json
import re
import time

from .adapters.sim import segment
from .published_workflows import graph_hash
from .sim_authoring import AuthoringError, decode_document, native_id
from .store import ROLE_REVISION_BLOCKS, SUBMISSION_REJECTION_BLOCK, Conflict, canonical


class ViewUnavailable(ValueError):
    pass


def bounded_id(value, limit=256):
    if (not isinstance(value, str) or not value or len(value) > limit
            or value.strip() != value or '\0' in value):
        raise ViewUnavailable('invalid_identity')
    return value


def visible_submission_block(task):
    """Pass through a block already computed by the task list. Do not infer a queue."""
    if not isinstance(task, dict):
        return {}
    block = task.get('submissionBlock')
    if type(block) is str and (block in ROLE_REVISION_BLOCKS or block == SUBMISSION_REJECTION_BLOCK):
        return {'submissionBlock': block}
    return {}


def _checked_binding(gateway, binding_id, run_id, scopes):
    binding = gateway.binding_for(binding_id, scopes)
    segment(run_id)
    publication = binding.get('publication')
    if not isinstance(publication, dict) or gateway.catalog is None:
        raise ViewUnavailable('publication_required')
    workflow, project = binding['workflowId'], binding['projectId']
    # Check the exact historical binding before reading any native definition.
    expected = canonical(gateway.publication_snapshot(binding_id, binding))
    with gateway.journal.connect() as db:
        db.execute('BEGIN')
        pin = db.execute("SELECT * FROM operations WHERE workflow=? AND run=? AND context='run-publication'",
                         (workflow, run_id)).fetchone()
        root = db.execute("SELECT * FROM operations WHERE workflow=? AND run=? AND context=''",
                          (workflow, run_id)).fetchone()
        if pin is None or root is None:
            raise KeyError(run_id)
        if pin['input'] != expected or pin['receipt'] != expected or pin['status'] != 'accepted':
            raise ViewUnavailable('publication_pin_invalid')
        for row in (pin, root):
            if (type(row['updated']) not in (int, float) or not math.isfinite(row['updated'])
                    or row['updated'] < 0 or row['status'] not in ('intent', 'accepted', 'rejected', 'uncertain')):
                raise ViewUnavailable('submission_record_invalid')
        try:
            if (canonical(json.loads(root['input'])) != root['input']
                    or (root['receipt'] is not None and canonical(json.loads(root['receipt'])) != root['receipt'])):
                raise ValueError
        except (ValueError, TypeError, RecursionError):
            raise ViewUnavailable('submission_record_invalid') from None
    revision = publication.get('revision')
    digest = publication.get('graphSha256')
    if (type(revision) is not int or revision < 1 or not isinstance(digest, str)
            or re.fullmatch(r'[0-9a-f]{64}', digest) is None):
        raise ViewUnavailable('invalid_publication')
    return binding


def _identity(binding_id, binding, run_id):
    return {'bindingId': binding_id, 'projectId': binding['projectId'], 'workflowId': binding['workflowId'],
            'runId': run_id, 'revision': binding['publication']['revision'],
            'graphSha256': binding['publication']['graphSha256']}


def _publication_document(gateway, binding):
    workflow, project = binding['workflowId'], binding['projectId']
    publication = binding['publication']
    revision, digest = publication['revision'], publication['graphSha256']
    try:
        version = gateway.catalog.authoring.clients[project].get_version(workflow, revision)
    except AuthoringError:
        raise ViewUnavailable('publication_unavailable') from None
    if (version.get('id') != publication.get('deploymentVersionId')
            or version.get('version') != revision or type(version.get('version')) is not int):
        raise ViewUnavailable('publication_mismatch')
    try:
        state = version['state']
        if graph_hash(state, workflow) != digest:
            raise ViewUnavailable('graph_mismatch')
        document = decode_document(state)
    except (AuthoringError, KeyError, TypeError, RecursionError):
        raise ViewUnavailable('graph_unavailable') from None
    if not 1 <= len(document['nodes']) <= 200 or len(document['edges']) > 1000:
        raise ViewUnavailable('graph_size')
    return document


def read_view(gateway, store, binding_id, run_id, scopes):
    binding = _checked_binding(gateway, binding_id, run_id, scopes)
    document = _publication_document(gateway, binding)
    workflow = binding['workflowId']
    nodes = []
    execution_nodes = set()
    for node in document['nodes']:
        identity = bounded_id(node['id'], 128)
        position = node['position']
        if any(type(position.get(key)) not in (int, float)
               or not math.isfinite(position[key]) or abs(position[key]) > 10000 for key in ('x', 'y')):
            raise ViewUnavailable('graph_position')
        execution_id = native_id(workflow, identity)
        execution_nodes.add(execution_id)
        nodes.append({'id': identity, 'executionNodeId': execution_id, 'type': node['type'],
                      'name': bounded_id(node['name'], 255),
                      'position': {key: position[key] for key in ('x', 'y')}})
    edges = [{key: edge[key] for key in ('id', 'source', 'target', 'branch') if key in edge}
             for edge in document['edges']]
    return {'source': 'fixed_publication_run_view', 'readOnly': True,
            'identity': _identity(binding_id, binding, run_id), 'graph': {'nodes': nodes, 'edges': edges},
            **_local_state(store, binding, run_id, scopes, execution_nodes=execution_nodes)}


def _local_state(store, binding, run_id, scopes, *, execution_nodes=None):
    workflow, project = binding['workflowId'], binding['projectId']
    revision = binding['publication']['revision']
    policies = (binding.get('externalTasks') or {}).get('agentPolicies', {})
    if not isinstance(policies, dict):
        raise ViewUnavailable('invalid_task_policy')
    allowed_nodes = set(policies)
    if execution_nodes is not None:
        allowed_nodes &= execution_nodes
    page = store.list_tasks(scopes, project_id=project, workflow_id=workflow, run_id=run_id, limit=500)
    tasks = []
    for task in page['tasks']:
        payload = task['payload']
        node = payload['workflow']
        if (payload['projectId'] != project or node.get('id') != workflow or node.get('runId') != run_id
                or node.get('revision') != str(revision) or node.get('nodeId') not in allowed_nodes):
            raise Conflict('Local task does not match the fixed run graph')
        agent = payload.get('roleAgentId')
        try:
            agent = bounded_id(agent)
        except ViewUnavailable:
            agent = None
        tasks.append({'taskId': bounded_id(task['id'], 128), 'nodeId': node['nodeId'],
                      'nodeExecutionId': bounded_id(node.get('nodeExecutionId')),
                      'attemptId': bounded_id(node.get('attemptId')), 'roleAgentId': agent,
                      **{key: task[key] for key in ('executionStatus', 'verificationStatus',
                                                  'memoryStatus', 'resourcesReleased')},
                      **visible_submission_block(task)})
    return {'tasks': tasks,
            'coverage': {'complete': page['nextCursor'] is None, 'limit': 500},
            'observedAt': time.time(), 'edgeExecution': 'unknown', 'nativeNodeExecution': 'unknown'}


def read_state(gateway, store, binding_id, run_id, scopes, graph_sha256):
    """Refresh a verified graph's local task facts without native API reads."""
    binding = _checked_binding(gateway, binding_id, run_id, scopes)
    if (not isinstance(graph_sha256, str) or graph_sha256 != binding['publication']['graphSha256']):
        raise ViewUnavailable('cached_graph_mismatch')
    return {'source': 'fixed_publication_run_state', 'readOnly': True,
            'identity': _identity(binding_id, binding, run_id),
            **_local_state(store, binding, run_id, scopes)}


def read_native_summary(gateway, store, binding_id, run_id, scopes, graph_sha256):
    """Explicit native run observation; local timers never invoke this path."""
    binding = _checked_binding(gateway, binding_id, run_id, scopes)
    if not isinstance(graph_sha256, str) or graph_sha256 != binding['publication']['graphSha256']:
        raise ViewUnavailable('cached_graph_mismatch')
    native = gateway.client.read_status_summary(binding['workflowId'], run_id)
    pause = native['native']['paused']
    if pause is not None:
        wait = 'unknown'
        if pause['kind'] == 'time':
            wait = 'scheduled_time'
        elif pause['kind'] == 'human':
            block = pause['blockedOnBlockId']
            if block in (binding.get('externalTasks') or {}).get('agentPolicies', {}):
                wait = 'agent_result'
            elif block in (binding.get('humanApproval') or {}).get('blockIds', []):
                wait = 'business_input'
        # Sim's human-pause mechanism also carries machine result waits.
        # Only frozen policies can distinguish these purposes.
        pause['waitingFor'] = wait
    local = _local_state(store, binding, run_id, scopes)
    return {'source': 'fixed_publication_run_native_summary', 'readOnly': True,
            'identity': _identity(binding_id, binding, run_id), **native,
            'local': {'observedAt': local['observedAt'], 'taskCount': len(local['tasks']),
                      'resourcesUnreleased': sum(task['executionStatus'] != 'registered'
                                                 and task['resourcesReleased'] is not True for task in local['tasks']),
                      'coverage': local['coverage']},
            'codexActivity': 'unknown', 'nodeExecution': 'unknown', 'edgeExecution': 'unknown'}


def read_condition_decisions(gateway, binding_id, run_id, scopes, graph_sha256, *, after=None, limit=20):
    """Native current block decisions, not all activations or a new authority."""
    binding = _checked_binding(gateway, binding_id, run_id, scopes)
    if graph_sha256 != binding['publication']['graphSha256']:
        raise ViewUnavailable('cached_graph_mismatch')
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ViewUnavailable('invalid_decision_page')
    document = _publication_document(gateway, binding)
    conditions = sorted((node for node in document['nodes'] if node['type'] == 'condition'), key=lambda node: node['id'])
    if after is not None and (type(after) is not str or after not in {node['id'] for node in conditions}):
        raise KeyError(after)
    remaining = [node for node in conditions if after is None or node['id'] > after]
    page = remaining[:limit]
    native_ids = {node['id']: native_id(binding['workflowId'], node['id']) for node in page}
    outputs = gateway.client.read_condition_outputs(binding['workflowId'], run_id, list(native_ids.values())) if page else {}
    result = []
    for node in page:
        execution_id = native_ids[node['id']]
        item = {'nodeId': node['id'], 'executionNodeId': execution_id,
                'state': 'unobserved' if execution_id not in outputs else 'unverified'}
        output = outputs.get(execution_id)
        if type(output) is dict:
            rule = output.get('ruleId')
            known_rules = {rule['id'] for rule in node['config']['rules']} | {'else'}
            if type(rule) is str and rule in known_rules:
                edges = [edge for edge in document['edges'] if edge['source'] == node['id'] and edge.get('branch') == rule]
                default = rule == 'else'
                if (len(edges) == 1 and type(output.get('conditionResult')) is bool
                        and output['conditionResult'] is (not default)
                        and output.get('targetBlockId') == native_id(binding['workflowId'], edges[0]['target'])):
                    edge = edges[0]
                    item.update(state='matched', decision={'ruleId': rule, 'edgeId': edge['id'],
                        'targetNodeId': edge['target'], 'isDefault': default, 'conditionResult': not default})
        result.append(item)
    return {'source': 'fixed_publication_run_condition_decisions', 'readOnly': True,
            'identity': _identity(binding_id, binding, run_id), 'scope': 'native_current_block_outputs',
            'observedAt': time.time(), 'conditions': result,
            'nextCursor': page[-1]['id'] if len(remaining) > limit else None}
