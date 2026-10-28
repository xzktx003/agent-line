"""Parse cached parent-role tool traces without consulting the native service."""
import hashlib
import json
import time


MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
MAX_MESSAGES = 2000
MAX_BLOCKS = 10000
MAX_TOOL_CALLS = 100
TOOL_NAMES = frozenset({'memory_search', 'agent_line_delegate'})


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      allow_nan=False).encode('utf-8')


def _hash(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _identifier(value):
    return (type(value) is str and bool(value.strip()) and len(value) <= 256
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def unknown_memory_trace(task_id, role_agent_id, read_at=None):
    return {
        'source': 'cached_native_memory_trace', 'readOnly': True, 'state': 'unknown',
        'taskId': task_id, 'roleAgentId': role_agent_id,
        'roleSessionId': 'agent-line-' + task_id,
        'parentTaskId': None, 'snapshotSha256': None,
        'searches': None, 'delegations': None,
        'memoryInjection': 'unverified', 'coverage': 'cached_parent_snapshot',
        'snapshotCapturedAt': None, 'readAt': time.time() if read_at is None else read_at,
    }


def _unique_json(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Ambiguous cached JSON object')
        value[key] = item
    return value


def _reject_constant(value):
    raise ValueError('Nonfinite cached JSON value')


def _arguments(value):
    if type(value) is str:
        value = json.loads(value, object_pairs_hook=_unique_json, parse_constant=_reject_constant)
    if type(value) is not dict:
        raise ValueError('Invalid cached tool arguments')
    return value


def _output_state(data):
    state = data.get('state')
    if state == 'success':
        return 'success'
    if state == 'error':
        return 'error'
    return 'unknown'


def _blocks(observation):
    result = observation.get('result')
    output = result.get('output') if type(result) is dict else None
    if type(output) is not list or len(output) > MAX_MESSAGES:
        raise ValueError('Invalid cached parent output')
    ordered = []
    total_blocks = 0
    for message in output:
        if type(message) is not dict or type(message.get('content')) is not list:
            raise ValueError('Invalid cached parent message')
        total_blocks += len(message['content'])
        if total_blocks > MAX_BLOCKS:
            raise ValueError('Cached parent output too large')
        for block in message['content']:
            if type(block) is not dict:
                raise ValueError('Invalid cached parent content block')
            kind = message.get('type')
            data = block.get('data')
            if kind not in ('plugin_call', 'plugin_call_output'):
                continue
            if type(data) is not dict:
                raise ValueError('Invalid cached plugin block')
            name = data.get('name')
            if name in TOOL_NAMES:
                call_id = data.get('call_id')
                if not _identifier(call_id):
                    raise ValueError('Invalid cached plugin call identity')
                ordered.append((kind, name, call_id, data))
    return ordered


def parse_memory_trace(task_id, role_agent_id, parent_task_id, observation,
                       expected_identity, *, read_at=None):
    """Return a complete hashed trace, or an all-unknown summary on bad evidence."""
    now = time.time() if read_at is None else read_at
    base = unknown_memory_trace(task_id, role_agent_id, now)
    try:
        if (not _identifier(task_id) or not _identifier(role_agent_id)
                or not _identifier(parent_task_id) or type(observation) is not dict):
            return base
        # The console snapshot often omits task_id. The caller already supplies the
        # parent bound to this cached row. A different id is still another parent.
        if observation.get('task_id', parent_task_id) != parent_task_id:
            return base
        encoded = _canonical(observation)
        if len(encoded) > MAX_SNAPSHOT_BYTES:
            return base
        ordered = _blocks(observation)
        calls = {}
        outputs = {}
        searches = []
        delegations = []
        successful_searches = []
        for kind, name, call_id, data in ordered:
            if kind == 'plugin_call':
                if call_id in calls:
                    return base
                if name == 'memory_search':
                    if len(searches) >= MAX_TOOL_CALLS:
                        return base
                    arguments = _arguments(data.get('arguments'))
                    calls[call_id] = (name, len(searches))
                    searches.append({'callId': call_id, 'argumentsSha256': _hash(arguments),
                                     'resultState': 'not_recorded', 'outputSha256': None})
                else:
                    if len(delegations) >= MAX_TOOL_CALLS:
                        return base
                    arguments = _arguments(data.get('arguments'))
                    if (set(arguments) != {'instruction'} or type(arguments['instruction']) is not str
                            or not arguments['instruction'].strip() or len(arguments['instruction']) > 28000):
                        return base
                    calls[call_id] = (name, len(delegations))
                    delegations.append({'callId': call_id,
                                        'instructionSha256': hashlib.sha256(
                                            arguments['instruction'].encode('utf-8')).hexdigest(),
                                        'resultState': 'not_recorded',
                                        'executionTaskMatched': None,
                                        'precedingSearchCallIds': list(successful_searches)})
            else:
                # An output must correspond to an earlier same-name call and occur once.
                if call_id not in calls or calls[call_id][0] != name or call_id in outputs:
                    return base
                outputs[call_id] = True
                call_index = calls[call_id][1]
                state = _output_state(data)
                if state in ('success', 'error') and 'output' not in data:
                    return base
                output = data.get('output')
                if state == 'unknown':
                    output_hash = None
                else:
                    output_hash = _hash(output)
                if name == 'memory_search':
                    searches[call_index]['resultState'] = state
                    searches[call_index]['outputSha256'] = output_hash
                    if state == 'success':
                        successful_searches.append(call_id)
                else:
                    delegated = delegations[call_index]
                    delegated['resultState'] = state
                    if state == 'success':
                        decoded = output
                        if type(decoded) is str:
                            try:
                                decoded = json.loads(decoded, object_pairs_hook=_unique_json,
                                                     parse_constant=_reject_constant)
                            except (ValueError, TypeError):
                                decoded = None
                        if type(decoded) is dict:
                            identity = decoded.get('identity')
                            expected_upstream = (expected_identity.get('upstreamTaskId')
                                                 if type(expected_identity) is dict else None)
                            if not _identifier(expected_upstream):
                                expected_upstream = None
                            output_task_id = decoded.get('taskId')
                            output_upstream = (identity.get('upstreamTaskId')
                                               if type(identity) is dict else None)
                            if (output_task_id is not None and output_task_id != task_id
                                    or expected_upstream is not None and output_upstream is not None
                                    and output_upstream != expected_upstream):
                                delegated['executionTaskMatched'] = False
                            elif (output_task_id == task_id and expected_upstream is not None
                                  and output_upstream == expected_upstream):
                                delegated['executionTaskMatched'] = True
        snapshot_hash = hashlib.sha256(encoded).hexdigest()
        return {
            'source': 'cached_native_memory_trace', 'readOnly': True, 'state': 'observed',
            'taskId': task_id, 'roleAgentId': role_agent_id,
            'roleSessionId': 'agent-line-' + task_id,
            'parentTaskId': parent_task_id, 'snapshotSha256': snapshot_hash,
            'searches': searches, 'delegations': delegations,
            'memoryInjection': 'unverified', 'coverage': 'cached_parent_snapshot',
            'snapshotCapturedAt': None, 'readAt': now,
        }
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, OverflowError):
        return base


def evaluate_cached_retrieval(trace, observation, expected_text):
    """Check an explicit literal criterion, not relevance or Codex injection."""
    if (not isinstance(expected_text, str) or not 4 <= len(expected_text) <= 256
            or expected_text != expected_text.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in expected_text)):
        raise ValueError('Invalid retrieval criterion')
    if (trace.get('state') != 'observed'
            or hashlib.sha256(_canonical(observation)).hexdigest() != trace.get('snapshotSha256')):
        raise ValueError('Retrieval snapshot unavailable')

    def contains_text(value):
        # Do not serialize objects: a match in a key or JSON escape is not a
        # text-value match. Do not expose native strings in the response.
        if type(value) is str:
            return expected_text in value
        if type(value) is list:
            return any(contains_text(item) for item in value)
        if type(value) is dict:
            return any(contains_text(item) for item in value.values())
        return False

    successful = {item['callId'] for item in trace['searches']
                  if item['resultState'] == 'success'}
    search_matches = set()
    instruction_matches = set()
    for kind, name, call_id, data in _blocks(observation):
        if (kind == 'plugin_call_output' and name == 'memory_search'
                and call_id in successful and contains_text(data.get('output'))):
            search_matches.add(call_id)
        if kind == 'plugin_call' and name == 'agent_line_delegate':
            if expected_text in _arguments(data.get('arguments'))['instruction']:
                instruction_matches.add(call_id)
    linked_matches = set()
    matched_delegations = 0
    for delegation in trace['delegations']:
        if delegation['executionTaskMatched'] is not True:
            continue
        preceding = search_matches.intersection(delegation['precedingSearchCallIds'])
        linked_matches.update(preceding)
        if preceding and delegation['callId'] in instruction_matches:
            matched_delegations += 1
    return {
        'source': 'cached_memory_retrieval_evaluation', 'readOnly': True,
        'taskId': trace['taskId'], 'roleAgentId': trace['roleAgentId'],
        'roleSessionId': trace['roleSessionId'], 'parentTaskId': trace['parentTaskId'],
        'snapshotSha256': trace['snapshotSha256'], 'readAt': trace['readAt'],
        'snapshotCapturedAt': None, 'coverage': 'cached_parent_snapshot',
        'criterion': 'user_supplied_literal_text',
        'criterionSha256': hashlib.sha256(expected_text.encode('utf-8')).hexdigest(),
        'successfulSearches': len(successful), 'textMatchedSearches': len(search_matches),
        'matchedSearchesBeforeDelegation': len(linked_matches),
        'matchedDelegations': matched_delegations,
        'semanticRelevance': 'unverified', 'codexInputInjection': 'unverified',
    }
