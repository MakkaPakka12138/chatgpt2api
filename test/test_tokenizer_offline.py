from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from utils import tokenizer


class OfflineTokenizerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.bundle = Path(self.directory.name) / "bundle"
        self.cache = Path(self.directory.name) / "cache"
        self.bundle.mkdir()
        payload = b"".join(base64.b64encode(bytes([i])) + f" {i}\n".encode() for i in range(256))
        manifest = {}
        for name in ("o200k_base", "cl100k_base"):
            filename = f"{name}.tiktoken"
            (self.bundle / filename).write_bytes(payload)
            manifest[name] = {"file": filename, "sha256": hashlib.sha256(payload).hexdigest(),
                              "arguments": {"name": name, "pat_str": "(?s).", "special_tokens": {}}}
        (self.bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {"TIKTOKEN_BUNDLE_DIR": str(self.bundle), "TIKTOKEN_CACHE_DIR": str(self.cache)})
        patcher.start()
        self.addCleanup(patcher.stop)
        tokenizer._load.cache_clear()
        self.addCleanup(tokenizer._load.cache_clear)
        network = mock.patch("requests.get", side_effect=ConnectionError("offline"))
        self.network = network.start()
        self.addCleanup(network.stop)

    def test_empty_cache_is_seeded_offline_and_reused_after_restart(self):
        first = tokenizer.encoding_for_model("gpt-image-2")
        self.assertEqual(len(first.encode("test")), 4)
        self.assertTrue((self.cache / "o200k_base.tiktoken").exists())
        tokenizer._load.cache_clear()
        (self.bundle / "o200k_base.tiktoken").unlink()
        self.assertEqual(len(tokenizer.encoding_for_model("gpt-image-2").encode("test")), 4)
        self.network.assert_not_called()

    def test_corrupt_cache_is_repaired_from_verified_bundle(self):
        self.cache.mkdir()
        target = self.cache / "cl100k_base.tiktoken"
        target.write_bytes(b"corrupt")
        self.assertEqual(len(tokenizer.encoding_for_model("gpt-4").encode("test")), 4)
        self.assertEqual(target.read_bytes(), (self.bundle / target.name).read_bytes())
        self.network.assert_not_called()

    def test_unwritable_cache_uses_bundle(self):
        with mock.patch.object(tokenizer, "_persist", side_effect=PermissionError):
            self.assertEqual(len(tokenizer.encoding_for_model("gpt-image-2").encode("test")), 4)
        self.network.assert_not_called()

    def test_both_copies_corrupt_return_estimate_without_download(self):
        (self.bundle / "o200k_base.tiktoken").write_bytes(b"corrupt")
        encoding = tokenizer.encoding_for_model("gpt-image-2")
        self.assertTrue(encoding.estimated)
        self.assertEqual(len(encoding.encode("你好 test")), 3)
        self.network.assert_not_called()

    def test_warmup_prepares_both_encodings_without_network(self):
        tokenizer.prepare_tokenizers()
        self.assertEqual(len(list(self.cache.glob("*.tiktoken"))), 2)
        self.network.assert_not_called()
