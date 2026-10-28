"""Load explicit human approval authority, separate from execution credentials."""
import math
from pathlib import Path

from .approvals import ApprovalCoordinator, ApprovalLedger, TrustedPrincipal
from .execution_runtime import _json, _path


def load_approval_api(path, *, projects, service_tokens, client, journal):
    # Reuse canonical-path/owner-only checks used by execution deployment.
    config = _json(_path(str(Path(path).absolute()), private=True))
    if (not isinstance(config, dict)
            or not {'schemaVersion', 'ledgerPath', 'credentials'} <= set(config)
            <= {'schemaVersion', 'ledgerPath', 'credentials', 'runLedgerPath'}
            or type(config['schemaVersion']) is not int or config['schemaVersion'] != 1
            or not isinstance(config['credentials'], list) or not config['credentials']):
        raise ValueError('Invalid approval deployment configuration')
    if client is None or journal is None:
        raise ValueError('Approval deployment requires a configured Sim connection')
    ledger_path = _path(config['ledgerPath'], private=True)
    credentials = {}
    subjects = set()
    for entry in config['credentials']:
        if (not isinstance(entry, dict) or set(entry) != {
                'token', 'subject', 'projectScopes', 'capabilities', 'expiresAt'}):
            raise ValueError('Invalid approval credential fields')
        token, subject = entry['token'], entry['subject']
        if (not isinstance(token, str) or len(token) < 32
                or any(c.isspace() for c in token) or token in credentials
                or token in service_tokens):
            raise ValueError('Approval credentials must be unique and separate from service tokens')
        if (not isinstance(subject, str) or not subject.strip()
                or len(subject) > 256 or subject in subjects):
            raise ValueError('Approval subjects must be nonempty and unique')
        scopes, capabilities = entry['projectScopes'], entry['capabilities']
        if (not isinstance(scopes, list) or not scopes
                or any(not isinstance(s, str) or s not in projects for s in scopes)
                or len(scopes) != len(set(scopes))
                or capabilities != ['approve']):
            raise ValueError('Approval scopes or capabilities are invalid')
        expiry = entry['expiresAt']
        if type(expiry) not in (int, float) or not math.isfinite(expiry) or expiry <= 0:
            raise ValueError('Approval credentials require finite expiration')
        credentials[token] = TrustedPrincipal(subject, frozenset(scopes), frozenset(capabilities), expiry)
        subjects.add(subject)
    # Existing storage only: restarting with a lost ledger must not erase decisions.
    ledger = ApprovalLedger(ledger_path)
    from .approval_api import ApprovalAPI, ApprovalAccess
    access = ApprovalAccess(credentials)
    coordinator = ApprovalCoordinator(ledger, client, journal, authorize=access.authorize)
    discovery = verifier = None
    if 'runLedgerPath' in config:
        from .approval_discovery import ApprovalDiscovery, WorkflowRunLedger
        from .publication import PublicationVerifier
        runs = WorkflowRunLedger(_path(config['runLedgerPath'], private=True))
        verifier = PublicationVerifier(client)
        discovery = ApprovalDiscovery(runs, ledger, client, publication_verifier=verifier)
    return ApprovalAPI(access, coordinator, discovery=discovery, publication_verifier=verifier)


class ApprovalDiscoveryWorker:
    # Own polling separately from API requests; drain before transport close.
    def __init__(self, discovery, *, interval=1.0):
        import threading
        self.discovery = discovery
        self.interval = interval
        self.stop_event = threading.Event()
        self.thread = None
        self.last_report = []

    def start(self):
        import threading
        if self.thread is not None:
            raise RuntimeError('Approval discovery worker may only start once')
        self.thread = threading.Thread(
            target=self.discovery.run, args=(self.stop_event,),
            kwargs={'interval': self.interval, 'report': self._report},
            name='agent-line-approval-discovery', daemon=True)
        self.thread.start()

    def _report(self, report):
        self.last_report = report

    def stop(self, timeout=35):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout)
            if self.thread.is_alive():
                raise RuntimeError('Approval discovery still owns its transport')

