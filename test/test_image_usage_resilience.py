from __future__ import annotations

import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.protocol import conversation, image_usage, openai_v1_image_edit, openai_v1_image_generations
from api import ai
from services.log_service import log_service
from utils.tokenizer import EstimatedEncoding


class ImageUsageResilienceTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(ai.create_router())
        self.client = TestClient(app)
        self.items = [{"url": f"http://testserver/images/{i}.png"} for i in range(5)]
        for target, replacement in (
            ("api.ai.require_identity", {"id": "test", "role": "admin", "name": "Test"}),
            ("api.ai.check_request", None),
        ):
            patcher = mock.patch(target, return_value=replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(log_service, "add")
        self.log = patcher.start()
        self.addCleanup(patcher.stop)

    def _request(self, edit=False):
        module = openai_v1_image_edit if edit else openai_v1_image_generations
        output = conversation.ImageOutput(kind="result", model="gpt-image-2", index=0, total=4, data=self.items)
        with mock.patch.object(module, "stream_image_outputs_with_pool", return_value=iter([output])):
            if edit:
                return self.client.post("/v1/images/edits", data={"prompt": "test", "n": "4"},
                                        files={"image": ("ref.png", b"reference", "image/png")})
            return self.client.post("/v1/images/generations", json={"prompt": "test", "n": 4})

    def _assert_preserved(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"], self.items)
        usage = response.json()["usage"]
        self.assertEqual(usage["total_tokens"], usage["input_tokens"] + usage["output_tokens"])
        self.assertEqual(self.log.call_args.args[2]["status"], "success")
        return usage

    def test_connection_reset_during_text_counting_preserves_all_five_images(self):
        for edit in (False, True):
            with self.subTest(edit=edit), mock.patch.object(image_usage, "count_text_tokens", side_effect=ConnectionError("reset")):
                usage = self._assert_preserved(self._request(edit))
                self.assertTrue(usage["estimated"])
                self.assertIn("input_text_tokens", usage["estimated_fields"])
                self.assertGreater(usage["output_tokens"], 0)

    def test_missing_tokenizer_marks_fallback_in_http_response(self):
        with mock.patch("utils.tokenizer.encoding_for_model", return_value=EstimatedEncoding()), \
             mock.patch.object(conversation, "encoding_for_model", return_value=EstimatedEncoding()), \
             mock.patch.object(image_usage, "encoding_for_model", return_value=EstimatedEncoding()):
            usage = self._assert_preserved(self._request())
            self.assertTrue(usage["estimated"])

    def test_image_counting_failures_preserve_response(self):
        with mock.patch.object(image_usage, "count_image_inputs_tokens", side_effect=ValueError), \
             mock.patch.object(image_usage, "count_image_output_items_tokens", side_effect=ValueError):
            usage = self._assert_preserved(self._request(True))
            self.assertTrue(usage["estimated"])
            self.assertIn("output_image_tokens", usage["estimated_fields"])
            self.assertIn("output_image_tokens", usage["unavailable_fields"])

    def test_working_tokenizer_keeps_existing_usage_shape(self):
        with mock.patch.object(image_usage, "count_text_tokens", return_value=1), \
             mock.patch.object(image_usage, "encoding_for_model", return_value=object()):
            usage = self._assert_preserved(self._request())
            self.assertNotIn("estimated", usage)
            self.assertEqual(usage["input_tokens_details"]["text_tokens"], 1)
