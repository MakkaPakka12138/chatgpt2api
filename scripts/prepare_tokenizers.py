"""Bundle tokenizer data during the image build, never during a request."""
from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path

import tiktoken_ext.openai_public as public


def prepare(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for name in ("o200k_base", "cl100k_base"):
        arguments = getattr(public, name)()
        payload = b"".join(
            base64.b64encode(token) + b" " + str(rank).encode("ascii") + b"\n"
            for token, rank in sorted(arguments.pop("mergeable_ranks").items(), key=lambda item: item[1])
        )
        filename = f"{name}.tiktoken"
        (destination / filename).write_bytes(payload)
        manifest[name] = {"file": filename, "sha256": hashlib.sha256(payload).hexdigest(), "arguments": arguments}
    (destination / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


if __name__ == "__main__":
    prepare(Path(sys.argv[1]))
