import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from proxy_pool import ProxyPool


def pool_for(addresses):
    return ProxyPool([
        {"id": str(i), "username": str(i), "screening": {"exitIP": ip}}
        for i, ip in enumerate(addresses)
    ], "127.0.0.1", 10801, "fixture")


class ProxyIPLeaseTests(unittest.TestCase):
    def test_aliases_are_busy_and_other_ip_remains_available(self):
        pool = pool_for(["192.0.2.1", "192.0.2.1", "192.0.2.2"])
        with pool.lease(exclude_ids=["1", "2"]):
            nodes = pool.reputation_status(include_nodes=True)["nodes"]
            self.assertEqual([n["status"] for n in nodes], ["in-use", "in-use", "available"])
            for _ in range(10):
                self.assertEqual(pool.select().node["id"], "2")
                with pool.lease() as second:
                    self.assertEqual(second.node["id"], "2")
        with pool.lease(exclude_ids=["0", "2"]) as alias:
            self.assertEqual(alias.node["id"], "1")

    def test_same_ip_waits_until_release(self):
        pool = pool_for(["192.0.2.1", "192.0.2.1"])
        started, acquired = threading.Event(), threading.Event()
        def waiting():
            started.set()
            with pool.lease(exclude_ids=["0"], wait=True):
                acquired.set()
        with pool.lease(exclude_ids=["1"]):
            with self.assertRaises(ValueError):
                with pool.lease():
                    self.fail("Shared IP allocated twice")
            thread = threading.Thread(target=waiting, daemon=True)
            thread.start()
            self.assertTrue(started.wait(1))
            self.assertFalse(acquired.wait(.05))
        thread.join(2)
        self.assertTrue(acquired.is_set())
        self.assertFalse(thread.is_alive())
        self.assertEqual(pool.leased_routes, {})

    def test_refresh_keeps_old_ip_reserved_even_if_node_disappears(self):
        pool = pool_for(["192.0.2.1", "192.0.2.2"])
        with pool.lease(exclude_ids=["1"]):
            replacement = pool_for(["192.0.2.3", "192.0.2.1", "192.0.2.4"])
            replacement.nodes = replacement.nodes[1:]
            pool.begin_refresh()
            pool.finish_refresh(replacement)
            for _ in range(10):
                with pool.lease() as other:
                    self.assertEqual(other.node["screening"]["exitIP"], "192.0.2.4")
        with pool.lease(exclude_ids=["2"]) as alias:
            self.assertEqual(alias.node["screening"]["exitIP"], "192.0.2.1")

    def test_exception_releases_ip_for_alias(self):
        pool = pool_for(["192.0.2.1", "192.0.2.1"])
        with self.assertRaises(RuntimeError):
            with pool.lease(exclude_ids=["1"]):
                raise RuntimeError("browser failed")
        with pool.lease(exclude_ids=["0"]) as alias:
            self.assertEqual(alias.node["id"], "1")

    def test_simultaneous_acquisitions_never_share_ip(self):
        pool = pool_for([f"192.0.2.{i % 3 + 1}" for i in range(12)])
        start = threading.Barrier(12)
        lock = threading.Lock()
        active, collisions = set(), []
        def acquire(_):
            start.wait(timeout=5)
            for _ in range(20):
                with pool.lease(wait=True) as proxy:
                    ip = proxy.node["screening"]["exitIP"]
                    with lock:
                        if ip in active:
                            collisions.append(ip)
                        active.add(ip)
                    # Every other alias must remain unavailable while this lease lives.
                    nodes = pool.reputation_status(include_nodes=True)["nodes"]
                    self.assertTrue(all(n["status"] == "in-use" for n in nodes if n["exit_ip"] == ip))
                    with lock:
                        active.remove(ip)
        with ThreadPoolExecutor(max_workers=12) as executor:
            list(executor.map(acquire, range(12)))
        self.assertEqual(collisions, [])
        self.assertEqual(pool.leased, set())
        self.assertEqual(pool.leased_routes, {})
