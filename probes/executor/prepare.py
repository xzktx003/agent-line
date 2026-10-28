"""Create only isolated executor directories/config; no running-role mutation."""
import json
import os
from pathlib import Path
import shutil
import subprocess
ROOT=Path(__file__).resolve().parents[2]
state=ROOT/'var/executor'
state.mkdir(parents=True,exist_ok=True)
workspace=state/'workspace'
home=state/'codex-home'
workspace.mkdir(exist_ok=True)
home.mkdir(mode=0o700,exist_ok=True)
binary=Path('/data01/home/zukang.xu/.nvm/versions/node/v24.14.0/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex')
image=subprocess.check_output(['docker','inspect','--format','{{.Id}}','78eba5f4a29a'],text=True).strip()
if (state/'image-build.json').exists():
    image=json.loads((state/'image-build.json').read_text())['image']
config={'workspace':str(workspace),'codex_home':str(home),'binary':str(binary),'image':image}
p=state/'config.json'
if p.exists() and json.loads(p.read_text())!=config:raise SystemExit('Existing configuration differs')
p.write_text(json.dumps(config,indent=2));p.chmod(0o600)
auth=home/'auth.json'
if not auth.exists():
    shutil.copyfile(ROOT/'var/codex-g0/auth.json',auth);auth.chmod(0o600)
print(json.dumps({'prepared':True,'credential_source':'project isolated codex-g0/auth.json','no_running_roles_modified':True}))
