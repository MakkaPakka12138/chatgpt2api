"""Offline token counting backed by bundled files and a persistent local cache."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import tempfile
from functools import lru_cache
from pathlib import Path

import tiktoken

log = logging.getLogger(__name__)
DEFAULT_CACHE = Path(__file__).resolve().parents[1] / "data" / "tiktoken-cache"


class EstimatedEncoding:
    estimated = True

    def encode(self, text: str):
        # A rough local estimate; callers must not present this as measured usage.
        return range((len(text.encode("utf-8")) + 3) // 4)


def estimate_text_tokens(text: str) -> int:
    return len(EstimatedEncoding().encode(text))


def _verified(path: Path, digest: str) -> bytes:
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError("tokenizer checksum mismatch")
    return payload


def _persist(cache: Path, filename: str, payload: bytes) -> None:
    cache.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=cache, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
        os.replace(temporary, cache / filename)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@lru_cache(maxsize=8)
def _load(name: str, bundle_dir: str, cache_dir: str):
    bundle, cache = Path(bundle_dir), Path(cache_dir)
    try:
        entry = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))[name]
        filename = entry["file"]
        if Path(filename).name != filename:
            raise ValueError("invalid tokenizer filename")
        try:
            payload = _verified(cache / filename, entry["sha256"])
        except (OSError, ValueError):
            payload = _verified(bundle / filename, entry["sha256"])
            try:
                _persist(cache, filename, payload)
            except OSError:
                log.warning("Tokenizer cache cannot be written; using bundled data")
        ranks = {base64.b64decode(token): int(rank) for token, rank in (line.split() for line in payload.splitlines())}
        return tiktoken.Encoding(**entry["arguments"], mergeable_ranks=ranks)
    except Exception as exc:
        # No tiktoken.get_encoding call here: its lazy loader may access the network.
        log.warning("Local tokenizer unavailable (%s); using estimated text tokens", type(exc).__name__)
        return EstimatedEncoding()


def encoding_for_model(model: str):
    try:
        name = tiktoken.encoding_name_for_model(model)
    except KeyError:
        name = "o200k_base"
    if name not in {"o200k_base", "cl100k_base"}:
        name = "o200k_base"
    return _load(
        name,
        os.environ.get("TIKTOKEN_BUNDLE_DIR", "/opt/chatgpt2api/tiktoken-cache"),
        os.environ.get("TIKTOKEN_CACHE_DIR") or str(DEFAULT_CACHE),
    )


def prepare_tokenizers() -> None:
    for model in ("gpt-4o", "gpt-4"):
        encoding_for_model(model)
