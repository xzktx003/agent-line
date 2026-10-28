"""Owner-checked container lifecycle and durable, positive exit evidence."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
STATE=ROOT/'var/executor/launches'


def start_ticks(pid):
    try:
        data=Path(f'/proc/{pid}/stat').read_text();parts=data[data.rfind(')')+2:].split()
        return None if parts[0]=='Z' else parts[19]
    except (OSError,IndexError):return None


@contextmanager
def locked(path):
    path=Path(path)
    with path.with_suffix('.lock').open('a') as lock:
        os.chmod(lock.name,0o600);fcntl.flock(lock,fcntl.LOCK_EX)
        record=json.loads(path.read_text())
        yield record
        temporary=path.with_suffix('.tmp')
        with temporary.open('w') as output:
            os.chmod(temporary,0o600);json.dump(record,output,indent=2);output.flush();os.fsync(output.fileno())
        os.replace(temporary,path)
        directory=os.open(path.parent,os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)


def inspect_owned(record):
    result=subprocess.run(['docker','inspect',record.get('containerId') or record['name']],capture_output=True,text=True)
    if result.returncode:return None
    container=json.loads(result.stdout)[0]
    labels=container['Config'].get('Labels') or {}
    expected={'agent-line.owner':record['owner'],'agent-line.pid':str(record['pid']),
              'agent-line.start':record['start'],'agent-line.uid':str(record['uid'])}
    if any(labels.get(k)!=v for k,v in expected.items()):
        raise RuntimeError('Container ownership mismatch; refusing cleanup')
    if record.get('containerId') and record['containerId']!=container['Id']:
        raise RuntimeError('Container identity changed')
    return container


def preserved_end_paths(record):
    """Files an ended flow must keep: agent, memory, project tree, review worktree."""
    workspace = record.get('workspace')
    if not isinstance(workspace, str) or not workspace:
        return []
    root = Path(workspace)
    if not root.exists():
        return []
    found = [root]
    for relative in (Path('agent.json'), Path('MEMORY.md'), Path('review-worktree')):
        candidate = root / relative
        if candidate.exists():
            found.append(candidate)
    return found


def end_removal(record, container_id):
    """The only automatic removal is the owned container, never durable state."""
    if not isinstance(container_id, str) or re.fullmatch(r'[a-f0-9]{64}', container_id) is None:
        raise RuntimeError('End cleanup refuses to delete durable state')
    workspace = record.get('workspace')
    if isinstance(workspace, str) and workspace and (container_id == workspace or workspace in container_id):
        raise RuntimeError('End cleanup refuses to delete durable state')
    for path in preserved_end_paths(record):
        if container_id in (path.name, str(path)):
            raise RuntimeError('End cleanup refuses to delete durable state')
    return ['docker', 'rm', container_id]


def cleanup(record):
    """Never infer exit from absence. Persist inspect proof before removing."""
    path=STATE/(record['owner']+'.json')
    # Used by ownership negative tests without creating an owner receipt.
    if not path.exists():
        inspect_owned(record)
        raise RuntimeError('No durable owner receipt; refusing cleanup')
    with locked(path) as current:
        if current.get('exitReceipt'):return current['exitReceipt']
        current['accepting']=False
    with locked(path) as current:
        container=inspect_owned(current)
        if container is None:return None
        removal = end_removal(current, container['Id'])
        current['containerId']=removal[2]
        if container['State']['Running']:
            subprocess.run(['docker','kill',removal[2]],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        container=inspect_owned(current)
        if not container:return None
        state=container['State']
        if state['Running'] or state.get('Pid')!=0 or state.get('Status') not in ('exited','dead'):
            return None
        if not state.get('FinishedAt') or state['FinishedAt'].startswith('0001-'):
            return None
        proof={'schemaVersion':1,'owner':current['owner'],'containerId':container['Id'],
               'imageId':container['Image'],'workspace':current.get('workspace'),
               'identities':current.get('identities',[]),'running':False,'pid':0,
               'status':state['Status'],'exitCode':state['ExitCode'],
               'startedAt':state['StartedAt'],'finishedAt':state['FinishedAt'],
               'observedAt':time.time(),'source':'docker-inspect-before-remove'}
        command = end_removal(current, container['Id'])
        current['exitReceipt']=proof
    kept = [item for item in preserved_end_paths(proof) if item.exists()]
    # Proof is fsynced before --rm. A failed remove does not invalidate actual exit.
    subprocess.run(command,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    if any(not item.exists() for item in kept):
        raise RuntimeError('End cleanup removed durable state')
    return proof


def guard(path):
    record=json.loads(Path(path).read_text())
    while start_ticks(record['pid'])==record['start']:time.sleep(.2)
    for _ in range(25):
        if cleanup(record):return
        time.sleep(.2)


if __name__=='__main__':guard(sys.argv[1])
