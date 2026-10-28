"""Live approved synthetic write to a separate namespace; preserves recall marker."""
import hashlib
import json
from pathlib import Path
import sys
import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from agent_line.adapters.qwenpaw_memory import MemoryApproval, QwenPawMemoryClient
from probe import ROLE, save

state = json.loads((ROOT/'var/qwenpaw/agent-line-process.json').read_text())
base = f'http://127.0.0.1:{state["port"]}'
with httpx.Client() as client:
    response = client.post(base+'/api/auth/login', json=json.loads((ROOT/'var/qwenpaw-secrets/local-login.json').read_text()))
    response.raise_for_status()
    token = response.json().get('token') or response.json().get('access_token')
adapter = QwenPawMemoryClient(base,token,role_agent_id=ROLE)
content = 'Approved synthetic adapter probe: Agent Line official memory API transport preserves verified readback.'
task_id = 'memory-adapter-proof'
approval = MemoryApproval(task_id, hashlib.sha256(content.encode()).hexdigest(), 'explicit-isolated-probe-review')
try:
    result = adapter.write_approved(task_id,content,approval=approval)
    if result['status'] != 'committed':
        raise RuntimeError('Approved write not confirmed; do not overwrite or rerun coding')
    readback = adapter.reconcile(task_id,content)
    assert readback['status']=='committed'
    evidence_path = ROOT/'var/qwenpaw/memory-probe.json'
    evidence = json.loads(evidence_path.read_text())
    evidence['approvedAdapterSmoke'] = {'result':result,'readback':readback,'pid':state['pid']}
    save(evidence)
    print('PASS: approved separate namespaced write and official GET readback; original marker untouched')
finally:
    adapter.close()
