"""Project concurrency derived from existing durable tasks and owned leases."""
import json

TERMINAL = frozenset({'succeeded', 'failed', 'canceled'})


def validate_project_limit(value):
    if type(value) is not int or not 1 <= value <= 100:
        raise ValueError('Invalid project concurrency limit')
    return value


def project_capacity(db, payload):
    """Use one caller-owned read/claim transaction; never mutate or free leases."""
    project = payload.get('projectId')
    if not isinstance(project, str) or not project.strip():
        raise ValueError('Invalid project scope')
    limits, candidates = [], []
    for row in db.execute('''SELECT t.payload,t.execution_status,t.submission_status,
            t.resources_released,EXISTS(SELECT 1 FROM workspace_leases l WHERE l.task_id=t.id) owns_lease
            FROM tasks t WHERE t.execution_status NOT IN ('succeeded','failed','canceled')
            OR t.resources_released<>1
            OR EXISTS(SELECT 1 FROM workspace_leases l WHERE l.task_id=t.id)'''):
        other = json.loads(row['payload'])
        if not isinstance(other, dict):
            raise ValueError('Invalid task payload')
        if other.get('projectId') != project:
            continue
        if row['execution_status'] in TERMINAL and row['resources_released'] == 1 and not row['owns_lease']:
            continue
        if 'maxConcurrentTasks' in other:
            limits.append(validate_project_limit(other['maxConcurrentTasks']))
        candidates.append(row)
    # Existing installations without declared limits keep their original guards.
    if not limits:
        return None
    occupied = 0
    for row in candidates:
        status, released, leased = row['execution_status'], row['resources_released'], row['owns_lease']
        if released not in (0, 1) or status not in TERMINAL | {
                'registered', 'submitting', 'uncertain', 'accepted', 'cancel_requested'}:
            raise ValueError('Unknown project task state')
        if status == 'registered':
            if leased or released != 0 or row['submission_status'] != 'pending':
                raise ValueError('Invalid queued project task')
        else:
            if released != 0 or not leased:
                raise ValueError('Project execution ownership unknown')
            occupied += 1
    limit = min(limits)
    return {'state': 'known', 'maxConcurrentTasks': limit, 'occupied': occupied,
            'available': max(0, limit - occupied), 'admissionBlocked': occupied >= limit,
            'policy': 'minimum_unfinished_task_limit'}
