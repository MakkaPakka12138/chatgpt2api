from __future__ import annotations

import json
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest import mock

from services.proxy_pool_service import ProxyPoolService, account_key, probe_proxy
from services.proxy_service import proxy_settings, ProxyPoolSession


class ProxyPoolTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "pool.json"
        self.now = 1000
        self.results = {}
        def probe(url):
            return self.results.get(url, {"ok": True, "exit_ip": "1.2.3." + url.rsplit(":", 1)[-1], "category": "ok", "latency_ms": 10})
        self.pool = ProxyPoolService(self.path, probe, lambda: self.now)
        patcher = mock.patch("services.proxy_pool_service.proxy_pool", self.pool)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(proxy_settings._config, "get_proxy_settings", return_value="http://global:8080")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.a = {"access_token": "account-a"}

    def node(self, name, port, concurrency=2):
        node_id = self.pool.save_node(name, f"http://proxy:{port}", max_concurrency=concurrency)
        self.pool.check(node_id)
        return node_id

    def fail(self, node_id, category="network_error", times=3):
        node = self.pool._node(node_id)
        self.results[node["url"]] = {"ok": False, "category": category, "exit_ip": node["exit_ip"]}
        for _ in range(times):
            self.pool.check(node_id)

    def test_disabled_pool_preserves_legacy_proxy_and_does_not_assign(self):
        self.node("A", 1)
        self.assertEqual(proxy_settings.get_profile(account=self.a).proxy_url, "http://global:8080")
        account = {**self.a, "proxy": "http://manual:9000"}
        self.assertEqual(proxy_settings.get_profile(account=account).proxy_source, "account")
        self.assertEqual(self.pool.state["bindings"], {})

    def test_enabled_empty_or_unverified_pool_uses_global(self):
        self.pool.configure(True)
        self.pool.save_node("unknown", "http://proxy:1234")
        self.assertEqual(proxy_settings.get_profile(account=self.a).proxy_url, "http://global:8080")
        self.assertEqual(self.pool.describe(self.a)["node_id"], "")

    def test_multiple_accounts_are_balanced_and_persistent(self):
        first, second = self.node("A", 1), self.node("B", 2)
        self.pool.configure(True)
        accounts = [{"access_token": str(i)} for i in range(8)]
        routes = [self.pool.route(a)["id"] for a in accounts]
        self.assertEqual(routes.count(first), 4)
        self.assertEqual(routes.count(second), 4)
        restarted = ProxyPoolService(self.path)
        self.assertEqual([restarted.route(a)["id"] for a in accounts], routes)

    def test_manual_selection_global_and_legacy_modes(self):
        first = self.node("A", 1)
        self.pool.configure(True)
        account = {**self.a, "proxy": "http://manual:9000"}
        self.pool.assign(account, first)
        self.assertEqual(proxy_settings.get_profile(account=account).pool_id, first)
        self.pool.assign(account, "global")
        self.assertEqual(proxy_settings.get_profile(account=account).proxy_url, "http://global:8080")
        self.pool.assign(account, "legacy")
        self.assertEqual(proxy_settings.get_profile(account=account).proxy_url, "http://manual:9000")

    def test_whole_failed_group_moves_to_one_proxy_and_other_group_stays(self):
        first, second = self.node("A", 1), self.node("B", 2)
        self.pool.configure(True)
        group = [{"access_token": str(i)} for i in range(5)]
        for account in group:
            self.pool.assign(account, first)
        other = {"access_token": "other"}
        self.pool.assign(other, second)
        self.fail(first, times=2)
        self.assertEqual(self.pool.route(group[0])["id"], first)
        self.fail(first, times=1)
        self.assertEqual([self.pool.route(a)["id"] for a in group], [second] * 5)
        self.assertEqual(self.pool.route(other)["id"], second)
        self.assertEqual(self.pool.public()["events"][-1]["accounts"], 5)

    def test_no_backup_proxy_falls_back_to_global_then_reassigns(self):
        first = self.node("A", 1)
        self.pool.configure(True)
        self.pool.assign(self.a, first)
        self.fail(first)
        self.assertEqual(proxy_settings.get_profile(account=self.a).proxy_url, "http://global:8080")
        second = self.node("B", 2)
        self.assertEqual(self.pool.route(self.a)["id"], second)

    def test_upstream_500_does_not_trigger_proxy_failover(self):
        first, _ = self.node("A", 1), self.node("B", 2)
        self.pool.configure(True)
        self.pool.assign(self.a, first)
        self.fail(first, "upstream_error", times=5)
        self.assertEqual(self.pool.route(self.a)["id"], first)
        self.assertEqual(self.pool.public()["events"], [])

    def test_recovery_requires_two_successes_and_does_not_move_accounts_back(self):
        first, second = self.node("A", 1), self.node("B", 2)
        self.pool.configure(True)
        self.pool.assign(self.a, first)
        self.fail(first)
        self.results.pop(self.pool._node(first)["url"])
        self.pool.check(first)
        self.assertEqual(self.pool._node(first)["status"], "cooldown")
        self.pool.check(first)
        self.assertEqual(self.pool._node(first)["status"], "healthy")
        self.assertEqual(self.pool.route(self.a)["id"], second)

    def test_inflight_route_is_frozen_while_later_task_uses_migrated_proxy(self):
        first, second = self.node("A", 1), self.node("B", 2)
        self.pool.configure(True)
        self.pool.assign(self.a, first)
        self.assertTrue(self.pool.reserve(self.a))
        frozen = proxy_settings.freeze_account(self.a)
        self.fail(first)
        self.assertEqual(proxy_settings.get_profile(account=frozen).pool_id, first)
        self.assertIs(proxy_settings.freeze_account(frozen), frozen)
        self.pool.release(self.a["access_token"])
        self.assertEqual(proxy_settings.get_profile(account=self.a).pool_id, second)

    def test_disabling_pool_immediately_restores_global_and_reenable_keeps_binding(self):
        node = self.node("A", 1)
        self.pool.configure(True)
        self.pool.assign(self.a, node)
        self.pool.configure(False)
        self.assertEqual(proxy_settings.get_profile(account=self.a).proxy_url, "http://global:8080")
        self.pool.configure(True)
        self.assertEqual(proxy_settings.get_profile(account=self.a).pool_id, node)

    def test_disabling_or_deleting_node_moves_entire_group(self):
        first, second = self.node("A", 1), self.node("B", 2)
        self.pool.configure(True)
        self.pool.assign(self.a, first)
        self.pool.save_node("A", node_id=first, enabled=False)
        self.assertEqual(self.pool.route(self.a)["id"], second)
        self.pool.delete_node(second)
        self.assertEqual(self.pool.route(self.a)["id"], "")

    def test_credentials_are_private_and_editing_without_url_preserves_them(self):
        url = "http://secret-user:secret-password@proxy:1234"
        node = self.pool.save_node("secret", url)
        self.pool.save_node("renamed", node_id=node)
        self.assertEqual(self.pool._node(node)["url"], url)
        public = json.dumps(self.pool.public())
        self.assertNotIn("secret-password", public)
        self.assertNotIn("secret-user", public)
        self.assertIn(url, self.path.read_text())

    def test_token_rotation_preserves_binding_and_releases_capacity(self):
        node = self.node("A", 1, concurrency=1)
        self.pool.configure(True)
        self.pool.assign(self.a, node)
        self.assertTrue(self.pool.reserve(self.a))
        self.pool.transfer(self.a["access_token"], "new-token")
        self.assertEqual(self.pool.route({"access_token": "new-token"})["id"], node)
        self.assertFalse(self.pool.reserve({"access_token": "other"}))
        self.pool.release("new-token")
        self.assertTrue(self.pool.reserve({"access_token": "other"}))

    def test_duplicate_exit_ips_share_concurrency_budget(self):
        first, second = self.node("A", 1, concurrency=1), self.node("B", 2, concurrency=1)
        self.pool._node(second)["exit_ip"] = self.pool._node(first)["exit_ip"]
        self.pool.configure(True)
        self.pool.assign(self.a, first)
        other = {"access_token": "other"}
        self.pool.assign(other, second)
        self.assertTrue(self.pool.reserve(self.a))
        self.assertFalse(self.pool.reserve(other))
        self.pool.release(self.a["access_token"])
        self.assertTrue(self.pool.reserve(other))

    def test_disabled_worker_makes_no_network_calls(self):
        self.pool.save_node("A", "http://proxy:1")
        stop = Event()
        with mock.patch.object(self.pool, "probe") as probe:
            thread = self.pool.start(stop)
            stop.set()
            self.pool._wake.set()
            thread.join(1)
            probe.assert_not_called()

    def test_edited_proxy_ignores_stale_check_result(self):
        node = self.node("A", 1)
        def probe(_):
            self.pool.save_node("changed", "http://proxy:2", node_id=node)
            return {"ok": True, "exit_ip": "1.2.3.1", "category": "ok"}
        self.pool.probe = probe
        self.pool.check(node)
        self.assertEqual(self.pool._node(node)["status"], "unknown")
        self.assertEqual(self.pool._node(node)["exit_ip"], "")

    def test_invalid_assignment_is_rejected_without_mutating_binding(self):
        with self.assertRaises(ValueError):
            self.pool.assign(self.a, "nonexistent")
        self.assertNotIn(account_key(self.a), self.pool.state["bindings"])

    def test_deleting_account_removes_binding_and_releases_capacity(self):
        node = self.node("A", 1, concurrency=1)
        self.pool.configure(True)
        self.pool.assign(self.a, node)
        self.pool.reserve(self.a)
        self.pool.drop([self.a["access_token"]])
        self.assertEqual(self.pool.public()["items"][0]["bound_accounts"], 0)
        self.assertTrue(self.pool.reserve({"access_token": "other"}))

    def test_account_scheduler_skips_busy_proxy_and_releases_after_validation_failure(self):
        from services.account_service import AccountService
        from services.storage.json_storage import JSONStorageBackend
        from services.config import config
        first, second = self.node("A", 1, concurrency=1), self.node("B", 2, concurrency=1)
        self.pool.configure(True)
        service = AccountService(JSONStorageBackend(self.path.parent / "accounts.json"))
        service._save_cumulative_total = lambda: None
        service.add_account_items([{"access_token": t, "quota": 25, "status": "正常", "upload_remaining": 80}
                                   for t in ("one", "two", "three")])
        for token, node in (("one", first), ("two", first), ("three", second)):
            self.pool.assign({"access_token": token}, node)
        service.fetch_remote_info = lambda token, *_args: service.get_account(token)
        with mock.patch.dict(config.data, {"account_scheduling_mode": "sequential", "image_account_concurrency": 1}):
            self.assertEqual(service.get_available_access_token(), "one")
            self.assertEqual(service.get_available_access_token(), "three")
            service.release_image_slot("one")
            service.release_image_slot("three")
            def validate(token, *_args):
                if token == "one":
                    raise TimeoutError("network error")
                return service.get_account(token)
            service.fetch_remote_info = validate
            self.assertEqual(service.get_available_access_token(), "two")
            service.release_image_slot("two")
        self.assertFalse(self.pool._leases)

    def test_proxy_403_is_target_blocked_and_ip_lookup_failure_alone_is_not_failure(self):
        session = mock.MagicMock()
        session.__enter__.return_value = session
        response = mock.Mock(status_code=403)
        session.get.side_effect = [ConnectionError(), response]
        with mock.patch("curl_cffi.requests.Session", return_value=session):
            result = probe_proxy("http://proxy:1")
        self.assertFalse(result["ok"])
        self.assertEqual(result["category"], "target_blocked")
        session.get.side_effect = [ConnectionError(), mock.Mock(status_code=200, json=lambda: {"csrfToken": "test"})]
        with mock.patch("curl_cffi.requests.Session", return_value=session):
            self.assertTrue(probe_proxy("http://proxy:1")["ok"])

    def test_passive_error_schedules_check_without_reassigning_or_marking_account(self):
        node = self.node("A", 1)
        self.pool.configure(True)
        self.pool.assign(self.a, node)
        self.now += 40
        error = ConnectionError("connection reset")
        error.code = 56
        session = ProxyPoolSession(pool_id=node, proxy="http://proxy:1")
        self.addCleanup(session.close)
        with mock.patch("services.proxy_service.Session.request", side_effect=error):
            with self.assertRaises(ConnectionError):
                session.get("https://chatgpt.com")
        self.assertEqual(self.pool._node(node)["next_check_at"], 0)
        self.assertEqual(self.pool.route(self.a)["id"], node)
