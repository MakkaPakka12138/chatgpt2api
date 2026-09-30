from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from api import proxy_pool as pool_api, accounts as accounts_api
from services.proxy_pool_service import ProxyPoolService


class ProxyPoolApiTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.pool = ProxyPoolService(Path(directory.name) / "pool.json", probe=lambda _: {"ok": True, "exit_ip": "1.2.3.4", "category": "ok"})
        self.account = {"access_token": "test-token", "status": "正常", "proxy": "", "quota": 10}
        for target in (pool_api, accounts_api):
            patcher = mock.patch.object(target, "proxy_pool", self.pool)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.service = mock.Mock()
        self.service.list_accounts.return_value = [self.account]
        self.service.get_account.return_value = self.account
        self.service.update_account.side_effect = lambda _, updates: {**self.account, **updates}
        for target in (pool_api, accounts_api):
            patcher = mock.patch.object(target, "account_service", self.service)
            patcher.start()
            self.addCleanup(patcher.stop)
        app = FastAPI()
        app.include_router(pool_api.create_router())
        app.include_router(accounts_api.create_router())
        self.client = TestClient(app)
        self.headers = {"Authorization": "Bearer test-auth"}

    def test_unauthorized_mutation_is_denied(self):
        r = self.client.post("/api/proxy-pool/settings", json={"enabled": True})
        self.assertIn(r.status_code, (401, 403))
        self.assertFalse(self.pool.state["enabled"])

    def test_crud_switch_check_and_account_dropdown_assignment(self):
        r = self.client.post("/api/proxy-pool/nodes", headers=self.headers,
            json={"name": "A", "url": "http://username:password@proxy:1234"})
        self.assertEqual(r.status_code, 200, r.text)
        node = r.json()["items"][0]["id"]
        self.assertNotIn("password", r.text)
        self.assertFalse(r.json()["enabled"])
        self.client.post(f"/api/proxy-pool/nodes/{node}/check", headers=self.headers)
        self.client.post("/api/proxy-pool/settings", headers=self.headers, json={"enabled": True})
        r = self.client.post("/api/accounts/update", headers=self.headers,
            json={"access_token": "test-token", "proxy_pool_choice": node})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["item"]["proxy_assignment"]["node_id"], node)
        r = self.client.get("/api/accounts", headers=self.headers)
        self.assertEqual(r.json()["items"][0]["proxy_assignment"]["name"], "A")
        r = self.client.delete(f"/api/proxy-pool/nodes/{node}", headers=self.headers)
        self.assertEqual(r.json()["items"], [])

    def test_bad_pool_choice_does_not_change_account_status(self):
        r = self.client.post("/api/accounts/update", headers=self.headers,
            json={"access_token": "test-token", "status": "禁用", "proxy_pool_choice": "bad"})
        self.assertEqual(r.status_code, 400)
        self.service.update_account.assert_not_called()
