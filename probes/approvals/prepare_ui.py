#!/usr/bin/env python3
"""Trusted test setup: create two new real pauses; never submit decisions."""
import json
from pathlib import Path
import sys
import time
import uuid
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from probes.sim.workflow_probe import Probe
from agent_line.approvals import ApprovalBinding, ApprovalLedger


def main():
    directory=ROOT/'var/approvals-live'
    source=json.loads((directory/'state.json').read_text())
    wid=source['workflowId'];revision=source['revision']
    probe=Probe(ROOT/'var/sim/probe-auth.json',directory/'ui-state.json')
    if 'expiresAt' not in probe.state:
        probe.state.update(expiresAt=time.time()+86400,workflowId=wid,revision=revision,
                           setup='trusted_test_setup_no_decision',projectId='local-validation')
        probe.save()
    if probe.state['workflowId']!=wid or probe.state['revision']!=revision:
        raise RuntimeError('Published fixture identity changed')
    ledger=ApprovalLedger(ROOT/'var/adapter/approvals.sqlite')
    ids=[]
    try:
        for choice in ('approve','reject'):
            run=probe.state['runs'].setdefault(choice,{'workflowId':wid,'runId':str(uuid.uuid4()),'approvalId':'ui-'+choice+'-'+uuid.uuid4().hex[:12]})
            probe.save()
            probe.once(choice+'-execute','POST','/api/v2/workflows/'+wid+'/execute',{'input':{}},{'X-Run-Id':run['runId']})
            observed=probe.observe(choice)
            if 'binding' not in run:
                pause=observed.get('paused',{})
                if (observed.get('status')!='paused' or pause.get('pauseKind')!='human'
                        or pause.get('pausePointCount')!=1):raise RuntimeError('Exact real pause unavailable')
                run['binding']={'approval_id':run['approvalId'],'project_id':'local-validation','workflow_id':wid,
                    'run_id':run['runId'],'block_id':pause['blockedOnBlockId'],'context_id':pause['contextId'],
                    'revision':revision,'activation_id':run['runId'],'attempt_id':'one','purpose':'human_approval',
                    'expires_at':probe.state['expiresAt']}
                probe.save()
            ledger.bind(ApprovalBinding(**run['binding']))
            ids.append({'choice':choice,'approvalId':run['approvalId'],'runId':run['runId'],'observedStatus':observed['status']})
        print(json.dumps({'setup':'trusted_test_setup_no_decision','workflowId':wid,'revision':revision,'approvals':ids}))
    finally:probe.client.close()


if __name__=='__main__':main()
