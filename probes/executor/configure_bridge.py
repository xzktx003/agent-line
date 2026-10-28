"""Explicit isolated bridge switch; preserve old plugin config and all old receipts."""
import json
import os
from pathlib import Path
import httpx
ROOT=Path(__file__).resolve().parents[2]
os.environ['QWENPAW_WORKING_DIR']=str(ROOT/'var/qwenpaw')
os.environ['QWENPAW_SECRET_DIR']=str(ROOT/'var/qwenpaw-secrets')
from qwenpaw.config.utils import load_config,save_config,load_agent_config
role=load_agent_config('agent-line-container-codex')
assert isinstance(role.backend_settings,dict)
launcher=ROOT/'deploy/executor/launcher.py'
assert Path(role.backend_settings['binary']).resolve()==launcher.resolve()
executor=json.loads((ROOT/'var/executor/config.json').read_text())
assert Path(role.workspace_dir).resolve()==Path(executor['workspace']).resolve()
config=load_config();plugin=dict(config.plugins['agent-line'])
backup=ROOT/'var/qwenpaw-secrets/plugin-before-container.json'
if not backup.exists():
    descriptor=os.open(backup,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,'w') as stream:json.dump(plugin,stream,indent=2)
plugin['codex_target_agent_id']=role.id
plugin['execution_bridge']={**plugin['execution_bridge'],'target_agent_id':role.id,'workspace':executor['workspace'],'codex_home':executor['codex_home'],'container_control':str(ROOT/'deploy/executor/control.py'),'container_launcher':str(launcher)}
config.plugins['agent-line']=plugin;save_config(config)
state=json.loads((ROOT/'var/qwenpaw/agent-line-process.json').read_text());base=f'http://127.0.0.1:{state["port"]}/api'
with httpx.Client(timeout=120) as client:
    response=client.post(base+'/auth/login',json=json.loads((ROOT/'var/qwenpaw-secrets/local-login.json').read_text()));response.raise_for_status();login=response.json()
    client.headers['Authorization']='Bearer '+(login.get('token') or login['access_token'])
    response=client.post(base+'/plugins/install',json={'source':str(ROOT/'plugins/agent-line'),'force':True})
    if response.status_code!=200:print({'installStatus':response.status_code,'detail':response.json()})
    response.raise_for_status()
# Official installer writes plugins dir; verify installed bytes without printing config.
installed=ROOT/'var/qwenpaw/plugins/agent-line'
if not installed.exists():raise RuntimeError('Installed plugin directory missing')
for name in ('plugin.py','execution_bridge.py','dist/index.js'):
    assert (installed/name).read_bytes()==(ROOT/'plugins/agent-line'/name).read_bytes(),name
print(json.dumps({'target':role.id,'backendSettingsIsDict':True,'installedSourceMatches':True,'oldConfigPrivatelyBackedUp':True}))
