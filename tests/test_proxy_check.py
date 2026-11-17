import unittest
import subprocess
import sys
import threading
import time
from unittest.mock import patch

from proxy_check import DEFAULT_CAMPAIGN, FALLBACK_GEO_URL, GEO_URL, exit_location, fresh_pool, inspect_node, request
from proxy_pool import ProxyPool


class ProxyCheckTests(unittest.TestCase):
    def setUp(self):
        self.node = {"id": "node-1", "name": "US-looking-name", "protocol": "shadowsocks",
                     "health": "unknown", "username": "private-route"}
        self.pool = ProxyPool([self.node], "127.0.0.1", 10801, "private-password")
        self.us = {"country": "US", "ip": "192.0.2.1"}
        self.html = '<html>Sign In<script>self.__next_f.push([1,"campaign"])</script></html>'

    def test_accepts_only_us_before_and_after_successful_campaign(self):
        with patch("proxy_check.exit_location", return_value=self.us) as geo, \
                patch("proxy_check.request", return_value=({"status": 200}, self.html)) as request:
            result = inspect_node(self.pool, self.node, "US", DEFAULT_CAMPAIGN)
        self.assertTrue(result["accepted"])
        self.assertEqual(geo.call_count, 2)
        request.assert_called_once_with(self.pool, self.node, DEFAULT_CAMPAIGN)
        self.assertNotIn("private", str(result))

    def test_node_name_does_not_override_actual_country(self):
        for country in ("JP", "TW", None):
            with self.subTest(country=country), \
                    patch("proxy_check.exit_location", return_value={"country": country}), \
                    patch("proxy_check.request") as request:
                result = inspect_node(self.pool, self.node, "US", DEFAULT_CAMPAIGN)
                self.assertFalse(result["accepted"])
                request.assert_not_called()

    def test_all_region_check_accepts_known_countries_and_rejects_unknown_exits(self):
        for country in ("HK", "JP", "TW", "US", None):
            with self.subTest(country=country), \
                    patch("proxy_check.exit_location", return_value={"country":country,"ip":"192.0.2.1"}), \
                    patch("proxy_check.request", return_value=({"status":200},self.html)) as probe:
                result=inspect_node(self.pool,self.node,None,DEFAULT_CAMPAIGN)
                self.assertEqual(result["accepted"],country is not None)
                if country is None:
                    probe.assert_not_called()

    def test_rejects_redirects_error_pages_and_partial_responses(self):
        cases = [({"status": 307, "location": "https://signup.ticketmaster.ca/noahkahan"}, self.html),
                 ({"status": 200}, "Access denied"),
                 ({"status": 200, "error": "timeout"}, self.html),
                 ({"status": 200}, self.html + "NEXT_REDIRECT")]
        for response, body in cases:
            with self.subTest(response=response), \
                    patch("proxy_check.exit_location", return_value=self.us), \
                    patch("proxy_check.request", return_value=(response, body)):
                result = inspect_node(self.pool, self.node, "US", DEFAULT_CAMPAIGN)
                self.assertFalse(result["accepted"])
                self.assertEqual(result["reason"], "campaign_unavailable")

    def test_balancer_country_change_is_rejected(self):
        with patch("proxy_check.exit_location", side_effect=[self.us, {"country": "JP"}]), \
                patch("proxy_check.request", return_value=({"status": 200}, self.html)):
            result = inspect_node(self.pool, self.node, "US", DEFAULT_CAMPAIGN)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "country_changed_or_unverified")

    def test_geo_fallback_and_invalid_data_are_handled(self):
        with patch("proxy_check.request", side_effect=[
            ({"status": 0, "error": "timeout"}, ""),
            ({"status": 200}, '{"ip":"192.0.2.2","country":"US"}')
        ]) as request:
            location = exit_location(self.pool, self.node)
        self.assertEqual(location["country"], "US")
        self.assertEqual(location["source"], FALLBACK_GEO_URL)
        self.assertEqual([call.args[2] for call in request.call_args_list], [GEO_URL, FALLBACK_GEO_URL])
        with patch("proxy_check.request", side_effect=[
            ({"status": 200}, "loc=US\nip=not-an-ip\n"),
            ({"status": 200}, "[]")
        ]):
            self.assertIsNone(exit_location(self.pool, self.node)["country"])

    def test_refresh_reads_new_api_nodes_each_time_and_reports_progress(self):
        first = ProxyPool([self.node, {**self.node, "id":"foreign"}], "127.0.0.1", 10801, "private-password")
        second = ProxyPool([{**self.node, "id":"new-us"}], "127.0.0.1", 10801, "private-password")
        progress = []
        def inspect(pool, node, country, campaign, **kwargs):
            self.assertIsNone(country)
            return {"accepted":node["id"] != "foreign", "exitAfterCampaign":self.us, "checkedAtUTC":"fixture-time"}
        with patch("proxy_check.ProxyPool.from_api", side_effect=[first, second]) as api, patch("proxy_check.inspect_node", side_effect=inspect):
            one = fresh_pool("http://localhost/api/nodes", DEFAULT_CAMPAIGN, on_progress=progress.append)
            two = fresh_pool("http://localhost/api/nodes", DEFAULT_CAMPAIGN)
        self.assertEqual(api.call_count, 2)
        self.assertEqual([n["id"] for n in one.nodes], ["node-1"])
        self.assertEqual([n["id"] for n in two.nodes], ["new-us"])
        self.assertEqual(progress[-1], {"checked":2, "total":2, "accepted":1})
        self.assertNotIn("private", str(progress))
        self.assertEqual(two.nodes[0]["screening"]["country"], "US")

    def test_refresh_with_no_verified_nodes_fails(self):
        with patch("proxy_check.ProxyPool.from_api", return_value=self.pool), patch("proxy_check.inspect_node", return_value={"accepted":False}):
            with self.assertRaisesRegex(ValueError, "没有可用的"):
                fresh_pool("http://localhost/api/nodes", DEFAULT_CAMPAIGN)

    def test_verified_pool_preserves_each_nodes_actual_region(self):
        nodes=[{**self.node,"id":country} for country in ("HK","JP")]
        pool=ProxyPool(nodes,"127.0.0.1",10801,"private-password")
        def inspect(pool,node,country,campaign,**kwargs):
            self.assertIsNone(country)
            return {"accepted":True,"exitAfterCampaign":{"country":node["id"],"ip":"192.0.2.1"},"checkedAtUTC":"fixture"}
        with patch("proxy_check.ProxyPool.from_api",return_value=pool), patch("proxy_check.inspect_node",side_effect=inspect):
            verified=fresh_pool("http://localhost/api/nodes",DEFAULT_CAMPAIGN)
        self.assertEqual({n["screening"]["country"] for n in verified.nodes},{"HK","JP"})

    def test_cancelled_refresh_does_not_return_a_partial_pool(self):
        stop = threading.Event()
        def inspect(*args, **kwargs):
            stop.set()
            return {"accepted":True, "exitAfterCampaign":self.us, "checkedAtUTC":"fixture-time"}
        with patch("proxy_check.ProxyPool.from_api", return_value=self.pool), patch("proxy_check.inspect_node", side_effect=inspect):
            with self.assertRaises(InterruptedError):
                fresh_pool("http://localhost/api/nodes", DEFAULT_CAMPAIGN, stop_event=stop)

    def test_cancelled_probe_terminates_its_subprocess(self):
        stop = threading.Event()
        children = []
        popen = subprocess.Popen
        def launch(*args, **kwargs):
            child = popen([sys.executable, "-c", "import sys,time;sys.stdin.read();time.sleep(30)"], **kwargs)
            children.append(child)
            return child
        timer = threading.Timer(0.2, stop.set)
        started = time.monotonic()
        timer.start()
        try:
            with patch("proxy_check.subprocess.Popen", side_effect=launch), self.assertRaises(InterruptedError):
                request(self.pool, self.node, GEO_URL, stop_event=stop)
        finally:
            timer.cancel()
            for child in children:
                if child.poll() is None: child.kill(); child.wait()
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].returncode)


if __name__ == "__main__":
    unittest.main()
