"""Opt-in persistent proxy assignments and verified group failover."""
from __future__ import annotations

from collections import Counter
import copy
import hashlib
import ipaddress
import json
import logging
import os
from pathlib import Path
import tempfile
from threading import Event, RLock, Thread
import time
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from services.config import DATA_DIR

log = logging.getLogger(__name__)


def account_key(account: dict) -> str:
    return hashlib.sha256(str(account.get("access_token") or "").encode()).hexdigest()


def masked_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], "", "", ""))


def probe_proxy(url: str) -> dict:
    from curl_cffi.requests import Session
    started = time.monotonic()
    result = {"ok": False, "exit_ip": "", "category": "network_error", "http_status": 0}
    try:
        with Session(proxy=url, impersonate="chrome110", verify=True) as session:
            try:
                response = session.get("https://api.ipify.org?format=json", timeout=10)
                response.raise_for_status()
                result["exit_ip"] = str(ipaddress.ip_address(response.json()["ip"]))
            except Exception:
                # An unavailable IP lookup service must not mark a working target route bad.
                pass
            response = session.get("https://chatgpt.com/api/auth/csrf", timeout=15)
            result["http_status"] = int(response.status_code)
            if response.status_code == 200 and isinstance(response.json(), dict) and response.json().get("csrfToken"):
                result.update(ok=True, category="ok")
            elif response.status_code in {403, 429}:
                result["category"] = "target_blocked"
            elif response.status_code >= 500:
                result["category"] = "upstream_error"
            else:
                result["category"] = "unexpected_response"
    except Exception:
        # Never store exception messages containing proxy credentials.
        pass
    result["latency_ms"] = int((time.monotonic() - started) * 1000)
    return result


