from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from services.protocol import conversation
from services.log_service import LoggedCall, log_service
from services.openai_backend_api import OpenAIBackendAPI
from utils.upstream_diagnostics import UpstreamTrace, bind_trace, current_trace, record_request, redact_diagnostic


TLS_ERROR = "Failed to perform, curl: (35) TLS connect error: OPENSSL_internal:invalid library (0)"


class TextRetryTests(unittest.TestCase):
    def setUp(self):
        self.trace = UpstreamTrace()
        self.created = []
        self.request = conversation.ConversationRequest(model="auto", messages=[{"role": "user", "content": "hello"}])
        self.initial = SimpleNamespace(access_token="private-test-token", close=mock.Mock())
        self.used = mock.Mock()
        self.refresh = mock.Mock(return_value="replacement")
        self.remove = mock.Mock()
        self.select = mock.Mock(return_value="replacement")
        self.wait = mock.Mock()
        patches = [
            mock.patch.object(conversation, "OpenAIBackendAPI", side_effect=self.factory),
            mock.patch.object(conversation.account_service, "mark_text_used", self.used),
            mock.patch.object(conversation.account_service, "refresh_access_token", self.refresh),
            mock.patch.object(conversation.account_service, "remove_invalid_token", self.remove),
            mock.patch.object(conversation.account_service, "get_text_access_token", self.select),
            mock.patch.object(conversation.time, "sleep", self.wait),
            mock.patch.object(conversation.proxy_settings, "get_profile", return_value=SimpleNamespace(
                proxy_url="http://proxy-user:proxy-password@proxy.test:4585", proxy_source="global", pool_id="",
                runtime_enabled=False, skip_ssl_verify=False)),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def factory(self, access_token):
        backend = SimpleNamespace(access_token=access_token, close=mock.Mock(), account={"email": "test@example.test"})
        self.created.append(backend)
        return backend

    def run_events(self, events):
        with bind_trace(self.trace), mock.patch.object(conversation, "conversation_events", side_effect=events):
            return list(conversation.stream_text_deltas(self.initial, self.request))

    def assert_no_account_mutation(self):
        self.refresh.assert_not_called()
        self.remove.assert_not_called()
        self.select.assert_not_called()

    def test_tls_reconnects_once_with_same_account_and_preserves_diagnostics(self):
        def events(backend, **kwargs):
            backend.diagnostic_attempt["stage"] = "requirements_prepare"
            if len(self.created) == 1:
                raise RuntimeError(TLS_ERROR)
            yield {"type": "conversation.delta", "delta": "ok"}
        self.assertEqual(self.run_events(events), ["ok"])
        self.assertEqual([b.access_token for b in self.created], ["private-test-token"] * 2)
        self.wait.assert_called_once_with(1.0)
        self.used.assert_called_once_with("private-test-token")
        self.assert_no_account_mutation()
        self.initial.close.assert_called_once()
        for backend in self.created:
            backend.close.assert_called_once()
        fields = self.trace.fields()
        self.assertTrue(fields["network_trace"]["recovered"])
        self.assertEqual(fields["network_trace"]["retry_count"], 1)
        first = fields["network_trace"]["attempts"][0]
        self.assertEqual(first["curl_code"], 35)
        self.assertEqual(first["stage"], "requirements_prepare")
        self.assertEqual(first["proxy"], "http://proxy.test:4585")
        for secret in ("private-test-token", "proxy-password", "proxy-user"):
            self.assertNotIn(secret, json.dumps(fields))

    def test_repeated_tls_stops_after_second_attempt(self):
        def events(*args, **kwargs):
            raise RuntimeError(TLS_ERROR)
        with self.assertRaisesRegex(RuntimeError, "curl"):
            self.run_events(events)
        self.assertEqual(len(self.created), 2)
        self.used.assert_not_called()
        self.assert_no_account_mutation()
        self.assertEqual(self.trace.attempts[-1]["retry_action"], "stop")

    def test_dns_and_connect_failures_use_same_bounded_retry(self):
        for code in (5, 6, 7):
            with self.subTest(code=code):
                self.trace = UpstreamTrace()
                self.created.clear()
                with self.assertRaises(RuntimeError):
                    self.run_events(lambda *a, **kw: (_ for _ in ()).throw(RuntimeError(f"curl: ({code})")))
                self.assertEqual(len(self.created), 2)

    def test_timeouts_resets_certificate_and_http_errors_are_not_replayed(self):
        for message in ("curl: (28)", "curl: (56)", "curl: (60)", "HTTP 429 quota exceeded", "HTTP 500"):
            with self.subTest(message=message):
                self.trace = UpstreamTrace()
                self.created.clear()
                with self.assertRaises(RuntimeError):
                    self.run_events(lambda *a, **kw: (_ for _ in ()).throw(RuntimeError(message)))
                self.assertEqual(len(self.created), 1)
        self.wait.assert_not_called()
        self.assert_no_account_mutation()

    def test_accepted_conversation_without_delta_is_not_replayed(self):
        def events(backend, **kwargs):
            backend.text_conversation_accepted = True
            raise RuntimeError(TLS_ERROR)
        with self.assertRaises(RuntimeError):
            self.run_events(events)
        self.assertEqual(len(self.created), 1)
        self.assertEqual(self.trace.attempts[0]["retry_action"], "stop_upstream_accepted")

    def test_failure_after_first_delta_is_not_replayed(self):
        def events(backend, **kwargs):
            yield {"type": "conversation.delta", "delta": "first"}
            raise RuntimeError(TLS_ERROR)
        with bind_trace(self.trace), mock.patch.object(conversation, "conversation_events", side_effect=events):
            stream = conversation.stream_text_deltas(self.initial, self.request)
            self.assertEqual(next(stream), "first")
            with self.assertRaises(RuntimeError):
                next(stream)
        self.assertEqual(len(self.created), 1)
        self.assertEqual(self.trace.attempts[0]["retry_action"], "stop_reply_started")

    def test_auth_rotation_and_network_retry_share_three_attempt_budget(self):
        def events(backend, **kwargs):
            if len(self.created) == 1:
                raise RuntimeError("token_invalidated")
            raise RuntimeError(TLS_ERROR)
        with self.assertRaises(RuntimeError):
            self.run_events(events)
        self.assertEqual(len(self.created), 3)
        self.assertEqual([b.access_token for b in self.created], ["private-test-token", "replacement", "replacement"])
        self.refresh.assert_called_once()
        self.used.assert_not_called()


class TraceIntegrationTests(unittest.TestCase):
    def test_http_steps_are_bounded_and_exclude_bodies_and_headers(self):
        trace = UpstreamTrace()
        attempt = trace.start_attempt("secret")
        backend = SimpleNamespace(diagnostic_attempt=attempt, session=mock.Mock())
        backend.session.post.return_value = SimpleNamespace(status_code=200)
        for _ in range(20):
            record_request(backend, "post", "https://chatgpt.com/endpoint?token=private", "requirements_prepare",
                           headers={"Authorization": "Bearer secret"}, json={"secret": "body"})
        self.assertEqual(len(attempt["steps"]), 16)
        serialized = json.dumps(trace.fields())
        for secret in ("private", "Bearer", "body", "Authorization"):
            self.assertNotIn(secret, serialized)
        self.assertEqual(attempt["steps"][0]["http_status"], 200)

    def test_real_backend_marks_conversation_accepted_before_reading_stream(self):
        backend = OpenAIBackendAPI.__new__(OpenAIBackendAPI)
        backend.access_token = "test"
        backend.base_url = "https://chatgpt.com"
        backend.session = mock.Mock()
        response = SimpleNamespace(status_code=200, close=mock.Mock())
        backend.session.post.return_value = response
        backend.diagnostic_attempt = UpstreamTrace().start_attempt("test")
        for name, value in (("_bootstrap", None), ("_get_chat_requirements", object()),
                            ("_chat_target", ("/backend-api/conversation", "Asia/Tokyo")),
                            ("_conversation_payload", {}), ("_conversation_headers", {})):
            setattr(backend, name, mock.Mock(return_value=value))
        with mock.patch("services.openai_backend_api.iter_sse_payloads", side_effect=RuntimeError(TLS_ERROR)):
            with self.assertRaises(RuntimeError):
                list(backend.stream_conversation(prompt="hello"))
        self.assertTrue(backend.text_conversation_accepted)
        self.assertEqual(backend.diagnostic_attempt["stage"], "conversation_stream")
        response.close.assert_called_once()
        backend._closed = True

    def test_json_call_trace_crosses_worker_boundary_and_stays_out_of_response(self):
        call = LoggedCall({}, "/v1/chat/completions", "auto", "文本生成")
        def handler():
            trace = current_trace()
            self.assertIs(trace, call.trace)
            trace.start_attempt("test")["status"] = "success"
            return {"choices": [{"text": "ok"}]}
        with mock.patch.object(log_service, "add") as add:
            result = asyncio.run(call.run(handler))
        self.assertNotIn("network_trace", result)
        self.assertNotIn("request_id", result)
        detail = add.call_args.args[2]
        self.assertEqual(detail["request_id"], call.trace.request_id)
        self.assertEqual(detail["network_trace"]["attempts"][0]["status"], "success")
        self.assertIsNone(current_trace())

    def test_stream_trace_covers_first_item_and_remaining_items(self):
        call = LoggedCall({}, "/v1/chat/completions", "auto", "文本生成")
        def handler():
            self.assertIs(current_trace(), call.trace)
            attempt = current_trace().start_attempt("test")
            yield {"choices": [{"delta": {"content": "first"}}]}
            self.assertIs(current_trace(), call.trace)
            attempt["status"] = "success"
            yield {"choices": [{"delta": {"content": "last"}}]}
        async def run():
            response = await call.run(handler)
            return "".join([str(chunk) async for chunk in response.body_iterator])
        with mock.patch.object(log_service, "add") as add:
            body = asyncio.run(run())
        self.assertIn("first", body)
        self.assertIn("last", body)
        self.assertNotIn("network_trace", body)
        self.assertEqual(add.call_args.args[2]["network_trace"]["attempts"][0]["status"], "success")

    def test_failure_before_first_stream_item_retains_trace(self):
        call = LoggedCall({}, "/v1/chat/completions", "auto", "文本生成")
        def handler():
            attempt = current_trace().start_attempt("test")
            attempt.update(status="failed", error_category="tls_connect", curl_code=35)
            raise RuntimeError(TLS_ERROR)
            yield
        with mock.patch.object(log_service, "add") as add:
            response = asyncio.run(call.run(handler))
        self.assertEqual(response.status_code, 502)
        detail = add.call_args.args[2]
        self.assertEqual(detail["network_trace"]["attempts"][0]["curl_code"], 35)
        self.assertEqual(detail["status"], "failed")

    def test_sensitive_error_strings_are_scrubbed(self):
        message = "Bearer private eyJabc.def.ghi sk-abcdefghijklmnop http://user:password@proxy.test:4585/path?token=secret"
        value = redact_diagnostic(message)
        for secret in ("private", "eyJabc.def.ghi", "sk-abcdefghijklmnop", "user:password", "token=secret"):
            self.assertNotIn(secret, value)


if __name__ == "__main__":
    unittest.main()
