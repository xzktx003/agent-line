"""Wire machine-pause discovery and result return into the existing service."""
from dataclasses import dataclass
from pathlib import Path
import sqlite3

from .adapters.sim_wait import SimWaitClient
from .approval_discovery import WorkflowRunLedger
from .completion_relay import CompletionRelay
from .completion_worker import CompletionLoop, CompletionWorker
from .execution_runtime import _json, _path
from .pause_bindings import PauseBindings
from .publication import PublicationVerifier
from .task_discovery import ExternalTaskDiscovery


@dataclass
class TaskFlowRuntime:
    runs: WorkflowRunLedger
    pauses: PauseBindings
    discovery: ExternalTaskDiscovery
    workers: tuple


def load_task_flow(path, *, tasks, projects, role_bindings, client, journal):
    config = _json(_path(str(Path(path).absolute()), private=True))
    if (not isinstance(config, dict) or set(config) != {'schemaVersion', 'runLedgerPath', 'pauseBindingsPath'}
            or type(config['schemaVersion']) is not int or config['schemaVersion'] != 1):
        raise ValueError('Invalid machine-task flow configuration')
    runs = WorkflowRunLedger(_path(config['runLedgerPath'], private=True))
    pauses = PauseBindings(_path(config['pauseBindingsPath'], private=True))
    if runs.path == pauses.path:
        raise ValueError('Run ledger and pause bindings must be distinct existing stores')
    try:
        with runs.connect() as db:
            db.execute('SELECT seq,workflow,run,snapshot,submission,complete,registration_status,registration_reason FROM runs LIMIT 0')
        with pauses.connect() as db:
            db.execute('SELECT task_id,workflow_id,run_id,context_id,payload FROM bindings LIMIT 0')
    except sqlite3.Error:
        raise ValueError('Machine-task storage does not match its declared type') from None
    discovery = ExternalTaskDiscovery(runs, tasks, pauses, client, SimWaitClient(client),
                                      publication_verifier=PublicationVerifier(client), projects=projects,
                                      role_bindings=role_bindings, journal=journal)
    relay = CompletionRelay(client, journal)
    workers = (CompletionWorker(discovery, name='agent-line-task-discovery'),
               CompletionWorker(CompletionLoop(tasks, pauses, relay)))
    return TaskFlowRuntime(runs, pauses, discovery, workers)
