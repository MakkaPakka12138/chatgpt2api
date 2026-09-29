import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.account_service import AccountService
from services.config import ConfigStore, config
from services.reference_uploads import reference_upload_cache
from services.storage.json_storage import JSONStorageBackend


class AccountSchedulingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.service = AccountService(JSONStorageBackend(Path(tmp.name) / "accounts.json"))
        self.service.add_account_items([
            {"access_token": "schedule-a", "type": "free", "status": "正常", "quota": 2, "upload_remaining": 20},
            {"access_token": "schedule-b", "type": "plus", "status": "正常", "quota": 2, "upload_remaining": 30},
        ])
        self.service.fetch_remote_info = lambda token, event="", **kwargs: self.service.get_account(token)
        self.service.refresh_access_token = lambda token, **kwargs: token
        self.mode = patch.dict(config.data, {"account_scheduling_mode": "sequential", "auto_remove_rate_limited_accounts": False})
        self.mode.start()
        self.addCleanup(self.mode.stop)
        for token in ("schedule-a", "schedule-b"):
            reference_upload_cache.invalidate(token)
            self.addCleanup(reference_upload_cache.invalidate, token)

    def select_image(self, **kwargs):
        token = self.service.get_available_access_token(**kwargs)
        self.service.release_image_slot(token)
        return token

    def test_sequential_uses_first_until_image_quota_exhausts(self):
        for _ in range(2):
            token = self.service.get_available_access_token()
            self.assertEqual(token, "schedule-a")
            self.service.mark_image_result(token, True)
        self.assertEqual(self.select_image(), "schedule-b")

    def test_sequential_obeys_upload_threshold_and_exclusions(self):
        digests = (reference_upload_cache.digest(b"new"),)
        self.assertEqual(self.select_image(reference_digests=digests), "schedule-a")
        self.service.update_account("schedule-a", {"upload_remaining": 19})
        self.assertEqual(self.select_image(reference_digests=digests), "schedule-b")
        self.assertEqual(self.select_image(), "schedule-a")
        self.assertEqual(self.select_image(excluded_tokens={"schedule-a"}), "schedule-b")
        self.assertEqual(self.select_image(plan_type="plus"), "schedule-b")

    def test_sequential_skips_busy_account_and_returns_to_it(self):
        with patch.dict(config.data, {"image_account_concurrency": 1}):
            first = self.service.get_available_access_token()
            second = self.service.get_available_access_token()
            self.assertEqual((first, second), ("schedule-a", "schedule-b"))
            self.service.release_image_slot(first)
            self.service.release_image_slot(second)
            self.assertEqual(self.select_image(), "schedule-a")

    def test_modes_can_switch_live_for_text_and_images(self):
        self.assertEqual([self.service.get_text_access_token() for _ in range(2)], ["schedule-a"] * 2)
        self.assertEqual(self.service.get_text_access_token(excluded_tokens={"schedule-a"}), "schedule-b")
        with patch.dict(config.data, {"account_scheduling_mode": "round_robin"}):
            self.assertEqual([self.select_image() for _ in range(2)], ["schedule-a", "schedule-b"])
            self.assertEqual([self.service.get_text_access_token() for _ in range(2)], ["schedule-a", "schedule-b"])
        self.assertEqual(self.select_image(), "schedule-a")
        self.service.update_account("schedule-a", {"status": "异常"})
        self.assertEqual(self.service.get_text_access_token(), "schedule-b")

    def test_cache_affinity_does_not_override_sequential_order(self):
        reference_upload_cache.get_or_upload("schedule-b", b"ref", lambda: {"file_id": "cached"})
        digests = (reference_upload_cache.digest(b"ref"),)
        self.assertEqual(self.select_image(reference_digests=digests), "schedule-a")
        with patch.dict(config.data, {"account_scheduling_mode": "round_robin"}):
            self.assertEqual(self.select_image(reference_digests=digests), "schedule-b")


class SchedulingConfigTests(unittest.TestCase):
    def test_defaults_persistence_validation_and_unrelated_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"auth-key": "test-auth", "image_poll_timeout_secs": 123}))
            store = ConfigStore(path)
            self.assertEqual(store.get()["account_scheduling_mode"], "round_robin")
            store.update({"account_scheduling_mode": "sequential"})
            self.assertEqual(ConfigStore(path).account_scheduling_mode, "sequential")
            self.assertEqual(store.image_poll_timeout_secs, 123)
            before = path.read_bytes()
            with self.assertRaises(ValueError):
                store.update({"account_scheduling_mode": "invalid"})
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
