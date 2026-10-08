"""Bounded, credential-free diagnostics shared by a call and its worker threads."""
from __future__ import annotations

import hashlib
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit
from uuid import uuid4


def safe_endpoint(url: str) -> str:
    try:
        parsed = urlsplit(str(url))
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{host}{port}{parsed.path}" if parsed.scheme else parsed.path
    except ValueError:
        return "[invalid URL]"


def redact_diagnostic(value: object, secrets: tuple[str, ...] = (), *, limit: int = 600) -> str:
    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    text = re.sub(r"(?i)Bearer\s+[^\s,;\"']+", "Bearer [redacted]", text)
    text = re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[redacted JWT]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "[redacted]", text)
    text = re.sub(r"(?i)([\"']?(?:access_token|refresh_token|prepare_token|proof_token|turnstile_token|token|password|cookie|authorization)[\"']?\s*[:=]\s*)[\"']?[^\s,}\"']+", r"\1[redacted]", text)
    text = re.sub(r"(?:https?|socks5h?)://[^\s\"'<>]+", lambda match: safe_endpoint(match.group()), text)
    return text[:limit]


def curl_error_code(exc: Exception) -> int | None:
    code = getattr(exc, "code", None)
    if isinstance(code, int) and not isinstance(code, bool):
        return int(code)
    match = re.search(r"curl:\s*\((\d+)\)", str(exc), re.IGNORECASE)
    return int(match.group(1)) if match else None


def error_category(exc: Exception) -> str:
    code = curl_error_code(exc)
    if code == 35:
        return "tls_connect"
    if code == 60:
        return "certificate_verify"
    if code in (5, 6):
        return "dns_failed"
    if code == 7:
        return "connect_failed"
    if code == 28:
        return "timeout"
    if code in (52, 55, 56):
        return "connection_reset"
    text = str(exc).lower()
    if "token_invalid" in text or "token_revoked" in text:
        return "token_invalid"
    if "quota" in text or "rate_limit" in text or "http 429" in text:
        return "quota_limited"
    if "http " in text or "status=" in text:
        return "http_error"
    return "other"


@dataclass
class UpstreamTrace:
    request_id: str = field(default_factory=lambda: uuid4().hex)
    attempts: list[dict] = field(default_factory=list)

    def start_attempt(self, token: str) -> dict:
        attempt = {
            "attempt": len(self.attempts) + 1,
            "account_id": hashlib.sha256(token.encode()).hexdigest()[:12] if token else "anonymous",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "status": "running",
            "steps": [],
        }
        self.attempts.append(attempt)
        return attempt

    def fields(self) -> dict:
        result = {"request_id": self.request_id}
        if self.attempts:
            result["network_trace"] = {
                "attempts": deepcopy(self.attempts),
                "retry_count": max(0, len(self.attempts) - 1),
                "recovered": len(self.attempts) > 1 and self.attempts[-1]["status"] == "success",
            }
        return result


_trace: ContextVar[UpstreamTrace | None] = ContextVar("upstream_trace", default=None)


def current_trace() -> UpstreamTrace | None:
    return _trace.get()


@contextmanager
def bind_trace(trace: UpstreamTrace):
    token = _trace.set(trace)
    try:
        yield trace
    finally:
        _trace.reset(token)


def record_request(backend, method: str, url: str, stage: str, **kwargs):
    """Record HTTP headers/connection timing; never bodies or request headers."""
    attempt = getattr(backend, "diagnostic_attempt", None)
    started = time.monotonic()
    step = {"stage": stage, "method": method.upper(), "target": safe_endpoint(url)}
    if isinstance(attempt, dict):
        # A text attempt has four normal HTTP steps; cap unexpected growth.
        if len(attempt["steps"]) < 16:
            attempt["steps"].append(step)
        attempt["stage"] = stage
    try:
        response = getattr(backend.session, method.lower())(url, **kwargs)
        step["http_status"] = response.status_code
        return response
    except Exception as exc:
        step.update(error_category=error_category(exc), curl_code=curl_error_code(exc),
                    error_type=type(exc).__name__)
        raise
    finally:
        step["duration_ms"] = round((time.monotonic() - started) * 1000)
