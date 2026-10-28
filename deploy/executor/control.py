#!/usr/bin/env python3
"""Trusted service API; model cannot access this host-side control directory."""
import argparse
import json
import os
import re
import stat
import time
import math
from pathlib import Path
from lifecycle import STATE, cleanup, locked, inspect_owned
from launcher import APPROVAL_METHODS, bounded_identity, rpc_key


def locate(thread_id,turn_id,workspace):
    matches=[]
    for path in STATE.glob('*.json'):
        if path.stat().st_uid!=os.getuid() or path.stat().st_mode & 0o077:continue
        record=json.loads(path.read_text())
        if record.get('workspace')!=str(Path(workspace).resolve()):continue
        if any(item.get('codexThreadId')==thread_id and item.get('codexTurnId')==turn_id for item in record.get('identities',[])):
            matches.append((path,record))
    if len(matches)!=1:raise RuntimeError('Native identity has no unique owned container')
    return matches[0]



def _launcher_snapshot(pid):
    """None means the proc directory itself is absent; all other errors propagate."""
    path=Path('/proc')/str(pid)
    try:
        directory=os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    try:
        before=os.fstat(directory)
        fd=os.open('stat',os.O_RDONLY|os.O_NOFOLLOW,dir_fd=directory)
        with os.fdopen(fd,'r') as stream:
            value=stream.read(16385)
        if len(value)>16384:raise ValueError('Oversized proc stat')
        close=value.rfind(')');first=value.find('(')
        if first<1 or close<=first or value[:first].strip()!=str(pid):
            raise ValueError('Malformed proc identity')
        fields=value[close+1:].split()
        if len(fields)<20 or fields[0] not in {'R','S','D','Z','T','t','X','x','K','W','P','I'}:
            raise ValueError('Malformed proc state')
        if not re.fullmatch(r'[0-9]+',fields[19]):raise ValueError('Malformed proc birth')
        after=os.stat(path,follow_symlinks=False)
        if (before.st_dev,before.st_ino,before.st_uid)!=(after.st_dev,after.st_ino,after.st_uid):
            raise ValueError('Proc identity changed during observation')
        return {'state':fields[0],'start':fields[19],'uid':before.st_uid,
                'inode':before.st_ino,'device':before.st_dev}
    finally:
        os.close(directory)


def launcher_exit_proof(record):
    owner=record.get('owner');container=record.get('containerId')
    pid=record.get('pid');start=record.get('start');uid=record.get('uid')
    if (type(pid) is not int or pid<=0 or type(uid) is not int or uid!=os.getuid()
            or not isinstance(start,str) or re.fullmatch(r'[0-9]+',start) is None
            or not isinstance(owner,str) or re.fullmatch('[0-9a-f]{32}',owner) is None
            or not isinstance(container,str) or re.fullmatch('[0-9a-f]{64}',container) is None):
        return None
    try:
        first=_launcher_snapshot(pid);second=_launcher_snapshot(pid)
        if first!=second:return None
        if first is None:state='absent'
        elif first['start']!=start:
            # A new process has this PID. Never signal it, even if owned by us.
            state='pid_reused'
        else:return None  # Includes zombies: original launcher is not reaped yet.
        observed=time.time()
        if not math.isfinite(observed) or observed<=0:return None
        return {'schemaVersion':1,'source':'procfs-launcher-exit','owner':owner,
                'containerId':container,'pid':pid,'startTicks':start,'uid':uid,
                'state':state,'observedAt':observed}
    except (OSError,ValueError,KeyError,TypeError):
        return None


def _exit_binding(record):
    return {key:record.get(key) for key in ('owner','containerId','pid','start','uid','exitReceipt','identities','workspace')}


