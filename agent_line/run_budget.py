"""Validation and history helpers for workflow-run submission budgets."""

import json
import math
import time

from .execution_limits import valid_pre_intent_events, valid_unaccepted_submission, valid_unstarted_history


RUN_BUDGET_KEYS = frozenset({'maxSubmissions', 'maxExecutionSeconds'})


def validate_run_budget(value):
    """Validate an optional run budget; ``None`` denotes no configured budget."""
    if value is None:
        return None
    if type(value) is not dict or set(value) != RUN_BUDGET_KEYS:
        raise ValueError('Invalid run budget')
    submissions = value.get('maxSubmissions')
    seconds = value.get('maxExecutionSeconds')
    if type(submissions) is not int or not 1 <= submissions <= 10000:
        raise ValueError('Invalid maxSubmissions')
    if type(seconds) is not int or not 1 <= seconds <= 604800:
        raise ValueError('Invalid run maxExecutionSeconds')
    return {'maxSubmissions': submissions, 'maxExecutionSeconds': seconds}


def run_scope(payload):
    workflow = payload.get('workflow')
    if not isinstance(workflow, dict):
        return None
    values = (payload.get('projectId'), workflow.get('id'), workflow.get('runId'))
    if not all(isinstance(value, str) and value.strip() for value in values):
        return None
    return values


def validate_run_history(row, events, now=None):
    """Return intent timestamps or raise when this task history is not provable."""
    registrations = [event for event in events if event['kind'] == 'registered']
    intents = [event for event in events if event['kind'] == 'submission_intent']
    if len(registrations) != 1 or len(intents) > 1:
        raise ValueError('Invalid run submission history')
    if intents:
        intent = intents[0]
        if not valid_pre_intent_events(events, registrations[0], intent):
            raise ValueError('Invalid run submission event order')
        started = intent['created_at']
        current = time.time() if now is None else now
        if (type(started) not in (int, float) or not math.isfinite(started)
                or started <= 0 or type(current) not in (int, float)
                or not math.isfinite(current) or started > current):
            raise ValueError('Invalid run submission timestamp')
        if (row['execution_status'] == 'registered'
                and not valid_unaccepted_submission(row, events)):
            raise ValueError('Registered task has a submission intent')
        return [started]
    if not valid_unstarted_history(row, events):
        raise ValueError('Run task without intent is not proven unstarted')
    return []


def run_history(db, target_scope, now=None):
    """Return (frozen declaration, intent times, has rows) for one run scope."""
    malformed = db.execute(
        "SELECT 1 FROM tasks WHERE json_valid(payload)=0 LIMIT 1"
    ).fetchone()
    if malformed:
        raise ValueError('Task payload history is invalid')
    frozen_marker = object()
    frozen = frozen_marker
    intent_times = []
    has_rows = False
    matching = []
    query = """SELECT t.id,t.payload,t.execution_status,t.submission_status,
        t.execution_identity,t.evidence,t.resources_released,
        (SELECT MIN(e.seq) FROM events e WHERE e.task_id=t.id AND e.kind='registered') registration_seq
        FROM tasks t
        WHERE json_extract(t.payload,'$.projectId')=?
          AND json_extract(t.payload,'$.workflow.id')=?
          AND json_extract(t.payload,'$.workflow.runId')=?
        ORDER BY COALESCE(registration_seq,0),t.id"""
    for row in db.execute(query, target_scope):
        payload = json.loads(row['payload'])
        has_rows = True
        if 'runBudget' in payload:
            if payload['runBudget'] is None:
                raise ValueError('Payload runBudget cannot be null')
            declared = validate_run_budget(payload['runBudget'])
        else:
            declared = None
        if frozen is frozen_marker:
            frozen = declared
        elif frozen != declared:
            raise ValueError('Run budget declaration changed')
        matching.append(row)
    if frozen is frozen_marker or frozen is None:
        # Legacy and explicitly budget-free runs retain their prior admission behavior.
        return None, [], has_rows
    for row in matching:
        events = db.execute(
            'SELECT seq,kind,created_at,payload FROM events WHERE task_id=? ORDER BY seq',
            (row['id'],),
        ).fetchall()
        intent_times.extend(validate_run_history(row, events, now=now))
    return (None if frozen is frozen_marker else frozen), intent_times, has_rows


def run_deadline(budget, intent_times):
    if budget is None or not intent_times:
        return None
    return min(intent_times) + budget['maxExecutionSeconds']
