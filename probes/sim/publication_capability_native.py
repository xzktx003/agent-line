#!/usr/bin/env python3
"""One owned native Agent draft; publication must fail without assembled tools.

First run requires --execute. Any existing state is observation only, even with
--execute: never replay a mutation or replace an uncertain operation identity.
No verifier, execution pipeline, model call or upstream source change is enabled.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import time
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from fastapi.testclient import TestClient

from agent_line.adapters.sim import SimClient
from agent_line.runtime import load_app
from agent_line.sim_journal import SimJournal
from agent_line.store import TaskStore
from probes.sim.authoring_probe import write

DATA = ROOT / 'var/publication-capability-native'
PROJECT = 'publication-capability-native'
KIND = 'publication-capability-native-v1'


class ProbeFailure(RuntimeError):
    pass


def require(condition, code):
    if not condition:
        raise ProbeFailure(code)


def private_json(path):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and info.st_mode & 0o077 == 0 and info.st_nlink == 1, 'private_file_required')
    return json.loads(path.read_text())


def document():
    def node(identifier, kind, config):
        return {'id': identifier, 'type': kind, 'name': identifier,
                'position': {'x': 0, 'y': 0}, 'config': config}
    return {'schemaVersion': 1, 'variables': {}, 'nodes': [
        node('start', 'start', {}), node('agent', 'agent', {
            'roleBindingId': 'existing-role', 'instruction': '仅保存草稿，本探针不执行任务',
            'inputFrom': {'nodeId': 'start', 'field': 'input'}, 'memoryPolicyId': 'native-role',
            'outputSchemaId': 'task-result', 'requireMemoryCommit': False, 'allowFailureBranch': False}),
        node('end', 'end', {'output': {'result': 'draft-only'}})],
        'edges': [{'id': 'a', 'source': 'start', 'target': 'agent'},
                  {'id': 'b', 'source': 'agent', 'target': 'end'}]}


def run_probe(*, execute=False, directory=DATA, auth_path=None, sim_factory=SimClient,
              role_agent_id='agent-line-native-loop-parent'):
    directory = Path(directory)
    auth_path = Path(auth_path or ROOT / 'var/sim/probe-auth.json')
    if not directory.exists() and not directory.is_symlink() and not execute:
        return {'status': 'not_executed', 'readiness': 'unknown', 'nativeWritesThisInvocation': 0}
    fresh = not directory.exists() and not directory.is_symlink()
    calls, sessions = [], []
    state = None
    report = {'status': 'incomplete', 'readiness': 'unknown', 'nativeWritesThisInvocation': 0}
    try:
        if fresh:
            directory.mkdir(mode=0o700)
            state = {'kind': KIND, 'projectId': PROJECT, 'steps': {}, 'initialization': 'intent'}
            write(directory / 'state.json', state)
        info = directory.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
                and info.st_mode & 0o077 == 0, 'private_directory_required')
        if not fresh:
            state = private_json(directory / 'state.json')
        require(state.get('kind') == KIND and state.get('projectId') == PROJECT
                and isinstance(state.get('steps'), dict), 'probe_identity_changed')
        auth = private_json(auth_path)
        if fresh:
            (directory / 'workspace').mkdir(mode=0o700)
            TaskStore.initialize(directory / 'tasks.sqlite')
            SimJournal.initialize(directory / 'sim.sqlite')
            human = secrets.token_urlsafe(40)
            write(directory / 'authoring.json', {'schemaVersion': 1, 'credentials': [{
                'token': human, 'subject': PROJECT, 'projectScopes': [PROJECT],
                'capabilities': ['edit_workflows', 'publish_workflows'], 'expiresAt': time.time() + 604800}],
                'workspaces': {PROJECT: auth['workspaceId']}})
            write(directory / 'service.json', {'schemaVersion': 1, 'database': 'tasks.sqlite',
                'tokens': {secrets.token_urlsafe(40): [PROJECT]},
                'projects': {PROJECT: {'workspace': str(directory / 'workspace'), 'name': '发布能力拒绝验收'}},
                'bindings': {'existing-role': {'projectId': PROJECT, 'roleAgentId': role_agent_id}},
                'authoringFile': 'authoring.json',
                'sim': {'authFile': str(auth_path.resolve()), 'journal': 'sim.sqlite',
                        'installationId': PROJECT, 'bindings': {}}})
            state.update(initialization='ready', workspaceId=auth['workspaceId'],
                         serviceSha256=hashlib.sha256((directory / 'service.json').read_bytes()).hexdigest(),
                         origin=auth['baseUrl'])
            write(directory / 'state.json', state)
        require(state.get('initialization') == 'ready', 'initialization_requires_reconciliation')
        require(state.get('workspaceId') == auth['workspaceId'] and state.get('origin') == auth['baseUrl'],
                'native_workspace_or_origin_changed')
        require(hashlib.sha256((directory / 'service.json').read_bytes()).hexdigest()
                == state.get('serviceSha256'), 'service_configuration_changed')
        credential = private_json(directory / 'authoring.json')['credentials'][0]['token']
        base = '/extension/v1/authoring/projects/' + PROJECT
        headers = {'Authorization': 'Bearer ' + credential}

        def event(request):
            method, path = request.method, request.url.path
            calls.append({'method': method, 'path': path})
            if method != 'GET':
                create = state['steps'].get('create', {}).get('intent')
                save = state['steps'].get('save', {}).get('intent')
                allowed = (method == 'POST' and path == '/api/v2/workflows' and create
                           or method == 'PUT' and state.get('workflowId')
                           and path == '/api/v2/workflows/' + state['workflowId'] + '/state' and save)
                require(fresh and allowed, 'forbidden_native_mutation')

        def factory(*args, **kwargs):
            sim = sim_factory(*args, **kwargs)
            sessions.append(sim)
            sim._client.event_hooks['request'].append(event)
            return sim

        def get(http, path):
            response = http.get(path, headers=headers)
            require(response.status_code == 200, 'read_failed')
            return response.json()

        def submit(http, kind, path, body):
            require(fresh and kind not in state['steps'], 'prior_intent_must_not_replay')
            state['steps'][kind] = {'intent': True, 'path': path, 'body': body}
            write(directory / 'state.json', state)
            response = http.post(path, headers=headers, json=body)
            require(response.status_code == 202, 'write_receipt_failed')
            receipt = response.json()
            state['steps'][kind]['response'] = receipt
            write(directory / 'state.json', state)
            require(receipt.get('status') == 'accepted' and isinstance(receipt.get('receipt'), dict),
                    'operation_requires_reconciliation')
            return receipt

        with patch('agent_line.adapters.sim.SimClient', factory):
            with TestClient(load_app(directory / 'service.json')) as http:
                get(http, '/extension/v1/authoring/projects')
                if fresh:
                    created = submit(http, 'create', base + '/workflows', {
                        'operationId': 'create-' + uuid.uuid4().hex,
                        'name': 'agent-line-publication-capability-native',
                        'description': '独占草稿：缺少验证与执行能力时禁止部署，无模型调用'})
                    require(created['receipt'].get('outcome') == 'created', 'create_not_confirmed')
                    state['workflowId'] = created['receipt']['workflowId']
                    write(directory / 'state.json', state)
                    path = base + '/workflows/' + state['workflowId']
                    detail = get(http, path)
                    saved = submit(http, 'save', path + '/save', {
                        'operationId': 'save-' + uuid.uuid4().hex,
                        'expectedStateSha256': detail['stateSha256'], 'document': document()})
                    require(saved['receipt'].get('outcome') == 'saved', 'save_not_confirmed')
                    detail = get(http, path)
                    submit(http, 'publish', path + '/publish', {
                        'operationId': 'publish-' + uuid.uuid4().hex,
                        'expectedStateSha256': detail['stateSha256']})
                observations = {}
                for kind, step in state['steps'].items():
                    observations[kind] = get(http, base + '/operations/' + step['body']['operationId'] + '?kind=' + kind)
                # Observe original create identity after an interrupted local state write.
                created = observations.get('create', {}).get('receipt') or {}
                workflow = state.get('workflowId') or created.get('workflowId')
                detail = get(http, base + '/workflows/' + workflow) if workflow else None
                published = observations.get('publish', {}).get('receipt') or {}
                complete = (set(observations) == {'create', 'save', 'publish'}
                            and all(item.get('status') == 'accepted' for item in observations.values())
                            and published.get('outcome') == 'verification_required'
                            and detail is not None and detail.get('isDeployed') is False
                            and detail.get('document') == document())
                # The HTTP detail reads Sim get_detail; there is no local deployment flag.
                report.update(status='passed_live' if complete else 'incomplete', workflowId=workflow,
                              observations=observations, detail=detail,
                              scope='real load_app HTTP and Sim draft; no execution/verifier pipeline')
        require(all(sim._client.is_closed for sim in sessions), 'transport_not_closed')
    except Exception as error:
        report.update(status='incomplete', code=str(error) if isinstance(error, ProbeFailure) else 'probe_check_failed',
                      errorType=type(error).__name__)
    finally:
        for sim in sessions:
            sim.close()
    report.update(nativeCalls=calls, nativeWritesThisInvocation=sum(call['method'] != 'GET' for call in calls),
                  deployExecuteResumeRequests=sum(any(part in call['path'].split('/')
                    for part in ('deploy', 'execute', 'resume')) for call in calls),
                  allTransportsClosed=all(sim._client.is_closed for sim in sessions), observationOnly=not fresh)
    if state is not None:
        write(directory / 'report.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    report = run_probe(execute=args.execute)
    print(json.dumps({key: report[key] for key in ('status', 'readiness', 'nativeWritesThisInvocation')}))
    return 0 if report['status'] in ('not_executed', 'passed_live') else 1


if __name__ == '__main__':
    raise SystemExit(main())
