"""Persist deferred conversation cleanup until cached reference files are unused."""
from __future__ import annotations

import json
import time
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Callable

from services.config import DATA_DIR
from services.reference_uploads import ReferenceUploadCache, reference_upload_cache
from utils.log import logger


class ReferenceConversationCleanup:
    def __init__(self, path: Path, cache: ReferenceUploadCache,
                 delete: Callable[[str, str], None] | None = None):
        self.path = path
        self.cache = cache
        self.delete = delete or self._delete
        self._lock = Lock()
        self._sweep_lock = Lock()
        self._jobs: dict[str, dict] = {}
        if path.exists():
            try:
                for job in json.loads(path.read_text(encoding="utf-8")):
                    if isinstance(job, dict) and job.get("token") and job.get("conversation_id"):
                        # A restart discards the upload cache. Resume outstanding cleanup.
                        job.update(attempts=0, next_attempt=0)
                        self._jobs[self._key(job["token"], job["conversation_id"])] = job
            except (ValueError, OSError, TypeError):
                logger.warning({"event": "reference_conversation_cleanup_load_failed"})

    @staticmethod
    def _key(token: str, conversation_id: str) -> str:
        return token + ":" + conversation_id

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(list(self._jobs.values()), ensure_ascii=False), encoding="utf-8")
        tmp.chmod(0o600)
        tmp.replace(self.path)

    def defer(self, token: str, conversation_id: str, file_ids: list[str]) -> None:
        with self._lock:
            key = self._key(token, conversation_id)
            self._jobs[key] = {"token": token, "conversation_id": conversation_id,
                               "file_ids": list(set(file_ids)), "attempts": 0, "next_attempt": 0}
            self._save()
        logger.info({"event": "reference_conversation_cleanup_deferred",
                     "conversation_id": conversation_id, "reference_count": len(set(file_ids))})

    @staticmethod
    def _delete(token: str, conversation_id: str) -> None:
        # Lazy imports avoid the upload-cache/backend import cycle.
        from services.openai_backend_api import OpenAIBackendAPI, account_service
        from utils.helper import UpstreamHTTPError
        current = account_service.get_account(token) or {}
        backend = OpenAIBackendAPI(str(current.get("access_token") or token))
        try:
            backend.delete_conversation(conversation_id)
        except UpstreamHTTPError as exc:
            if exc.status_code != 404:
                raise
        finally:
            backend.close()

    def cleanup_ready(self) -> None:
        # Never allow two sweepers to race a deletion or overwrite queue state.
        with self._sweep_lock:
            with self._lock:
                jobs = [(key, dict(job)) for key, job in self._jobs.items()]
            for key, job in jobs:
                if job["attempts"] >= 3 or job["next_attempt"] > time.time():
                    continue
                if self.cache.protected(job["token"], job.get("file_ids", [])):
                    continue
                try:
                    self.delete(job["token"], job["conversation_id"])
                except Exception as exc:
                    with self._lock:
                        current = self._jobs.get(key)
                        if current:
                            current["attempts"] += 1
                            current["next_attempt"] = time.time() + 30 * current["attempts"]
                            self._save()
                    # Tokens/file URLs and arbitrary upstream bodies are not logged here.
                    logger.warning({"event": "reference_conversation_cleanup_failed",
                                    "conversation_id": job["conversation_id"],
                                    "error_type": type(exc).__name__})
                else:
                    with self._lock:
                        self._jobs.pop(key, None)
                        self._save()
                    logger.info({"event": "image_conversation_removed",
                                 "conversation_id": job["conversation_id"], "deferred": True})

    def start(self, stop_event: Event) -> Thread:
        def run() -> None:
            while not stop_event.is_set():
                try:
                    self.cleanup_ready()
                except Exception as exc:
                    logger.warning({"event": "reference_conversation_cleanup_worker_failed",
                                    "error_type": type(exc).__name__})
                stop_event.wait(5)
        thread = Thread(target=run, name="reference-conversation-cleanup", daemon=True)
        thread.start()
        return thread


reference_conversation_cleanup = ReferenceConversationCleanup(
    DATA_DIR / "reference_conversation_cleanup.json", reference_upload_cache,
)
