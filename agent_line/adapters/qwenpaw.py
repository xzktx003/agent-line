"""Transport for the official Agent Line QwenPaw plugin execution bridge."""

from urllib.parse import urlsplit
import httpx
from .sim import segment


def configuration_marker(headers):
    """Only the explicit model-unavailable marker changes query classification."""
    if headers is None:
        return None
    value = headers.get('X-Agent-Line-Configuration')
    return value if value == 'model_unavailable' else None


def submission_marker(headers):
    """Only an exact pre-acceptance refusal is a corrected-resubmit candidate."""
    if headers is None:
        return None
    value = headers.get('X-Agent-Line-Submission')
    return value if value == 'rejected_unaccepted' else None


def query_failure_hint(status_code=None, retry_after=None, configuration=None, submission=None):
    """Bounded transport hints influence polling only, never task authority.

    Authentication and an explicit missing model are configuration blocks.
    They do not choose another model or execution path. A precise rejection
    before acceptance keeps its reason; unknown, timeout and 5xx do not.
    """
    if configuration == 'model_unavailable':
        return {'pollHint': 'model_missing'}
    if (submission == 'rejected_unaccepted' and type(status_code) is int
            and status_code in (409, 422)):
        return {'pollHint': 'submission_rejected'}
    reason = ('rate_limited' if status_code == 429 else 'authentication'
              if status_code in (401, 403) else 'unavailable'
              if status_code is None or type(status_code) is int and status_code >= 500 else 'unknown')
    result = {'pollHint': reason}
    if reason == 'rate_limited' and isinstance(retry_after, str):
        value = retry_after.strip()
        if value.isascii() and value.isdigit() and len(value) <= 8:
            result['pollAfterSeconds'] = max(1, min(30, int(value)))
    return result


class QwenPawExecutionGateway:
    def __init__(self, base_url, user_token, service_token, *, client=None):
        url = urlsplit(base_url)
        if (
            url.scheme not in ("http", "https")
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("Trusted QwenPaw origin required")
        if any(
            not isinstance(token, str) or not token.strip()
            for token in (user_token, service_token)
        ):
            raise ValueError("Both host and execution service credentials required")
        self.base_url = base_url.rstrip("/") + "/api/agent-line/executions"
        self.headers = {
            "Authorization": "Bearer " + user_token,
            "X-Agent-Line-Service-Token": service_token,
        }
        self.client = client or httpx.Client(timeout=30, follow_redirects=False)
        self.owns_client = client is None

    def close(self):
        if self.owns_client:
            self.client.close()

    @staticmethod
    def result(response, task_id):
        if response.status_code not in (200, 202):
            hint = query_failure_hint(
                response.status_code, response.headers.get("Retry-After"),
                configuration_marker(response.headers),
                submission_marker(response.headers))
            if hint.get('pollHint') == 'submission_rejected':
                return {
                    'taskId': task_id, 'status': 'rejected', 'identity': {},
                    'accepted': False, 'terminalConfirmed': False,
                    'resourcesReleased': False,
                    'rejectionReason': 'submission_rejected_unaccepted', **hint}
            return {"status": "unknown", **hint}
        try:
            result = response.json()
        except ValueError:
            return {"status": "unknown"}
        if not isinstance(result, dict) or result.get("taskId") != task_id:
            return {"status": "unknown"}
        result = dict(result)
        result.pop("pollHint", None)
        result.pop("pollAfterSeconds", None)
        return result

    def submit(self, *, task_id, role_session_id, instruction, resubmit_proof=None):
        segment(task_id)
        # No retries, including authentication errors: the dispatcher retains intent.
        response = self.client.post(
            self.base_url,
            headers=self.headers,
            json={
                "taskId": task_id,
                "roleSessionId": role_session_id,
                "instruction": instruction,
            },
            follow_redirects=False,
        )
        return self.result(response, task_id)

    def observe(self, task_id):
        response = self.client.get(
            self.base_url + "/" + segment(task_id),
            headers=self.headers,
            follow_redirects=False,
        )
        return self.result(response, task_id)

    def cancel(self, task_id):
        response = self.client.post(
            self.base_url + "/" + segment(task_id) + "/cancel",
            headers=self.headers,
            json={},
            follow_redirects=False,
        )
        return self.result(response, task_id)
