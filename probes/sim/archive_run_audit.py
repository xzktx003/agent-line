"""Real owned draft/version soft archive through current API, then offline audit.

One private journal and stable operation identities; execute one pure Start/End Sim run, no model.
After first success, reopening only observes persisted operations and audit data.
"""
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import time
import uuid
from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
from agent_line.adapters.sim import SimClient
from agent_line.approvals import TrustedPrincipal
from agent_line.authoring_api import AuthoringAPI, AuthoringAccess, namespace
from agent_line.sim_authoring import SimAuthoringClient, decode_document
from agent_line.sim_journal import SimJournal
from probes.qwenpaw_memory.native_roundtrip import save

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'var/workflow-archive-run-audit-20261022'


def main():
    OUT.mkdir(exist_ok=True, mode=0o700)
    service_path = ROOT / 'var/adapter/service.json'
    config = json.loads(service_path.read_text())
    auth = json.loads((service_path.parent / config['sim']['authFile']).read_text())
    authoring = json.loads((service_path.parent / config['authoringFile']).read_text())
    main_journal = (service_path.parent / config['sim']['journal']).resolve()

    def main_digest():
        with sqlite3.connect(main_journal.as_uri()+'?mode=ro', uri=True) as db:
            rows = db.execute('SELECT * FROM operations ORDER BY workflow,run,context').fetchall()
        return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
    before = main_digest()
    requests, offline = [], False
    known_workflow = None
    run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'agent-line-archive-zero-model-run-20261022'))

    def observed(request):
        assert not offline, 'Archive audit must not call the unavailable native API'
        path = request.url.path
        assert path.startswith('/api/v2/workflows') and not any(x in path for x in ('/resume', '/cancel'))
        assert request.method in ('GET', 'POST', 'PUT', 'DELETE')
        if request.method == 'POST':
            assert path == '/api/v2/workflows' or path.endswith('/deploy') or (
                path == '/api/v2/workflows/'+str(known_workflow)+'/execute' and request.headers.get('X-Run-Id') == run_id)
        requests.append(request.method)

    path = OUT / 'authoring.sqlite'
    journal = SimJournal(path) if path.exists() else SimJournal.initialize(path)
    project, subject = 'archive-audit-proof', 'archive-audit-proof'
    token = secrets.token_urlsafe(48)
    access = AuthoringAccess({token: TrustedPrincipal(subject, frozenset({project}),
        frozenset({'edit_workflows', 'publish_workflows'}), time.time()+600)})
    document = {'schemaVersion': 1, 'nodes': [
        {'id': 'start', 'type': 'start', 'name': '开始', 'position': {'x': 30, 'y': 80}, 'config': {}},
        {'id': 'end', 'type': 'end', 'name': '结束', 'position': {'x': 330, 'y': 80}, 'config': {'output': {'audit': 'kept'}}}],
        'edges': [{'id': 'edge', 'source': 'start', 'target': 'end'}], 'variables': {}}
    with httpx.Client(timeout=30, event_hooks={'request': [observed]}) as http:
        remote = SimAuthoringClient(SimClient(auth['baseUrl'], auth['apiKey'], client=http),
                                    next(iter(authoring['workspaces'].values())))
        api = AuthoringAPI(access, journal, {project: remote}, projects={project: {'workspace': str(OUT)}})
        app = FastAPI()
        app.include_router(api.router())
        base, headers = '/extension/v1/authoring/projects/'+project, {'Authorization': 'Bearer '+token}
        with TestClient(app) as client:
            def post(path, body):
                response = client.post(base+path, headers=headers, json=body)
                assert response.status_code == 202, f'Unaccepted {path} HTTP{response.status_code}'
                record = response.json()
                if record['status'] != 'accepted':
                    save(OUT/'pending.json', {'operationId': body['operationId'], 'status': record['status'],
                                             'safeToResubmit': False})
                    raise RuntimeError('Original operation pending; no new identity or retry')
                return record

            created = post('/workflows', {'operationId': 'create-once', 'name': 'Agent Line 已运行流程归档审计验证 20261022',
                                         'description': '自有开始结束流程，零模型运行一次后归档保留审计'})
            wid = created['receipt']['workflowId']; known_workflow = wid
            intent_path = OUT/'save-intent.json'
            if not intent_path.exists():
                read = client.get(base+'/workflows/'+wid, headers=headers)
                assert read.status_code == 200
                save(intent_path, {'operationId': 'save-once', 'expectedStateSha256': read.json()['stateSha256'],
                                   'document': document})
            saved = post('/workflows/'+wid+'/save', json.loads(intent_path.read_text()))
            assert saved['receipt']['outcome'] == 'saved'
            publication = post('/workflows/'+wid+'/publish', {'operationId': 'publish-once',
                                'expectedStateSha256': saved['receipt']['stateSha256']})
            assert publication['receipt']['outcome'] == 'published'
            try:
                journal.get(namespace(project), 'archive-once', 'author-archive')
                archive_exists = True
            except KeyError:
                archive_exists = False
            if not archive_exists:
                executed = journal.execute(remote.sim, wid, run_id, {})
                assert executed['status'] == 'accepted', 'Original run pending; never submit another run'
                for _ in range(30):
                    observed_run = remote.sim.observe(wid, run_id)
                    if observed_run['status'] == 'completed':
                        break
                    if observed_run['status'] in ('failed', 'cancelled'):
                        raise AssertionError('Original pure control run failed')
                    time.sleep(0.2)
                else:
                    save(OUT/'pending-run.json', {'runId': run_id, 'status': observed_run['status'],
                                                'safeToResubmit': False})
                    raise RuntimeError('Original run still pending; observe this same identity')
            archive_body = {'operationId': 'archive-once', 'expectedStateSha256': saved['receipt']['stateSha256']}
            archived = post('/workflows/'+wid+'/archive', archive_body)
            assert archived['receipt']['outcome'] == 'archived'
            native_request_count = len(requests)
            offline = True
            audit_response = client.get(base+'/archives/archive-once', headers=headers)
            assert audit_response.status_code == 200
            audit = audit_response.json()
            snapshot = audit['snapshot']
            assert len(snapshot['versions']) == 1 and len(snapshot['runs']) == 1
            assert snapshot['runs'][0]['runId'] == run_id and snapshot['runs'][0]['status'] == 'completed'
            assert 'output' in snapshot['runs'][0]
            assert 'kept' in json.dumps(snapshot['runs'][0]['output'])
            actual = decode_document(snapshot['versions'][0]['state'])
            assert {n['id']: n for n in actual['nodes']} == {n['id']: n for n in document['nodes']}
            assert actual['edges'] == document['edges']
            assert post('/workflows/'+wid+'/archive', archive_body) == archived
            active = client.get(base+'/workflows', headers=headers)
            assert active.status_code == 200 and active.json()['workflows'] == []
            directory = client.get(base+'/archives', headers=headers)
            assert directory.status_code == 200 and len(directory.json()['archives']) == 1
            denied = client.post(base+'/workflows/'+wid+'/archive', headers=headers,
                                 json=archive_body | {'operationId': 'never-resubmit'})
            assert denied.status_code == 422
            assert len(requests) == native_request_count
            save(OUT/'audit.json', audit)
    report = {'mode': 'current_api_real_sim_soft_archive_and_offline_audit', 'status': 'passed',
              'versionDefinitionsRetained': 1, 'apiVisibleRunsAtCapture': 1,
              'draftRetained': True, 'auditWorksWithNativeOffline': True, 'sameOperationReplayedWithoutNativeCall': True,
              'newArchiveOperationRefused': True, 'activeDirectoryHidesArchived': True,
              'mainJournalUnchanged': before == main_digest(), 'nativeMethodsThisObservation': requests,
              'modelCalls': 0, 'workflowExecutions': 1, 'mainReloaded': False}
    assert report['mainJournalUnchanged']
    save(OUT/'report.json', report)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
