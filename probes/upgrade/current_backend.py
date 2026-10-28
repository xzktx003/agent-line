"""Reload only the verified main adapter after an idle, guarded backup.

No role/model/configuration change, proof replacement or model execution.
Existing state is observation-only; never start a duplicate process on replay.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
import httpx
from agent_line.store import TaskStore
from probes.executor.inflight_adapter_restart import original_adapter, same_process, stop_adapter, adapter_identity, listeners
from probes.qwenpaw_memory.native_roundtrip import save

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'var/backend-rollout-20261022'
SERVICE = ROOT / 'var/adapter/service.json'


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def db_digest(path, table):
    assert table in ('tasks', 'events')
    with sqlite3.connect(Path(path).as_uri()+'?mode=ro', uri=True) as db:
        values = db.execute('SELECT * FROM '+table+' ORDER BY rowid').fetchall()
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def deploy():
    if (OUT/'state.json').exists():
        state=json.loads((OUT/'state.json').read_text())
        if state.get('phase') != 'preparing' or set(state) != {'phase','startedAt'}:
            print(json.dumps({'mode':'existing_state_observation','phase':state['phase'],
                              'processLive':same_process(state['newProcess']) if 'newProcess' in state else False}))
            return
    OUT.mkdir(mode=0o700, exist_ok=True)
    state={'phase':'preparing','startedAt':time.time()}
    def persist():
        save(OUT/'state.json',state)
    persist()
    original=original_adapter()
    environment=subprocess_environment(original['pid'])
    cfg=json.loads(SERVICE.read_text())
    database=Path(cfg['database']); database=(database if database.is_absolute() else SERVICE.parent/database).resolve()
    store=TaskStore(database)
    initial=store.maintenance_status()
    assert not initial['guarded'] and initial['activeExecutions']==0 and initial['queuedTasks']==0, 'main_not_idle'
    with store.connect() as db:
        assert db.execute('SELECT count(*) FROM workspace_leases').fetchone()[0]==0
        state['taskCount']=db.execute('SELECT count(*) FROM tasks').fetchone()[0]
    assert state['taskCount']==19
    state['originalProcess']=original
    state['taskDigest']=db_digest(database,'tasks'); state['eventDigest']=db_digest(database,'events')
    native_config=ROOT/'var/qwenpaw/config.json'
    profiles=json.loads(native_config.read_text())['agents']['profiles']
    assert len(profiles)==9
    protected=[SERVICE,native_config,ROOT/'var/qwenpaw/agent-line-process.json']
    protected += [Path(profile['workspace_dir'])/'agent.json' for profile in profiles.values()]
    state['protectedHashes']={str(path):file_hash(path) for path in protected}
    # Find only the original configuration graph and its declared SQLite files.
    files, databases, pending={SERVICE}, {database}, [SERVICE]
    while pending:
        current=pending.pop()
        try:
            payload=json.loads(current.read_text())
        except (ValueError,UnicodeError):
            continue
        def walk(value):
            if isinstance(value,dict):
                for item in value.values(): walk(item)
            elif isinstance(value,list):
                for item in value: walk(item)
            elif isinstance(value,str) and value.endswith(('.json','.sqlite')):
                path=Path(value); path=(path if path.is_absolute() else current.parent/path).resolve()
                if not path.is_relative_to(ROOT) or not path.is_file(): return
                if path.suffix=='.sqlite': databases.add(path)
                elif path not in files: files.add(path); pending.append(path)
        walk(payload)
    state['configurationHashes']={str(path):file_hash(path) for path in files}
    state['maintenanceOwner']=store.enter_maintenance(); state['phase']='guarded'; persist()
    stop_adapter(original,time.time()+55)
    assert not listeners()
    state['phase']='stopped'; persist()
    backup=OUT/'before'; backup.mkdir(mode=0o700)
    for index,path in enumerate(sorted(files)):
        target=backup/f'configuration-{index}.json'; target.write_bytes(path.read_bytes()); target.chmod(0o600)
    state['databaseBackups']=[]
    for index,path in enumerate(sorted(databases)):
        destination=backup/f'database-{index}.sqlite'
        with destination.open('xb'): pass
        destination.chmod(0o600)
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as source:
            target=sqlite3.connect(destination)
            try:
                source.backup(target)
                assert target.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
            finally: target.close()
        state['databaseBackups'].append({'source':str(path),'backup':str(destination),'sha256':file_hash(destination)})
    assert db_digest(database,'tasks')==state['taskDigest'] and db_digest(database,'events')==state['eventDigest']
    state['phase']='backed_up'; persist()
    assert not listeners()
    state['phase']='start_intent'; persist()
    log_path=OUT/'adapter.log'
    fd=os.open(log_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'ab',buffering=0) as log:
        process=subprocess.Popen(original['argv'],cwd=ROOT,env=environment,stdin=subprocess.DEVNULL,
                                 stdout=log,stderr=log,start_new_session=True)
    state['newProcess']=adapter_identity(process.pid); state['phase']='started'; persist()
    token=next(key for key,scopes in cfg['tokens'].items() if 'native-memory-roundtrip' in scopes)
    auth=json.loads((SERVICE.parent/cfg['authoringFile']).read_text())
    credential=next(item for item in auth['credentials'] if item['expiresAt']>time.time()+30)
    project=credential['projectScopes'][0]
    with httpx.Client(timeout=20) as client:
        deadline=time.time()+45
        while time.time()<deadline:
            assert same_process(state['newProcess']), 'new_adapter_exited'
            try:
                response=client.get('http://127.0.0.1:3193/extension/v1/health',headers={'Authorization':'Bearer '+token})
                if response.status_code==200: break
            except httpx.TransportError: pass
            time.sleep(.25)
        else: raise RuntimeError('new_adapter_listener_unconfirmed')
        health=response.json()
        assert health['readyForExecution'] is False and health['controlledExecution']=='unknown'
        headers={'Authorization':'Bearer '+credential['token']}
        base='http://127.0.0.1:3193/extension/v1/authoring/projects/'+project
        archive=client.get(base+'/archives',headers=headers)
        search=client.get(base+'/workflows',headers=headers,params={'limit':1,'search':'部署验证无匹配'})
        assert archive.status_code==200 and isinstance(archive.json()['archives'],list)
        assert search.status_code==200 and search.json()['search']=='部署验证无匹配'
        assert client.get(base+'/archives').status_code==401
        state['apiChecks']={'archiveStatus':archive.status_code,'searchStatus':search.status_code,'anonymousStatus':401}
    assert db_digest(database,'tasks')==state['taskDigest'] and db_digest(database,'events')==state['eventDigest']
    assert all(file_hash(path)==digest for path,digest in state['protectedHashes'].items())
    assert all(file_hash(path)==digest for path,digest in state['configurationHashes'].items())
    with store.connect() as db: assert db.execute('SELECT count(*) FROM workspace_leases').fetchone()[0]==0
    store.leave_maintenance(state['maintenanceOwner'])
    state['phase']='complete'; state['completedAt']=time.time(); persist()
    report={'phase':state['phase'],'mainAdapterReloaded':True,'host':original['host'],'port':3193,
            'oldPid':original['pid'],'newPid':state['newProcess']['pid'],'tasksPreserved':state['taskCount'],
            'eventsPreserved':True,'leases':0,'nativeAgentsPreserved':9,'qwenpawNotRestarted':True,
            'configurationFilesUnchanged':len(files),'databasesBackedUp':len(databases),
            'newArchiveAndSearchApi':state['apiChecks'],'controlledExecution':'unknown','readyForExecution':False,
            'maintenanceReleased':True,'newModelCalls':0}
    save(OUT/'report.json',report); print(json.dumps(report,ensure_ascii=False))


def subprocess_environment(pid):
    import psutil
    return psutil.Process(pid).environ()


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--deploy',action='store_true'); args=parser.parse_args()
    if args.deploy: deploy()
    else:
        path=OUT/'report.json'
        print(path.read_text() if path.exists() else json.dumps({'phase':'not_started','modelCalls':0}))
