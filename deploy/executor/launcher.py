#!/usr/bin/env python3
"""Fixed server-owned container boundary for QwenPaw CODEX_BINARY."""
import hashlib
import json
import re
import os
from pathlib import Path
import stat
import sys
import signal
import subprocess
import uuid
import threading
import time
from lifecycle import cleanup, start_ticks, locked, inspect_owned

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / 'var/executor/config.json'


def _sandbox_helper(binary):
    """Return the bundled Linux sandbox helper, if this Codex install has one."""
    path = Path(binary).parent.parent / 'codex-resources' / 'bwrap'
    try:
        resources = path.parent.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(resources.st_mode) or stat.S_ISLNK(resources.st_mode):
        raise RuntimeError('Codex sandbox resources must be a real directory')
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if (path.resolve(strict=True) != path or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid() or info.st_mode & stat.S_IWOTH
            or not info.st_mode & 0o111 or not os.access(path, os.X_OK)):
        raise RuntimeError('Codex sandbox helper must be an owned, executable regular file')
    if ',' in str(path):
        raise RuntimeError('Container mount paths cannot contain commas')
    return path



def _owned_private_dir(path):
    """Create a real, owner-only directory. Refuse symlinks and shared writes."""
    if ',' in str(path):
        raise RuntimeError('Container mount paths cannot contain commas')
    if path.is_symlink():
        raise RuntimeError('Container state directory must be a real directory')
    try:
        path.mkdir(mode=0o700, exist_ok=True)
    except FileExistsError as error:
        raise RuntimeError('Container state directory must be a real directory') from error
    resolved = path.resolve(strict=True)
    info = path.lstat()
    if resolved != path or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError('Container state directory must be a real directory owned by the current user')
    os.chmod(path, 0o700)
    if path.lstat().st_mode & 0o077:
        raise RuntimeError('Container state directory must be private to the owner')
    return path


def _real_directory(path):
    if path.is_symlink() or not path.is_dir() or path.resolve(strict=True) != path:
        raise RuntimeError('Container state directory must be a real directory')
    return path


def _scope_digest(workspace, task_field):
    material = str(workspace) if task_field is None else str(workspace) + '\0' + task_field
    return hashlib.sha256(material.encode()).hexdigest()


