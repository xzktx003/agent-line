"""Read-only, scope-bound health counters from one local task-store snapshot."""
import json
import math


QUIET_WINDOW_SECONDS = 300
TERMINAL = frozenset({'succeeded', 'failed', 'canceled'})
EXECUTION_STATES = frozenset({
    'registered', 'submitting', 'uncertain', 'accepted',
    'cancel_requested', 'succeeded', 'failed', 'canceled',
})
VERIFICATION_STATES = frozenset({'pending', 'passed', 'failed', 'canceled'})
MEMORY_STATES = frozenset({
    'not_requested', 'pending', 'committed', 'failed', 'needs_review', 'deleting', 'deleted',
})
OBSERVATION_KINDS = frozenset({'execution_observed', 'execution_observation_unknown'})
COUNT_KEYS = (
    'total', 'queued', 'unsettled', 'resourcesAwaitingExit', 'verificationPending',
    'verificationFailed', 'memoryPending', 'memoryFailed', 'observationUnknown',
    'observationNotRecorded', 'quietUnsettled', 'activityUnknown', 'workspaceLeases',
)


def unknown_run_health(target_scope, observed_at):
    return {
        'schemaVersion': 1,
        'scope': dict(zip(('projectId', 'workflowId', 'runId'), target_scope)),
        'state': 'unknown',
        'observedAt': observed_at,
        'quietWindowSeconds': QUIET_WINDOW_SECONDS,
        'counts': {key: None for key in COUNT_KEYS},
        'lastTaskEventAt': None,
        'nativeProgress': 'unknown',
        'codexActivity': 'unknown',
    }


def _valid_time(value, observed_at):
    return (type(value) in (int, float) and math.isfinite(value)
            and value > 0 and value <= observed_at)


def summarize_run_health(db, target_scope, observed_at):
    """Summarize one exact scope using the caller's already-open read transaction."""
    base = unknown_run_health(target_scope, observed_at)
    if (len(target_scope) != 3 or any(type(value) is not str or not value.strip()
                                      for value in target_scope)
            or type(observed_at) not in (int, float) or not math.isfinite(observed_at) or observed_at <= 0):
        return base
    project_id, workflow_id, run_id = target_scope
    # A malformed payload anywhere makes ownership of that row unknowable, so
    # no subset can be claimed as a complete run snapshot.
    if db.execute('SELECT 1 FROM tasks WHERE json_valid(payload)=0 LIMIT 1').fetchone():
        return base
    rows = db.execute("""SELECT t.id,t.payload,t.execution_status,t.verification_status,
            t.memory_status,t.resources_released,
            (SELECT count(*) FROM workspace_leases l WHERE l.task_id=t.id) AS lease_count,
            (SELECT e.kind FROM events e WHERE e.task_id=t.id
             AND e.kind IN ('execution_observed','execution_observation_unknown')
             ORDER BY e.seq DESC LIMIT 1) AS observation_kind,
            (SELECT e.created_at FROM events e WHERE e.task_id=t.id
             AND e.kind NOT IN ('execution_observed','execution_observation_unknown')
             ORDER BY e.seq DESC LIMIT 1) AS latest_task_event_at
        FROM tasks t
        WHERE json_extract(t.payload,'$.projectId')=?
          AND json_extract(t.payload,'$.workflow.id')=?
          AND json_extract(t.payload,'$.workflow.runId')=?
        ORDER BY t.id""", (project_id, workflow_id, run_id)).fetchall()
    counts = {key: 0 for key in COUNT_KEYS}
    counts['total'] = len(rows)
    latest_any_event = db.execute("""SELECT e.created_at FROM events e
        JOIN tasks t ON t.id=e.task_id
        WHERE json_extract(t.payload,'$.projectId')=?
          AND json_extract(t.payload,'$.workflow.id')=?
          AND json_extract(t.payload,'$.workflow.runId')=?
        ORDER BY e.seq DESC LIMIT 1""", (project_id, workflow_id, run_id)).fetchone()
    last_event_at = latest_any_event['created_at'] if latest_any_event is not None else None
    if last_event_at is not None and not _valid_time(last_event_at, observed_at):
        last_event_at = None

    for row in rows:
        try:
            payload = json.loads(row['payload'])
            workflow = payload['workflow']
        except (TypeError, ValueError, KeyError, UnicodeError, RecursionError):
            return base
        if (type(payload) is not dict or type(workflow) is not dict
                or payload.get('projectId') != project_id
                or workflow.get('id') != workflow_id or workflow.get('runId') != run_id
                or type(row['execution_status']) is not str or row['execution_status'] not in EXECUTION_STATES
                or type(row['verification_status']) is not str or row['verification_status'] not in VERIFICATION_STATES
                or type(row['memory_status']) is not str or row['memory_status'] not in MEMORY_STATES
                or type(row['resources_released']) is not int or row['resources_released'] not in (0, 1)
                or type(row['lease_count']) is not int or row['lease_count'] < 0):
            return base
        execution = row['execution_status']
        resources_released = row['resources_released'] == 1
        unsettled = execution != 'registered' and (execution not in TERMINAL or not resources_released)
        verification_pending = execution == 'succeeded' and row['verification_status'] == 'pending'
        counts['queued'] += execution == 'registered'
        counts['unsettled'] += unsettled
        counts['resourcesAwaitingExit'] += execution != 'registered' and not resources_released
        counts['verificationPending'] += verification_pending
        counts['verificationFailed'] += row['verification_status'] == 'failed'
        counts['memoryPending'] += row['memory_status'] in ('pending', 'needs_review', 'deleting')
        counts['memoryFailed'] += row['memory_status'] == 'failed'
        counts['workspaceLeases'] += row['lease_count']
        observation_kind = row['observation_kind']
        if observation_kind is not None and observation_kind not in OBSERVATION_KINDS:
            return base
        counts['observationUnknown'] += observation_kind == 'execution_observation_unknown'
        counts['observationNotRecorded'] += observation_kind is None and unsettled
        if unsettled or verification_pending:
            event_at = row['latest_task_event_at']
            if event_at is None or not _valid_time(event_at, observed_at):
                counts['activityUnknown'] += 1
            elif observed_at - event_at >= QUIET_WINDOW_SECONDS:
                counts['quietUnsettled'] += 1

    return {
        'schemaVersion': 1,
        'scope': {'projectId': project_id, 'workflowId': workflow_id, 'runId': run_id},
        'state': 'known',
        'observedAt': observed_at,
        'quietWindowSeconds': QUIET_WINDOW_SECONDS,
        'counts': counts,
        'lastTaskEventAt': last_event_at,
        'nativeProgress': 'unknown',
        'codexActivity': 'unknown',
    }
