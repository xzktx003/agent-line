"""Approved-content writes via official QwenPaw memory APIs.

No execution lifecycle changes, model inference, private storage writes or
unconditional overwrite. The caller owns the durable approval/submission ledger
and serializes this role/task scope; QwenPaw's file API has no compare-and-swap.
After a pending receipt, call reconcile(), never blindly retry write_approved().
"""
from dataclasses import dataclass
import hashlib
import re
from urllib.parse import urlsplit

import httpx
from .sim import segment


@dataclass(frozen=True)
class MemoryApproval:
    """Supplied by the trusted review service, never deserialized from model output."""
    task_id: str
    content_sha256: str
    decision_id: str


def content_hash(content):
    if not isinstance(content, str) or not content.strip() or len(content.encode()) > 128_000:
        raise ValueError('Memory content must contain 1 to 128000 bytes')
    # QwenPaw's official GET strips outer whitespace.
    return hashlib.sha256(content.strip().encode()).hexdigest()


class QwenPawMemoryClient:
    def __init__(self, base_url, user_token, *, role_agent_id, client=None, deletion_service_token=None, native_write_rejections=False):
        url = urlsplit(base_url)
        if (url.scheme not in ('http', 'https') or not url.hostname or url.username
                or url.password or url.query or url.fragment or url.path not in ('', '/')):
            raise ValueError('Trusted QwenPaw origin required')
        if not isinstance(user_token, str) or not user_token.strip():
            raise ValueError('Authenticated host token required')
        self.base_url = base_url.rstrip('/') + '/api/workspace/memory/'
        self.origin = base_url.rstrip('/')
        if deletion_service_token is not None and (
                not isinstance(deletion_service_token, str) or len(deletion_service_token) < 32
                or any(c.isspace() for c in deletion_service_token)):
            raise ValueError('Independent memory deletion service token required')
        self.deletion_service_token = deletion_service_token
        if type(native_write_rejections) is not bool:
            raise ValueError('Trusted native rejection policy must be boolean')
        self.role = segment(role_agent_id)
        # Only the trusted deployment factory opts in after native host checks.
        # Arbitrary origins/custom transports keep the original pending policy.
        self.direct_native_origin = native_write_rejections and url.scheme == 'http' and url.hostname == '127.0.0.1' and url.port is not None
        self.headers = {'Authorization': 'Bearer '+user_token, 'X-Agent-Id': self.role}
        self.client = client or httpx.Client(timeout=30, follow_redirects=False, trust_env=not native_write_rejections)
        self.owns_client = client is None

    def close(self):
        if self.owns_client:self.client.close()

    def observe_skill_candidate(self, name, expected_hash):
        """Read the bound role's native skill detail without installing/enabling."""
        if (not isinstance(name, str) or len(name) > 64
                or re.fullmatch(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)*', name) is None
                or not isinstance(expected_hash, str)
                or re.fullmatch(r'[a-f0-9]{64}', expected_hash) is None):
            raise ValueError('Candidate name and digest required')
        result = {'state': 'unknown', 'name': name, 'roleAgentId': self.role,
                  'expectedContentSha256': expected_hash, 'actualContentSha256': None,
                  'enabled': None, 'contentMatches': None, 'safeToCreate': None}
        try:
            response = self.client.get(self.origin + '/api/skills/' + name,
                                       headers=self.headers, follow_redirects=False)
            # Native 404 may hide an enabled manifest entry with missing files.
            if response.status_code == 404:
                return {**result, 'state': 'not_observed'}
            if response.status_code != 200:
                return result
            body = response.json()
            if (not isinstance(body, dict) or body.get('name') != name
                    or type(body.get('enabled')) is not bool
                    or not isinstance(body.get('content'), str)
                    or len(body['content'].encode()) > 200_000):
                return result
            digest = hashlib.sha256(body['content'].encode()).hexdigest()
            matches = digest == expected_hash
            return {**result, 'state': ('disabled_match' if not body['enabled'] else 'enabled_match')
                    if matches else 'content_conflict', 'actualContentSha256': digest,
                    'enabled': body['enabled'], 'contentMatches': matches}
        except (httpx.HTTPError, ValueError):
            return result

    def _path(self, task_id):
        return 'agent-line-task-' + segment(task_id) + '.md'

    def _read(self, path):
        try:
            result = self.client.get(self.base_url + path, params={'section':'digest'},
                                     headers=self.headers, follow_redirects=False)
            if result.status_code == 404:return 'missing', None
            if result.status_code != 200:return 'unknown', None
            body = result.json()
            if not isinstance(body, dict) or not isinstance(body.get('content'), str):
                return 'unknown', None
            return 'found', body['content']
        except (httpx.TransportError, ValueError):
            return 'unknown', None

    def _result(self, task_id, content, status, reason):
        return {'status':status, 'reason':reason, 'taskId':task_id,
                'roleAgentId':self.role, 'memoryPath':self._path(task_id),
                'contentSha256':content_hash(content),
                'nextAction':'reconcile_only' if status == 'pending' else
                             'explicit_memory_retry' if status == 'failed' else
                             'review' if status == 'needs_review' else 'none'}

    def reconcile(self, task_id, content):
        """Read-only reconciliation, including after timeout or process restart."""
        path = self._path(task_id)
        content_hash(content)
        state, existing = self._read(path)
        if state == 'found':
            if existing.strip() == content.strip():
                return self._result(task_id,content,'committed','official_readback_matches')
            return self._result(task_id,content,'needs_review','existing_content_conflict')
        return self._result(task_id,content,'pending','readback_missing' if state == 'missing' else 'readback_unknown')

    def write_approved(self, task_id, content, *, approval=None):
        """One submission opportunity, under the caller's durable admission lease."""
        path = self._path(task_id)
        digest = content_hash(content)
        if (not isinstance(approval, MemoryApproval) or approval.task_id != task_id
                or approval.content_sha256 != digest
                or not isinstance(approval.decision_id, str) or not approval.decision_id.strip()):
            return self._result(task_id,content,'needs_review','matching_approval_required')
        state, existing = self._read(path)
        if state == 'found':
            if existing.strip() == content.strip():
                return self._result(task_id,content,'committed','already_matches_approved_content')
            return self._result(task_id,content,'needs_review','existing_content_conflict')
        if state == 'unknown':
            return self._result(task_id,content,'pending','prewrite_read_unknown')
        rejected = False
        try:
            response = self.client.put(self.base_url + path, params={'section':'digest'},
                                       headers=self.headers, json={'content':content},
                                       follow_redirects=False)
            # In the pinned native handler all in-handler exceptions become500;
            # these JSON responses can only reject before its file write begins.
            if self.direct_native_origin and response.status_code in (401,403,404,422):
                try:
                    body=response.json()
                    rejected=(response.headers.get('content-type','').split(';',1)[0].strip().lower()=='application/json'
                              and isinstance(body,dict) and isinstance(body.get('detail'),(str,list))
                              and bool(body['detail']))
                except ValueError:
                    pass
        except httpx.TransportError:
            pass
        result=self.reconcile(task_id,content)
        if rejected and result['status']=='pending' and result['reason']=='readback_missing':
            return self._result(task_id,content,'failed','official_write_rejected')
        return result


    def _deletion_request(self, task_id, expected_hash, *, decision_id=None, actor=None):
        task_id = segment(task_id)
        if not self.deletion_service_token:
            raise ValueError('Native memory deletion is not configured')
        if not isinstance(expected_hash, str) or not re.fullmatch(r'[a-f0-9]{64}', expected_hash):
            raise ValueError('Expected memory SHA-256 required')
        result = {'roleAgentId': self.role, 'taskId': task_id, 'contentHash': expected_hash,
                  'status': 'deleting', 'reason': 'native_deletion_unknown',
                  'fileAbsent': False, 'indexSourceAbsent': False, 'roleBlocked': None}
        headers = {**self.headers, 'X-Agent-Line-Memory-Token': self.deletion_service_token}
        path = self.origin + '/api/agent-line/memory/' + self.role + '/' + task_id
        try:
            if decision_id is None:
                response = self.client.get(path + '/deletion',
                    params={'expectedContentSha256': expected_hash}, headers=headers, follow_redirects=False)
            else:
                segment(decision_id)
                if not isinstance(actor, str) or not actor.strip():
                    raise ValueError('Trusted deletion actor required')
                response = self.client.post(path + '/delete', headers=headers, json={
                    'expectedContentSha256': expected_hash, 'decisionId': decision_id, 'actor': actor},
                    follow_redirects=False)
            if response.status_code not in (200, 202):
                return result
            body = response.json()
            if (not isinstance(body, dict) or body.get('roleAgentId') != self.role
                    or body.get('taskId') != task_id or body.get('contentHash') != expected_hash
                    or body.get('status') not in ('deleted', 'deleting', 'needs_review')):
                return result
            result.update(status=body['status'],
                          fileAbsent=body.get('fileAbsent') is True,
                          indexSourceAbsent=body.get('indexSourceAbsent') is True,
                          roleBlocked=body.get('roleBlocked') if type(body.get('roleBlocked')) is bool else None)
            if result['status'] == 'deleted' and not (result['fileAbsent'] and result['indexSourceAbsent']):
                result['status'] = 'deleting'
            result['reason'] = ('native_absence_verified' if result['status'] == 'deleted' else
                                'native_deletion_needs_review' if result['status'] == 'needs_review' else
                                'index_sync_pending' if result['fileAbsent'] else 'native_deletion_unknown')
            return result
        except (httpx.TransportError, ValueError):
            return result

    def delete_checked(self, task_id, expected_hash, decision_id, actor):
        return self._deletion_request(task_id, expected_hash, decision_id=decision_id, actor=actor)

    def observe_deletion(self, task_id, expected_hash):
        return self._deletion_request(task_id, expected_hash)