class ProxyPoolService:
    def __init__(self, path: Path, probe=probe_proxy, clock=time.time):
        self.path, self.probe, self.clock = path, probe, clock
        self._lock = RLock()
        self._checking: set[str] = set()
        self._leases: dict[str, list[str]] = {}
        self._wake = Event()
        self.state = {"enabled": False, "check_interval_seconds": 300, "failure_threshold": 3,
                      "cooldown_seconds": 300, "nodes": [], "bindings": {}, "events": []}
        if path.exists():
            # Corrupt configuration must not silently overwrite persisted credentials.
            saved = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(saved, dict) or not isinstance(saved.get("nodes"), list) or not isinstance(saved.get("bindings"), dict):
                raise ValueError("Invalid proxy pool state")
            self.state.update(saved)

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(self.state, stream, ensure_ascii=False)
            temporary.chmod(0o600)
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _node(self, node_id):
        return next((n for n in self.state["nodes"] if n["id"] == node_id), None)

    def _healthy(self, node):
        return bool(node and node["enabled"] and node["status"] == "healthy")

    def _choose(self, excluded="", excluded_ip=""):
        counts = Counter(b.get("node_id") for b in self.state["bindings"].values())
        candidates = [n for n in self.state["nodes"] if self._healthy(n) and n["id"] != excluded
                      and (not excluded_ip or n.get("exit_ip") != excluded_ip)]
        return min(candidates, key=lambda n: (counts[n["id"]], n["id"])) if candidates else None

    def _migrate(self, node_id, reason):
        failed = self._node(node_id)
        # Move all accounts bound to the same confirmed bad exit, including duplicate entry points.
        bad_ids = {node_id}
        bad_ip = failed.get("exit_ip", "") if failed else ""
        if bad_ip:
            bad_ids.update(n["id"] for n in self.state["nodes"] if n.get("exit_ip") == bad_ip and not self._healthy(n))
        target = self._choose(node_id, bad_ip)
        affected = [b for b in self.state["bindings"].values() if b.get("node_id") in bad_ids]
        for binding in affected:
            binding["node_id"] = target["id"] if target else ""
        if affected:
            self.state["events"] = (self.state["events"] + [{"time": self.clock(), "from_id": node_id,
                "to_id": target["id"] if target else "", "accounts": len(affected), "reason": reason}])[-50:]

    def configure(self, enabled: bool, check_interval_seconds: int = 300):
        with self._lock:
            self.state.update(enabled=bool(enabled), check_interval_seconds=max(60, min(3600, check_interval_seconds)))
            self._save()
        self._wake.set()

    def save_node(self, name: str, url: str | None = None, node_id="", enabled=True, max_concurrency=2):
        from services.proxy_service import normalize_proxy_url
        with self._lock:
            node = self._node(node_id) if node_id else None
            if node_id and node is None:
                raise ValueError("代理不存在")
            normalized = normalize_proxy_url(url) if url is not None else (node or {}).get("url", "")
            try:
                parts = urlsplit(normalized)
                valid = parts.scheme in {"http", "https", "socks5h"} and parts.hostname and parts.port
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("请填写有效代理地址及端口")
            if not name.strip():
                raise ValueError("请填写代理名称")
            if any(n["url"] == normalized and n is not node for n in self.state["nodes"]):
                raise ValueError("这个代理地址已经存在")
            if node is None:
                node = {"id": uuid4().hex, "status": "unknown", "failures": 0, "recoveries": 0,
                        "checked_at": 0, "next_check_at": 0, "exit_ip": "", "revision": 0}
                self.state["nodes"].append(node)
            elif normalized != node["url"]:
                node.update(status="unknown", checked_at=0, next_check_at=0, exit_ip="", failures=0, recoveries=0)
            node.update(name=name.strip()[:80], url=normalized, enabled=bool(enabled),
                        max_concurrency=max(1, min(20, int(max_concurrency))), revision=node["revision"] + 1)
            if not enabled:
                if self.state["enabled"]:
                    self._migrate(node["id"], "disabled")
            self._save()
            result = node["id"]
        self._wake.set()
        return result

    def delete_node(self, node_id):
        with self._lock:
            node = self._node(node_id)
            if node is None:
                raise ValueError("代理不存在")
            node["enabled"] = False
            self._migrate(node_id, "deleted")
            self.state["nodes"].remove(node)
            self._save()

    def assign(self, account: dict, choice: str):
        with self._lock:
            if choice not in {"auto", "global", "legacy"} and self._node(choice) is None:
                raise ValueError("代理不存在，请刷新列表")
            if choice not in {"auto", "global", "legacy"} and not self._healthy(self._node(choice)):
                raise ValueError("该代理尚未检测通过或已停用")
            key = account_key(account)
            node_id = choice if choice not in {"auto", "global", "legacy"} else ""
            self.state["bindings"][key] = {"mode": choice if not node_id else "manual", "node_id": node_id}
            self._save()
        return self.describe(account)

    def transfer(self, old_token, new_token):
        with self._lock:
            old, new = account_key({"access_token": old_token}), account_key({"access_token": new_token})
            if old in self.state["bindings"]:
                self.state["bindings"][new] = self.state["bindings"].pop(old)
                self._save()
            if old in self._leases:
                self._leases.setdefault(new, []).extend(self._leases.pop(old))

    def drop(self, tokens):
        with self._lock:
            changed = False
            for token in tokens:
                key = account_key({"access_token": token})
                changed = self.state["bindings"].pop(key, None) is not None or changed
                self._leases.pop(key, None)
            if changed:
                self._save()

    def route(self, account: dict, honor_lease=True):
        if not account or not account.get("access_token"):
            return None
        with self._lock:
            if not self.state["enabled"]:
                return None
            key = account_key(account)
            if honor_lease and self._leases.get(key):
                leased = self._node(self._leases[key][-1])
                if leased:
                    return {"url": leased["url"], "id": leased["id"], "source": "pool", "name": leased["name"]}
            binding = self.state["bindings"].get(key)
            if binding is None:
                if account.get("proxy"):
                    return None  # Existing dedicated proxy continues to work.
                binding = {"mode": "auto", "node_id": ""}
            if binding["mode"] == "legacy":
                return None
            if binding["mode"] == "global":
                return {"url": "", "id": "", "source": "global", "name": "统一代理"}
            node = self._node(binding.get("node_id"))
            if not self._healthy(node):
                node = self._choose()
                next_binding = {**binding, "node_id": node["id"] if node else ""}
                if self.state["bindings"].get(key) != next_binding:
                    self.state["bindings"][key] = next_binding
                    self._save()
            if node:
                return {"url": node["url"], "id": node["id"], "source": "pool", "name": node["name"]}
            return {"url": "", "id": "", "source": "global", "name": "统一代理（池内暂无可用代理）"}

    def describe(self, account):
        route = self.route(account)
        with self._lock:
            binding = self.state["bindings"].get(account_key(account), {})
            choice = binding.get("node_id") if binding.get("mode") == "manual" else binding.get("mode")
            choice = choice or ("legacy" if account.get("proxy") else "auto")
            assigned = self._node(binding.get("node_id"))
            node = self._node(route["id"]) if route and route["id"] else None
            return {"choice": choice, "pool_enabled": self.state["enabled"], "node_id": route["id"] if route else "",
                    "assigned_id": assigned["id"] if assigned else "", "assigned_name": assigned["name"] if assigned else "",
                    "name": route["name"] if route else ("专属代理" if account.get("proxy") else "统一代理"),
                    "exit_ip": (node or {}).get("exit_ip", ""), "status": (node or {}).get("status", "fallback")}

    def annotate(self, items):
        return [{**item, "proxy_assignment": self.describe(item)} for item in items]

    def _bucket(self, node_id):
        node = self._node(node_id) or {}
        return node.get("exit_ip") or node_id

    def reserve(self, account):
        with self._lock:
            route = self.route(account, honor_lease=False)
            if not route or not route["id"]:
                return True
            node_id, key = route["id"], account_key(account)
            bucket = self._bucket(node_id)
            count = sum(self._bucket(p) == bucket for leases in self._leases.values() for p in leases)
            limit = min(n["max_concurrency"] for n in self.state["nodes"] if self._bucket(n["id"]) == bucket)
            if count >= limit:
                return False
            self._leases.setdefault(key, []).append(node_id)
            return True

    def release(self, token):
        with self._lock:
            key = account_key({"access_token": token})
            leases = self._leases.get(key, [])
            if leases:
                leases.pop(0)
            if not leases:
                self._leases.pop(key, None)

    def check(self, node_id):
        with self._lock:
            node = self._node(node_id)
            if node is None:
                raise ValueError("代理不存在")
            if node_id in self._checking:
                return
            self._checking.add(node_id)
            snapshot = dict(node)
        try:
            result = self.probe(snapshot["url"])
            with self._lock:
                node = self._node(node_id)
                if not node or node["revision"] != snapshot["revision"]:
                    return
                now = self.clock()
                node.update(checked_at=now, latency_ms=result.get("latency_ms", 0),
                            category=result.get("category", "network_error"), http_status=result.get("http_status", 0))
                if result.get("exit_ip"):
                    node["exit_ip"] = result["exit_ip"]
                if result.get("ok"):
                    node["failures"] = 0
                    node["recoveries"] += 1
                    if node["status"] != "cooldown" or node["recoveries"] >= 2:
                        node["status"] = "healthy"
                elif result.get("category") == "upstream_error":
                    # An upstream 500 alone does not establish a failed proxy.
                    node["recoveries"] = 0
                    node["failures"] = 0
                else:
                    node["recoveries"] = 0
                    node["failures"] += 1
                    if node["failures"] >= self.state["failure_threshold"]:
                        node["status"] = "cooldown"
                        if self.state["enabled"]:
                            self._migrate(node_id, node["category"])
                interval = self.state["cooldown_seconds"] if node["status"] == "cooldown" else self.state["check_interval_seconds"]
                if node["failures"] and node["status"] != "cooldown":
                    interval = 30
                node["next_check_at"] = now + interval
                node["suspected"] = False
                self._save()
        finally:
            with self._lock:
                self._checking.discard(node_id)

    def suspect(self, node_id):
        with self._lock:
            node = self._node(node_id)
            if self.state["enabled"] and node and not node.get("suspected") and self.clock() - node.get("checked_at", 0) >= 30:
                node.update(suspected=True, next_check_at=0)
                self._wake.set()

    def public(self):
        with self._lock:
            result = {k: copy.deepcopy(self.state[k]) for k in ("enabled", "check_interval_seconds", "failure_threshold", "cooldown_seconds", "events")}
            counts = Counter(b.get("node_id") for b in self.state["bindings"].values())
            result["items"] = [{**{k: v for k, v in n.items() if k != "url"}, "address": masked_url(n["url"]),
                "has_credentials": "@" in n["url"], "bound_accounts": counts[n["id"]],
                "inflight": sum(p == n["id"] for leases in self._leases.values() for p in leases)} for n in self.state["nodes"]]
            return result

    def start(self, stop: Event):
        def worker():
            while not stop.is_set():
                try:
                    with self._lock:
                        due = [n["id"] for n in self.state["nodes"] if self.state["enabled"] and n["enabled"]
                               and n.get("next_check_at", 0) <= self.clock()]
                    for node_id in due:
                        if stop.is_set():
                            break
                        self.check(node_id)
                except Exception as exc:
                    log.warning("Proxy pool check failed (%s)", type(exc).__name__)
                self._wake.wait(5)
                self._wake.clear()
        thread = Thread(target=worker, name="proxy-pool-health", daemon=True)
        thread.start()
        return thread


proxy_pool = ProxyPoolService(DATA_DIR / "proxy_pool.json")
