from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "test-auth")

from services.account_selection_errors import (
    AccountSelectionError, NoAvailableImageAccountError, validation_diagnostic,
)
from services.account_service import AccountService
from services.config import config
from services.image_task_service import ImageTaskService
from services.log_service import LOG_TYPE_CALL, LoggedCall, log_service
from services.protocol import conversation, openai_v1_image_edit
from services.storage.json_storage import JSONStorageBackend


class AccountSelectionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.logs = self.enterContext(patch.object(log_service, "add"))
        self.enterContext(patch("services.account_service.logger.warning"))
        self.enterContext(patch("services.protocol.conversation.logger.warning"))
        self.enterContext(patch.dict(config.data, {"account_scheduling_mode": "sequential",
                                                  "image_parallel_generation": True}))
        self.service = AccountService(JSONStorageBackend(Path(tmp.name) / "accounts.json"))
        self.service._save_cumulative_total = lambda: None
        self.service.add_account_items([
            {"access_token": "selection-private-a", "email": "a@example.test", "quota": 25,
             "status": "正常", "upload_remaining": 80},
            {"access_token": "selection-private-b", "email": "b@example.test", "quota": 25,
             "status": "正常", "upload_remaining": 80},
        ])
        self.logs.reset_mock()

    def fail_validation(self, token, event=""):
        raise TimeoutError("Failed to perform, curl: (28). Connection timed out")

    def test_all_validation_failures_are_not_reported_as_empty_quota(self):
        self.service.fetch_remote_info = self.fail_validation
        with self.assertRaises(AccountSelectionError) as raised:
            self.service.get_available_access_token(reference_digests=("fresh-reference",))
        error = raised.exception
        self.assertEqual((error.status_code, error.code), (502, "account_validation_failed"))
        self.assertIn("连接上游超时", str(error))
        self.assertEqual(error.account_selection["attempted_accounts"], 2)
        self.assertEqual(error.account_selection["validation_failed"], 2)
        self.assertEqual(self.logs.call_count, 2)
        self.assertFalse(self.service._image_inflight)
        self.assertTrue(all(a["status"] == "正常" and a["quota"] == 25
                            for a in self.service.list_accounts()))
        for call in self.logs.call_args_list:
            self.assertEqual(call.args[1], "生图前账号验证失败")
            self.assertIn("curl: (28)", call.args[2]["error"])
            self.assertNotIn("selection-private", json.dumps(call.args))

    def test_failed_account_is_logged_even_when_next_account_succeeds(self):
        def validate(token, event=""):
            if token.endswith("-a"):
                raise RuntimeError("/backend-api/me failed: HTTP 403")
            return self.service.get_account(token)
        self.service.fetch_remote_info = validate
        token = self.service.get_available_access_token()
        self.assertEqual(token, "selection-private-b")
        self.service.release_image_slot(token)
        self.assertFalse(self.service._image_inflight)
        self.assertEqual(self.logs.call_count, 1)
        self.assertEqual(self.logs.call_args.args[2]["upstream_status"], 403)
        self.assertEqual(self.logs.call_args.args[2]["account_email"], "a@example.test")

    def test_real_upload_threshold_exhaustion_returns_quota_429(self):
        for account in self.service.list_accounts():
            self.service.update_account(account["access_token"], {"upload_remaining": 19}, quiet=True)
        self.service.fetch_remote_info = lambda token, event="": self.fail("Should not validate filtered accounts")
        with self.assertRaises(NoAvailableImageAccountError) as raised:
            self.service.get_available_access_token(reference_digests=("fresh-reference",))
        self.assertEqual((raised.exception.status_code, raised.exception.code), (429, "insufficient_quota"))
        self.assertIn("20", str(raised.exception))
        self.assertFalse(self.service._image_inflight)

    def test_mixed_confirmed_quota_and_unknown_validation_returns_502(self):
        def validate(token, event=""):
            if token.endswith("-a"):
                return self.service.update_account(token, {"quota": 0}, quiet=True)
            raise RuntimeError("/backend-api/conversation/init failed: HTTP 429")
        self.service.fetch_remote_info = validate
        with self.assertRaises(AccountSelectionError) as raised:
            self.service.get_available_access_token()
        self.assertEqual(raised.exception.code, "account_validation_failed")
        self.assertEqual(raised.exception.account_selection["attempted_accounts"], 2)
        self.assertEqual(raised.exception.account_selection["validation_failed"], 1)
        self.assertIn("HTTP 429", str(raised.exception))
        self.assertFalse(self.service._image_inflight)

    def test_attempt_limit_does_not_claim_untried_accounts_have_no_quota(self):
        self.service.add_account_items([{"access_token": f"bound-{i}", "quota": 25} for i in range(19)])
        self.service.fetch_remote_info = lambda token, event="": {"access_token": token, "quota": 0}
        with self.assertRaises(AccountSelectionError) as raised:
            self.service.get_available_access_token()
        self.assertEqual((raised.exception.status_code, raised.exception.code), (503, "account_selection_exhausted"))
        self.assertEqual(raised.exception.account_selection["attempted_accounts"], 20)
        self.assertFalse(self.service._image_inflight)

    def test_parallel_four_image_call_preserves_error_and_logs_diagnostics(self):
        self.service.fetch_remote_info = self.fail_validation
        call = LoggedCall(identity={"id": "qa", "role": "admin"}, endpoint="/v1/images/edits",
                          model="gpt-image-2", summary="图生图")
        body = {"model": "gpt-image-2", "n": 4, "prompt": "test",
                "images": [(b"test-reference", "reference.png", "image/png")]}
        with patch.object(conversation, "account_service", self.service):
            response = asyncio.run(call.run(openai_v1_image_edit.handle, body))
        public = json.loads(response.body)["error"]
        self.assertEqual(response.status_code, 502)
        self.assertEqual(public["code"], "account_validation_failed")
        self.assertIn("连接上游超时", public["message"])
        self.assertNotIn("account_selection", public)
        self.assertNotIn("curl:", public["message"])
        self.assertFalse(self.service._image_inflight)
        call_logs = [c.args[2] for c in self.logs.call_args_list if c.args[0] == LOG_TYPE_CALL]
        self.assertEqual(len(call_logs), 1)
        self.assertEqual(call_logs[0]["error_code"], "account_validation_failed")
        self.assertEqual(call_logs[0]["http_status"], 502)
        self.assertEqual(call_logs[0]["account_selection"]["validation_failed"], 2)

    def test_parallel_quota_error_preserves_http_429(self):
        for account in self.service.list_accounts():
            self.service.update_account(account["access_token"], {"quota": 0}, quiet=True)
        with patch.object(conversation, "account_service", self.service):
            with self.assertRaises(conversation.ImageGenerationError) as raised:
                list(conversation.stream_image_outputs_with_pool(
                    conversation.ConversationRequest(model="gpt-image-2", n=4)))
        self.assertEqual((raised.exception.status_code, raised.exception.code), (429, "insufficient_quota"))

    def test_parallel_validation_error_is_not_hidden_by_another_quota_error(self):
        validation = conversation.ImageGenerationError("验证失败", code="account_validation_failed",
                                                       account_selection={"validation_failed": 1})
        quota = conversation.ImageGenerationError("额度不足", status_code=429, code="insufficient_quota")
        def generate(request, index, total):
            raise validation if index == 1 else quota
        with patch.object(conversation, "_generate_single_image", side_effect=generate):
            with self.assertRaises(conversation.ImageGenerationError) as raised:
                list(conversation.stream_image_outputs_with_pool(
                    conversation.ConversationRequest(model="gpt-image-2", n=4)))
        self.assertIs(raised.exception, validation)

    def test_stream_failure_logs_the_same_diagnostics(self):
        error = conversation.ImageGenerationError("连接上游超时", code="account_validation_failed",
                                                   account_selection={"attempted_accounts": 2, "errors": []})
        def stream():
            yield {"progress": "starting"}
            raise error
        call = LoggedCall(identity={"id": "qa"}, endpoint="/v1/images/edits", model="gpt-image-2", summary="图生图")
        with self.assertRaises(conversation.ImageGenerationError):
            list(call.stream(stream()))
        self.assertEqual(self.logs.call_args.args[2]["account_selection"]["attempted_accounts"], 2)

    def test_background_image_task_log_preserves_diagnostics(self):
        error = conversation.ImageGenerationError("连接上游超时", code="account_validation_failed",
                                                   account_selection={"attempted_accounts": 2, "errors": []})
        service = ImageTaskService.__new__(ImageTaskService)
        service._update_task = Mock()
        service.edit_handler = Mock(side_effect=error)
        service._run_task("task-test", "edit", {"prompt": "test"}, {"id": "qa"}, "gpt-image-2")
        detail = self.logs.call_args.args[2]
        self.assertEqual(detail["error_code"], "account_validation_failed")
        self.assertEqual(detail["http_status"], 502)
        self.assertEqual(detail["account_selection"]["attempted_accounts"], 2)


class DiagnosticTests(unittest.TestCase):
    def test_error_categories_include_original_http_status(self):
        cases = [("HTTP 401", "authentication"), ("HTTP 403", "http_error"),
                 ("HTTP 429", "rate_limit"), ("HTTP 503", "http_error"),
                 ("curl: (28). timeout", "timeout"), ("curl: (35). SSL", "tls"),
                 ("curl: (7). proxy refused", "proxy_connection")]
        for text, category in cases:
            with self.subTest(text=text):
                diagnostic = validation_diagnostic(RuntimeError(text), "")
                self.assertEqual(diagnostic["category"], category)
                self.assertEqual(diagnostic["error"], text)

    def test_diagnostics_scrub_credentials_inside_errors(self):
        token = "private-access-token"
        jwt = "eyJ" + "x" * 35 + ".payload.signature"
        key = "sk-" + "a" * 30
        text = f"{token} Bearer unrelated-secret {jwt} {key} http://user:pass@proxy.test/?token=secret"
        sanitized = validation_diagnostic(RuntimeError(text), token)["error"]
        for secret in [token, "unrelated-secret", jwt, key, "user:pass", "token=secret"]:
            self.assertNotIn(secret, sanitized)


if __name__ == "__main__":
    unittest.main()
