#!/usr/bin/env python3
"""Temporary read-only UI fixture using existing, acknowledged native receipts.

prepare imports exactly one historical probe's acknowledged identities into a
separate journal; it never submits execute/resume. serve is an ordinary guarded
runtime with no execution, authoring, approval or task-flow configuration.
"""
import hashlib
import json
from pathlib import Path
import secrets
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agent_line.adapters.sim import SimClient
from agent_line.runtime import load_app
from agent_line.sim_journal import SimJournal
from agent_line.store import TaskStore, canonical
from probes.sim.authoring_probe import write

DATA = ROOT / 'var/resumption-browser'


def prepare():
    DATA.mkdir(mode=0o700, exist_ok=True)
    manifest = DATA / 'receipt-import.json'
    if manifest.exists():
        raise RuntimeError('Existing fixture must be observed, never reinitialized')
    source = ROOT / 'var/sim-agent-result/state.json'
    source_bytes = source.read_bytes()
    state = json.loads(source_bytes)
    workflow = state['workflowId']
    execute = state['steps']['missing_status-execute']
    resume = state['steps']['missing_status-resume']
    if execute['status'] != 'received' or resume['status'] != 'received':
        raise RuntimeError('Acknowledged native receipts required')
    run = execute['result']['runId']
    child = resume['result']['resumedRunId']
    if (resume['result']['workflowId'] != workflow or resume['result']['runId'] != run
            or execute['result']['workflowId'] != workflow):
        raise RuntimeError('Historical receipt identity mismatch')
    auth = json.loads((ROOT / 'var/sim/probe-auth.json').read_text())
    sim = SimClient(auth['baseUrl'], auth['apiKey'])
    calls = []
    sim._client.event_hooks['request'].append(lambda request: calls.append(request.method))
    try:
        parent_state = sim.observe(workflow, run)
        child_state = sim.observe(workflow, child)
    finally:
        sim.close()
    if (parent_state['status'] != 'paused' or child_state['status'] != 'failed'
            or any(method != 'GET' for method in calls)):
        raise RuntimeError('Historical native failure not confirmed')
    write(manifest, {'status': 'import_intent', 'source': str(source),
                     'sourceSha256': hashlib.sha256(source_bytes).hexdigest(),
                     'workflowId': workflow, 'runId': run, 'childRunId': child,
                     'nativeWrites': 0, 'scope': 'Historical acknowledged receipt import, not a new submission'})
    tasks = TaskStore.initialize(DATA / 'tasks.sqlite')
    journal = SimJournal.initialize(DATA / 'sim.sqlite')
    with journal.connect() as db:
        for offset, step in enumerate((execute, resume)):
            receipt = step['result']
            db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,?)',
                       (workflow, run, receipt['contextId'] or '', canonical(step['payload']['input']),
                        'accepted', canonical(receipt), time.time() + offset))
    token = secrets.token_urlsafe(40)
    write(DATA / 'sim-auth.json', {'baseUrl': auth['baseUrl'], 'apiKey': auth['apiKey']})
    config = {'schemaVersion': 1, 'database': str(tasks.path), 'tokens': {token: ['probe']},
              'projects': {'probe': {'workspace': str(DATA)}}, 'bindings': {},
              'sim': {'authFile': 'sim-auth.json', 'journal': 'sim.sqlite',
                      'installationId': 'read-only-resumption-ui-probe',
                      'bindings': {'historical-failure': {'projectId': 'probe', 'workflowId': workflow}}}}
    write(DATA / 'service.json', config)
    write(DATA / 'browser.json', {'baseUrl': 'http://127.0.0.1:3196',
                                 'serviceFile': str(DATA / 'service.json'),
                                 'bindingId': 'historical-failure', 'runId': run})
    receipt_import = json.loads(manifest.read_text())
    receipt_import.update(status='imported_acknowledged_history', nativeCalls=calls)
    write(manifest, receipt_import)
    print(json.dumps({'status': 'prepared', 'nativeWrites': 0, 'parentStatus': 'paused', 'childStatus': 'failed'}))


if __name__ == '__main__':
    if sys.argv[1:] == ['prepare']:
        prepare()
    elif sys.argv[1:] == ['serve']:
        import uvicorn
        uvicorn.run(load_app(DATA / 'service.json'), host='127.0.0.1', port=3196, access_log=False)
    else:
        raise SystemExit('Use prepare or serve')
