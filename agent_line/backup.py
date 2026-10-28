"""Consistent extension database snapshots; not a whole-product backup.

QwenPaw memory, Sim storage, project code and live processes have separate
owners. Restoring this snapshot always closes admission until reconciliation.
"""

import hashlib
import json
import os
import shutil
import sqlite3
import time
import uuid
from pathlib import Path

from .store import READABLE_SCHEMA_VERSIONS, TaskStore


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def private_file(path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(fd, "wb")


def create_backup(database, destination):
    store = TaskStore(Path(database))
    destination = Path(destination)
    destination.mkdir(mode=0o700)
    snapshot = destination / "tasks.sqlite"
    with private_file(snapshot):
        pass
    with store.connect() as source:
        schema_version = source.execute("SELECT schema_version FROM installation").fetchone()[0]
        if schema_version not in READABLE_SCHEMA_VERSIONS:
            raise ValueError("Unsupported task database schema")
        target = sqlite3.connect(snapshot)
        try:
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Snapshot integrity check failed")
            installation = target.execute("SELECT id FROM installation").fetchone()[0]
        finally:
            target.close()
    manifest = {
        "formatVersion": 1,
        "schemaVersion": schema_version,
        "installationId": installation,
        "createdAt": time.time(),
        "sha256": digest(snapshot),
        "scope": "extension-task-database-only",
    }
    with private_file(destination / "manifest.json") as stream:
        stream.write(json.dumps(manifest, indent=2).encode())
        stream.flush()
        os.fsync(stream.fileno())
    return manifest


def restore_backup(bundle, target):
    bundle, target = Path(bundle), Path(target)
    manifest = json.loads((bundle / "manifest.json").read_text())
    if (
        manifest.get("formatVersion") != 1
        or manifest.get("schemaVersion") not in READABLE_SCHEMA_VERSIONS
    ):
        raise ValueError("Unsupported backup format; explicit migration required")
    snapshot = bundle / "tasks.sqlite"
    if digest(snapshot) != manifest.get("sha256"):
        raise ValueError("Backup checksum mismatch")
    # Never replace the live database; operators switch to a reconciled target later.
    with private_file(target) as stream:
        with snapshot.open("rb") as source:
            shutil.copyfileobj(source, stream)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        if digest(target) != manifest["sha256"]:
            raise ValueError("Snapshot changed while restoring")
        store = TaskStore(target)
        with store.connect() as db:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Restored database integrity failed")
            db.execute("BEGIN IMMEDIATE")
            if (
                db.execute("SELECT id FROM installation").fetchone()[0]
                != manifest["installationId"]
            ):
                raise ValueError("Installation identity mismatch")
            db.execute(
                "INSERT OR REPLACE INTO admission_guard VALUES (1,?)",
                ("restored_snapshot_requires_external_reconciliation",),
            )
        return store
    except BaseException:
        # Preserve failure evidence without leaving a normally openable target.
        target.rename(target.with_name(target.name + ".restore-failed-" + uuid.uuid4().hex))
        raise


def fingerprint_workspace(root, names):
    """Hash named files inside one real workspace. Missing files stay explicit."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir() or root.resolve() != root:
        raise ValueError('Workspace must be a real directory')
    if not isinstance(names, (list, tuple)) or not names:
        raise ValueError('Workspace fingerprint needs at least one relative path')
    result = {}
    for name in names:
        if (not isinstance(name, str) or not name or Path(name).is_absolute()
                or '..' in Path(name).parts):
            raise ValueError('Workspace fingerprint path must stay inside the workspace')
        path = root / name
        if path.is_symlink():
            raise ValueError('Workspace fingerprint cannot follow a symlink')
        if not path.exists():
            result[name] = None
            continue
        if not path.is_file() or path.resolve() != path or not path.is_relative_to(root):
            raise ValueError('Workspace fingerprint path must stay inside the workspace')
        result[name] = digest(path)
    return result


def reconcile_restored_workspace(restored_database, *, backup_fingerprint, current_fingerprint,
                                 execution_observation, native_triggers='unknown'):
    """Report backup-versus-workspace differences without reopening admission.

    This never writes the workspace, never starts execution, and never removes
    the restore guard. Unknown execution or unquiesced native triggers block
    new writes. A known code difference is reported and left in place.
    """
    if execution_observation not in ('observed', 'absent', 'unknown'):
        raise ValueError('Execution observation must be observed, absent, or unknown')
    if native_triggers not in ('stopped', 'unknown'):
        raise ValueError('Native trigger quiescence must be stopped or unknown')
    if not isinstance(backup_fingerprint, dict) or not isinstance(current_fingerprint, dict):
        raise ValueError('Workspace fingerprints must be objects')
    store = TaskStore(Path(restored_database))
    with store.connect() as db:
        row = db.execute('SELECT reason FROM admission_guard WHERE id=1').fetchone()
    if row is None or row['reason'] != 'restored_snapshot_requires_external_reconciliation':
        raise ValueError('Restored snapshot guard is missing')
    names = sorted(set(backup_fingerprint) | set(current_fingerprint))
    diff = [name for name in names if backup_fingerprint.get(name) != current_fingerprint.get(name)]
    unknown = execution_observation == 'unknown' or native_triggers != 'stopped'
    return {
        'admissionRemainsClosed': True,
        'continuationAttempted': False,
        'codeOverwritten': False,
        'newWritesBlocked': True if unknown or diff else False,
        'unknownBlocksWrites': unknown,
        'workspaceDiff': diff,
        'executionObservation': execution_observation,
        'nativeTriggers': native_triggers,
    }
