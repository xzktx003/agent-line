"""One owned empty native workflow: persist intent, archive once, inspect reads.

Reopening this probe observes its original journal; never creates a second
workflow or resubmits an uncertain archive. No model execution or restore.
"""
import json
from pathlib import Path
import httpx
from agent_line.adapters.sim import SimClient
from agent_line.sim_authoring import SimAuthoringClient
from agent_line.sim_journal import SimJournal
from probes.qwenpaw_memory.native_roundtrip import save

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'var/workflow-archive-20261022'


def main():
    OUT.mkdir(exist_ok=True, mode=0o700)
    settings_path = ROOT / 'var/adapter/service.json'
    settings = json.loads(settings_path.read_text())
    auth = json.loads((settings_path.parent / settings['sim']['authFile']).read_text())
    authoring = json.loads((settings_path.parent / settings['authoringFile']).read_text())
    workspace = next(iter(authoring['workspaces'].values()))
    path = OUT / 'sim.sqlite'
    journal = SimJournal(path) if path.exists() else SimJournal.initialize(path)
    requests = []
    with httpx.Client(timeout=30, event_hooks={'request': [lambda r: requests.append(r.method)]}) as http:
        sim = SimClient(auth['baseUrl'], auth['apiKey'], client=http)
        client = SimAuthoringClient(sim, workspace)
        created = journal.mutate('archive-probe', 'create-owned-once', 'create',
            {'name': 'Agent Line 归档边界验证 20261022', 'workspaceId': workspace},
            lambda: client.create('Agent Line 归档边界验证 20261022', '空流程，仅验证原生归档读取边界，不调用模型'))
        if created['status'] != 'accepted':
            save(OUT / 'report.json', {'status': 'create_unknown', 'safeToResubmit': False})
            return
        wid = created['receipt']['id']

        def read_status(suffix):
            response = http.get(sim.base_url + '/api/v2/workflows/' + wid + suffix,
                                headers={'x-api-key': sim._key}, follow_redirects=False)
            return response.status_code

        baseline_path = OUT / 'before.json'
        if not baseline_path.exists():
            save(baseline_path, {'detail': read_status(''), 'runs': read_status('/runs'),
                                'versions': read_status('/versions')})
        archive = journal.mutate('archive-probe', 'archive-owned-once', 'archive', {'workflowId': wid},
                                 lambda: client.archive(wid))
        report = {'status': archive['status'], 'before': json.loads(baseline_path.read_text()),
                  'after': {'detail': read_status(''), 'runs': read_status('/runs'),
                            'versions': read_status('/versions')},
                  'archiveReceiptConfirmed': archive['receipt'] == {'id': wid, 'deleted': True, 'archived': True},
                  'modelCalls': 0, 'mainJournalWrites': 0, 'requestMethodsThisObservation': requests,
                  'archivedScopeFound': False, 'archivedPagesRead': 0}
        cursor = None
        for _ in range(10):
            page = client.list_archived(cursor, 100)
            report['archivedPagesRead'] += 1
            if any(item['id'] == wid for item in page['data']):
                report['archivedScopeFound'] = True
                break
            cursor = page.get('nextCursor')
            if not cursor:
                break
        save(OUT / 'report.json', report)
        print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
