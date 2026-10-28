"""Trusted, bounded artifact verification; never trusts model success claims."""
import hashlib
import json
import os
import re
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .adapters.sim import segment


@dataclass(frozen=True)
class ArtifactPolicy:
    """Deployment/test policy, not expected values supplied by the executing model."""
    relative_path: str
    expected_sha256: str
    exact_size: int | None = None
    max_bytes: int = 10_000_000

    def __post_init__(self):
        path=self.relative_path
        if (not isinstance(path,str) or not path or '\\' in path or '\x00' in path
                or PurePosixPath(path).is_absolute()
                or any(part in ('','..','.') for part in path.split('/'))):
            raise ValueError('Artifact must have a strict relative path')
        if not isinstance(self.expected_sha256,str) or not re.fullmatch(r'[0-9a-f]{64}',self.expected_sha256):
            raise ValueError('Trusted expected SHA256 required')
        if type(self.max_bytes) is not int or not 1<=self.max_bytes<=100_000_000:
            raise ValueError('Invalid artifact read limit')
        if self.exact_size is not None and (type(self.exact_size) is not int or not 0<=self.exact_size<=self.max_bytes):
            raise ValueError('Invalid exact artifact size')


def _directory(path):
    """Open each component without following symlinks, retaining an anchored FD."""
    absolute=Path(path).absolute()
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        for part in absolute.parts[1:]:
            if part in ('.','..'):raise ValueError('Unnormalized directory')
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd);fd=child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _check(root_fd,policy,*,capture=None):
    if capture is not None and (type(capture) is not bytearray or capture
                                or policy.max_bytes > 1_048_576):
        raise ValueError('Artifact capture requires an empty bounded buffer')
    result={'kind':'artifact_sha256','path':policy.relative_path,
            'expectedSha256':policy.expected_sha256,'expectedSize':policy.exact_size,
            'maxBytes':policy.max_bytes,'status':'failed'}
    parent=os.dup(root_fd);file_fd=None
    try:
        parts=policy.relative_path.split('/')
        for part in parts[:-1]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
            os.close(parent);parent=child
        file_fd=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
        before=os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode):
            result['reason']='artifact_not_regular_file';return result
        if before.st_size>policy.max_bytes:
            result['reason']='artifact_exceeds_limit';return result
        digest=hashlib.sha256();size=0
        while True:
            chunk=os.read(file_fd,min(65536,policy.max_bytes-size+1))
            if not chunk:break
            size+=len(chunk)
            if size>policy.max_bytes:
                result['reason']='artifact_exceeds_limit';return result
            digest.update(chunk)
            if capture is not None:
                capture.extend(chunk)
        after=os.fstat(file_fd)
        current=os.stat(parts[-1],dir_fd=parent,follow_symlinks=False)
        result.update(actualSha256=digest.hexdigest(),actualSize=size)
        if not stat.S_ISREG(current.st_mode) or (current.st_dev,current.st_ino)!=(after.st_dev,after.st_ino):
            result['reason']='artifact_path_replaced_during_read'
        elif (before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):
            result['reason']='artifact_changed_during_read'
        elif result['actualSha256']!=policy.expected_sha256:
            result['reason']='sha256_mismatch'
        elif policy.exact_size is not None and size!=policy.exact_size:
            result['reason']='size_mismatch'
        else:
            result.update(status='passed',reason='exact_artifact_policy_matched')
        return result
    except OSError:
        result['reason']='artifact_unavailable_or_symlink'
        return result
    finally:
        if file_fd is not None:os.close(file_fd)
        os.close(parent)


def _task_scope(task,workspace):
    if task.get('executionStatus')!='succeeded' or task.get('resourcesReleased') is not True:
        raise ValueError('Execution must succeed and release owned resources before verification')
    identity=task.get('executionIdentity')
    if (not isinstance(identity,dict) or not identity
            or not all(isinstance(v,str) and v for v in identity.values())
            or not (identity.get('upstreamTaskId') or (identity.get('codexThreadId') and identity.get('codexTurnId')))):
        raise ValueError('Queryable native execution identity required')
    configured=task.get('payload',{}).get('workspace')
    if not configured or Path(configured).resolve()!=Path(workspace).resolve():
        raise ValueError('Workspace does not match persisted task binding')
    return dict(identity)


def _persist(evidence_dir,receipt):
    fd=_directory(evidence_dir)
    name=receipt['evidenceFile'];temporary='.'+name+'.tmp'
    opened=None
    try:
        opened=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
        data=(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n').encode()
        with os.fdopen(opened,'wb') as stream:
            opened=None
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        os.rename(temporary,name,src_dir_fd=fd,dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        if opened is not None:os.close(opened)
        try:os.unlink(temporary,dir_fd=fd)
        except FileNotFoundError:pass
        os.close(fd)


def verify_artifacts(store,task_id,*,workspace,policies,evidence_dir,evidence_file=None,verification_policy=None):
    """Read actual files and atomically store a receipt, without changing TaskStore."""
    segment(task_id)
    policies=list(policies)
    if not policies or any(not isinstance(p,ArtifactPolicy) for p in policies):
        raise ValueError('At least one trusted artifact policy required')
    if len({p.relative_path for p in policies})!=len(policies):
        raise ValueError('Duplicate artifact policies')
    if evidence_file is not None and (not isinstance(evidence_file,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,160}\.json',evidence_file)):
        raise ValueError('Invalid evidence filename')
    metadata={}
    if verification_policy is not None:
        from .verification_policy import validate_policy, policy_hash
        policy=validate_policy(verification_policy)
        metadata={'verificationPolicy':policy,'policyId':policy['policyId'],'policySha256':policy_hash(policy)}
    task=store.get(task_id)
    identity=_task_scope(task,workspace)
    fd=_directory(workspace)
    try:checks=[_check(fd,policy) for policy in policies]
    finally:os.close(fd)
    if _task_scope(store.get(task_id),workspace)!=identity:
        raise ValueError('Task execution identity changed during verification')
    receipt={'kind':'verification','schemaVersion':1,'taskId':task_id,
             'executionIdentity':identity,'checkedAt':time.time(),
             'status':'passed' if all(c['status']=='passed' for c in checks) else 'failed',
             'checks':checks,'scope':'Only the declared artifact byte/size policies; not full code or test acceptance',
             'evidenceFile':evidence_file or task_id+'-'+uuid.uuid4().hex+'.json',**metadata}
    _persist(evidence_dir,receipt)
    return receipt
