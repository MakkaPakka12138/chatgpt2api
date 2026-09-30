"""Account-scoped reference reuse and independently tracked upload limits."""
from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter
from datetime import datetime, timezone
from threading import Condition
from typing import Any, Callable

from utils.helper import UpstreamHTTPError


def timestamp(value: Any) -> float | None:
    if not value:
        return None
    try:
        date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return date.replace(tzinfo=timezone.utc).timestamp() if date.tzinfo is None else date.timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def reset_time(value: Any) -> str | None:
    # Match the image limit's absolute reset_after format; accept relative seconds too.
    if timestamp(value) is not None:
        return str(value)
    try:
        seconds = float(value)
        if seconds >= 0:
            return datetime.fromtimestamp(time.time() + seconds, timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError):
        pass
    return None


def extract_upload_limits(limits: list[Any]) -> dict[str, Any]:
    for item in limits:
        if not isinstance(item, dict) or item.get("feature_name") not in {"file_upload", "file_uploads", "uploads", "file_upload_limit"}:
            continue
        try:
            remaining = max(0, int(item["remaining"]))
        except (KeyError, ValueError, TypeError):
            continue
        return {"upload_remaining": remaining, "upload_reset_at": reset_time(item.get("reset_after"))}
    return {"upload_remaining": None, "upload_reset_at": None}


def upload_remaining(account: dict[str, Any]) -> int | None:
    reset = timestamp(account.get("upload_reset_at"))
    if reset is not None and reset <= time.time():
        return None  # The old count expired; do not invent a renewed quota.
    value = account.get("upload_remaining")
    return max(0, int(value)) if value is not None else None


class UploadLimitError(RuntimeError):
    def __init__(self, message: str, retry_after: int = 3600) -> None:
        super().__init__(message)
        self.retry_after = max(1, retry_after)


def upload_limit_error(exc: UpstreamHTTPError) -> UploadLimitError | None:
    if not exc.context.startswith("/backend-api/files"):
        return None
    text = json.dumps(exc.body, ensure_ascii=False).lower()
    signals = ("upload_limit", "file_upload_limit", "file_upload_rate_limit", "file_uploads_limit",
               "file upload limit", "upload limit", "too many files", "文件上传上限", "上传次数上限")
    if exc.status_code != 429 and not any(signal in text for signal in signals):
        return None
    seconds = exc.retry_after
    if seconds is None:
        match = re.search(r"(\d+)\s*(?:分钟|minutes?|mins?)", text)
        if match:
            seconds = int(match.group(1)) * 60
    return UploadLimitError(str(exc), seconds or 3600)


class ReferenceUploadCache:
    """One upload per account/content, including concurrent requests; bounded one-hour TTL."""
    def __init__(self, ttl: float = 3600, capacity: int = 512) -> None:
        self.ttl = ttl
        self.capacity = capacity
        self._condition = Condition()
        self._entries: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
        self._pending: set[tuple[str, str]] = set()
        self._active: Counter[tuple[str, str]] = Counter()

    @staticmethod
    def digest(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _prune(self) -> None:
        now = time.monotonic()
        for key, (expires, _) in list(self._entries.items()):
            if expires <= now:
                self._entries.pop(key, None)

    def missing(self, token: str, digests: tuple[str, ...]) -> int:
        with self._condition:
            self._prune()
            return sum((token, digest) not in self._entries and (token, digest) not in self._pending
                       for digest in set(digests))

    def invalidate(self, token: str, file_ids: list[str] | None = None) -> None:
        with self._condition:
            for key in list(self._entries):
                if key[0] == token and (file_ids is None or self._entries[key][1].get("file_id") in file_ids):
                    self._entries.pop(key, None)
            self._condition.notify_all()

    def protected(self, token: str, file_ids: list[str]) -> bool:
        """A conversation may be deleted only after its files are neither cached nor in use."""
        wanted = set(file_ids)
        with self._condition:
            self._prune()
            return any(self._active[(token, file_id)] > 0 for file_id in wanted) or any(
                key[0] == token and value.get("file_id") in wanted
                for key, (_, value) in self._entries.items()
            )

    def release(self, token: str, file_ids: list[str]) -> None:
        with self._condition:
            for file_id in file_ids:
                key = (token, file_id)
                if self._active[key] <= 1:
                    self._active.pop(key, None)
                else:
                    self._active[key] -= 1
            self._condition.notify_all()

    def _retain(self, token: str, value: dict[str, Any], retain: bool) -> None:
        if retain and value.get("file_id"):
            self._active[(token, value["file_id"])] += 1

    def get_or_upload(self, token: str, data: bytes, upload: Callable[[], dict[str, Any]], *,
                      retain: bool = False, on_reuse: Callable[[], None] | None = None) -> dict[str, Any]:
        key = (token, self.digest(data))
        with self._condition:
            while True:
                self._prune()
                cached = self._entries.get(key)
                if cached:
                    self._retain(token, cached[1], retain)
                    if on_reuse is not None:
                        on_reuse()
                    return dict(cached[1])
                if key not in self._pending:
                    self._pending.add(key)
                    break
                self._condition.wait()
        try:
            result = upload()
            with self._condition:
                if len(self._entries) >= self.capacity:
                    oldest = min(self._entries, key=lambda k: self._entries[k][0])
                    self._entries.pop(oldest, None)
                self._entries[key] = (time.monotonic() + self.ttl, dict(result))
                self._retain(token, result, retain)
            return dict(result)
        finally:
            with self._condition:
                self._pending.discard(key)
                self._condition.notify_all()


reference_upload_cache = ReferenceUploadCache()
