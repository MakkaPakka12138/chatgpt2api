"""Cached, credential-free account-pool management summary; no upstream requests."""
from __future__ import annotations

import copy
import time
from collections import Counter
from datetime import datetime, timezone
from threading import Event, Lock, Thread

from services.account_service import account_service
from services.config import config
from services.reference_uploads import timestamp, upload_remaining
from utils.log import logger


class PoolStatusCache:
    def __init__(self, accounts=account_service, settings=config, interval: float = 30):
        self.accounts = accounts
        self.settings = settings
        self.interval = interval
        self._lock = Lock()
        self._refresh_lock = Lock()
        self._cache: dict | None = None
        self._updated_monotonic = 0.0

    def refresh(self) -> None:
        with self._refresh_lock:
            snapshot = self.accounts.pool_summary_snapshot()
            items = snapshot["items"]
            statuses = Counter(item.get("status") for item in items)
            image_accounts = [a for a in items if a.get("status") == "正常" and int(a.get("quota") or 0) > 0]
            uploads = {"known_remaining_total": 0, "available_known_remaining": 0,
                       "unknown_accounts": 0, "low_accounts": 0, "blocked_accounts": 0,
                       "eligible_accounts": 0, "not_applicable_accounts": 0, "switch_threshold": 20}
            now = time.time()
            image_resets, upload_resets = [], []
            for item in items:
                image_reset = timestamp(item.get("restore_at"))
                if image_reset and image_reset > now:
                    image_resets.append(image_reset)
                if str(item.get("source_type") or "web").lower() == "codex":
                    uploads["not_applicable_accounts"] += 1
                    continue
                remaining = upload_remaining(item)
                blocked_until = timestamp(item.get("upload_blocked_until"))
                blocked = bool(blocked_until and blocked_until > now)
                reset = blocked_until if blocked else timestamp(item.get("upload_reset_at"))
                if reset and reset > now:
                    upload_resets.append(reset)
                if remaining is None:
                    uploads["unknown_accounts"] += 1
                else:
                    uploads["known_remaining_total"] += remaining
                    if remaining < 20:
                        uploads["low_accounts"] += 1
                if blocked:
                    uploads["blocked_accounts"] += 1
                if item.get("status") == "正常" and int(item.get("quota") or 0) > 0 and not blocked and (remaining is None or remaining >= 20):
                    uploads["eligible_accounts"] += 1
                    uploads["available_known_remaining"] += remaining or 0
            def iso(value):
                return datetime.fromtimestamp(value, timezone.utc).isoformat()
            result = {
                "status": "ok" if image_accounts else "degraded",
                "version": self.settings.app_version,
                "scheduling_mode": self.settings.account_scheduling_mode,
                "accounts": {
                    "total": len(items) + snapshot["quarantined"], "in_pool": len(items),
                    "normal": statuses["正常"], "limited": statuses["限流"],
                    "abnormal": statuses["异常"], "disabled": statuses["禁用"],
                    "quarantined": snapshot["quarantined"], "cumulative_total": snapshot["cumulative_total"],
                    "by_type": dict(Counter(str(a.get("type") or "unknown") for a in items)),
                    "by_source": dict(Counter(str(a.get("source_type") or "web") for a in items)),
                },
                "images": {
                    "remaining_total": sum(int(a.get("quota") or 0) for a in image_accounts),
                    "eligible_accounts": len(image_accounts),
                    "inflight": sum(int(a.get("image_inflight") or 0) for a in items),
                    "success_total": sum(int(a.get("success") or 0) for a in items),
                    "fail_total": sum(int(a.get("fail") or 0) for a in items),
                    "next_reset_at": iso(min(image_resets)) if image_resets else None,
                },
                "uploads": {**uploads, "next_reset_at": iso(min(upload_resets)) if upload_resets else None},
                "cache": {"updated_at": iso(now), "refresh_interval_seconds": self.interval},
                "upstream_refresh_interval_minutes": self.settings.refresh_account_interval_minute,
            }
            with self._lock:
                self._cache = result
                self._updated_monotonic = time.monotonic()

    def get(self) -> dict:
        with self._lock:
            initialized = self._cache is not None
        if not initialized:
            self.refresh()
        with self._lock:
            result = copy.deepcopy(self._cache)
            age = max(0.0, time.monotonic() - self._updated_monotonic)
        result["cache"].update(age_seconds=round(age, 2), stale=age > self.interval * 2)
        return result

    def start(self, stop_event: Event) -> Thread:
        self.refresh()
        def worker():
            while not stop_event.wait(self.interval):
                try:
                    self.refresh()
                except Exception as exc:
                    logger.warning({"event": "pool_status_cache_refresh_failed", "error_type": type(exc).__name__})
        thread = Thread(target=worker, name="pool-status-cache", daemon=True)
        thread.start()
        return thread


pool_status_cache = PoolStatusCache()
