"""One-shot live Sim -> Codex -> verifier -> Sim relay proof.

A persisted state permits observation only on subsequent invocations. No upstream
code edits, notification tools, human decisions, or task admission changes.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
import uuid

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from probes.sim.workflow_probe import Probe, block, edge, response, write_private
from agent_line.adapters.sim import SimClient
from agent_line.adapters.qwenpaw import QwenPawExecutionGateway
from agent_line.store import TaskStore
from agent_line.dispatcher import Dispatcher
from agent_line.verification import ArtifactPolicy, verify_artifacts
from agent_line.completion_relay import ResumeBinding, CompletionRelay
from agent_line.pause_bindings import PauseBindings
from agent_line.sim_journal import SimJournal

STATE = ROOT / 'var/full-workflow/state.json'


def observe(state, sim):
    result = sim.observe(state['workflowId'], state['runId'])
    state['latestSimObservation'] = result
    if state.get('taskId'):
        task = TaskStore(ROOT / 'var/adapter/tasks.sqlite').get(state['taskId'])
        state['latestTaskObservation'] = {key: task[key] for key in (
            'executionStatus', 'verificationStatus', 'memoryStatus', 'executionIdentity', 'resourcesReleased')}
    write_private(STATE, state)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['run', 'observe'])
    args = parser.parse_args()
    settings = json.loads((ROOT / 'var/sim/probe-auth.json').read_text())
    sim = SimClient(settings['baseUrl'], settings['apiKey'])
    if STATE.exists():
        state = json.loads(STATE.read_text())
        if not state.get('workflowId'):
            raise RuntimeError('Existing setup intent requires manual reconciliation; no recreation')
        observed = observe(state, sim)
        print(json.dumps({'mode': 'observation_only', 'simStatus': observed['status'], 'evidenceFile': str(STATE)}))
        return
    if args.action != 'run':
        raise RuntimeError('No existing probe identity')
    STATE.parent.mkdir(parents=True, exist_ok=True)
    suffix = uuid.uuid4().hex[:12]
    state = {'purpose': 'external_task_result', 'requireMemoryCommit': False,
             'runId': str(uuid.uuid4()), 'blockId': 'external_wait_' + suffix,
             'artifact': 'full-workflow-' + suffix + '.txt', 'createdAt': time.time()}
    write_private(STATE, state)
    probe = Probe(ROOT / 'var/sim/probe-auth.json', STATE.parent / 'graph-operations.json')
    created = probe.once('create', 'POST', '/api/v2/workflows', {
        'workspaceId': settings['workspaceId'], 'name': 'Agent Line external task result ' + suffix,
        'description': 'External machine task-result wait, NOT human approval. Trusted completion relay only. No notifications. Memory commit excluded.'})
    state['workflowId'] = created['id']; write_private(STATE, state)
    start = block('external_start_' + suffix, 'start_trigger')
    wait = block(state['blockId'], 'human_in_the_loop_v2', {
        'inputFormat': ('input-format', [{'id': k, 'name': k, 'type': 'string', 'value': ''}
            for k in ('taskId', 'executionStatus', 'verificationStatus', 'memoryStatus')]),
        'notification': ('tool-input', []), 'builderData': ('response-format', [])}, 300)
    done = response('external_done_' + suffix,
        {k: '<' + state['blockId'] + '.resumeInput.' + k + '>'
         for k in ('taskId', 'executionStatus', 'verificationStatus', 'memoryStatus')})
    graph = {'blocks': {b['id']: b for b in (start, wait, done)},
             'edges': [edge(start['id'], wait['id']), edge(wait['id'], done['id'])],
             'loops': {}, 'parallels': {}, 'variables': {}}
    state['trustedGraph'] = graph; write_private(STATE, state)
    base = '/api/v2/workflows/' + state['workflowId']
    written = probe.once('state', 'PUT', base + '/state', graph)
    if written.get('warnings'): raise RuntimeError('Graph warnings require review')
    probe.once('deploy', 'POST', base + '/deploy', {})
    journal = SimJournal.initialize(STATE.parent / 'sim-journal.sqlite')
    bindings = PauseBindings.initialize(STATE.parent / 'pause-bindings.sqlite')
    journal.execute(sim, state['workflowId'], state['runId'], {})
    for _ in range(40):
        observed = observe(state, sim)
        run = observed.get('response', {}).get('data', {})
        if run.get('status') == 'paused': break
        time.sleep(1)
    else: raise RuntimeError('Original run pause not confirmed; observation only on rerun')
    pause = run['paused']
    if pause.get('blockedOnBlockId') != state['blockId'] or pause.get('pausePointCount') != 1:
        raise RuntimeError('Pause identity mismatch')
    state['contextId'] = pause['contextId']; state['pausedObservation'] = run
    write_private(STATE, state)
    workspace = ROOT / 'var/qwenpaw/workspaces/agent-line-g0-codex'
    expected = ('AGENT_LINE_FULL_WORKFLOW_' + suffix + '\n').encode()
    if (workspace / state['artifact']).exists(): raise RuntimeError('Artifact already exists; never overwrite')
    store = TaskStore(ROOT / 'var/adapter/tasks.sqlite')
    payload = {'projectId': 'local-validation', 'roleAgentId': 'agent-line-g0-parent',
               'roleBindingId': 'g0-full-workflow-probe', 'workspace': str(workspace),
               'workflow': {'id': state['workflowId'], 'revision': '1', 'runId': state['runId'],
                            'nodeId': state['blockId'], 'nodeExecutionId': state['contextId'], 'attemptId': '1'},
               'input': {'instruction': f"Create ONLY {state['artifact']} in your assigned working directory with exact content {expected.decode().strip()} followed by one newline. Verify it using a shell command. Do not read other files or use network tools."}}
    task, _ = store.register('full-workflow-' + suffix, payload)
    state['taskId'] = task['id']; write_private(STATE, state)
    bindings.bind(ResumeBinding(task['id'], state['workflowId'], state['runId'], state['blockId'],
                               state['contextId'], 'external_task_result', require_memory_commit=False))
    process = json.loads((ROOT / 'var/qwenpaw/agent-line-process.json').read_text())
    qwenbase = f'http://127.0.0.1:{process["port"]}'
    with httpx.Client(timeout=30) as client:
        login = client.post(qwenbase + '/api/auth/login', json=json.loads((ROOT / 'var/qwenpaw-secrets/local-login.json').read_text()))
        login.raise_for_status(); login_data = login.json()
    gateway = QwenPawExecutionGateway(qwenbase, login_data.get('token') or login_data.get('access_token'),
                                     (ROOT / 'var/qwenpaw-secrets/execution-service-token').read_text().strip())
    dispatcher = Dispatcher(store, {'local-validation': gateway})
    try:
        dispatcher.submit(task['id'])
        for _ in range(150):
            current = dispatcher.reconcile(task['id'])
            if current['executionStatus'] in ('succeeded', 'failed', 'canceled') and current['resourcesReleased']: break
            time.sleep(2)
        else: raise RuntimeError('Native execution unresolved; do not resubmit')
        if current['executionStatus'] != 'succeeded': raise RuntimeError('Native execution did not succeed')
        artifacts = STATE.parent / 'artifacts'; artifacts.mkdir(exist_ok=True)
        receipt = verify_artifacts(store, task['id'], workspace=workspace,
            policies=[ArtifactPolicy(state['artifact'], hashlib.sha256(expected).hexdigest(), len(expected))], evidence_dir=artifacts)
        store.record_verification(task['id'], receipt)
        state['verificationReceipt'] = receipt; write_private(STATE, state)
        if receipt['status'] != 'passed': raise RuntimeError('Artifact policy failed; no resume')
        state['relayReceipt'] = CompletionRelay(sim, journal).deliver(store.get(task['id']), bindings.get(task['id']))
        write_private(STATE, state)
        for _ in range(40):
            observed = observe(state, sim)
            if observed['status'] == 'completed': break
            time.sleep(1)
        else: raise RuntimeError('Original Sim completion not confirmed; do not resume again')
        result = observed['response']['data']
        expected_result = {'taskId': task['id'], 'executionStatus': 'succeeded',
                           'verificationStatus': 'passed', 'memoryStatus': store.get(task['id'])['memoryStatus']}
        if result.get('output', {}).get('data') != expected_result:
            raise RuntimeError('Sim completed output does not match verified task correlation')
        state['result'] = 'passed_live'; state['limitations'] = ['No memory commit full loop', 'G0 native role isolation only; not production sandbox proof', 'No generic workflow authoring or automatic node enrollment proof']
        write_private(STATE, state)
        print(json.dumps({'status': 'passed_live', 'taskId': task['id'], 'runId': state['runId'], 'evidenceFile': str(STATE)}))
    finally:
        gateway.close(); sim.close()


if __name__ == '__main__':
    try: main()
    except Exception as exc:
        print(json.dumps({'status': 'stopped', 'errorType': type(exc).__name__, 'reason': str(exc) if isinstance(exc, RuntimeError) else 'Inspect private evidence; credentials suppressed'}))
        sys.exit(1)
