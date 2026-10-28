"""Validation and derived views for limits already carried by node-task payloads."""

import json
import math
import time


DEFAULT_LIMITS = {'maxExecutionSeconds': 3600, 'maxBusinessAttempts': 3}
PRE_SUBMISSION_EVENTS = frozenset({
    'registered', 'role_revision_required', 'role_revision_mismatch',
    'role_revision_unavailable', 'execution_readiness_stale', 'role_revision_ready',
})


def validate_limit_values(limits):
    """Validate limit values before a workflow has an execution identity."""
    if not isinstance(limits, dict) or set(limits) != set(DEFAULT_LIMITS):
        raise ValueError('Invalid execution limits')
    seconds = limits.get('maxExecutionSeconds')
    attempts = limits.get('maxBusinessAttempts')
    if type(seconds) is not int or not 1 <= seconds <= 86400:
        raise ValueError('Invalid maxExecutionSeconds')
    if type(attempts) is not int or not 1 <= attempts <= 100:
        raise ValueError('Invalid maxBusinessAttempts')
    return {'maxExecutionSeconds': seconds, 'maxBusinessAttempts': attempts}


def validate_limits(payload):
    """Validate explicitly configured limits and their grouping identity."""
    if 'limits' not in payload:
        return None
    limits = validate_limit_values(payload['limits'])
    workflow = payload.get('workflow')
    revision = workflow.get('revision') if isinstance(workflow, dict) else None
    if not isinstance(revision, str) or not revision.strip():
        raise ValueError('Execution limits require a workflow revision')
    if not scope(payload):
        raise ValueError('Execution limits require complete workflow scope')
    return limits


def scope(payload):
    workflow = payload.get('workflow')
    if not isinstance(workflow, dict):
        return None
    values = (payload.get('projectId'), workflow.get('id'), workflow.get('runId'), workflow.get('nodeId'))
    if not all(isinstance(value, str) and value.strip() for value in values):
        return None
    return values


def scope_projection(payload):
    workflow = payload.get('workflow', {})
    return {
        'projectId': payload['projectId'],
        'workflowId': workflow['id'],
        'workflowRevision': workflow.get('revision'),
        'runId': workflow['runId'],
        'nodeId': workflow['nodeId'],
    }


def matching_rows(db, target_scope):
    """Return rows in a quota group; revision, role and attempt are intentionally ignored."""
    query = """SELECT t.id,t.payload,t.execution_status,t.submission_status,
        t.execution_identity,t.evidence,t.resources_released,
        (SELECT MIN(e.seq) FROM events e WHERE e.task_id=t.id AND e.kind='registered') registration_seq
        FROM tasks t ORDER BY COALESCE(registration_seq,0),t.id"""
    for row in db.execute(query):
        payload = json.loads(row['payload'])
        if scope(payload) == target_scope:
            yield row, payload


def valid_unstarted_history(row, events):
    """Whether no intent is proven to mean no native submission was attempted."""
    kinds = [event['kind'] for event in events]
    if not events or kinds.count('registered') != 1:
        return False
    try:
        empty_identity = json.loads(row['execution_identity']) == {}
        empty_evidence = json.loads(row['evidence']) == []
    except (TypeError, ValueError):
        return False
    if row['execution_status'] == 'registered':
        return (
            row['submission_status'] == 'pending'
            and empty_identity and empty_evidence and not row['resources_released']
            and all(kind in PRE_SUBMISSION_EVENTS for kind in kinds)
        )
    if row['execution_status'] == 'canceled':
        return (
            row['submission_status'] == 'pending'
            and empty_identity and empty_evidence and row['resources_released']
            and kinds[-1] == 'canceled' and kinds.count('canceled') == 1
            and all(kind in PRE_SUBMISSION_EVENTS or kind == 'canceled' for kind in kinds)
        )
    return False


def valid_pre_intent_events(events, registration, intent):
    """Only registration/preflight events may precede the durable submit intent."""
    if registration['seq'] >= intent['seq']:
        return False
    return all(event['kind'] in PRE_SUBMISSION_EVENTS
               for event in events if event['seq'] < intent['seq'])


UNACCEPTED_TRAILING_KINDS = frozenset({
    'submission_rejected', 'submission_rejection_released',
})
UNACCEPTED_REJECTION_REASON = 'submission_rejected_unaccepted'


