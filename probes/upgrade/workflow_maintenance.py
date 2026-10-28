#!/usr/bin/env python3
"""Real Sim paused run: maintenance blocks starts/decisions without losing identity."""
import argparse
import copy
import json
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from fastapi.testclient import TestClient
from agent_line.approval_discovery import WorkflowRunLedger
from agent_line.approvals import ApprovalLedger
from agent_line.runtime import load_app
from agent_line.sim_journal import SimJournal
from agent_line.store import TaskStore
from probes.sim.workflow_probe import write_private


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--attempt', required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', args.attempt):
        raise ValueError('Invalid attempt')
    directory = ROOT / 'var/upgrade-maintenance' / args.attempt
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    source = ROOT / 'var/approvals-automatic'
    config = copy.deepcopy(json.loads((source / 'service.json').read_text()))
    approval = copy.deepcopy(json.loads((source / 'approval.json').read_text()))
    store = TaskStore.initialize(directory / 'tasks.sqlite')
    journal = SimJournal.initialize(directory / 'journal.sqlite')
    approvals = ApprovalLedger.initialize(directory / 'approvals.sqlite')
    runs = WorkflowRunLedger.initialize(directory / 'runs.sqlite')
    config['database'] = str(store.path)
    config['sim']['journal'] = str(journal.path)
    config['sim']['authFile'] = str(ROOT / 'var/sim/probe-auth.json')
    config['sim']['installationId'] = 'upgrade-maintenance-' + args.attempt
    config['approvalFile'] = 'approval.json'
    for project in config['projects'].values():
        project['workspace'] = str(directory)
    approval['ledgerPath'], approval['runLedgerPath'] = str(approvals.path), str(runs.path)
    write_private(directory / 'service.json', config)
    write_private(directory / 'approval.json', approval)
    project_id = config['sim']['bindings']['automatic']['projectId']
    workflow_id = config['sim']['bindings']['automatic']['workflowId']
    headers = {'Authorization': 'Bearer ' + next(iter(config['tokens']))}
    human = {'Authorization': 'Bearer ' + approval['credentials'][0]['token']}
    path = '/extension/v1/workflows/automatic/runs'
    request = {'idempotencyKey': 'existing', 'input': {}}
    guard_owner = None
    report = {'verified': False, 'scope': 'real_sim_maintenance_submission_boundary',
              'doesNotProveGlobalQuiescence': True}
    try:
        with TestClient(load_app(directory / 'service.json')) as http:
            started = http.post(path, headers=headers, json=request)
            assert started.status_code == 202
            run_id = started.json()['runId']
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                matches = [row for row in approvals.list_approvals({project_id})['approvals']
                           if row['binding'].run_id == run_id]
                if matches:
                    break
                time.sleep(.2)
            assert len(matches) == 1
            binding = matches[0]['binding']
            guard_owner = store.enter_maintenance()
            replayed = http.post(path, headers=headers, json=request)
            assert replayed.status_code == 202 and replayed.json()['runId'] == run_id
            with journal.connect() as db:
                operations_before = db.execute('SELECT COUNT(*) FROM operations').fetchone()[0]
            blocked = http.post(path, headers=headers,
                                json={'idempotencyKey': 'blocked', 'input': {}})
            assert blocked.status_code == 503
            decision_path = '/extension/v1/approvals/' + binding.approval_id + '/decision'
            decision_body = {'decisionId': 'maintenance-live-decision', 'decision': 'approve'}
            held = http.post(decision_path, headers=human, json=decision_body)
            assert held.status_code == 503
            assert approvals.get(binding.approval_id)['decision'] is None
            with journal.connect() as db:
                assert db.execute('SELECT COUNT(*) FROM operations').fetchone()[0] == operations_before
            assert len(runs.page(limit=10)) == 1
            detail = http.get('/extension/v1/approvals/' + binding.approval_id, headers=human)
            assert detail.status_code == 200
            store.leave_maintenance(guard_owner)
            guard_owner = None
            decided = http.post(decision_path, headers=human, json=decision_body)
            assert decided.status_code == 200
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                observed = http.get(path + '/' + run_id, headers=headers)
                assert observed.status_code == 200
                execution = observed.json()['execution']['response']['data']
                if execution['status'] == 'completed':
                    break
                time.sleep(.2)
            assert execution['status'] == 'completed'
            assert execution['output']['data']['decision'] == 'approved'
            report.update(verified=True, runId=run_id, approvalId=binding.approval_id,
                          blockedStartStatus=blocked.status_code, blockedDecisionStatus=held.status_code,
                          maintenanceDecisionUnwritten=True, noNewIntentWhileGuarded=True,
                          existingReceiptReplayed=True, finalStatus=execution['status'])
            write_private(directory / 'report.json', report)
            print(json.dumps(report))
    except BaseException as error:
        report['failureType'] = type(error).__name__
        write_private(directory / 'report.json', report)
        raise
    finally:
        if guard_owner is not None:
            store.leave_maintenance(guard_owner)


if __name__ == '__main__':
    main()
