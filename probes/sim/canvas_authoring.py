"""Persist the browser-authored graph via current AuthoringAPI and real Sim.

Dedicated journal/workflow; no publication, model execution or main reload.
Repeat observes original operation identities and never retransmits mutations.
"""
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import time
from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
from agent_line.adapters.sim import SimClient
from agent_line.approvals import TrustedPrincipal
from agent_line.authoring_api import AuthoringAPI, AuthoringAccess
from agent_line.sim_authoring import SimAuthoringClient
from agent_line.sim_journal import SimJournal
from probes.qwenpaw_memory.native_roundtrip import save

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'var/canvas-authoring-20261022'


def main():
    original_intent = OUT / 'save-intent.json'
    document = (json.loads(original_intent.read_text())['document'] if original_intent.exists()
                else json.loads((OUT / 'document-1440.json').read_text()))
    service_path = ROOT / 'var/adapter/service.json'
    service = json.loads(service_path.read_text())
    native_auth = json.loads((service_path.parent / service['sim']['authFile']).read_text())
    authoring = json.loads((service_path.parent / service['authoringFile']).read_text())
    main_journal = (service_path.parent / service['sim']['journal']).resolve()

    def main_snapshot():
        with sqlite3.connect(main_journal.as_uri() + '?mode=ro', uri=True) as db:
            rows = db.execute('SELECT * FROM operations ORDER BY workflow,run,context').fetchall()
        return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()

    before = main_snapshot()
    calls = []

    def permitted(request):
        path = request.url.path
        assert path.startswith('/api/v2/workflows')
        assert request.method == 'GET' or (request.method == 'POST' and path == '/api/v2/workflows') or (
            request.method == 'PUT' and path.endswith('/state'))
        calls.append(request.method)

    journal_path = OUT / 'authoring.sqlite'
    journal = SimJournal(journal_path) if journal_path.exists() else SimJournal.initialize(journal_path)
    project = 'canvas-proof'
    token = secrets.token_urlsafe(48)
    access = AuthoringAccess({token: TrustedPrincipal('canvas-proof', frozenset({project}),
                                frozenset({'edit_workflows'}), time.time()+600)})
    with httpx.Client(timeout=30, event_hooks={'request': [permitted]}) as native_http:
        native = SimAuthoringClient(SimClient(native_auth['baseUrl'], native_auth['apiKey'], client=native_http),
                                    next(iter(authoring['workspaces'].values())))
        api = AuthoringAPI(access, journal, {project: native}, projects={project: {'workspace': str(OUT)}})
        app = FastAPI()
        app.include_router(api.router())
        headers = {'Authorization': 'Bearer ' + token}
        base = '/extension/v1/authoring/projects/' + project
        with TestClient(app) as client:
            created = client.post(base + '/workflows', headers=headers, json={
                'operationId': 'canvas-create-once', 'name': 'Agent Line 画布真实保存验证 20261022',
                'description': '独立测试草稿，不发布、不执行；保留原生角色和记忆'})
            assert created.status_code == 202
            if created.json()['status'] != 'accepted':
                save(OUT / 'native-report.json', {'status': 'create_unknown', 'safeToResubmit': False})
                return
            wid = created.json()['receipt']['workflowId']
            body_path = OUT / 'save-intent.json'
            if not body_path.exists():
                draft = client.get(base + '/workflows/' + wid, headers=headers)
                assert draft.status_code == 200
                save(body_path, {'operationId': 'canvas-save-once',
                                 'expectedStateSha256': draft.json()['stateSha256'], 'document': document})
            body = json.loads(body_path.read_text())
            assert body['document'] == document, 'Browser graph changed: do not overwrite existing native proof'
            saved = client.post(base + '/workflows/' + wid + '/save', headers=headers, json=body)
            assert saved.status_code == 202
            record = saved.json()
            if record['status'] != 'accepted':
                save(OUT / 'native-report.json', {'status': record['status'], 'safeToResubmit': False})
                return
            assert record['receipt']['outcome'] == 'saved'
            reloaded = client.get(base + '/workflows/' + wid, headers=headers)
            assert reloaded.status_code == 200
            save(OUT / 'native-readback.json', reloaded.json()['document'])
            actual_document = reloaded.json()['document']
            # Sim may reorder its block rows; IDs, positions and all other
            # document fields must still be identical. No field is discarded.
            expected_nodes = {node['id']: node for node in document['nodes']}
            actual_nodes = {node['id']: node for node in actual_document['nodes']}
            assert len(expected_nodes) == len(document['nodes'])
            assert len(actual_nodes) == len(actual_document['nodes'])
            assert expected_nodes == actual_nodes
            assert {k:v for k,v in actual_document.items() if k != 'nodes'} == {
                k:v for k,v in document.items() if k != 'nodes'}
            assert reloaded.json()['stateSha256'] == record['receipt']['stateSha256']
    report = {'mode': 'current_api_real_sim_isolated_owned_workflow', 'status': 'saved_and_graph_readback_equal',
              'nativeNodeArrayOrderChanged': actual_document['nodes'] != document['nodes'],
              'nodes': len(document['nodes']), 'edges': len(document['edges']),
              'branchIdsPreserved': True, 'positionsPreserved': True, 'journalMainUnchanged': before == main_snapshot(),
              'nativeRequestsThisObservation': calls, 'modelCalls': 0, 'publications': 0, 'mainReloaded': False}
    assert report['journalMainUnchanged']
    save(OUT / 'native-report.json', report)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
