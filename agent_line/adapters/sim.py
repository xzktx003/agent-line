"""Sim v2 transport. Caller durably reserves run/context IDs before mutation.

This client never retries a mutation and never interprets a missing run as proof
that a submission did not execute. HTTP receipts are not workflow results.
"""

import re
import json
import math
import time
from datetime import datetime
from urllib.parse import urlsplit

import httpx


class UpstreamFailure(RuntimeError):
    def __init__(self, status_code, operation):
        self.status_code = status_code
        super().__init__(f"Sim {operation} rejected (HTTP {status_code})")


class SubmissionUncertain(RuntimeError):
    def __init__(self, run_id, context_id=None):
        self.run_id = run_id
        self.context_id = context_id
        super().__init__(
            "Sim operation outcome unknown; reconcile the original identity"
        )


def segment(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value):
        raise ValueError("Invalid upstream identifier")
    return value


def host_role_shared_across_projects(bindings, role_agent_id, project_id):
    """Native ReMe recall and Codex history follow the host role, not the project label."""
    if not isinstance(bindings, dict) or not isinstance(role_agent_id, str) or not isinstance(project_id, str):
        return False
    return any(isinstance(binding, dict)
               and binding.get('roleAgentId') == role_agent_id
               and isinstance(binding.get('projectId'), str)
               and binding.get('projectId') != project_id
               for binding in bindings.values())


