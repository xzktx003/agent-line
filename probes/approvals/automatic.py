#!/usr/bin/env python3
"""Real existing Sim graph through runtime background discovery, without manual bind."""
import json
from pathlib import Path
import secrets
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from fastapi.testclient import TestClient
from agent_line.adapters.sim import SimClient
from agent_line.approval_discovery import WorkflowRunLedger
from agent_line.approvals import ApprovalLedger
from agent_line.publication import PublicationVerifier
from agent_line.runtime import load_app
from agent_line.sim_journal import SimJournal
from agent_line.store import TaskStore
from probes.sim.workflow_probe import write_private


def main():
    directory = ROOT / 'var/approvals-automatic'
    directory.mkdir(mode=0o700, exist_ok=True)
    source = json.loads((ROOT / 'var/approvals-live/state.json').read_text())
    wid, revision = source['workflowId'], source['revision']
    auth_path = ROOT / 'var/sim/probe-auth.json'
    auth = json.loads(auth_path.read_text())
    sim = SimClient(auth['baseUrl'], auth['apiKey'])
    try:
        verifier = PublicationVerifier(sim)
        version = verifier._get(f'/api/v2/workflows/{wid}/versions/{revision}')
        human_ids = [key for key, value in version['state']['blocks'].items()
                     if value.get('type') == 'human_in_the_loop_v2']
        verifier.check_definition(wid, revision, version['id'], human_ids, require_active=True)
        service = directory / 'service.json'
        if not service.exists():
            # Explicit first-time installation; normal restarts never recreate ledgers.
            for name, cls in [('tasks', TaskStore), ('journal', SimJournal),
                              ('approvals', ApprovalLedger), ('runs', WorkflowRunLedger)]:
                path = directory / (name + '.sqlite')
                if not path.exists():
                    cls.initialize(path)
            write_private(directory / 'approval.json', {
                'schemaVersion': 1, 'ledgerPath': str(directory / 'approvals.sqlite'),
                'runLedgerPath': str(directory / 'runs.sqlite'), 'credentials': [{
                    'token': secrets.token_urlsafe(32), 'subject': 'automatic-probe-owner',
                    'projectScopes': ['automatic-validation'], 'capabilities': ['approve'],
                    'expiresAt': time.time() + 86400}]})
            write_private(service, {
                'schemaVersion': 1, 'database': str(directory / 'tasks.sqlite'),
                'tokens': {secrets.token_urlsafe(32): ['automatic-validation']},
                'projects': {'automatic-validation': {'workspace': str(directory)}},
                'bindings': {}, 'approvalFile': 'approval.json',
                'sim': {'authFile': str(auth_path), 'journal': 'journal.sqlite',
                        'installationId': 'automatic-approval-probe',
                        'bindings': {'automatic': {'projectId': 'automatic-validation',
                            'workflowId': wid, 'humanApproval': {'revision': revision,
                                'deploymentVersionId': version['id'], 'blockIds': human_ids,
                                'ttlSeconds': 86400}}}}})
        config = json.loads(service.read_text())
        credentials = json.loads((directory / 'approval.json').read_text())
        service_headers = {'Authorization': 'Bearer ' + next(iter(config['tokens']))}
        human_headers = {'Authorization': 'Bearer ' + credentials['credentials'][0]['token']}
        run_ledger = WorkflowRunLedger(directory / 'runs.sqlite')
        approval_ledger = ApprovalLedger(directory / 'approvals.sqlite')
        results = []
        for choice in ('approve', 'reject'):
            path = '/extension/v1/workflows/automatic/runs'
            with TestClient(load_app(service)) as http:
                started = http.post(path, json={'idempotencyKey': 'auto-' + choice, 'input': {}},
                                    headers=service_headers)
                if started.status_code != 202:
                    raise RuntimeError('Start failed: HTTP ' + str(started.status_code))
                run_id = started.json()['runId']
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    rows = approval_ledger.list_approvals({'automatic-validation'})['approvals']
                    rows = [row for row in rows if row['binding'].run_id == run_id]
                    if rows:
                        break
                    time.sleep(0.1)
                if len(rows) != 1:
                    raise RuntimeError('Background registration absent: ' +
                                       run_ledger.get(wid, run_id)['registrationStatus'])
                binding = rows[0]['binding']
            # New application, new Sim connection and worker; existing pause identity survives.
            with TestClient(load_app(service)) as http:
                repeated = http.post(path, json={'idempotencyKey': 'auto-' + choice, 'input': {}},
                                     headers=service_headers)
                assert repeated.status_code == 202 and repeated.json()['runId'] == run_id
                blocked = http.post(path + '/' + run_id + '/resume', headers=service_headers,
                                    json={'contextId': binding.context_id, 'input': {}})
                assert blocked.status_code == 403
                decision = http.post('/extension/v1/approvals/' + binding.approval_id + '/decision',
                                     headers=human_headers,
                                     json={'decisionId': 'automatic-' + choice, 'decision': choice})
                if decision.status_code != 200:
                    raise RuntimeError('Decision failed: HTTP ' + str(decision.status_code))
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    observed = sim.observe(wid, run_id)['response']['data']
                    if observed.get('status') == 'completed':
                        break
                    time.sleep(0.2)
                assert observed.get('status') == 'completed'
                expected = 'approved' if choice == 'approve' else 'rejected'
                assert observed.get('output', {}).get('data', {}).get('decision') == expected, observed.get('output')
                assert approval_ledger.get(binding.approval_id)['binding'] == binding
                results.append({'choice': choice, 'runId': run_id, 'approvalId': binding.approval_id,
                                'status': observed['status'], 'output': observed['output'],
                                'registration': 'background_only', 'restartIdentityPreserved': True,
                                'genericResumeBlocked': True})
        report = {'verified': True, 'workflowId': wid, 'runs': results}
        write_private(directory / 'report.json', report)
        print(json.dumps(report))
    finally:
        sim.close()


if __name__ == '__main__':
    main()