def _task_field(config, workspace):
    """Return one existing field directory, or None when the optional key is absent."""
    if 'taskField' not in config or config['taskField'] is None:
        return None
    value = config['taskField']
    if not isinstance(value, str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', value) is None:
        raise RuntimeError('taskField must be one safe path segment')
    root = _real_directory(workspace / 'task-fields')
    field = _real_directory(root / value)
    if not field.is_relative_to(root) or field == root:
        raise RuntimeError('taskField must stay inside task-fields')
    return value, field


def _task_field_cover(home, field_name, digest):
    """Private empty cover so sibling fields and loose files stay unmounted."""
    if ',' in str(home):
        raise RuntimeError('Container mount paths cannot contain commas')
    covers = home / 'task-field-covers'
    if not covers.exists():
        _owned_private_dir(covers)
    _real_directory(covers)
    cover = _owned_private_dir(covers / digest)
    if not cover.is_relative_to(covers) or cover == covers:
        raise RuntimeError('Task field cover must stay inside the cover directory')
    point = cover / field_name
    if point.is_symlink():
        raise RuntimeError('Task field cover cannot contain symlinks')
    if not point.exists():
        _owned_private_dir(point)
    else:
        _real_directory(point)
    return cover


def _project_runtime(home, workspace, task_field=None):
    """Return a per-workspace Codex view and the session directory the bridge can see."""
    if ',' in str(home) or ',' in str(workspace):
        raise RuntimeError('Container mount paths cannot contain commas')
    digest = _scope_digest(workspace, task_field)
    sessions_root = home / 'sessions'
    if not sessions_root.exists():
        _owned_private_dir(sessions_root)
    _real_directory(sessions_root)
    projects = sessions_root / 'projects'
    if not projects.exists():
        _owned_private_dir(projects)
    _real_directory(projects)
    sessions = _owned_private_dir(projects / digest)
    if not sessions.is_relative_to(sessions_root) or sessions == sessions_root:
        raise RuntimeError('Project session directory must stay inside Codex sessions')
    views = home / 'views'
    if not views.exists():
        _owned_private_dir(views)
    _real_directory(views)
    view = _owned_private_dir(views / digest)
    if not view.is_relative_to(views) or view == home:
        raise RuntimeError('Project Codex view must stay inside the Codex view directory')
    config = home / 'config.toml'
    target = view / 'config.toml'
    if config.is_file() and not config.is_symlink() and not target.exists():
        target.write_bytes(config.read_bytes())
        os.chmod(target, 0o600)
    _mount_private_file(home / 'auth.json', view / 'auth.json')
    # Codex opens installation_id and rewrites models_cache.json during startup.
    # Read-only mounts of those files make app-server exit EROFS. Keep per-view
    # copies so the shared files stay unmounted and the container can write.
    for name in ('installation_id', 'models_cache.json'):
        _seed_private_file(home / name, view / name)
    return view, sessions


def _require_private_regular(source):
    if (not source.exists() or source.is_symlink() or not source.is_file()
            or source.stat().st_uid != os.getuid() or source.stat().st_mode & 0o022):
        raise RuntimeError('Codex home files mounted into a project view must be private regular files')


def _mount_private_file(source, placeholder):
    """Create the mount point for a shared file that must stay read-only."""
    if not source.exists():
        return
    _require_private_regular(source)
    if placeholder.is_symlink():
        raise RuntimeError('Project Codex view cannot contain symlinks')
    if not placeholder.exists():
        placeholder.touch(mode=0o600)
        os.chmod(placeholder, 0o600)
    if not placeholder.is_file():
        raise RuntimeError('Project Codex view mount points must be regular files')


def _seed_private_file(source, placeholder):
    """Copy a shared Codex file into the view once; later writes stay there."""
    if not source.exists():
        return
    _require_private_regular(source)
    if placeholder.is_symlink():
        raise RuntimeError('Project Codex view cannot contain symlinks')
    if placeholder.exists() and not placeholder.is_file():
        raise RuntimeError('Project Codex view mount points must be regular files')
    if placeholder.exists() and placeholder.stat().st_size > 0:
        return
    placeholder.write_bytes(source.read_bytes())
    os.chmod(placeholder, 0o600)


def _private_file_mounts(home, view):
    mounts = []
    source = home / 'auth.json'
    if source.exists():
        mounts.append(f'type=bind,src={source},dst=/codex-home/auth.json,readonly')
    return mounts


def command(arguments, *, probe=False, owner=None):
    config = json.loads(CONFIG.read_text())
    if CONFIG.stat().st_mode & 0o022:
        raise RuntimeError('Executor config must not be group/world writable')
    workspace = Path(config['workspace']).resolve(strict=True)
    home = Path(config['codex_home']).resolve(strict=True)
    binary = Path(config['binary']).resolve(strict=True)
    if workspace == home or workspace in home.parents or home in workspace.parents:
        raise RuntimeError('Workspace and credential state must be separate')
    field = _task_field(config, workspace)
    view, sessions = _project_runtime(home, workspace, None if field is None else field[0])
    if not config['image'].startswith('sha256:'):
        raise RuntimeError('Immutable local image ID required')
    sandbox_helper = _sandbox_helper(binary)
    result = ['docker', 'run', '-i', '--init', '--read-only',
              '--cap-drop=ALL', '--security-opt=no-new-privileges',
              '--pids-limit=128', '--memory=2g', '--cpus=2',
              '--user', f'{os.getuid()}:{os.getgid()}',
              '--network', 'bridge' if not probe else 'none',
              '--tmpfs', '/tmp:rw,nosuid,nodev,size=128m,mode=1777',
              '--mount', f'type=bind,src={workspace},dst={workspace}',
              '--mount', f'type=bind,src={view},dst=/codex-home',
              '--mount', f'type=bind,src={sessions},dst=/codex-home/sessions',
              '--mount', f'type=bind,src={binary},dst=/usr/local/bin/codex,readonly',
              '--mount', f'type=bind,src={binary.parent / "codex-code-mode-host"},dst=/usr/local/bin/codex-code-mode-host,readonly',
              '--workdir', str(workspace), '--env', 'CODEX_HOME=/codex-home',
              '--env', 'HOME=/tmp', '--env', 'PATH=/usr/local/bin:/usr/bin:/bin',
              '--entrypoint', '/bin/sh' if probe else '/usr/local/bin/codex',
              config['image']]
    if field is not None:
        field_name, field_dir = field
        cover = _task_field_cover(home, field_name, _scope_digest(workspace, field_name))
        field_at = result.index(f'type=bind,src={workspace},dst={workspace}') + 1
        result[field_at:field_at] = [
            '--mount', f'type=bind,src={cover},dst={workspace}/task-fields',
            '--mount', f'type=bind,src={field_dir},dst={workspace}/task-fields/{field_name}']
    file_mounts = _private_file_mounts(home, view)
    insert_at = result.index(f'type=bind,src={sessions},dst=/codex-home/sessions') + 1
    pairs = []
    for mount in file_mounts:
        pairs.extend(('--mount', mount))
    result[insert_at:insert_at] = pairs
    if sandbox_helper is not None:
        result[3:3] = ['--mount', f'type=bind,src={sandbox_helper},dst=/usr/local/bin/bwrap,readonly']
    if not owner:
        result.insert(2,'--rm')
    if owner:
        identity=['--name',owner['name']]
        for key,value in {'owner':owner['owner'],'pid':owner['pid'],'start':owner['start'],'uid':owner['uid']}.items():
            identity += ['--label',f'agent-line.{key}={value}']
        result[2:2]=identity
    return result + arguments


APPROVAL_METHODS = frozenset({'item/commandExecution/requestApproval',
                             'item/fileChange/requestApproval', 'item/permissions/requestApproval'})


def bounded_identity(value):
    return isinstance(value, str) and 0 < len(value) <= 256 and all(ord(c) >= 32 for c in value)


def rpc_key(value):
    if type(value) is int and -(2**53) < value < 2**53:
        return ('int', value)
    if bounded_identity(value):
        return ('str', value)
    return None


def record_approval(record, message):
    """Keep identity only; any ambiguity permanently closes approval correlation."""
    params = message.get('params')
    if (message.get('method') not in APPROVAL_METHODS or rpc_key(message.get('id')) is None
            or not isinstance(params, dict)
            or not all(bounded_identity(params.get(k)) for k in ('threadId', 'turnId', 'itemId'))):
        record['approvalBindingsUnavailable'] = True
        return
    binding = {'requestId': message['id'], 'method': message['method'],
               **{k: params[k] for k in ('threadId', 'turnId', 'itemId')}, 'resolved': False}
    entries = record.setdefault('approvalBindings', [])
    for old in entries:
        if rpc_key(old['requestId']) == rpc_key(binding['requestId']):
            if {k: old[k] for k in binding if k != 'resolved'} != {k: binding[k] for k in binding if k != 'resolved'}:
                record['approvalBindingsUnavailable'] = True
            return
        if (old['threadId'], old['turnId'], old['itemId']) == (binding['threadId'], binding['turnId'], binding['itemId']):
            record['approvalBindingsUnavailable'] = True
    if len(entries) >= 256:
        record['approvalBindingsUnavailable'] = True
        return
    entries.append(binding)


def resolve_approval(record, request_id, *, thread_id=None):
    key = rpc_key(request_id)
    if key is None:
        record['approvalBindingsUnavailable'] = True
        return
    matches = [entry for entry in record.get('approvalBindings', [])
               if rpc_key(entry['requestId']) == key and (thread_id is None or entry['threadId'] == thread_id)]
    if len(matches) > 1:
        record['approvalBindingsUnavailable'] = True
    elif matches:
        matches[0]['resolved'] = True


def proxy(child, receipt):
    pending={}
    allowed={'initialize','initialized','account/read','model/list','skills/list',
             'skills/extraRoots/set','thread/list','thread/read','thread/start',
             'thread/resume','turn/start','turn/interrupt'}
    protocol_lock=threading.Lock()
    output_lock=threading.Lock()
    def emit(raw):
        with output_lock:
            sys.stdout.buffer.write(raw);sys.stdout.buffer.flush()
    def upstream():
        try:
            for raw in sys.stdin.buffer:
                try:message=json.loads(raw)
                except (ValueError,TypeError):message={}
                method=message.get('method')
                if method and method not in allowed:
                    emit((json.dumps({'id':message.get('id'),'error':{'code':-32000,'message':'Method not enabled for controlled executor'}})+'\n').encode());continue
                with protocol_lock:
                    with locked(receipt) as record:
                        if method in ('thread/start','thread/resume','turn/start') and not record.get('accepting',True):
                            emit((json.dumps({'id':message.get('id'),'error':{'code':-32000,'message':'Executor is closing'}})+'\n').encode());continue
                        if 'id' in message and method in ('thread/start','thread/resume','turn/start'):
                            pending[rpc_key(message['id'])]={'method':method,'threadId':(message.get('params') or {}).get('threadId')}
                            if method=='turn/start':record['pendingTurns']=record.get('pendingTurns',0)+1
                        if not method and 'id' in message and ('result' in message or 'error' in message):
                            resolve_approval(record, message['id'])
                    # Commit response resolution before forwarding to the native server.
                    child.stdin.write(raw);child.stdin.flush()
        except (BrokenPipeError,OSError):pass
        finally:
            try:child.stdin.close()
            except OSError:pass
    def downstream():
        for raw in child.stdout:
            try:message=json.loads(raw)
            except (ValueError,TypeError):message={}
            with protocol_lock:
                request=pending.pop(rpc_key(message.get('id')),None) if 'method' not in message and ('result' in message or 'error' in message) else None
                if message.get('method') in APPROVAL_METHODS:
                    with locked(receipt) as record:
                        record_approval(record, message)
                if message.get('method')=='serverRequest/resolved':
                    params=message.get('params') or {}
                    with locked(receipt) as record:
                        if not bounded_identity(params.get('threadId')):
                            record['approvalBindingsUnavailable']=True
                        else:
                            resolve_approval(record,params.get('requestId'),thread_id=params['threadId'])
                if request:
                    with locked(receipt) as record:
                        result=message.get('result') or {}
                        if request['method']=='turn/start':
                            record['pendingTurns']=max(0,record.get('pendingTurns',0)-1)
                            turn=(result.get('turn') or {}).get('id')
                            if turn:
                                record.setdefault('identities',[]).append({'codexThreadId':request['threadId'],'codexTurnId':turn,'nativeTerminal':False})
                        elif result.get('thread',{}).get('id'):
                            record.setdefault('threads',[]).append(result['thread']['id'])
                if message.get('method')=='turn/completed':
                    params=message.get('params') or {};turn=params.get('turn') or {}
                    with locked(receipt) as record:
                        for identity in record.get('identities',[]):
                            if identity['codexThreadId']==params.get('threadId') and identity['codexTurnId']==turn.get('id'):
                                identity['nativeTerminal']=True
            emit(raw)
    reader=threading.Thread(target=upstream,daemon=True);reader.start()
    downstream()
    return child.wait()


def launch_executor(argv):
    """Own the Codex client session. A service-group signal must not reach it."""
    child=subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,start_new_session=True)
    child.agent_line_start=start_ticks(child.pid)
    return child


