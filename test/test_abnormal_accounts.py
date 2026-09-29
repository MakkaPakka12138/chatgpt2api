from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.accounts as accounts_api
from services.account_service import AccountService
from services.config import config
from services.storage.json_storage import JSONStorageBackend


class AbnormalAccountsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage = JSONStorageBackend(Path(self.tmp.name) / "accounts.json")
        self.service = AccountService(self.storage)
        flag_patch = patch.dict(config.data, {"auto_remove_invalid_accounts": True, "auto_remove_rate_limited_accounts": False})
        flag_patch.start()
        self.addCleanup(flag_patch.stop)
        cumulative_patch = patch.object(self.service, "_save_cumulative_total")
        cumulative_patch.start()
        self.addCleanup(cumulative_patch.stop)
        self.service.add_account_items([{
            "access_token": "bad-token", "email": "account@example.test", "quota": 5,
            "refresh_token": "saved-refresh", "password": "saved-password",
            "last_refresh_error": "token_invalidated",
        }, {"access_token": "healthy-token", "quota": 3}])

    def quarantine(self):
        self.assertTrue(self.service.remove_invalid_token("bad-token", "test_invalid"))

    def test_auto_removal_retains_credentials_across_restart_but_never_schedules(self):
        self.quarantine()
        self.assertIsNone(self.service.get_account("bad-token"))
        reloaded = AccountService(self.storage)
        self.assertEqual(reloaded.list_tokens(), ["healthy-token"])
        self.assertEqual(reloaded._list_ready_candidate_tokens(), ["healthy-token"])
        self.assertEqual(reloaded._quarantined_accounts["bad-token"]["refresh_token"], "saved-refresh")
        self.assertEqual(reloaded._quarantined_accounts["bad-token"]["password"], "saved-password")
        listed = reloaded.list_abnormal_accounts()[0]
        self.assertEqual(listed["email"], "account@example.test")
        self.assertEqual(listed["quarantine_reason"], "token_invalidated")
        self.assertTrue(listed["quarantined_at"])
        self.assertNotIn("password", listed)
        self.assertNotIn("refresh_token", listed)

    def test_disabled_auto_removal_keeps_account_visible_in_abnormal_page(self):
        with patch.dict(config.data, {"auto_remove_invalid_accounts": False}):
            self.assertFalse(self.service.remove_invalid_token("bad-token", "manual", error="expired"))
        self.assertEqual(self.service.get_account("bad-token")["status"], "异常")
        self.assertEqual(self.service.list_abnormal_accounts()[0]["last_refresh_error"], "expired")
        self.assertEqual(self.service._list_ready_candidate_tokens(), ["healthy-token"])

    def test_recovery_verifies_before_restoring_and_retains_refresh_token(self):
        self.quarantine()
        def validate(token, event, defer_invalid_removal):
            self.assertEqual(self.service.get_account(token)["status"], "异常")
            self.assertNotIn(token, self.service._list_ready_candidate_tokens())
            self.assertEqual(self.service.get_account(token)["refresh_token"], "saved-refresh")
            return self.service.update_account(token, {"status": "正常", "quota": 7})
        with patch.object(self.service, "fetch_remote_info", side_effect=validate):
            result = self.service.recover_abnormal_accounts(["bad-token"])
        self.assertEqual(result["restored"], 1)
        self.assertEqual(result["items"], [])
        self.assertEqual(self.service.get_account("bad-token")["quota"], 7)
        self.assertNotIn("quarantined_at", self.service.get_account("bad-token"))
        reloaded = AccountService(self.storage)
        self.assertIsNotNone(reloaded.get_account("bad-token"))
        self.assertFalse(reloaded.list_abnormal_accounts())

    def test_failed_recovery_keeps_archive_and_reason_after_restart(self):
        self.quarantine()
        with patch.object(self.service, "fetch_remote_info", side_effect=RuntimeError("still invalid")):
            result = self.service.recover_abnormal_accounts(["bad-token"])
        self.assertEqual(result["restored"], 0)
        self.assertEqual(result["errors"][0]["error"], "still invalid")
        reloaded = AccountService(self.storage)
        self.assertIsNone(reloaded.get_account("bad-token"))
        self.assertEqual(reloaded.list_abnormal_accounts()[0]["quarantine_reason"], "still invalid")

    def test_failed_validation_that_quarantines_itself_does_not_duplicate_record(self):
        self.quarantine()
        def invalid(token, event, defer_invalid_removal):
            self.service.remove_invalid_token(token, event, error="invalid again")
            raise RuntimeError("invalid again")
        with patch.object(self.service, "fetch_remote_info", side_effect=invalid):
            self.service.recover_abnormal_accounts(["bad-token"])
        self.assertEqual(len(self.service.list_abnormal_accounts()), 1)
        self.assertEqual(len(self.storage.load_accounts()), 2)

    def test_manual_delete_permanently_removes_archive_but_protects_healthy_account(self):
        self.quarantine()
        result = self.service.delete_abnormal_accounts(["bad-token", "healthy-token"])
        self.assertEqual(result["removed"], 1)
        self.assertFalse(AccountService(self.storage).list_abnormal_accounts())
        self.assertIsNotNone(self.service.get_account("healthy-token"))
        self.assertEqual(len(self.storage.load_accounts()), 1)

    def test_reimport_archived_token_keeps_credentials_without_duplicate_snapshot(self):
        self.quarantine()
        self.service.add_accounts(["bad-token"])
        self.assertEqual(self.service.get_account("bad-token")["refresh_token"], "saved-refresh")
        self.assertEqual(len(self.storage.load_accounts()), 2)
        self.assertNotIn("bad-token", self.service._quarantined_accounts)
        self.assertIsNotNone(AccountService(self.storage).get_account("bad-token"))

    def test_rate_limited_auto_removal_is_also_retained(self):
        with patch.dict(config.data, {"auto_remove_rate_limited_accounts": True}):
            self.service.update_account("bad-token", {"status": "限流", "quota": 0})
        self.assertIsNone(self.service.get_account("bad-token"))
        self.assertEqual(self.service.list_abnormal_accounts()[0]["status"], "限流")
        self.assertEqual(len(AccountService(self.storage).list_abnormal_accounts()), 1)

    def test_recover_rejects_healthy_account_and_concurrent_recovery(self):
        with patch.object(self.service, "fetch_remote_info") as remote:
            result = self.service.recover_abnormal_accounts(["healthy-token"])
        self.assertEqual(result["restored"], 0)
        remote.assert_not_called()
        self.service._abnormal_recovery_lock.acquire()
        try:
            with self.assertRaises(RuntimeError):
                self.service.recover_abnormal_accounts(["bad-token"])
        finally:
            self.service._abnormal_recovery_lock.release()

    def api_client(self):
        app = FastAPI()
        app.include_router(accounts_api.create_router())
        return TestClient(app)

    def test_api_admin_can_list_recover_and_delete(self):
        self.quarantine()
        headers = {"Authorization": "Bearer " + config.auth_key}
        with patch.object(accounts_api, "account_service", self.service):
            client = self.api_client()
            response = client.get("/api/accounts/abnormal", headers=headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.json()["items"]), 1)
            with patch.object(self.service, "fetch_remote_info", side_effect=RuntimeError("test invalid")):
                response = client.post("/api/accounts/abnormal/recover", headers=headers, json={"tokens": ["bad-token"]})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["restored"], 0)
            response = client.request("DELETE", "/api/accounts/abnormal", headers=headers, json={"tokens": ["bad-token"]})
            self.assertEqual(response.json()["removed"], 1)

    def test_api_denies_non_admin_and_checks_input(self):
        with patch.object(accounts_api, "account_service", self.service), \
             patch("api.support.auth_service.authenticate", return_value={"role": "user"}):
            client = self.api_client()
            user_headers = {"Authorization": "Bearer user-test-key"}
            self.assertEqual(client.get("/api/accounts/abnormal", headers=user_headers).status_code, 403)
            self.assertEqual(client.post("/api/accounts/abnormal/recover", headers=user_headers, json={"tokens": ["bad-token"]}).status_code, 403)
            self.assertEqual(client.request("DELETE", "/api/accounts/abnormal", headers=user_headers, json={"tokens": ["bad-token"]}).status_code, 403)
            admin = {"Authorization": "Bearer " + config.auth_key}
            self.assertEqual(client.post("/api/accounts/abnormal/recover", headers=admin, json={"tokens": []}).status_code, 400)
            self.assertEqual(client.post("/api/accounts/abnormal/recover", headers=admin, json={"tokens": [str(i) for i in range(21)]}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
