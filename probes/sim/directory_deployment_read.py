"""Read existing owned workflow deployment metadata; never submit mutations."""
import hashlib
import json
from pathlib import Path
import sqlite3
import httpx
from agent_line.adapters.sim import SimClient
from agent_line.authoring_api import namespace
from agent_line.sim_authoring import SimAuthoringClient, AuthoringError
from probes.qwenpaw_memory.native_roundtrip import save

ROOT = Path(__file__).resolve().parents[2]


def main():
    config_path = ROOT / 'var/adapter/service.json'
    config = json.loads(config_path.read_text())
    authoring = json.loads((config_path.parent / config['authoringFile']).read_text())
    auth = json.loads((config_path.parent / config['sim']['authFile']).read_text())
    journal = (config_path.parent / config['sim']['journal']).resolve()
    calls = []

    def readonly(request):
        assert request.method == 'GET'
        calls.append(request.url.path.rsplit('/', 1)[-1] == 'deployment')

    def snapshot():
        with sqlite3.connect(journal.as_uri() + '?mode=ro', uri=True) as db:
            rows = db.execute('SELECT * FROM operations ORDER BY workflow,run,context').fetchall()
        return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()

    before = snapshot()
    results = []
    with httpx.Client(event_hooks={'request': [readonly]}, timeout=30) as http:
        sim = SimClient(auth['baseUrl'], auth['apiKey'], client=http)
        for project, workspace in authoring['workspaces'].items():
            with sqlite3.connect(journal.as_uri() + '?mode=ro', uri=True) as db:
                rows = db.execute("SELECT receipt FROM operations WHERE workflow=? AND context='author-create' "
                                  "AND status='accepted' ORDER BY run LIMIT 3", (namespace(project),)).fetchall()
            client = SimAuthoringClient(sim, workspace)
            for (raw,) in rows:
                wid = json.loads(raw)['workflowId']
                try:
                    state = client.get_deployment(wid)
                except AuthoringError as error:
                    results.append({'status': 'unknown', 'reason': error.code})
                else:
                    active = state['activeDeployment']
                    results.append({'status': 'deployed' if state['isDeployed'] else 'draft',
                                    'currentVersion': active['version'] if active else None,
                                    'needsRedeployment': state['needsRedeployment']})
    report = {'mode': 'real_sim_current_source_readonly', 'results': results,
              'getRequests': len(calls), 'deploymentReads': sum(calls),
              'mutatingHttpRequests': 0, 'modelCalls': 0, 'mainReloaded': False,
              'journalUnchanged': before == snapshot()}
    assert report['journalUnchanged'] and results
    save(ROOT / 'var/workflow-directory-20261022/native-deployment-report.json', report)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
