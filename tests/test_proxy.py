import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from proxy_pool import NETWORK_MODE, ProxyPool, ProxyRefreshError
from runner import DEFAULT_URL, run_account


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.payload = {"items": [
            {"id": str(i), "name": f"node-{i}", "username": f"route-{i}",
             "health": "unknown", "protocol": "shadowsocks", "outbound": {"password": "private-outbound"}}
            for i in range(3)
        ]}
        self.config = {"socks_listen": "127.0.0.1:10801", "socks_password": "shared-secret"}
        self.requests = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                fixture.requests.append(self.path)
                payload = fixture.payload if self.path == "/api/nodes" else fixture.config
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.url = f"http://127.0.0.1:{self.server.server_port}/api/nodes"

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_api_filter_uses_only_existing_ip_check_json(self):
        from proxy_check import filtered_pool_from_api
        self.payload["items"][0]["ip_check"] = {"checked_at":"fixture-time", "data":{"ip_info":{"country_code":"US","ip":"192.0.2.1"},"secret":"do-not-retain"}}
        self.payload["items"][1]["ip_check"] = {"data":{"ip_info":{"country_code":"JP","ip":"192.0.2.2"}}}
        self.payload["items"][2]["ip_check"] = {"error":"failed", "data":{"ip_info":{"country_code":"US","ip":"192.0.2.3"}}}
        with patch("proxy_check.request") as probe:
            pool = filtered_pool_from_api(self.url, DEFAULT_URL)
            probe.assert_not_called()
        self.assertEqual([n["id"] for n in pool.nodes], ["0", "1"])
        self.assertEqual(pool.nodes[0]["screening"]["exitIP"], "192.0.2.1")
        self.assertEqual(self.requests, ["/api/nodes", "/api/config"])
        self.assertNotIn("do-not-retain", str(pool.nodes))
        self.assertNotIn("private-outbound", str(pool.nodes))

    def test_api_filter_skips_unchecked_and_invalid_nodes(self):
        from proxy_check import filtered_pool_from_api
        for address in (None, "invalid-ip", 123):
            self.payload["items"][0]["ip_check"] = {"data":{"ip_info":{"country_code":"US","ip":address}}}
            with patch("proxy_check.request") as probe, self.assertRaisesRegex(ValueError, "接口暂无"):
                filtered_pool_from_api(self.url, DEFAULT_URL)
            probe.assert_not_called()

    def test_api_loads_all_regions_with_correct_country_metadata(self):
        from proxy_check import filtered_pool_from_api
        for i, country in enumerate(("US", "SG", "AU")):
            self.payload["items"][i]["ip_check"] = {"data":{"ip_info":{"country_code":country,"ip":f"192.0.2.{i+1}"}}}
        for country in ("HK", "JP", "CA", "TW"):
            self.payload["items"].append({"id":country,"name":country,"username":country,
                "ip_check":{"data":{"ip_info":{"country_code":country,"ip":"192.0.2.99"}}}})
        with patch("proxy_check.request") as probe:
            pool = filtered_pool_from_api(self.url, DEFAULT_URL)
            probe.assert_not_called()
        self.assertEqual({n["screening"]["country"] for n in pool.nodes}, {"US","SG","AU","HK","JP","CA","TW"})
        self.assertEqual(len(pool.nodes), 7)
        self.assertEqual(self.requests, ["/api/nodes", "/api/config"])

    def test_random_cycles_preserve_node_routing_and_hide_credentials(self):
        with patch.dict("os.environ", {"http_proxy": "http://127.0.0.1:1", "no_proxy": ""}):
            pool = ProxyPool.from_api(self.url)
        selections = [pool.select() for _ in range(30)]
        for i in range(0, 30, 3):
            self.assertEqual({s.node["id"] for s in selections[i:i + 3]}, {"0", "1", "2"})
        for first, second in zip(selections, selections[1:]):
            self.assertNotEqual(first.node["id"], second.node["id"])
        for selected in selections:
            self.assertEqual(selected.upstream["username"], "route-" + selected.node["id"])
            self.assertEqual(selected.upstream["password"], "shared-secret")
            self.assertNotIn("shared-secret", repr(selected))
            self.assertNotIn("username", selected.node)
            self.assertNotIn("outbound", selected.node)
        self.assertEqual(self.requests, ["/api/nodes", "/api/config"])

    def test_filters_failed_malformed_and_duplicate_nodes_but_accepts_unknown(self):
        self.payload["items"] += [self.payload["items"][0], None, {"id": "bad"},
                                  {"id": "bad2", "username": "bad", "health": "unhealthy"}]
        self.config["socks_password"] = ""
        self.config["socks_listen"] = "[::]:10801"
        pool = ProxyPool.from_api(self.url)
        self.assertEqual(len(pool.nodes), 3)
        self.assertEqual(pool.select().upstream["password"], "anything")
        self.assertEqual(pool.host, "127.0.0.1")

    def test_invalid_api_data_fails_without_exposing_secrets(self):
        for payload in ({"items": []}, {"items": "secret-data"}, ["secret-data"]):
            self.payload = payload
            with self.assertRaises(ValueError) as error:
                ProxyPool.from_api(self.url)
            self.assertNotIn("secret-data", str(error.exception))

    def test_invalid_socks_config_fails(self):
        for address in ("bad", "host:0", "host:70000", "user:secret@host:10801", "host:10801/path"):
            self.config["socks_listen"] = address
            with self.assertRaises(ValueError) as error:
                ProxyPool.from_api(self.url)
            self.assertNotIn("secret", str(error.exception))

    def test_run_passes_proxy_on_stdin_and_only_saves_node_metadata(self):
        selected = ProxyPool.from_api(self.url).select()
        with tempfile.TemporaryDirectory() as temporary:
            out = Path(temporary) / "attempt"
            with patch("runner.subprocess.Popen") as popen:
                popen.return_value.returncode = 0
                popen.return_value.communicate.return_value = (
                    json.dumps({"authenticated": False, "proxyNode": selected.node}), "")
                result = run_account("test@example.invalid", "account-secret", out,
                                     ["node", "login.cjs"], False, DEFAULT_URL, proxy=selected)
            context = (out / "run-context.json").read_text()
            self.assertEqual(json.loads(context)["networkMode"], NETWORK_MODE)
            self.assertNotIn("shared-secret", context)
            self.assertNotIn("route-", context)
            data = json.loads(popen.return_value.communicate.call_args.args[0])
            self.assertEqual(data["proxy"], selected.upstream)
            self.assertEqual(result["proxyNode"], selected.node)
            self.assertNotIn("shared-secret", str(popen.call_args))
            self.assertNotIn("shared-secret", json.dumps(result))

    def test_restricted_pool_only_selects_screened_live_nodes(self):
        pool = ProxyPool.from_api(self.url)
        pool.select()  # Populate an old random cycle before applying the filter.
        report = {"version": 1, "complete": True, "nodesAPI": self.url, "campaignURL": DEFAULT_URL,
                  "country": "US", "checkedAtUTC": "2026-11-14T00:00:00Z", "nodeIds": ["1", "gone-node"]}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "us.json"
            path.write_text(json.dumps(report))
            pool.restrict(path, self.url, DEFAULT_URL)
        for _ in range(10):
            selected = pool.select()
            self.assertEqual(selected.node["id"], "1")
            self.assertEqual(selected.node["screening"]["country"], "US")

    def test_all_region_report_keeps_per_node_exit_metadata(self):
        pool=ProxyPool.from_api(self.url)
        report={"version":1,"complete":True,"nodesAPI":self.url,"campaignURL":DEFAULT_URL,
                "country":None,"nodeIds":["0","1"],"results":[
                    {"accepted":True,"node":{"id":str(i)},"exitAfterCampaign":{"ip":f"192.0.2.{i+1}","country":country}}
                    for i,country in enumerate(("HK","JP"))]}
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"report.json"
            path.write_text(json.dumps(report))
            pool.restrict(path,self.url,DEFAULT_URL)
        self.assertEqual({n["screening"]["country"] for n in pool.nodes},{"HK","JP"})

    def test_invalid_or_empty_reports_never_enable_all_nodes(self):
        base = {"version": 1, "complete": True, "nodesAPI": self.url, "campaignURL": DEFAULT_URL,
                "country": "US", "nodeIds": ["1"]}
        changes = [{"complete": False}, {"nodeIds": []}, {"nodeIds": ["gone-node"]},
                   {"nodesAPI": "http://other.invalid/api/nodes"}, {"nodesAPI": None},
                   {"campaignURL": "https://signup.ticketmaster.com/other"}]
        pool = ProxyPool.from_api(self.url)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "us.json"
            for change in changes:
                with self.subTest(change=change):
                    path.write_text(json.dumps({**base, **change}))
                    with self.assertRaises(ValueError):
                        pool.restrict(path, self.url, DEFAULT_URL)

    def test_leased_node_is_not_reused_when_random_cycles_wrap(self):
        pool = ProxyPool.from_api(self.url)
        with pool.lease() as long_running:
            for _ in range(20):
                with pool.lease() as other:
                    self.assertNotEqual(other.node["id"], long_running.node["id"])
        self.assertEqual(pool.leased, set())

    def test_lease_releases_on_failure_and_exhaustion_cannot_reuse_active_nodes(self):
        pool = ProxyPool.from_api(self.url)
        with contextlib.ExitStack() as stack:
            selections = [stack.enter_context(pool.lease()) for _ in range(3)]
            self.assertEqual(len({selection.node["id"] for selection in selections}), 3)
            with self.assertRaises(ValueError):
                with pool.lease():
                    self.fail("all nodes already occupied")
        with self.assertRaises(RuntimeError):
            with pool.lease():
                raise RuntimeError("runner failed")
        self.assertEqual(pool.leased, set())


