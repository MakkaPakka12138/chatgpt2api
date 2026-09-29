import json
import unittest
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import system
from services.config import config
from services.pool_status_service import PoolStatusCache


class PoolStatusTests(unittest.TestCase):
    def make_cache(self, interval=30):
        self.items = [
            {"type": "free", "status": "正常", "quota": 10, "upload_remaining": 19, "image_inflight": 1},
            {"type": "plus", "status": "正常", "quota": 20, "upload_remaining": 20},
            {"type": "free", "status": "正常", "quota": 5, "upload_remaining": 0, "upload_reset_at": "2000-01-01T00:00:00Z"},
            {"type": "free", "status": "正常", "quota": 5, "upload_remaining": 0, "upload_blocked_until": "2099-01-01T00:00:00Z"},
            {"type": "plus", "source_type": "codex", "status": "正常", "quota": 50, "upload_remaining": 99},
            {"type": "free", "status": "禁用", "quota": 500, "upload_remaining": 80},
        ]
        self.accounts = Mock()
        self.snapshot = {"items": self.items, "quarantined": 2, "cumulative_total": 10}
        self.accounts.pool_summary_snapshot.return_value = self.snapshot
        settings = SimpleNamespace(app_version="1.8.0", account_scheduling_mode="sequential", refresh_account_interval_minute=5)
        return PoolStatusCache(self.accounts, settings, interval)

    def test_summary_counts_threshold_unknown_cooldown_and_codex(self):
        data = self.make_cache().get()
        self.assertEqual(data["accounts"]["total"], 8)
        self.assertEqual(data["accounts"]["quarantined"], 2)
        self.assertEqual(data["images"]["remaining_total"], 90)
        self.assertEqual(data["images"]["inflight"], 1)
        self.assertEqual(data["uploads"]["eligible_accounts"], 2)
        self.assertEqual(data["uploads"]["available_known_remaining"], 20)
        self.assertEqual(data["uploads"]["known_remaining_total"], 119)
        self.assertEqual(data["uploads"]["unknown_accounts"], 1)
        self.assertEqual(data["uploads"]["low_accounts"], 2)
        self.assertEqual(data["uploads"]["blocked_accounts"], 1)
        self.assertEqual(data["uploads"]["not_applicable_accounts"], 1)
        self.assertEqual(data["uploads"]["next_reset_at"], "2099-01-01T00:00:00+00:00")

    def test_reads_use_cache_and_cannot_mutate_it(self):
        cache = self.make_cache()
        first = cache.get()
        first["images"]["remaining_total"] = -1
        self.items[0]["quota"] = 0
        self.assertEqual(cache.get()["images"]["remaining_total"], 90)
        self.accounts.pool_summary_snapshot.assert_called_once()
        cache.refresh()
        self.assertEqual(cache.get()["images"]["remaining_total"], 80)
        self.accounts.fetch_remote_info.assert_not_called()

    def test_worker_refreshes_and_stops(self):
        cache = self.make_cache(interval=0.01)
        refreshed = Event()
        calls = 0
        def snapshot():
            nonlocal calls
            calls += 1
            if calls > 1:
                refreshed.set()
            return self.snapshot
        self.accounts.pool_summary_snapshot.side_effect = snapshot
        stop = Event()
        thread = cache.start(stop)
        try:
            self.assertTrue(refreshed.wait(1))
        finally:
            stop.set()
            thread.join(1)
        self.assertFalse(thread.is_alive())

    def test_failed_refresh_keeps_previous_data_and_reports_staleness(self):
        cache = self.make_cache()
        with patch("services.pool_status_service.time.monotonic", return_value=100):
            cache.refresh()
        self.accounts.pool_summary_snapshot.side_effect = RuntimeError("temporary")
        with self.assertRaises(RuntimeError):
            cache.refresh()
        with patch("services.pool_status_service.time.monotonic", return_value=161):
            data = cache.get()
        self.assertTrue(data["cache"]["stale"])
        self.assertEqual(data["images"]["remaining_total"], 90)

    def test_empty_pool_and_no_credentials_in_response(self):
        cache = self.make_cache()
        self.items[0].update(access_token="secret-token", email="secret-email", refresh_token="secret-refresh", password="secret-password")
        output = json.dumps(cache.get())
        self.assertNotIn("secret", output)
        self.accounts.pool_summary_snapshot.return_value = {"items": [], "quarantined": 0, "cumulative_total": 0}
        cache.refresh()
        self.assertEqual(cache.get()["status"], "degraded")
        self.assertEqual(cache.get()["images"]["remaining_total"], 0)

    def test_api_requires_admin(self):
        cache = self.make_cache()
        app = FastAPI()
        app.include_router(system.create_router("test"))
        with patch.object(system, "pool_status_cache", cache):
            client = TestClient(app)
            self.assertEqual(client.get("/api/pool/status").status_code, 401)
            with patch("api.support.auth_service.authenticate", return_value={"role": "user"}):
                self.assertEqual(client.get("/api/pool/status", headers={"Authorization": "Bearer test-user"}).status_code, 403)
            response = client.get("/api/pool/status", headers={"Authorization": "Bearer " + config.auth_key})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["scheduling_mode"], "sequential")


if __name__ == "__main__":
    unittest.main()