def stop_owned_executor(child):
    """Signal one recorded PID only after its start ticks still match."""
    if child is None or child.poll() is not None:return
    birth=getattr(child,'agent_line_start',None)
    if not isinstance(birth,str) or re.fullmatch(r'[0-9]+',birth) is None or start_ticks(child.pid)!=birth:return
    child.terminate()
    try:child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        if child.poll() is None and start_ticks(child.pid)==birth:child.kill()


if __name__ == '__main__':
    if not sys.argv[1:] or sys.argv[1] not in ('app-server', '--version'):
        raise SystemExit('Only app-server and version are permitted')
    owner_id=uuid.uuid4().hex
    config=json.loads(CONFIG.read_text())
    owner={'name':'agent-line-executor-'+owner_id,'owner':owner_id,'pid':os.getpid(),
           'start':start_ticks(os.getpid()),'uid':os.getuid(),
           'workspace':str(Path(config['workspace']).resolve()),'accepting':True,
           'pendingTurns':0,'identities':[]}
    state=ROOT/'var/executor/launches';state.mkdir(mode=0o700,exist_ok=True)
    receipt=state/(owner_id+'.json')
    with receipt.open('w') as output:
        receipt.chmod(0o600);json.dump(owner,output);output.flush();os.fsync(output.fileno())
    subprocess.Popen([sys.executable,str(Path(__file__).with_name('lifecycle.py')),str(receipt)],
                     stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
    def terminate(signum,frame):raise SystemExit(128+signum)
    signal.signal(signal.SIGTERM,terminate);signal.signal(signal.SIGINT,terminate)
    child=None
    try:
        child=launch_executor(command(sys.argv[1:],owner=owner))
        for _ in range(100):
            container=inspect_owned(owner)
            if container:
                with locked(receipt) as record:record['containerId']=container['Id']
                break
            if child.poll() is not None:break
            time.sleep(.05)
        raise SystemExit(proxy(child,receipt))
    finally:
        cleanup(owner)
        stop_owned_executor(child)
