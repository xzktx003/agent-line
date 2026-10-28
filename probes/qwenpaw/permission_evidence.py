"""Read-only evidence for the pinned probe's single rejected native command.

Native command item IDs and outer exec call IDs are different namespaces. This
parser establishes an explicitly reported unique-candidate correlation within a
complete single turn; it does not claim a direct item-ID-to-call-ID mapping.
"""
import json
import re
import shlex


IDENTITY_KEYS = {'upstreamTaskId', 'codexThreadId', 'codexTurnId'}
METHOD = 'item/commandExecution/requestApproval'
JS_STRING = r'"(?:[^"\\\r\n]|\\[^\r\n])*"'
CALL = re.compile(
    r'\s*text\(await tools\.exec_command\(\{\s*cmd\s*:\s*(' + JS_STRING + r')'
    r'\s*,\s*sandbox_permissions\s*:\s*(' + JS_STRING + r')'
    r'\s*,\s*justification\s*:\s*(' + JS_STRING + r')'
    r'\s*,\s*max_output_tokens\s*:\s*1000\s*\}\)\);\s*')


def require(condition, code):
    if not condition:
        raise ValueError(code)


def text_id(value):
    return isinstance(value, str) and 0 < len(value) <= 256 and all(ord(x) >= 32 for x in value)


