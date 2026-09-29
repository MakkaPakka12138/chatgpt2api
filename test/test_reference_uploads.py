from __future__ import annotations

import base64
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from services.account_service import AccountService
from services.openai_backend_api import OpenAIBackendAPI
from services.protocol import conversation
from services.reference_uploads import (
    ReferenceUploadCache, UploadLimitError, extract_upload_limits, reference_upload_cache,
    upload_limit_error, upload_remaining,
)
from services.storage.json_storage import JSONStorageBackend
from utils.helper import UpstreamHTTPError


class ReferenceCacheTests(unittest.TestCase):
    def test_same_account_reuses_content_but_accounts_are_isolated(self):
        cache = ReferenceUploadCache()
        upload = Mock(side_effect=[{"file_id": "a"}, {"file_id": "b"}])
        self.assertEqual(cache.get_or_upload("a", b"image", upload)["file_id"], "a")
        self.assertEqual(cache.get_or_upload("a", b"image", upload)["file_id"], "a")
        self.assertEqual(cache.get_or_upload("b", b"image", upload)["file_id"], "b")
        self.assertEqual(upload.call_count, 2)

    def test_concurrent_identical_upload_runs_once(self):
        cache = ReferenceUploadCache()
        entered, release = threading.Event(), threading.Event()
        def upload():
            entered.set()
            self.assertTrue(release.wait(2))
            return {"file_id": "shared"}
        uploader = Mock(side_effect=upload)
        with ThreadPoolExecutor(max_workers=4) as pool:
            tasks = [pool.submit(cache.get_or_upload, "account", b"image", uploader) for _ in range(4)]
            self.assertTrue(entered.wait(2))
            self.assertEqual(cache.missing("account", (cache.digest(b"image"),)), 0)
            release.set()
            self.assertTrue(all(task.result()["file_id"] == "shared" for task in tasks))
        uploader.assert_called_once()

    def test_expiration_failure_and_invalidation_do_not_reuse_stale_ids(self):
        cache = ReferenceUploadCache(ttl=10)
        upload = Mock(side_effect=[RuntimeError("failed"), {"file_id": "1"}, {"file_id": "2"}, {"file_id": "3"}])
        with self.assertRaises(RuntimeError):
            cache.get_or_upload("a", b"img", upload)
        with patch("services.reference_uploads.time.monotonic", return_value=0):
            cache.get_or_upload("a", b"img", upload)
        with patch("services.reference_uploads.time.monotonic", return_value=11):
            self.assertEqual(cache.get_or_upload("a", b"img", upload)["file_id"], "2")
            cache.invalidate("a")
            self.assertEqual(cache.get_or_upload("a", b"img", upload)["file_id"], "3")

    def test_upload_limit_parses_wait_but_does_not_classify_blob_error(self):
        error = upload_limit_error(UpstreamHTTPError("/backend-api/files", 400, {"detail": "你已达到文件上传上限。请46分钟 内重试"}))
        self.assertEqual(error.retry_after, 46 * 60)
        self.assertEqual(upload_limit_error(UpstreamHTTPError("/backend-api/files", 429, {}, retry_after=60)).retry_after, 60)
        self.assertIsNone(upload_limit_error(UpstreamHTTPError("image_upload", 429, {})))
        self.assertIsNone(upload_limit_error(UpstreamHTTPError("/backend-api/files", 413, "too large")))

    def test_unknown_upload_quota_and_expired_counts_remain_unknown(self):
        self.assertIsNone(extract_upload_limits([{"feature_name": "image_gen", "remaining": 5}])["upload_remaining"])
        limits = extract_upload_limits([{"feature_name": "file_upload", "remaining": 2, "reset_after": 1800}])
        self.assertEqual(limits["upload_remaining"], 2)
        self.assertIsNotNone(limits["upload_reset_at"])
        self.assertIsNone(upload_remaining({"upload_remaining": 0, "upload_reset_at": "2000-01-01T00:00:00Z"}))


class UploadAccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = AccountService(JSONStorageBackend(Path(self.temp.name) / "accounts.json"))
        self.service.add_account_items([
            {"access_token": "reference-test-a", "status": "正常", "quota": 10},
            {"access_token": "reference-test-b", "status": "正常", "quota": 10},
        ])
        self.service.fetch_remote_info = lambda token, event="", **kwargs: self.service.get_account(token)
        for token in ("reference-test-a", "reference-test-b"):
            reference_upload_cache.invalidate(token)
            self.addCleanup(reference_upload_cache.invalidate, token)

    def test_upload_cooldown_skips_references_but_keeps_text_to_image(self):
        self.service.mark_upload_limited("reference-test-a", "limit", 2760)
        digest = (reference_upload_cache.digest(b"img"),)
        token = self.service.get_available_access_token(reference_digests=digest)
        self.assertEqual(token, "reference-test-b")
        self.service.release_image_slot(token)
        self.service._index = 0
        self.assertEqual(self.service.get_available_access_token(), "reference-test-a")
        self.assertEqual(self.service.get_account("reference-test-a")["quota"], 10)

    def test_cached_reference_works_with_zero_upload_quota_and_has_affinity(self):
        self.service.update_account("reference-test-b", {"upload_remaining": 0})
        reference_upload_cache.get_or_upload("reference-test-b", b"img", lambda: {"file_id": "cached"})
        token = self.service.get_available_access_token(reference_digests=(reference_upload_cache.digest(b"img"),))
        self.assertEqual(token, "reference-test-b")
        self.service.release_image_slot(token)

    def test_required_upload_count_filters_accounts(self):
        self.service.update_account("reference-test-a", {"upload_remaining": 1})
        digests = tuple(reference_upload_cache.digest(data) for data in (b"one", b"two"))
        self.assertEqual(self.service.get_available_access_token(reference_digests=digests), "reference-test-b")

    def test_new_uploads_switch_below_twenty_but_twenty_is_eligible(self):
        digests = (reference_upload_cache.digest(b"new-reference"),)
        self.service.update_account("reference-test-a", {"upload_remaining": 19})
        self.service.update_account("reference-test-b", {"upload_remaining": 20})
        token = self.service.get_available_access_token(reference_digests=digests)
        self.assertEqual(token, "reference-test-b")
        self.service.release_image_slot(token)
        self.service.update_account("reference-test-b", {"upload_remaining": 19})
        with self.assertRaises(RuntimeError):
            self.service.get_available_access_token(reference_digests=digests)
        self.assertFalse(self.service._image_inflight)
        self.service._index = 0
        token = self.service.get_available_access_token()
        self.assertEqual(token, "reference-test-a")
        self.service.release_image_slot(token)

    def test_partial_cache_still_requires_twenty_uploads_remaining(self):
        self.service.update_account("reference-test-a", {"upload_remaining": 19})
        reference_upload_cache.get_or_upload("reference-test-a", b"cached", lambda: {"file_id": "cached"})
        digests = tuple(reference_upload_cache.digest(data) for data in (b"cached", b"new"))
        token = self.service.get_available_access_token(reference_digests=digests)
        self.assertEqual(token, "reference-test-b")
        self.service.release_image_slot(token)

    def test_reservation_is_atomic_and_refresh_subtracts_pending_upload(self):
        self.service.update_account("reference-test-a", {"upload_remaining": 1})
        self.assertTrue(self.service.reserve_upload("reference-test-a"))
        self.service.update_account("reference-test-a", {"upload_remaining": 1})
        with self.assertRaises(UploadLimitError):
            self.service.reserve_upload("reference-test-a")
        self.service.finish_upload("reference-test-a", True, consumed=False)
        self.assertEqual(self.service.get_account("reference-test-a")["upload_remaining"], 1)
        self.assertFalse(self.service._upload_reservations)

    def test_upload_failure_switches_account_once_and_releases_slots(self):
        result = conversation.ImageOutput(kind="result", model="gpt-image-2", index=1, total=1, data=[{"b64_json": "out"}])
        calls = []
        def stream(active_backend, request, index, total):
            calls.append(active_backend.access_token)
            if len(calls) == 1:
                raise UploadLimitError("upload limit", 60)
            yield result
        def make_backend(access_token):
            backend = Mock(access_token=access_token)
            return backend
        request = conversation.ConversationRequest(model="gpt-image-2", images=[base64.b64encode(b"img").decode()])
        constructor = Mock(side_effect=make_backend)
        constructor._decode_image_base64 = OpenAIBackendAPI._decode_image_base64
        with patch.object(conversation, "account_service", self.service), \
             patch.object(conversation, "OpenAIBackendAPI", constructor), \
             patch.object(conversation, "stream_image_outputs", side_effect=stream):
            outputs = conversation._generate_single_image(request, 1, 1)
        self.assertEqual(outputs, [result])
        self.assertEqual(calls, ["reference-test-a", "reference-test-b"])
        self.assertFalse(self.service._image_inflight)

    def test_second_upload_failure_stops_after_one_retry(self):
        self.service.add_account_items([{"access_token": "reference-test-c", "quota": 10}])
        request = conversation.ConversationRequest(model="gpt-image-2", images=[base64.b64encode(b"img").decode()])
        stream = Mock(side_effect=UploadLimitError("upload limit", 60))
        constructor = Mock(side_effect=lambda **kw: Mock(**kw))
        constructor._decode_image_base64 = OpenAIBackendAPI._decode_image_base64
        with patch.object(conversation, "account_service", self.service), \
             patch.object(conversation, "OpenAIBackendAPI", constructor), \
             patch.object(conversation, "stream_image_outputs", stream):
            with self.assertRaises(conversation.ImageGenerationError) as error:
                conversation._generate_single_image(request, 1, 1)
        self.assertEqual(error.exception.code, "file_upload_limit")
        self.assertEqual(stream.call_count, 2)
        self.assertFalse(self.service._image_inflight)

    def test_backend_reuses_upload_without_spending_quota_twice(self):
        backend = object.__new__(OpenAIBackendAPI)
        backend.access_token = "reference-test-a"
        self.service.update_account(backend.access_token, {"upload_remaining": 2})
        def create(data, filename, on_created):
            on_created()
            return {"file_id": "file-one"}
        with patch("services.openai_backend_api.account_service", self.service), \
             patch.object(backend, "_upload_image_data", side_effect=create) as upload:
            image = base64.b64encode(b"img").decode()
            backend._upload_image(image)
            backend._upload_image("data:image/png;base64," + image)
        upload.assert_called_once()
        self.assertEqual(self.service.get_account(backend.access_token)["upload_remaining"], 1)

    def test_backend_http_limit_cools_account_without_refunding_or_caching(self):
        backend = object.__new__(OpenAIBackendAPI)
        backend.access_token = "reference-test-a"
        self.service.update_account(backend.access_token, {"upload_remaining": 2})
        with patch("services.openai_backend_api.account_service", self.service), \
             patch.object(backend, "_upload_image_data", side_effect=UpstreamHTTPError("/backend-api/files", 429, {}, retry_after=2760)):
            with self.assertRaises(UploadLimitError) as error:
                backend._upload_image(base64.b64encode(b"img").decode())
        self.assertEqual(error.exception.retry_after, 2760)
        self.assertEqual(self.service.get_account(backend.access_token)["upload_remaining"], 0)
        self.assertFalse(self.service._upload_reservations)
        self.assertEqual(reference_upload_cache.missing(backend.access_token, (reference_upload_cache.digest(b"img"),)), 1)
        self.service.update_account(backend.access_token, {"upload_remaining": None, "upload_reset_at": None})
        self.assertFalse(self.service._can_upload_references(self.service.get_account(backend.access_token), ("new",)))

    def test_failed_transfer_keeps_created_file_cost_but_failed_creation_refunds(self):
        backend = object.__new__(OpenAIBackendAPI)
        backend.access_token = "reference-test-a"
        self.service.update_account(backend.access_token, {"upload_remaining": 2})
        def failed_transfer(data, filename, on_created):
            on_created()
            raise RuntimeError("transfer failed")
        with patch("services.openai_backend_api.account_service", self.service), \
             patch.object(backend, "_upload_image_data", side_effect=failed_transfer):
            with self.assertRaises(RuntimeError):
                backend._upload_image(base64.b64encode(b"img").decode())
        self.assertEqual(self.service.get_account(backend.access_token)["upload_remaining"], 1)
        with patch("services.openai_backend_api.account_service", self.service), \
             patch.object(backend, "_upload_image_data", side_effect=RuntimeError("creation failed")):
            with self.assertRaises(RuntimeError):
                backend._upload_image(base64.b64encode(b"img").decode())
        self.assertEqual(self.service.get_account(backend.access_token)["upload_remaining"], 1)
        self.assertFalse(self.service._upload_reservations)

    def test_single_upstream_result_keeps_four_images(self):
        result = conversation.ImageOutput(kind="result", model="gpt-image-2", index=1, total=1,
                                          data=[{"b64_json": str(i)} for i in range(4)])
        constructor = Mock(side_effect=lambda **kw: Mock(**kw))
        request = conversation.ConversationRequest(model="gpt-image-2", n=1)
        with patch.object(conversation, "account_service", self.service), \
             patch.object(conversation, "OpenAIBackendAPI", constructor), \
             patch.object(conversation, "stream_image_outputs", return_value=iter([result])) as stream:
            outputs = list(conversation.stream_image_outputs_with_pool(request))
        stream.assert_called_once()
        self.assertEqual(len(outputs[0].data), 4)
        self.assertFalse(self.service._image_inflight)


if __name__ == "__main__":
    unittest.main()
