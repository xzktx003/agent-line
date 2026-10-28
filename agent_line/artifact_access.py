"""Read a currently matching artifact declared by a frozen verification receipt."""
import base64
import hashlib
import os
import sqlite3

from .store import canonical
from .verification import ArtifactPolicy, _check, _directory, _task_scope
from .verification_policy import validate_policy

_MAX_ARTIFACT_BYTES = 1_048_576


class ArtifactUnavailable(ValueError):
    """The requested verified artifact cannot be safely confirmed right now."""

    def __init__(self):
        super().__init__('已验证文件当前不可读取或校验不匹配')


class ArtifactTooLarge(ArtifactUnavailable):
    """The requested artifact exceeds the bounded read limit."""

    def __init__(self):
        super().__init__()
        self.args = ('文件超过允许读取上限（1 MiB）',)


def _verification_receipt(task):
    evidence = task.get('evidence')
    if not isinstance(evidence, list):
        raise ArtifactUnavailable()
    matches = [item for item in evidence if isinstance(item, dict) and item.get('kind') == 'verification']
    if len(matches) != 1:
        raise ArtifactUnavailable()
    return matches[0]


def _same_frozen_task(before, after, receipt):
    try:
        return (
            isinstance(after, dict)
            and after.get('id') == before.get('id')
            and after.get('executionStatus') == 'succeeded'
            and after.get('resourcesReleased') is True
            and after.get('verificationStatus') == before.get('verificationStatus')
            and after.get('executionIdentity') == before.get('executionIdentity')
            and canonical(after.get('payload')) == canonical(before.get('payload'))
            and canonical(_verification_receipt(after)) == canonical(receipt)
        )
    except (TypeError, ValueError, KeyError):
        return False


def read_artifact(verifier, task, check_index, workspace):
    """Return bounded base64 only when the current file still matches its receipt."""
    try:
        if (not isinstance(task, dict) or type(check_index) is not int
                or not 0 <= check_index <= 39):
            raise ArtifactUnavailable()
        task_id = task.get('id')
        if not isinstance(task_id, str) or not task_id or len(task_id) > 160:
            raise ArtifactUnavailable()
        identity = _task_scope(task, workspace)
        if task.get('verificationStatus') not in ('passed', 'failed'):
            raise ArtifactUnavailable()
        payload = task.get('payload')
        if not isinstance(payload, dict):
            raise ArtifactUnavailable()
        frozen = validate_policy(payload.get('verificationPolicy'))
        receipt = _verification_receipt(task)
        if receipt.get('status') != task.get('verificationStatus'):
            raise ArtifactUnavailable()
        verifier._validate(receipt, task, frozen, receipt.get('evidenceFile'))
        checks = receipt.get('checks')
        artifacts = frozen.get('artifacts')
        if (not isinstance(checks, list) or not isinstance(artifacts, list)
                or check_index >= len(artifacts) or check_index >= len(checks)):
            raise ArtifactUnavailable()
        artifact = artifacts[check_index]
        check = checks[check_index]
        if (not isinstance(check, dict) or check.get('kind') != 'artifact_sha256'
                or check.get('status') != 'passed'
                or check.get('path') != artifact['path']
                or check.get('expectedSha256') != artifact['sha256']
                or check.get('actualSha256') != artifact['sha256']
                or type(check.get('actualSize')) is not int
                or check.get('actualSize') < 0
                or (artifact['exactSize'] is not None
                    and check['actualSize'] != artifact['exactSize'])):
            raise ArtifactUnavailable()
        if ((artifact['exactSize'] is not None and artifact['exactSize'] > _MAX_ARTIFACT_BYTES)
                or check['actualSize'] > _MAX_ARTIFACT_BYTES):
            raise ArtifactTooLarge()
        root = _directory(workspace)
        captured = bytearray()
        try:
            bounded = ArtifactPolicy(
                artifact['path'], artifact['sha256'], artifact['exactSize'],
                min(artifact['maxBytes'], _MAX_ARTIFACT_BYTES))
            current = _check(root, bounded, capture=captured)
        finally:
            os.close(root)
        if current.get('reason') == 'artifact_exceeds_limit':
            if artifact['maxBytes'] > _MAX_ARTIFACT_BYTES:
                raise ArtifactTooLarge()
            raise ArtifactUnavailable()
        if (current.get('status') != 'passed'
                or current.get('actualSha256') != check['actualSha256']
                or current.get('actualSize') != check['actualSize']
                or len(captured) != check['actualSize']
                or hashlib.sha256(captured).hexdigest() != check['actualSha256']):
            raise ArtifactUnavailable()
        latest = verifier.store.get(task_id)
        if (not _same_frozen_task(task, latest, receipt)
                or _task_scope(latest, workspace) != identity):
            raise ArtifactUnavailable()
        return {
            'source': 'verified_current_artifact',
            'readOnly': True,
            'taskId': task_id,
            'executionIdentity': identity,
            'checkIndex': check_index,
            'path': artifact['path'],
            'sha256': check['actualSha256'],
            'size': check['actualSize'],
            'encoding': 'base64',
            'contentBase64': base64.b64encode(captured).decode('ascii'),
        }
    except ArtifactUnavailable:
        raise
    except (ValueError, OSError, TypeError, KeyError, OverflowError, AttributeError,
            sqlite3.Error, RecursionError):
        raise ArtifactUnavailable() from None