class SimClient:
    def __init__(self, base_url, api_key, *, client=None):
        url = urlsplit(base_url)
        if (
            url.scheme not in ("http", "https")
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("Use a trusted Sim origin without embedded credentials")
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("Sim API key required")
        self.base_url = base_url.rstrip("/")
        self._key = api_key
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(30), follow_redirects=False
        )

    def close(self):
        if self._owns_client:
            self._client.close()

    def _mutate(self, path, run_id, body, context_id=None, run_header=False, *, workflow_id):
        headers = {"x-api-key": self._key}
        if run_header:
            headers["X-Run-Id"] = run_id
        try:
            response = self._client.post(
                self.base_url + path, json=body, headers=headers, follow_redirects=False
            )
        except httpx.TransportError as exc:
            raise SubmissionUncertain(run_id, context_id) from exc
        # A conflict may describe a previously accepted operation. Server errors,
        # redirects, malformed replies and lost receipts require reconciliation.
        if response.status_code in (400, 401, 403, 422):
            raise UpstreamFailure(response.status_code, "submission")
        if response.status_code not in (200, 202):
            raise SubmissionUncertain(run_id, context_id)
        try:
            payload = response.json()
            if (
                not isinstance(payload, dict)
                or payload.get("success") is False
                or "error" in payload
            ):
                raise ValueError("Invalid envelope")
            data = payload.get("data", payload)
            if not isinstance(data, dict):
                raise ValueError("Invalid data")
            received_run = segment(data.get("runId"))
            header_run = response.headers.get("X-Run-Id")
            if context_id is None:
                if received_run != run_id:
                    raise ValueError("Mismatched execute identity")
            elif header_run is None:
                raise ValueError("Resume receipt requires upstream run identity header")
            if header_run is not None and header_run != received_run:
                raise ValueError("Contradictory run identity header")
            if data.get("workflowId", workflow_id) != workflow_id:
                raise ValueError("Mismatched workflow identity")
            if data.get("contextId", context_id) != context_id:
                raise ValueError("Mismatched resume context")
            if "statusUrl" in data:
                status_url = data["statusUrl"]
                if not isinstance(status_url, str):
                    raise ValueError("Invalid status URL")
                parsed = urlsplit(status_url)
                if (parsed.scheme not in ("http", "https") or not parsed.hostname
                        or parsed.username or parsed.password or parsed.query or parsed.fragment
                        or parsed.path != f"/api/v2/workflows/{workflow_id}/runs/{received_run}"):
                    raise ValueError("Contradictory status URL")
        except ValueError as exc:
            raise SubmissionUncertain(run_id, context_id) from exc
        return {
            "workflowId": workflow_id,
            "runId": run_id,
            "contextId": context_id,
            **({"resumedRunId": received_run} if context_id is not None else {}),
            "transportStatus": "accepted",
            "verifiedComplete": False,
            "response": payload,
        }

    def execute(self, workflow_id, run_id, inputs):
        path = f"/api/v2/workflows/{segment(workflow_id)}/execute"
        segment(run_id)
        return self._mutate(
            path, run_id, {"input": inputs, "async": True}, run_header=True, workflow_id=workflow_id
        )

    def resume(self, workflow_id, run_id, context_id, inputs):
        path = f"/api/v2/workflows/{segment(workflow_id)}/runs/{segment(run_id)}/resume"
        segment(context_id)
        return self._mutate(
            path, run_id, {"contextId": context_id, "input": inputs}, context_id, workflow_id=workflow_id
        )

    def cancel(self, workflow_id, run_id):
        """Request best-effort cancellation; acknowledgement never proves executor exit.

        workflowId is request attribution: the official receipt only identifies runId.
        The caller must durably record this intent before calling; no retries occur here.
        """
        path = f"/api/v2/workflows/{segment(workflow_id)}/runs/{segment(run_id)}/cancel"
        try:
            response = self._client.post(
                self.base_url + path, headers={"x-api-key": self._key},
                follow_redirects=False,
            )
        except httpx.TransportError as exc:
            raise SubmissionUncertain(run_id) from exc
        if 400 <= response.status_code < 500:
            raise UpstreamFailure(response.status_code, "cancellation")
        if response.status_code != 200:
            raise SubmissionUncertain(run_id)
        try:
            payload = response.json()
            if (not isinstance(payload, dict) or "error" in payload
                    or payload.get("success") is False):
                raise ValueError("Invalid envelope")
            data = payload.get("data")
            if not isinstance(data, dict) or "error" in data:
                raise ValueError("Invalid cancellation data")
            if (data.get("runId") != run_id
                    or data.get("workflowId", workflow_id) != workflow_id
                    or response.headers.get("X-Run-Id", run_id) != run_id):
                raise ValueError("Mismatched cancellation identity")
            flags = ("success", "redisAvailable", "durablyRecorded",
                     "locallyAborted", "pausedCancelled")
            if any(type(data.get(key)) is not bool for key in flags):
                raise ValueError("Invalid cancellation flags")
            reason = data.get("reason")
            reasons = {
                "recorded", "already_cancelled", "already_completed", "already_failed",
                "redis_unavailable", "redis_write_failed", "paused_event_publish_failed",
                "paused_database_cancel_failed", "queue_cancelled",
                "active_resume_signal_failed", "cancellation_not_finalized",
            }
            if reason is not None and (not isinstance(reason, str) or reason not in reasons):
                raise ValueError("Invalid cancellation reason")
        except ValueError as exc:
            raise SubmissionUncertain(run_id) from exc
        return {
            "workflowId": workflow_id, "runId": run_id,
            "transportStatus": "accepted", "cancelRequested": data["success"],
            "verifiedStopped": False,
            **{key: data[key] for key in flags if key != "success"},
            "reason": reason,
        }

    def observe(self, workflow_id, run_id):
        path = f"/api/v2/workflows/{segment(workflow_id)}/runs/{segment(run_id)}?includeOutput=true"
        try:
            response = self._client.get(
                self.base_url + path,
                headers={"x-api-key": self._key},
                follow_redirects=False,
            )
        except httpx.TransportError:
            return {"runId": run_id, "status": "unknown", "safeToResubmit": False}
        if response.status_code in (401, 403):
            raise UpstreamFailure(response.status_code, "observation")
        if response.status_code != 200:
            return {"runId": run_id, "status": "unknown", "safeToResubmit": False}
        try:
            payload = response.json()
            data = payload.get("data", payload) if isinstance(payload, dict) else None
            if (not isinstance(data, dict) or data.get("runId", run_id) != run_id
                    or data.get('workflowId', workflow_id) != workflow_id
                    or payload.get('success') is False or 'error' in payload):
                raise ValueError("Invalid run response")
        except ValueError:
            return {"runId": run_id, "status": "unknown", "safeToResubmit": False}
        return {
            "runId": run_id,
            "status": data.get("status", "unknown"),
            "safeToResubmit": False,
            "response": payload,
        }

    def read_condition_outputs(self, workflow_id, run_id, block_ids):
        """Bounded selected blocks; discard all source content before returning."""
        workflow_id, run_id = segment(workflow_id), segment(run_id)
        if (type(block_ids) is not list or not 1 <= len(block_ids) <= 20
                or any(type(block) is not str for block in block_ids)
                or len(set(block_ids)) != len(block_ids)):
            raise ValueError('Bounded unique condition blocks required')
        for block in block_ids:
            segment(block)

        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('Ambiguous condition response')
                result[key] = value
            return result

        def invalid(_value):
            raise ValueError('Nonfinite condition response')

        def finite(value):
            parsed = float(value)
            return parsed if math.isfinite(parsed) else invalid(value)

        try:
            with self._client.stream('GET', self.base_url + f'/api/v2/workflows/{workflow_id}/runs/{run_id}',
                    params={'includeOutput': 'false', 'selectedOutputs': ','.join(block_ids)},
                    headers={'x-api-key': self._key, 'Accept-Encoding': 'identity'}, follow_redirects=False) as response:
                if response.status_code != 200:
                    raise UpstreamFailure(response.status_code, 'condition decisions')
                if response.headers.get('Content-Encoding', '').lower() not in ('', 'identity'):
                    raise ValueError('Encoded condition response unavailable')
                raw = bytearray()
                for chunk in response.iter_bytes(chunk_size=65536):
                    if len(raw) + len(chunk) > 1_048_576:
                        raise ValueError('Condition response too large')
                    raw.extend(chunk)
            payload = json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                                 parse_constant=invalid, parse_float=finite)
            if (type(payload) is not dict or payload.get('success') is not True
                    or 'error' in payload or type(payload.get('data')) is not dict):
                raise ValueError('Invalid condition response')
            data = payload['data']
            if data.get('workflowId') != workflow_id or data.get('runId') != run_id:
                raise ValueError('Condition run mismatch')
            outputs = data.get('blockOutputs')
            if outputs is None:
                return {}
            if type(outputs) is not dict or not set(outputs) <= set(block_ids):
                raise ValueError('Condition block mismatch')
            result = {}
            for block, output in outputs.items():
                path = output.get('selectedPath') if type(output) is dict else None
                result[block] = ({'ruleId': output.get('selectedOption'),
                                  'conditionResult': output.get('conditionResult'),
                                  'targetBlockId': path.get('blockId') if type(path) is dict else None}
                                 if type(output) is dict else None)
            return result
        except httpx.HTTPError:
            raise UpstreamFailure(503, 'condition decisions') from None
        except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
            raise UpstreamFailure(502, 'condition decisions') from None

    def read_status_summary(self, workflow_id, run_id):
        """Read native status without requesting outputs, files or trace bodies."""
        workflow_id, run_id = segment(workflow_id), segment(run_id)
        try:
            response = self._client.get(
                self.base_url + f'/api/v2/workflows/{workflow_id}/runs/{run_id}',
                params={'includeOutput': 'false'}, headers={'x-api-key': self._key},
                follow_redirects=False)
        except httpx.HTTPError:
            raise UpstreamFailure(503, 'status summary') from None
        if response.status_code != 200:
            raise UpstreamFailure(response.status_code, 'status summary')

        def timestamp(value, *, nullable=True):
            if value is None and nullable:
                return None
            if not isinstance(value, str) or not 1 <= len(value) <= 64:
                raise ValueError
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is None:
                raise ValueError
            result = parsed.timestamp()
            if not math.isfinite(result) or not 0 <= result <= 8_640_000_000_000:
                raise ValueError
            return result

        def identity(value):
            if value is None:
                return None
            if (not isinstance(value, str) or not value or len(value) > 256
                    or value.strip() != value or '\0' in value):
                raise ValueError
            return value

        try:
            if len(response.content) > 65536:
                raise ValueError
            envelope = response.json()
            if not isinstance(envelope, dict) or envelope.get('success') is False or 'error' in envelope:
                raise ValueError
            data = envelope.get('data')
            if (not isinstance(data, dict) or data.get('runId') != run_id
                    or data.get('workflowId') != workflow_id
                    or data.get('status') not in ('queued', 'pending', 'running', 'paused', 'redacting',
                                                 'completed', 'failed', 'cancelled')):
                raise ValueError
            if any(data.get(field) is not None for field in ('output', 'blockOutputs', 'files', 'traceSpans')):
                raise ValueError
            duration = data.get('durationMs')
            if duration is not None and (type(duration) not in (int, float)
                                         or not math.isfinite(duration) or duration < 0):
                raise ValueError
            paused = data.get('paused')
            if paused is not None:
                if not isinstance(paused, dict) or data['status'] != 'paused':
                    raise ValueError
                points, resumed = paused.get('pausePointCount'), paused.get('resumedCount')
                reason = paused.get('automaticResumeWaitingReason')
                if (type(points) is not int or type(resumed) is not int
                        or not 0 <= resumed <= points <= 2147483647
                        or paused.get('pauseKind') not in ('human', 'time', None)
                        or (reason is not None and not isinstance(reason, str))):
                    raise ValueError
                paused = {'kind': paused.get('pauseKind'), 'contextId': identity(paused.get('contextId')),
                          'blockedOnBlockId': identity(paused.get('blockedOnBlockId')),
                          'pausedAt': timestamp(paused.get('pausedAt'), nullable=False),
                          'resumeAt': timestamp(paused.get('resumeAt')),
                          'pausePointCount': points, 'resumedCount': resumed,
                          'automaticResumeNeedsAttention': bool(reason and reason.strip())}
            return {'observedAt': time.time(), 'native': {
                'status': data['status'], 'startedAt': timestamp(data.get('startedAt')),
                'endedAt': timestamp(data.get('endedAt')), 'durationMs': duration, 'paused': paused}}
        except (ValueError, TypeError, OverflowError, OSError, RecursionError):
            raise UpstreamFailure(502, 'status summary') from None