def proof(thread_id,turn_id,workspace,*,stop=False):
    path,record=locate(thread_id,turn_id,workspace)
    if stop and not record.get('exitReceipt'):
        with locked(path) as current:
            others=[item for item in current.get('identities',[]) if not item.get('nativeTerminal') and (item['codexThreadId'],item['codexTurnId'])!=(thread_id,turn_id)]
            if current.get('pendingTurns') or others:raise RuntimeError('Container has other or pending native work; refusing stop')
            current['accepting']=False
        cleanup(record)
    with locked(path) as current:
        snapshot=dict(current)
    # Never retain the receipt flock while examining the launcher: its finally
    # must be free to finish cleanup and exit before a later poll can prove exit.
    launcher=launcher_exit_proof(snapshot)
    with locked(path) as current:
        receipt=current.get('exitReceipt')
        if _exit_binding(current)!=_exit_binding(snapshot):launcher=None
        container_proved=(isinstance(receipt,dict) and receipt.get('owner')==current.get('owner')
            and receipt.get('containerId')==current.get('containerId')
            and receipt.get('source')=='docker-inspect-before-remove'
            and receipt.get('running') is False and type(receipt.get('pid')) is int and receipt['pid']==0
            and receipt.get('status') in ('exited','dead') and bool(receipt.get('finishedAt')))
        return {'schemaVersion':1,'codexThreadId':thread_id,'codexTurnId':turn_id,
                'owner':current['owner'],'containerId':current.get('containerId'),
                'resourcesReleased':bool(container_proved and launcher),
                'exitReceipt':receipt,'launcherExitProof':launcher}


def approvals(thread_id, turn_id, workspace):
    if not bounded_identity(thread_id) or not bounded_identity(turn_id):
        raise RuntimeError('Bounded native identity required')
    path, _ = locate(thread_id, turn_id, workspace)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError('Private approval receipt required')
    with locked(path) as current:
        if (not isinstance(current.get('owner'), str) or re.fullmatch('[0-9a-f]{32}',current['owner']) is None
                or not isinstance(current.get('containerId'), str) or re.fullmatch('[0-9a-f]{64}',current['containerId']) is None):
            raise RuntimeError('Exact owned container identity required')
        identities = current.get('identities', [])
        active = [i for i in identities if i.get('nativeTerminal') is not True]
        exact = [i for i in identities if i.get('codexThreadId') == thread_id and i.get('codexTurnId') == turn_id]
        if (current.get('workspace') != str(Path(workspace).resolve()) or current.get('exitReceipt')
                or current.get('accepting') is not True or current.get('pendingTurns') != 0
                or current.get('approvalBindingsUnavailable') or len(exact) != 1
                or exact[0].get('nativeTerminal') is not False or active != exact):
            raise RuntimeError('Approval execution identity unavailable')
        container = inspect_owned(current)
        if (not container or container.get('Id') != current.get('containerId')
                or container.get('State', {}).get('Running') is not True
                or type(container['State'].get('Pid')) is not int or container['State']['Pid'] <= 0):
            raise RuntimeError('Approval requires running owned container')
        entries = current.get('approvalBindings', [])
        if not isinstance(entries, list) or len(entries) > 256:
            raise RuntimeError('Invalid approval bindings')
        result = []; seen_ids = set(); seen_items = set()
        for entry in entries:
            if (not isinstance(entry, dict) or set(entry) != {'requestId','method','threadId','turnId','itemId','resolved'}
                    or entry['method'] not in APPROVAL_METHODS or rpc_key(entry['requestId']) is None
                    or type(entry['resolved']) is not bool
                    or not all(bounded_identity(entry.get(k)) for k in ('threadId','turnId','itemId'))):
                raise RuntimeError('Invalid approval binding')
            if entry['resolved']:
                continue
            if entry['threadId'] != thread_id or entry['turnId'] != turn_id:
                raise RuntimeError('Unresolved approval belongs to other native work')
            key = rpc_key(entry['requestId']); item = entry['itemId']
            if key in seen_ids or item in seen_items:
                raise RuntimeError('Ambiguous native approval')
            seen_ids.add(key); seen_items.add(item)
            result.append({k:v for k,v in entry.items() if k != 'resolved'})
        return {'schemaVersion':1,'codexThreadId':thread_id,'codexTurnId':turn_id,
                'owner':current['owner'],'containerId':current['containerId'],'approvals':result}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['observe','stop','approvals'])
    parser.add_argument('--thread',required=True);parser.add_argument('--turn',required=True);parser.add_argument('--workspace',required=True)
    args=parser.parse_args()
    print(json.dumps(approvals(args.thread,args.turn,args.workspace) if args.action=='approvals'
                     else proof(args.thread,args.turn,args.workspace,stop=args.action=='stop')))
