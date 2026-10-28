"""Bounded audit evidence retained in the original intent before soft archive.

These snapshots are read-only historical evidence, never editable graph state
or an alternative scheduler. Sim's official archive hides its history APIs.
"""
import hashlib
import json
import time
from .sim_authoring import AuthoringError
from .store import canonical

MAX_ITEMS = 1000
MAX_BYTES = 16 * 1024 * 1024
TERMINAL = frozenset({'completed', 'failed', 'cancelled'})


def project_namespace(project):
    return 'authoring-' + hashlib.sha256(project.encode()).hexdigest()


def record(journal, project, workflow):
    with journal.connect() as db:
        rows = db.execute(
            "SELECT * FROM operations WHERE workflow=? AND context='author-archive' "
            "AND json_extract(input,'$.request.workflowId')=? ORDER BY run LIMIT 2",
            (project_namespace(project), workflow)).fetchall()
    if len(rows) > 1:
        raise AuthoringError('archive_records_conflict')
    if not rows:
        return None
    row = rows[0]
    return {**journal._public(row), 'payload': json.loads(row['input'])}


def ensure_mutable(journal, project, workflow):
    if record(journal, project, workflow) is not None:
        raise AuthoringError('workflow_archived_or_pending')


def pages(fetch):
    cursor, seen, items = None, set(), []
    while True:
        page = fetch(cursor)
        if not isinstance(page, dict) or not isinstance(page.get('data'), list):
            raise AuthoringError('archive_read_invalid')
        items.extend(page['data'])
        if len(items) > MAX_ITEMS:
            raise AuthoringError('archive_history_too_large')
        following = page.get('nextCursor')
        if following is None:
            return items
        if (not isinstance(following, str) or not following or len(following) > 4096
                or following in seen or len(seen) >= MAX_ITEMS):
            raise AuthoringError('archive_cursor_invalid')
        seen.add(following)
        cursor = following


def _identities(items, key):
    ids = [item.get(key) for item in items]
    if any(type(value) not in (str, int) for value in ids) or len(set(ids)) != len(ids):
        raise AuthoringError('archive_duplicate_history')
    return set(ids)


def capture(client, journal, workflow, expected_hash):
    from .authoring_api import state_hash
    draft = client.get_state(workflow)
    if state_hash(draft) != expected_hash:
        raise AuthoringError('archive_draft_changed')
    detail = client.get_detail(workflow)
    version_index = pages(lambda cursor: client.versions(workflow, cursor, 100))
    run_index = pages(lambda cursor: client.runs(workflow, cursor, 100))
    _identities(version_index, 'version')
    _identities(version_index, 'id')
    run_ids = _identities(run_index, 'runId')
    if any(item.get('status') not in TERMINAL for item in run_index):
        raise AuthoringError('archive_run_active')
    with journal.connect() as db:
        local_runs = db.execute("SELECT run,status FROM operations WHERE workflow=? AND context='' LIMIT 1001", (workflow,)).fetchall()
        publications = db.execute("SELECT run,receipt FROM operations WHERE workflow=? AND context='run-publication' LIMIT 1001",
                                  (workflow,)).fetchall()
    if len(local_runs)+len(publications)+len(version_index)+len(run_index) > MAX_ITEMS:
        raise AuthoringError('archive_history_too_large')
    if any(row['status'] in ('intent', 'uncertain')
           or (row['status'] == 'accepted' and row['run'] not in run_ids) for row in local_runs):
        raise AuthoringError('archive_local_run_unresolved')
    snapshot = {'schemaVersion': 1, 'workflowId': workflow, 'capturedAt': time.time(),
                'detail': detail, 'draft': draft, 'draftSha256': expected_hash,
                'versions': [], 'runs': [],
                'runPublications': [{'runId': row['run'], 'publication': json.loads(row['receipt'])}
                                    for row in publications]}

    def bounded():
        if len(canonical(snapshot).encode()) > MAX_BYTES:
            raise AuthoringError('archive_history_too_large')
    bounded()
    for version in version_index:
        observed = client.get_version(workflow, version['version'])
        if any(observed.get(key) != version.get(key) for key in ('id', 'version', 'isActive')):
            raise AuthoringError('archive_version_mismatch')
        snapshot['versions'].append(observed)
        bounded()
    for run in run_index:
        observed = client.get_run_audit(workflow, run['runId'])
        if observed.get('status') not in TERMINAL or observed['status'] != run['status']:
            raise AuthoringError('archive_run_active')
        snapshot['runs'].append(observed)
        bounded()
    # Same-instance starts and edits share the authoring lock. Re-read the
    # native indexes as well: external Sim editors do not take that lock.
    if (state_hash(client.get_state(workflow)) != expected_hash
            or canonical(pages(lambda cursor: client.versions(workflow, cursor, 100))) != canonical(version_index)
            or canonical(pages(lambda cursor: client.runs(workflow, cursor, 100))) != canonical(run_index)):
        raise AuthoringError('archive_history_changed')
    return snapshot


def snapshot_digest(snapshot):
    return hashlib.sha256(canonical(snapshot).encode()).hexdigest()


def execution_scope(journal):
    """Share the exact editor lock even for statically configured bindings."""
    from contextlib import contextmanager
    import fcntl
    import os
    import stat

    @contextmanager
    def locked():
        path = journal.path.with_name(journal.path.name + '.authoring.lock')
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1):
                raise ValueError('Invalid authoring lock')
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)
    return locked()
