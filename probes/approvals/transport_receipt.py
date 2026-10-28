#!/usr/bin/env python3
"""New runs on the immutable approval fixture; never touch old uncertain intents."""
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from probes.approvals.live import main as live
from probes.sim.workflow_probe import write_private
from agent_line.sim_journal import SimJournal


def main():
    directory=ROOT/'var/approvals-transport-v2'
    directory.mkdir(mode=0o700,exist_ok=True)
    path=directory/'state.json'
    if not path.exists():
        old=json.loads((ROOT/'var/approvals-live/state.json').read_text())
        state={key:old[key] for key in ('workspaceId','prefix','workflowId','revision','expiresAt')}
        state.update(runs={},steps={key:old['steps'][key] for key in ('create','state','deploy')},
                     purpose='fresh_runs_verify_resume_child_receipt')
        write_private(path,state)
    live(directory)
    state=json.loads(path.read_text());report=json.loads((directory/'report.json').read_text())
    journal=SimJournal(directory/'journal.sqlite')
    receipts=[]
    for choice,run in state['runs'].items():
        b=run['binding'];entry=journal.get(b['workflow_id'],b['run_id'],b['context_id'])
        receipt=entry['receipt']
        if (entry['status']!='accepted' or receipt['runId']!=run['runId']
                or receipt['contextId']!=b['context_id'] or receipt['workflowId']!=state['workflowId']
                or not receipt.get('resumedRunId') or receipt['resumedRunId']==run['runId']):
            raise RuntimeError('Transport failed to preserve parent/child identities')
        receipts.append({'choice':choice,'parentRunId':receipt['runId'],
                         'resumedRunId':receipt['resumedRunId'],'contextId':receipt['contextId'],
                         'deliveryStatus':entry['status']})
    write_private(directory/'transport-report.json',{'status':'passed_live','receipts':receipts,
                    'resumeCallsThisInvocation':report['totalResumeCallsThisInvocation'],
                    'scope':'fresh runs only; old uncertain journals untouched'})
    print(json.dumps({'transportReceipt':'passed_live','parentChildPairs':receipts}))


if __name__=='__main__':main()
