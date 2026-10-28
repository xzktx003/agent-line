"""Opt-in formal role creation using official binary/workspace configuration."""
import json
from pathlib import Path
import httpx
ROOT=Path(__file__).resolve().parents[2]
ROLE='agent-line-container-codex'
config=json.loads((ROOT/'var/executor/config.json').read_text())
state=json.loads((ROOT/'var/qwenpaw/agent-line-process.json').read_text())
base=f'http://127.0.0.1:{state["port"]}/api'
with httpx.Client(timeout=60) as client:
    response=client.post(base+'/auth/login',json=json.loads((ROOT/'var/qwenpaw-secrets/local-login.json').read_text()))
    response.raise_for_status();login=response.json()
    client.headers['Authorization']='Bearer '+(login.get('token') or login['access_token'])
    response=client.get(base+'/agents');response.raise_for_status()
    agents={item['id']:item for item in response.json()['agents']}
    if ROLE not in agents:
        response=client.post(base+'/agents',json={'id':ROLE,'name':'容器隔离 Codex 执行角色','description':'固定项目目录、容器生命周期回执；网络出口尚未限域','backend':'codex','workspace_dir':config['workspace'],'language':'zh','skill_names':[]})
        response.raise_for_status()
    response=client.get(base+'/agents/'+ROLE);response.raise_for_status();role=response.json()
    assert role['backend']=='codex'
    assert Path(role['workspace_dir']).resolve()==Path(config['workspace']).resolve()
    settings={'binary':str(ROOT/'deploy/executor/launcher.py'),'model':'gpt-6.1-sol','sandbox':'danger-full-access','approval_policy':'never'}
    role['backend_settings']={**role.get('backend_settings',{}),**settings}
    response=client.put(base+'/agents/'+ROLE,json=role);response.raise_for_status()
    response=client.get(base+'/agents/'+ROLE);response.raise_for_status();saved=response.json()
    assert all(saved['backend_settings'].get(key)==value for key,value in settings.items())
    result={'roleId':ROLE,'workspace':config['workspace'],'binary':settings['binary'],'officialRolePersisted':True,'nativeExecutionVerified':False}
    (ROOT/'var/executor/formal-role.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result))
