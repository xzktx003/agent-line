"""Bounded, credential-free reader for the local Kanban session directory."""

from __future__ import annotations

import ipaddress
import asyncio
import json
import math
import re
import time
from datetime import datetime, timezone
from collections.abc import Mapping
from urllib.parse import urlsplit

import httpx


class KanbanReadUnavailable(Exception):
    """The local Kanban directory could not be read safely."""

    def __init__(self) -> None:
        super().__init__("kanban_read_unavailable")


_MAX_BODY = 2 * 1024 * 1024
_MAX_ITEMS = 2000
_MAX_SUMMARY = 4000
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,160}$")
_CONNECTION_STATES = {"online", "degraded", "offline"}
_INTERACTION_STATES = {"awaiting_input", "running", "idle", "detached", "exited"}


def validate_settings(value: object) -> dict[str, str]:
    """Validate and detach the administrator-configured loopback origin."""
    try:
        if not isinstance(value, Mapping) or set(value) != {'baseUrl'}:
            raise ValueError
        raw = value.get("baseUrl")
        if (not isinstance(raw, str) or not raw or raw != raw.strip()
                or any(ord(char) < 33 or ord(char) == 127 for char in raw)):
            raise ValueError
        parts = urlsplit(raw)
        if parts.scheme.lower() != "http" or not parts.netloc:
            raise ValueError
        if parts.username is not None or parts.password is not None:
            raise ValueError
        if parts.query or parts.fragment or parts.path not in ("", "/"):
            raise ValueError
        # A port must be written explicitly, and its spelling must be decimal.
        authority = parts.netloc
        if authority.startswith("["):
            closing = authority.find("]")
            if closing < 0 or authority[closing + 1:closing + 2] != ":":
                raise ValueError
            host_text = authority[1:closing]
            port_text = authority[closing + 2:]
            if "%" in host_text:
                raise ValueError
            address = ipaddress.ip_address(host_text)
            if not isinstance(address, ipaddress.IPv6Address) or not address.is_loopback:
                raise ValueError
            host = "[{}]".format(address.compressed)
        else:
            if authority.count(":") != 1:
                raise ValueError
            host_text, port_text = authority.rsplit(":", 1)
            address = ipaddress.ip_address(host_text)
            if not isinstance(address, ipaddress.IPv4Address) or not address.is_loopback:
                raise ValueError
            host = str(address)
        if not port_text or not port_text.isascii() or not port_text.isdigit():
            raise ValueError
        port = int(port_text)
        if not 1 <= port <= 65535 or parts.port != port:
            raise ValueError
        return {"baseUrl": "http://{}:{}".format(host, port)}
    except (ValueError, TypeError, UnicodeError):
        raise KanbanReadUnavailable() from None


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError('non-finite JSON number')
    return result


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _utc_timestamp(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("invalid timestamp")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("timestamp is not UTC")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_authorized_local(item: dict[str, object], workspace: str,
                         codex_thread_id: str) -> bool:
    if (item.get("agentSessionId") != codex_thread_id
            or item.get("workingDirectory") != workspace
            or item.get("sourceType") != "local"
            or item.get("agentKind") != "codex"):
        return False
    if item.get("hostId") not in (None, "local") or "sshTarget" in item:
        return False
    transport = item.get("transportRef")
    if transport is not None and not isinstance(transport, dict):
        return False
    if isinstance(transport, dict) and any(key in transport for key in ('sshHost','sshPort','sshUsername')):
        return False
    return True


async def read_session_summary(settings: object, *, workspace: str,
                         codex_thread_id: str,
                         redact_tokens: tuple[str, ...] = ()) -> dict[str, object]:
    """Read one bounded local-session projection, or report ambiguity/absence."""
    normalized = validate_settings(settings)
    if (not isinstance(workspace, str) or not workspace
            or not isinstance(codex_thread_id, str) or not codex_thread_id
            or not isinstance(redact_tokens, (tuple, list))
            or any(not isinstance(token, str) or not token for token in redact_tokens)):
        raise KanbanReadUnavailable()

    deadline = time.monotonic() + 7.0
    body = bytearray()
    try:
        timeout = httpx.Timeout(5.0, connect=2.0, read=5.0, write=5.0, pool=2.0)
        async with asyncio.timeout(7.0), httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client:
            async with client.stream("GET", normalized["baseUrl"] + "/api/agent-sessions",
                               headers={"Accept": "application/json", "Accept-Encoding": "identity"}) as response:
                if response.status_code != 200:
                    raise ValueError
                encoding = response.headers.get("content-encoding", "identity").strip().lower()
                if encoding not in ("", "identity"):
                    raise ValueError
                media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if media_type != "application/json":
                    raise ValueError
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    if not content_length.isascii() or not content_length.isdigit() or int(content_length) > _MAX_BODY:
                        raise ValueError
                async for chunk in response.aiter_bytes(chunk_size=16384):
                    if time.monotonic() > deadline:
                        raise ValueError
                    if len(body) + len(chunk) > _MAX_BODY:
                        raise ValueError
                    body.extend(chunk)
        if time.monotonic() > deadline:
            raise ValueError
        payload = json.loads(bytes(body), object_pairs_hook=_unique_object,
                             parse_constant=_reject_constant, parse_float=_finite_float)
        if not isinstance(payload, dict):
            raise ValueError
        items = payload.get("items")
        if (not isinstance(items, list) or len(items) > _MAX_ITEMS
                or any(not isinstance(item, dict) for item in items)):
            raise ValueError
        if "activeAgentSessionId" not in payload or not (
                payload["activeAgentSessionId"] is None
                or isinstance(payload["activeAgentSessionId"], str)):
            raise ValueError
        generated_at = _utc_timestamp(payload.get("updatedAt"))

        candidates = [item for item in items
                      if isinstance(item, dict)
                      and _is_authorized_local(item, workspace, codex_thread_id)]
        if time.monotonic() > deadline:
            raise ValueError
        if len(candidates) > 1:
            return {"state": "ambiguous", "directoryGeneratedAt": generated_at,
                    "session": None}
        if not candidates:
            return {"state": "not_observed", "directoryGeneratedAt": generated_at,
                    "session": None}

        item = candidates[0]
        session_id = item.get("id")
        connection = item.get("connectionState")
        interaction = item.get("interactionState")
        if (not isinstance(session_id, str) or not _IDENTIFIER.fullmatch(session_id)
                or connection not in _CONNECTION_STATES
                or interaction not in _INTERACTION_STATES):
            raise ValueError
        summary = item.get("lastAgentMessageSummary")
        if summary is not None and not isinstance(summary, str):
            raise ValueError
        if summary is not None:
            summary.encode('utf-8')
            for token in redact_tokens:
                summary = summary.replace(token, "[凭证已隐藏]")
        truncated = summary is not None and len(summary) > _MAX_SUMMARY
        if truncated:
            summary = summary[:_MAX_SUMMARY]
        summary_updated = item.get("taskSummaryUpdatedAt")
        if summary_updated is not None:
            summary_updated = _utc_timestamp(summary_updated)
        session = {
            "sessionId": session_id,
            "connectionState": connection,
            "interactionState": interaction,
            "lastAgentMessageSummary": summary,
            "summaryUpdatedAt": summary_updated,
            "summaryTruncated": truncated,
        }
        if time.monotonic() > deadline:
            raise ValueError
        return {"state": "observed", "directoryGeneratedAt": generated_at,
                "session": session}
    except Exception:
        # Never expose response contents, URL details, or transport exception text.
        raise KanbanReadUnavailable() from None