def rejected_native_evidence(rows, identity, approval, decision_receipt, launcher_record, nonce):
    """Return compact verified facts, or fail closed without touching any state.

    Inputs must come from the original append-only rollout, approval receipts,
    and owned launcher record. Model-authored messages are never evidence.
    Only the exact single-command probe wrapper and native output shape are
    supported. Extra tool calls, turns, approvals, retries, or outputs fail.
    """
    require(isinstance(identity, dict) and set(identity) == IDENTITY_KEYS
            and all(text_id(x) for x in identity.values()), 'invalid_identity')
    require(isinstance(nonce, str) and re.fullmatch('[0-9a-f]{32}', nonce) is not None,
            'invalid_nonce')
    require(isinstance(rows, list) and 0 < len(rows) <= 10000, 'invalid_rows')
    require(all(isinstance(x, dict) and isinstance(x.get('payload'), dict) for x in rows),
            'invalid_row')
    require(all(isinstance(x, dict) for x in (approval, decision_receipt, launcher_record)),
            'invalid_receipts')
    thread, turn = identity['codexThreadId'], identity['codexTurnId']
    for value in (approval, decision_receipt):
        require(value.get('identity') == identity and value.get('purpose') == 'execution_permission'
                and value.get('bound') is True and value.get('method') == METHOD
                and text_id(value.get('requestId')) and text_id(value.get('providerItemId'))
                and text_id(value.get('taskId')) and text_id(value.get('targetAgentId'))
                and value.get('roleSessionId') == 'agent-line-' + value['taskId'],
                'approval_identity_mismatch')
    for key in ('requestId', 'providerItemId', 'taskId', 'roleSessionId', 'targetAgentId',
                'identity', 'method', 'summary', 'projectId', 'roleAgentId', 'workflowId',
                'runId', 'nodeId', 'nodeExecutionId', 'attemptId', 'revision'):
        require(approval.get(key) == decision_receipt.get(key), 'decision_binding_mismatch')
    decision = decision_receipt.get('decision')
    require(approval.get('status') == 'waiting' and approval.get('decision') is None
            and approval.get('deliveryStatus') == 'not_submitted'
            and decision_receipt.get('status') == 'decision_recorded'
            and decision_receipt.get('deliveryStatus') == 'accepted'
            and isinstance(decision, dict) and set(decision) == {'decisionId', 'decision', 'decidedBy'}
            and decision.get('decision') == 'reject' and text_id(decision.get('decisionId'))
            and text_id(decision.get('decidedBy')), 'reject_decision_not_accepted')

    owner, container = launcher_record.get('owner'), launcher_record.get('containerId')
    require(isinstance(owner, str) and re.fullmatch('[0-9a-f]{32}', owner) is not None
            and isinstance(container, str) and re.fullmatch('[0-9a-f]{64}', container) is not None,
            'invalid_launcher_owner')
    require(launcher_record.get('threads') == [thread]
            and launcher_record.get('identities') == [
                {'codexThreadId': thread, 'codexTurnId': turn, 'nativeTerminal': True}]
            and launcher_record['identities'][0]['nativeTerminal'] is True
            and (launcher_record.get('approvalBindingsUnavailable') is None
                 or launcher_record.get('approvalBindingsUnavailable') is False),
            'launcher_identity_mismatch')
    bindings = launcher_record.get('approvalBindings')
    require(isinstance(bindings, list) and len(bindings) == 1 and isinstance(bindings[0], dict),
            'ambiguous_launcher_approval')
    binding = bindings[0]
    require(set(binding) == {'requestId', 'method', 'threadId', 'turnId', 'itemId', 'resolved'}
            and binding['method'] == METHOD and binding['threadId'] == thread
            and binding['turnId'] == turn and binding['itemId'] == approval['providerItemId']
            and binding['resolved'] is True
            and (type(binding['requestId']) is int and -(2**53) < binding['requestId'] < 2**53
                 or text_id(binding['requestId'])), 'launcher_approval_mismatch')

    selected = {'session_meta': [], 'turn_context': [], 'task_started': [], 'terminal': [],
                'call': [], 'output': []}
    for index, row in enumerate(rows):
        kind, payload = row.get('type'), row['payload']
        if kind in ('session_meta', 'turn_context'):
            selected[kind].append((index, payload))
        if kind == 'event_msg':
            event = payload.get('type')
            require(event in ('task_started', 'task_complete', 'turn_aborted', 'item_completed',
                              'token_count', 'thread_settings_applied'), 'unexpected_native_event')
            if event == 'task_started':
                selected['task_started'].append((index, payload))
            if event in ('task_complete', 'turn_aborted'):
                selected['terminal'].append((index, payload))
            if 'turn_id' in payload:
                require(payload['turn_id'] == turn, 'foreign_event_turn')
            if 'root_turn_id' in payload:
                require(payload['root_turn_id'] == turn, 'foreign_root_turn')
            if event == 'item_completed':
                item = payload.get('item')
                require(isinstance(item, dict) and item.get('type') in (
                    'UserMessage', 'AgentMessage', 'Reasoning'), 'unexpected_native_tool_item')
        if kind == 'response_item':
            item_type = payload.get('type')
            require(item_type in ('message', 'reasoning', 'custom_tool_call', 'custom_tool_call_output'),
                    'unexpected_response_tool')
            if item_type == 'message':
                require(payload.get('role') in ('user', 'assistant', 'system', 'developer'),
                        'unexpected_response_role')
            if item_type in ('custom_tool_call', 'custom_tool_call_output'):
                metadata = payload.get('internal_chat_message_metadata_passthrough')
                require(isinstance(metadata, dict) and metadata.get('turn_id') == turn,
                        'response_turn_mismatch')
                selected['call' if item_type == 'custom_tool_call' else 'output'].append((index, payload))
    require(all(len(values) == 1 for values in selected.values()), 'ambiguous_or_missing_native_evidence')
    meta = selected['session_meta'][0][1]
    context = selected['turn_context'][0][1]
    started = selected['task_started'][0][1]
    terminal = selected['terminal'][0][1]
    require(meta.get('id') == thread and text_id(meta.get('cwd'))
            and meta['cwd'] == launcher_record.get('workspace') == context.get('cwd'),
            'native_workspace_mismatch')
    require(context.get('turn_id') == turn and started.get('turn_id') == turn
            and terminal.get('turn_id') == turn and terminal.get('type') == 'task_complete'
            and context.get('approval_policy') == 'on-request'
            and isinstance(context.get('sandbox_policy'), dict)
            and context['sandbox_policy'].get('type') == 'workspace-write', 'native_turn_mismatch')
    require(context.get('root_turn_id', turn) == turn, 'foreign_root_turn')
    order = [selected[key][0][0] for key in (
        'session_meta', 'task_started', 'turn_context', 'call', 'output', 'terminal')]
    require(order == sorted(order), 'native_evidence_order_mismatch')
    call = selected['call'][0][1]
    output = selected['output'][0][1]
    require(call.get('name') == 'exec' and call.get('status') == 'completed'
            and text_id(call.get('id')) and text_id(call.get('call_id'))
            and output.get('call_id') == call['call_id'] and text_id(output.get('id')),
            'native_call_mismatch')
    require(isinstance(call.get('input'), str), 'invalid_call_input')
    match = CALL.fullmatch(call['input'])
    require(match is not None, 'unsupported_or_multiple_commands')
    require(isinstance(approval.get('summary'), str), 'invalid_approval_summary')
    try:
        command, policy, justification = (json.loads(x) for x in match.groups())
        summary = shlex.split(approval.get('summary', ''))
    except (ValueError, TypeError):
        raise ValueError('invalid_command_encoding') from None
    expected_command = 'python3 -c "print(\'' + nonce + '\')"'
    require(command.strip() == expected_command and policy == 'require_escalated'
            and isinstance(justification, str) and justification.strip()
            and len(summary) == 3 and summary[:2] in (
                ['/usr/bin/bash', '-lc'], ['/bin/bash', '-lc'])
            and summary[2].strip() == command.strip(), 'command_or_summary_mismatch')
    chunks = output.get('output')
    require(isinstance(chunks, list) and len(chunks) == 2
            and all(isinstance(x, dict) and set(x) == {'type', 'text'}
                    and x.get('type') == 'input_text' and isinstance(x.get('text'), str) for x in chunks),
            'invalid_tool_output')
    require(re.fullmatch(r'Script failed\nWall time [0-9]+(?:\.[0-9]+)? seconds\nOutput:\n',
                         chunks[0]['text']) is not None
            and chunks[1]['text'] == 'Script error:\nexec_command failed: CreateProcess { message: "Rejected(\\"rejected by user\\")" }'
            and nonce not in ''.join(x['text'] for x in chunks), 'native_rejection_not_observed')
    return {'status': 'verified_native_rejection', 'identity': dict(identity),
            'requestId': approval['requestId'], 'providerItemId': approval['providerItemId'],
            'launcherRequestId': binding['requestId'], 'callId': call['call_id'],
            'owner': owner, 'containerId': container,
            'correlation': 'unique_single_tool_and_approval_same_turn',
            'directItemToCallIdMapping': False,
            'commandExecution': 'rejected_before_create_process'}