def _present_event_payload(event):
    names = event.keys() if hasattr(event, 'keys') else event
    if 'payload' not in names:
        return None
    raw = event['payload']
    if raw is None or raw == '':
        return None
    if isinstance(raw, dict):
        parsed = raw
    elif isinstance(raw, str):
        parsed = json.loads(raw)
    else:
        raise ValueError('Event payload is not an object')
    if not isinstance(parsed, dict):
        raise ValueError('Event payload is not an object')
    return parsed


def valid_unaccepted_submission(row, events):
    """Whether one registered task still holds its original unaccepted intent."""
    if not events:
        return False
    ordered = sorted(events, key=lambda event: event['seq'])
    registrations = [event for event in ordered if event['kind'] == 'registered']
    intents = [event for event in ordered if event['kind'] == 'submission_intent']
    if (row['execution_status'] != 'registered'
            or row['submission_status'] != 'pending'
            or row['resources_released']
            or len(registrations) != 1 or len(intents) != 1
            or ordered[-1]['kind'] != 'submission_rejected'):
        return False
    try:
        empty_identity = json.loads(row['execution_identity']) == {}
        empty_evidence = json.loads(row['evidence']) == []
    except (TypeError, ValueError):
        return False
    if not empty_identity or not empty_evidence:
        return False
    intent = intents[0]
    if not valid_pre_intent_events(ordered, registrations[0], intent):
        return False
    trailing = [event for event in ordered if event['seq'] > intent['seq']]
    if (not trailing or trailing[-1]['kind'] != 'submission_rejected'
            or any(event['kind'] not in UNACCEPTED_TRAILING_KINDS for event in trailing)):
        return False
    try:
        for event in trailing:
            payload = _present_event_payload(event)
            if payload is not None and payload.get('reason') != UNACCEPTED_REJECTION_REASON:
                return False
    except (TypeError, ValueError, KeyError):
        return False
    return True


def group_limits(db, target_scope):
    """Return (frozen limit, used intent count, consistent) for a quota group."""
    frozen = None
    used = 0
    consistent = True
    now = time.time()
    for row, payload in matching_rows(db, target_scope):
        limits = validate_limits(payload) if 'limits' in payload else None
        if limits is None:
            consistent = False
            continue
        if frozen is None:
            frozen = limits
        elif limits != frozen:
            consistent = False
        events = db.execute(
            'SELECT seq,kind,created_at,payload FROM events WHERE task_id=? ORDER BY seq',
            (row['id'],),
        ).fetchall()
        registrations = [event for event in events if event['kind'] == 'registered']
        intents = [event for event in events if event['kind'] == 'submission_intent']
        if len(registrations) != 1:
            consistent = False
        if len(intents) > 1:
            consistent = False
        if (intents and registrations
                and not valid_pre_intent_events(events, registrations[0], intents[0])):
            consistent = False
        if (row['execution_status'] == 'registered' and intents
                and not valid_unaccepted_submission(row, events)):
            consistent = False
        if not intents and not valid_unstarted_history(row, events):
            consistent = False
        for intent in intents:
            started = intent['created_at']
            if (type(started) not in (int, float) or not math.isfinite(started)
                    or started <= 0 or started > now):
                consistent = False
        used += len(intents)
    return frozen, used, consistent


def deadline_for(db, task, payload, *, now=None):
    """Derive the deadline from the sole durable submit intent, if one exists."""
    limits = validate_limits(payload)
    if limits is None:
        return None
    events = db.execute(
        'SELECT seq,kind,created_at,payload FROM events WHERE task_id=? ORDER BY seq',
        (task['id'],),
    ).fetchall()
    registrations = [event for event in events if event['kind'] == 'registered']
    intents = [event for event in events if event['kind'] == 'submission_intent']
    if len(registrations) != 1 or len(intents) > 1:
        raise ValueError('Limited task has invalid submit history')
    if not intents:
        if not valid_unstarted_history(task, events):
            raise ValueError('Limited task without submission intent is not proven unstarted')
        return None
    if not valid_pre_intent_events(events, registrations[0], intents[0]):
        raise ValueError('Limited task has invalid submit event order')
    if (task['execution_status'] == 'registered'
            and not valid_unaccepted_submission(task, events)):
        raise ValueError('Limited task has invalid submit event order')
    started = intents[0]['created_at']
    if type(started) not in (int, float) or not math.isfinite(started) or started <= 0:
        raise ValueError('Invalid submission intent timestamp')
    current = time.time() if now is None else now
    if type(current) not in (int, float) or not math.isfinite(current) or started > current:
        raise ValueError('Submission intent timestamp is in the future or clock is invalid')
    return started + limits['maxExecutionSeconds']
