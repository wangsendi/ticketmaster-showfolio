import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from jobs import proxy_outcome
from proxy_pool import ProxyPool, PROXY_DISABLED
from store import Store


def make_pool(addresses=("192.0.2.1", "192.0.2.2")):
    return ProxyPool([{"id":str(i), "username":str(i), "screening":{"exitIP":ip}}
                      for i, ip in enumerate(addresses)], "127.0.0.1", 10801, "fixture")


class ProxyReputationTests(unittest.TestCase):
    def test_five_failures_then_success_restores_ten(self):
        pool = make_pool()
        with pool.lease(exclude_ids=["1"]) as proxy:
            for expected in range(9, 4, -1):
                self.assertEqual(pool.record_outcome(proxy, False)["confidence"], expected)
            pool.record_outcome(proxy, None)
            self.assertEqual(pool.record_outcome(proxy, True), {"confidence":10, "disabled_until":0})
        self.assertEqual(pool.reputation_status()["disabled_nodes"], 0)

    @patch("proxy_pool.time.time")
    def test_tenth_failure_stays_disabled_until_manually_restored(self, clock):
        clock.return_value = 1000
        pool = make_pool()
        with pool.lease(exclude_ids=["1"]) as proxy:
            for _ in range(9):
                pool.record_outcome(proxy, False)
            self.assertEqual(pool.reputation_status()["disabled_nodes"], 0)
            self.assertEqual(pool.record_outcome(proxy, False), {"confidence":0, "disabled_until":PROXY_DISABLED})
            self.assertEqual(pool.record_outcome(proxy, True)["confidence"], 0)
        clock.return_value += 365 * 24 * 60 * 60
        self.assertEqual(pool.reputation_status()["disabled_nodes"], 1)
        for _ in range(5):
            self.assertEqual(pool.select().node["id"], "1")
        pool.restore("0")
        with pool.lease(exclude_ids=["1"]) as proxy:
            self.assertEqual(pool.record_outcome(proxy, False)["confidence"], 9)

    def test_nodes_sharing_ip_have_independent_scores_and_manual_restore(self):
        pool = make_pool(("192.0.2.1", "192.0.2.1", "192.0.2.2"))
        for index in ("0", "1"):
            with pool.lease(exclude_ids=[n for n in ("0", "1", "2") if n != index]) as proxy:
                for _ in range(10 if index == "0" else 5):
                    pool.record_outcome(proxy, False)
        self.assertEqual(pool.reputation_status()["disabled_nodes"], 1)
        pool.restore("1")
        self.assertEqual(pool.reputation_status()["disabled_nodes"], 1)
        pool.restore("0")
        self.assertEqual(pool.reputation_status()["disabled_nodes"], 0)
        self.assertTrue(all(r["confidence"] == 10 for r in pool.reputation_status()["reputations"]))

    @patch("proxy_pool.time.time", return_value=1000)
    def test_disable_survives_refresh_and_restart(self, clock):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.sqlite3"
            pool = make_pool()
            pool.configure_reputation(Store(path))
            with pool.lease(exclude_ids=["1"]) as proxy:
                for _ in range(10):
                    pool.record_outcome(proxy, False)
            pool.begin_refresh()
            pool.finish_refresh(make_pool())
            self.assertEqual(pool.select().node["id"], "1")
            restarted = make_pool()
            restarted.configure_reputation(Store(path))
            self.assertEqual(restarted.reputation_status()["disabled_nodes"], 1)
            self.assertEqual(restarted.select().node["id"], "1")

    @patch("proxy_pool.time.time", return_value=1000)
    def test_all_disabled_waits_and_can_be_cancelled(self, clock):
        pool = make_pool(("192.0.2.1",))
        with pool.lease() as proxy:
            for _ in range(10):
                pool.record_outcome(proxy, False)
        with self.assertRaisesRegex(ValueError, "禁用"):
            with pool.lease():
                self.fail("cooling route leased")
        stop = threading.Event()
        done = threading.Event()
        def waiting():
            try:
                with pool.lease(wait=True, stop_event=stop):
                    self.fail("cooling route leased")
            except InterruptedError:
                done.set()
        thread = threading.Thread(target=waiting)
        thread.start()
        self.assertFalse(done.wait(.05))
        stop.set()
        with pool.available:
            pool.available.notify_all()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(done.is_set())

    def test_disabled_node_stays_disabled_after_exit_ip_changes(self):
        pool = make_pool()
        with pool.lease(exclude_ids=["1"]) as proxy:
            for _ in range(10):
                pool.record_outcome(proxy, False)
        pool.finish_refresh(make_pool(("192.0.2.3", "192.0.2.2")))
        with self.assertRaisesRegex(ValueError, "禁用"):
            with pool.lease(exclude_ids=["1"]):
                self.fail("Disabled node acquired after IP change")
        self.assertEqual(pool.select().node["id"], "1")

    @patch("proxy_pool.time.time", return_value=1000)
    def test_waiting_lease_resumes_only_after_manual_restore(self, clock):
        pool = make_pool(("192.0.2.1",))
        with pool.lease() as proxy:
            for _ in range(10):
                pool.record_outcome(proxy, False)
        acquired = threading.Event()
        def waiting():
            with pool.lease(wait=True):
                acquired.set()
        thread = threading.Thread(target=waiting, daemon=True)
        thread.start()
        self.assertFalse(acquired.wait(.05))
        clock.return_value = 1000 + 365 * 24 * 60 * 60
        self.assertFalse(acquired.wait(.05))
        pool.restore("0")
        thread.join(timeout=2)
        self.assertTrue(acquired.is_set())
        self.assertFalse(thread.is_alive())

    def test_old_expired_cooldown_is_migrated_and_restore_is_persistent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.sqlite3"
            store = Store(path)
            store.save_proxy_reputation("node:0", 0, 1)
            restarted = make_pool()
            restarted.configure_reputation(Store(path))
            self.assertEqual(restarted.reputation_status()["disabled_nodes"], 1)
            restarted.restore("0")
            again = make_pool()
            again.configure_reputation(Store(path))
            self.assertEqual(again.reputation_status()["disabled_nodes"], 0)
            self.assertEqual(again.reputation_status()["reputations"][0]["confidence"], 10)
            with self.assertRaises(ValueError):
                again.restore("missing-node")

    def test_old_shared_ip_penalty_does_not_disable_independent_nodes(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "store.sqlite3")
            store.save_proxy_reputation("ip:192.0.2.1", 0, PROXY_DISABLED)
            pool = make_pool(("192.0.2.1", "192.0.2.1"))
            pool.configure_reputation(store)
            self.assertEqual(pool.reputation_status()["disabled_nodes"], 0)
            self.assertTrue(all(r["confidence"] == 10 for r in pool.reputation_status()["reputations"]))

    def test_restore_all_wakes_waiter_and_preserves_ip_exclusivity(self):
        pool = make_pool(("192.0.2.1", "192.0.2.1"))
        for index in ("0", "1"):
            with pool.lease(exclude_ids=["1" if index == "0" else "0"]) as proxy:
                for _ in range(10):
                    pool.record_outcome(proxy, False)
        acquired, release, started = threading.Event(), threading.Event(), threading.Event()
        def waiting():
            started.set()
            with pool.lease(wait=True):
                acquired.set()
                release.wait(2)
        thread = threading.Thread(target=waiting, daemon=True)
        thread.start()
        try:
            self.assertTrue(started.wait(1))
            self.assertFalse(acquired.wait(.05))
            self.assertEqual(pool.restore_all(), 2)
            self.assertTrue(acquired.wait(1))
            with self.assertRaises(ValueError):
                with pool.lease():
                    self.fail("Bulk restore bypassed IP exclusivity")
        finally:
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_persona_cancel_and_local_crashes_do_not_penalize_proxy(self):
        for data in ({"status":"success", "result":{"personaVerificationRequired":True}},
                     {"status":"cancelled", "result":{}},
                     {"status":"failed", "result":{"errorType":"PlaywrightWebSocketAssertion"}}):
            self.assertIsNone(proxy_outcome(data))
        self.assertTrue(proxy_outcome({"status":"partial", "result":{"verification":{"authenticated":True}}}))
        self.assertFalse(proxy_outcome({"status":"failed", "result":{"browsingActivityPaused":True}}))
        self.assertFalse(proxy_outcome({"status":"failed", "result":{"passwordStatus":403}}))
