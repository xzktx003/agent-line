"""Actual stdio disconnect/SIGTERM/SIGKILL probes; never list or stop other containers."""
import json
import os
from pathlib import Path
import signal
import subprocess
import time
ROOT=Path(__file__).resolve().parents[2]
launcher=ROOT/'deploy/executor/launcher.py'
results=[]
for mode in ('disconnect','sigterm','sigkill'):
    process=subprocess.Popen([str(launcher),'app-server','--listen','stdio://'],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    record=None
    for _ in range(100):
        for path in (ROOT/'var/executor/launches').glob('*.json'):
            candidate=json.loads(path.read_text())
            if candidate['pid']==process.pid:record=candidate
        if record and subprocess.run(['docker','inspect',record['name']],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0:break
        time.sleep(.1)
    else:raise RuntimeError('Container did not start')
    if mode=='disconnect':process.stdin.close()
    else:os.kill(process.pid,signal.SIGTERM if mode=='sigterm' else signal.SIGKILL)
    process.wait(timeout=20)
    removed=False
    for _ in range(100):
        if subprocess.run(['docker','inspect',record['name']],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode:
            removed=True;break
        time.sleep(.1)
    results.append({'mode':mode,'launcherReturnCode':process.returncode,'owner':record['owner'],'containerRemoved':removed})
    assert removed,results[-1]
(ROOT/'var/executor/lifecycle-evidence.json').write_text(json.dumps(results,indent=2))
print(json.dumps(results))