class ProxyRetryTests(unittest.TestCase):
    def pool(self):
        return ProxyPool([{"id":str(i), "name":f"node-{i}", "username":str(i)} for i in range(3)], "127.0.0.1", 10801, "test")

    def test_live_refresh_preserves_active_lease_and_replaces_future_routes(self):
        pool = self.pool()
        acquired, selected = threading.Event(), []
        with pool.lease(exclude_ids=["1", "2"]) as running:
            old_upstream = dict(running.upstream)
            pool.begin_refresh()
            def waiting():
                with pool.lease(wait=True) as proxy:
                    selected.append(proxy)
                    acquired.set()
            thread = threading.Thread(target=waiting, daemon=True)
            thread.start()
            self.assertFalse(acquired.wait(timeout=0.05))
            replacement = ProxyPool([{"id":"0", "username":"new-zero"}, {"id":"new", "username":"new-route"}], "127.0.0.2", 10802, "new-password")
            pool.finish_refresh(replacement)
            self.assertTrue(acquired.wait(timeout=2))
            thread.join(timeout=2)
            self.assertEqual(selected[0].node["id"], "new")
            self.assertEqual(selected[0].upstream["password"], "new-password")
            self.assertEqual(running.upstream, old_upstream)
            self.assertEqual(pool.leased, {"0"})
        self.assertEqual(pool.leased, set())

    def test_failed_refresh_does_not_fall_back_to_old_routes(self):
        pool = self.pool()
        pool.begin_refresh()
        pool.finish_refresh(error="fixture refresh failed")
        with self.assertRaisesRegex(ProxyRefreshError, "fixture refresh failed"):
            with pool.lease(wait=True): self.fail("stale route used")
        self.assertEqual(pool.nodes, [])

    def test_retry_rechecks_exit_exclusions_after_refresh(self):
        pool = self.pool()
        pool.nodes[0]["screening"] = {"exitIP":"192.0.2.1"}
        with pool.lease(exclude_ids=["1", "2"]) as previous:
            pass
        pool.begin_refresh()
        selected = []
        def waiting():
            with pool.lease(previous=previous, wait=True) as proxy: selected.append(proxy.node["id"])
        thread = threading.Thread(target=waiting, daemon=True)
        thread.start()
        replacement = self.pool()
        for node in replacement.nodes:
            node["screening"] = {"exitIP":"192.0.2.2" if node["id"] == "2" else "192.0.2.1"}
        pool.finish_refresh(replacement)
        thread.join(timeout=2)
        self.assertEqual(selected, ["2"])

    def test_retry_waits_for_a_different_node_and_releases_it(self):
        pool = self.pool()
        started, acquired = threading.Event(), threading.Event()
        selected, errors = [], []
        with contextlib.ExitStack() as stack:
            occupied = [stack.enter_context(pool.lease()) for _ in range(3)]
            def waiting():
                started.set()
                try:
                    with pool.lease(exclude_ids=[occupied[0].node["id"]], wait=True) as proxy:
                        selected.append(proxy.node["id"])
                        acquired.set()
                except Exception as error:
                    errors.append(type(error).__name__)
            thread = threading.Thread(target=waiting, daemon=True)
            thread.start()
            self.assertTrue(started.wait(timeout=1))
            self.assertFalse(acquired.wait(timeout=0.05))
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(acquired.is_set())
        self.assertNotEqual(selected[0], occupied[0].node["id"])
        self.assertEqual(pool.leased, set())

    def test_proxy_wait_can_be_cancelled_without_acquiring_another_node(self):
        pool = self.pool()
        stop, started = threading.Event(), threading.Event()
        errors = []
        with contextlib.ExitStack() as stack:
            occupied = [stack.enter_context(pool.lease()) for _ in range(3)]
            def waiting():
                started.set()
                try:
                    with pool.lease(exclude_ids=[occupied[0].node["id"]], stop_event=stop, wait=True):
                        errors.append("unexpected acquisition")
                except InterruptedError:
                    errors.append("cancelled")
            thread = threading.Thread(target=waiting, daemon=True)
            thread.start()
            self.assertTrue(started.wait(timeout=1))
            stop.set()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, ["cancelled"])
        self.assertEqual(pool.leased, set())

    def test_screened_exit_ip_excludes_duplicate_routes_on_retry(self):
        pool = self.pool()
        report = {"version":1, "complete":True, "nodesAPI":"http://localhost/api/nodes",
                  "campaignURL":DEFAULT_URL, "country":"US", "nodeIds":["0", "1", "2"],
                  "results":[{"node":{"id":str(i)}, "accepted":True,
                              "exitAfterCampaign":{"ip":"192.0.2.1" if i < 2 else "192.0.2.2"}} for i in range(3)]}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.json"
            path.write_text(json.dumps(report))
            pool.restrict(path, report["nodesAPI"], DEFAULT_URL)
        with pool.lease(exclude_ids=["1", "2"]) as first:
            self.assertEqual(first.node["screening"]["exitIP"], "192.0.2.1")
        excluded = pool.retry_exclusions(first)
        self.assertEqual(excluded, {"0", "1"})
        with pool.lease(exclude_ids=excluded) as second:
            self.assertEqual(second.node["id"], "2")
        with self.assertRaisesRegex(ValueError, "没有其他可用"):
            with pool.lease(exclude_ids=["0", "1", "2"], wait=True):
                self.fail("must not reuse an excluded route")


if __name__ == "__main__":
    unittest.main()
