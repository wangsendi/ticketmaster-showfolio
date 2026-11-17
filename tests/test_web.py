import io
import csv
from collections import deque
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time
import unittest
import zipfile
from unittest.mock import patch

from accounts import Account, parse_accounts, parse_import
from helpers import PNG, successful_run
from jobs import QueryService
from proxy_pool import ProxyPool
from results import collect, selections_from_har
from runner import DEFAULT_URL
from store import Store
from web import create_app


def fixture_pool(_):
    return ProxyPool([{"id": str(i), "name": f"node-{i}", "username": str(i)} for i in range(3)], "127.0.0.1", 10801, "test-only")


class AccountImportTests(unittest.TestCase):
    def test_import_skips_human_statuses_before_password_validation(self):
        imported = parse_import("email,password,status\na@example.invalid,, NEEDS-HUMAN \nb@example.invalid,,需人工验证\nc@example.invalid,,需要人工验证\nd@example.invalid,password,failed\n".encode(), "human.csv")
        self.assertEqual(imported.skipped_count, 3)
        self.assertEqual([a.email for a in imported.accounts], ["d@example.invalid"])

    def test_csv_quoted_values_and_literal_password_preserved(self):
        rows = parse_accounts('\ufeff邮箱,密码\nfirst@example.invalid," #=x,`$()` "\n'.encode(), "a.csv")
        self.assertEqual(rows[0].password, " #=x,`$()` ")
        self.assertNotIn("first@", repr(rows[0]))

    def test_tsv_comments_headers_and_duplicates(self):
        rows = parse_accounts(b"# comment\nemail\tpassword\nfirst@example.invalid\t pass \n", "a.txt")
        self.assertEqual((rows[0].source_line, rows[0].password), (3, " pass "))
        for data in (b"first@example.invalid\tsecret\nFIRST@example.invalid\tsecret", b"broken secret"):
            with self.assertRaises(ValueError) as error:
                parse_accounts(data, "a.tsv")
            self.assertNotIn("secret", str(error.exception))

    def test_excel_headers_reordered_and_numeric_password_rejected(self):
        from openpyxl import Workbook
        book = Workbook()
        book.active.append(["备注", "密码", "邮箱"])
        book.active.append(["", "001234", "first@example.invalid"])
        output = io.BytesIO(); book.save(output)
        self.assertEqual(parse_accounts(output.getvalue(), "a.xlsx")[0].password, "001234")
        book.active["B2"] = 1234
        output = io.BytesIO(); book.save(output)
        with self.assertRaises(ValueError):
            parse_accounts(output.getvalue(), "a.xlsx")
        book.active["B2"] = '=CONCAT("s","ecret")'
        output = io.BytesIO(); book.save(output)
        with self.assertRaises(ValueError):
            parse_accounts(output.getvalue(), "a.xlsx")
        book.close()

    def test_csv_and_tsv_skip_success_rows_before_validating_credentials(self):
        for filename, delimiter in (("a.csv", ","), ("a.tsv", "\t"), ("a.txt", "\t")):
            with self.subTest(filename=filename):
                output = io.StringIO(newline="")
                writer = csv.writer(output, delimiter=delimiter)
                writer.writerow([" STATUS ", "PASSWORD", "Email"])
                writer.writerow([" SUCCESS ", "", "invalid-but-skipped"])
                writer.writerow(["success", "old-secret", "same@example.invalid"])
                writer.writerow(["failed", " current-secret ", "same@example.invalid"])
                writer.writerow(["", "new-secret", "new@example.invalid"])
                imported = parse_import(output.getvalue().encode(), filename)
                self.assertEqual(imported.skipped_count, 2)
                self.assertEqual([a.source_line for a in imported.accounts], [4, 5])
                self.assertEqual(imported.accounts[0].password, " current-secret ")

    def test_excel_skips_english_and_exported_chinese_success_statuses(self):
        from openpyxl import Workbook
        book = Workbook()
        book.active.append(["邮箱", "状态", "密码"])
        book.active.append(["skipped@example.invalid", "success", "=1+1"])
        book.active.append(["", " 已完成 ", ""])
        book.active.append(["other@example.invalid", "成功", 1234])
        book.active.append(["keep@example.invalid", "查询失败", "001234"])
        output = io.BytesIO(); book.save(output); book.close()
        imported = parse_import(output.getvalue(), "result.xlsx")
        self.assertEqual(imported.skipped_count, 3)
        self.assertEqual([(a.source_line, a.email, a.password) for a in imported.accounts], [(5, "keep@example.invalid", "001234")])

    def test_only_success_is_skipped_and_history_can_keep_all_rows(self):
        data = b"email,password,status\nsuccess@example.invalid,a,success\nkeep@example.invalid,b,not-success\n"
        self.assertEqual(len(parse_accounts(data, "a.csv")), 1)
        self.assertEqual(len(parse_accounts(data, "a.csv", skip_success=False)), 2)
        with self.assertRaises(ValueError):
            parse_accounts(b"email,password,status\ninvalid,,failed\n", "a.csv")

    def test_csv_and_excel_accept_more_than_two_thousand_accounts(self):
        from openpyxl import Workbook
        count = 2505
        csv_data = "email,password\n" + "".join(f"user{i}@example.invalid,secret\n" for i in range(count))
        self.assertEqual(len(parse_accounts(csv_data.encode(), "large.csv")), count)
        book = Workbook(write_only=True)
        sheet = book.create_sheet()
        sheet.append(["email", "password"])
        for i in range(count):
            sheet.append([f"user{i}@example.invalid", "secret"])
        output = io.BytesIO(); book.save(output); book.close()
        self.assertEqual(len(parse_accounts(output.getvalue(), "large.xlsx")), count)

    def test_manual_limit_counts_only_accounts_remaining_after_success_skip(self):
        data = ("email,password,status\n"
                "done@example.invalid,,success\n"
                "first@example.invalid,one,failed\n"
                "second@example.invalid,two,\n"
                "later@example.invalid,three,pending\n"
                "also-done@example.invalid,,已完成\n").encode()
        imported = parse_import(data, "limited.csv", max_accounts=2)
        self.assertEqual([account.source_line for account in imported.accounts], [3, 4])
        self.assertEqual((imported.skipped_count, imported.deferred_count), (2, 1))
        unlimited = parse_import(data, "unlimited.csv")
        self.assertEqual((len(unlimited.accounts), unlimited.deferred_count), (3, 0))


class ResultsTests(unittest.TestCase):
    def test_inquiry_data_alone_does_not_prove_persona_identity_requirement(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = collect(tmp, "first@example.invalid", {"authenticated":False, "verificationRequired":True,
                           "failureStage":"wait_exchange", "errorMessage":"COM exchange not observed"})
            self.assertEqual(data["status"], "failed")
            self.assertNotIn("verificationRequirement", data["result"])

    def test_account_verification_completes_without_claiming_authenticated_or_selected(self):
        for flags, expected in (({"personaVerificationRequired":True}, "未进行身份验证"),
                                ({"requiresHumanInteraction":True, "humanVerificationObservation":{"personaPageVisible":True}}, "未进行身份验证")):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                (Path(tmp) / "failure.png").write_bytes(PNG)
                data = collect(tmp, "first@example.invalid", {"authenticated":False, **flags})
                self.assertEqual(data["status"], "success")
                self.assertIn(expected, data["result"]["verificationRequirement"])
                self.assertIsNone(data["error"])
                self.assertIsNone(data["selections"])
                self.assertIsNone(data["cookie_header"])
                self.assertEqual(data["screenshot"], PNG)
                self.assertFalse(data["result"]["authenticated"])

    def test_access_paused_stays_distinct_from_account_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = collect(tmp, "first@example.invalid", {"requiresHumanInteraction":True, "browsingActivityPaused":True})
            self.assertEqual(data["status"], "failed")
            self.assertIn("访问已暂停", data["result"]["verificationRequirement"])

    def test_selected_shows_and_no_selections_are_distinct_from_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            summary = successful_run(path, "first@example.invalid")
            data = collect(path, "first@example.invalid", summary)
            self.assertEqual(data["status"], "success")
            self.assertEqual(data["selections"][0]["date"], "2027-05-19")
            self.assertEqual(data["screenshot"], PNG)
            self.assertEqual(data["cookie_header"], "SID=first")
            successful_run(path, "first@example.invalid", record=None)
            self.assertEqual(selections_from_har(path, "first@example.invalid")[0], [])
            successful_run(path, "first@example.invalid", record="missing")
            data = collect(path, "first@example.invalid", summary)
            self.assertEqual(data["status"], "success")
            self.assertIsNone(data["selections"])
            self.assertIsNone(data["error"])
            self.assertEqual(data["screenshot"], PNG)
            self.assertIn("selectionWarning", data["result"])

    def test_unknown_market_preserves_id_and_screenshot_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = successful_run(tmp, "first@example.invalid", markets=[])
            data = collect(tmp, "first@example.invalid", summary)
            self.assertEqual(data["status"], "success")
            self.assertIsNone(data["error"])
            self.assertEqual(data["selections"][0]["id"], "m1")
            self.assertFalse(data["selections"][0]["resolved"])

    def test_missing_screenshot_still_reports_partial_when_selections_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = successful_run(tmp, "first@example.invalid", record="missing")
            (Path(tmp) / "com-result.png").unlink()
            data = collect(tmp, "first@example.invalid", summary)
            self.assertEqual(data["status"], "partial")
            self.assertIn("整页截图未生成", data["error"])

    def test_claimed_success_without_matching_cookie_is_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = successful_run(tmp, "first@example.invalid")
            (Path(tmp) / "cookie-header.txt").write_text("SID=someone-else")
            data = collect(tmp, "first@example.invalid", summary)
            self.assertEqual(data["status"], "failed")
            self.assertIsNone(data["cookie_header"])
            self.assertIsNone(data["selections"])


class WebTests(unittest.TestCase):
    def test_manual_proxy_restore_endpoint_recovers_only_node_and_requires_valid_request(self):
        pool = fixture_pool(None)
        for node in pool.nodes[:2]:
            node["screening"] = {"exitIP":"192.0.2.1", "country":"US"}
        pool.configure_reputation(self.store)
        self.app.extensions["queries"].current_pool = pool
        with pool.lease(exclude_ids=["1", "2"]) as proxy:
            for _ in range(10):
                pool.record_outcome(proxy, False)
        self.assertEqual(pool.reputation_status()["disabled_nodes"], 1)
        self.assertEqual(self.client.post("/api/proxies/restore", json={"node_id":"0"}).status_code, 403)
        for body in ({}, {"node_id":123}, {"node_id":"unknown"}, []):
            self.assertEqual(self.client.post("/api/proxies/restore", json=body, headers=self.headers).status_code, 400)
        response = self.client.post("/api/proxies/restore", json={"node_id":"1"}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["confidence"], 10)
        self.assertEqual(pool.reputation_status()["disabled_nodes"], 1)
        response = self.client.post("/api/proxies/restore", json={"node_id":"0"}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(pool.reputation_status()["disabled_nodes"], 0)
        self.assertEqual(self.store.proxy_reputations()["node:0"]["disabled_until"], 0)

    def test_restore_all_disabled_proxies_requires_token_and_persists(self):
        pool = fixture_pool(None)
        pool.configure_reputation(self.store)
        self.app.extensions["queries"].current_pool = pool
        for index in ("0", "1", "2"):
            with pool.lease(exclude_ids=[n for n in ("0", "1", "2") if n != index]) as proxy:
                for _ in range(10 if index != "2" else 3):
                    pool.record_outcome(proxy, False)
        self.assertEqual(self.client.post("/api/proxies/restore-all").status_code, 403)
        self.assertEqual(pool.reputation_status()["disabled_nodes"], 2)
        response = self.client.post("/api/proxies/restore-all", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["restored"], 2)
        self.assertEqual(pool.reputation_status()["disabled_nodes"], 0)
        saved = self.store.proxy_reputations()
        self.assertEqual(saved["node:0"], {"confidence":10, "disabled_until":0})
        self.assertEqual(saved["node:1"], {"confidence":10, "disabled_until":0})
        self.assertEqual(saved["node:2"]["confidence"], 7)
        self.assertEqual(self.client.post("/api/proxies/restore-all", headers=self.headers).get_json()["restored"], 0)

    def test_proxy_page_and_nodes_api_show_scores_without_credentials(self):
        page = self.client.get("/proxies")
        self.assertEqual(page.status_code, 200)
        self.assertIn("代理池", page.get_data(as_text=True))
        self.assertEqual(self.client.get("/api/proxies/nodes").get_json()["nodes"], [])
        pool = fixture_pool(None)
        pool.nodes[0]["screening"] = {"exitIP":"192.0.2.1", "country":"US"}
        service = self.app.extensions["queries"]
        service.current_pool = pool
        with pool.lease(exclude_ids=["1", "2"]) as proxy:
            busy = self.client.get("/api/proxies/nodes").get_json()
            self.assertEqual(busy["nodes"][0]["status"], "in-use")
            for _ in range(10):
                pool.record_outcome(proxy, False)
        response = self.client.get("/api/proxies/nodes")
        data = response.get_json()
        self.assertEqual(data["disabled_nodes"], 1)
        self.assertEqual(data["nodes"][0]["confidence"], 0)
        self.assertEqual(data["nodes"][0]["status"], "disabled")
        self.assertEqual(data["nodes"][0]["exit_ip"], "192.0.2.1")
        self.assertEqual(data["nodes"][0]["disabled_until"], -1)
        for secret in ("username", "password", "socksHost", "test-only"):
            self.assertNotIn(secret, response.get_data(as_text=True))

    def test_legacy_inquiry_data_success_is_repaired_without_discarding_screenshot(self):
        batch_id = self.store.create_batch([Account(1, "spinner@example.invalid", "fixture")], "legacy", DEFAULT_URL)
        rid = self.store.runs(batch_id)[0]["id"]
        self.store.finish_run(rid, {"status":"success", "screenshot":PNG, "result":{
            "verificationRequired":True, "verificationRequirement":"未进行身份验证", "failureStage":"wait_exchange"}})
        self.store.normalize_verification_results()
        row = self.store.run(rid)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["screenshot"], PNG)
        self.assertNotIn("verificationRequirement", json.loads(row["result"]))

    def test_saved_verification_results_are_normalized_and_exposed_in_events_column(self):
        accounts = [Account(1, "identity@example.invalid", "fixture"), Account(2, "paused@example.invalid", "fixture")]
        batch_id = self.store.create_batch(accounts, "verification", DEFAULT_URL)
        ids = [r["id"] for r in self.store.runs(batch_id)]
        for rid, flags in zip(ids, ({"personaVerificationRequired":True}, {"requiresHumanInteraction":True, "browsingActivityPaused":True})):
            self.store.finish_run(rid, {"status":"needs-human", "error":"old", "screenshot":PNG, "result":flags})
        self.store.normalize_verification_results()
        rows = self.store.runs(batch_id)
        self.assertEqual([r["status"] for r in rows], ["success", "failed"])
        self.assertEqual(rows[0]["verification_requirement"], "未进行身份验证")
        self.assertIsNone(rows[0]["error"])
        self.assertEqual(self.store.run(ids[0])["screenshot"], PNG)
        from exports import results_csv
        self.assertIn("未进行身份验证", results_csv(self.store.runs(batch_id, include_credentials=True)).decode("utf-8-sig"))

    def test_manual_human_retry_api_resets_human_status(self):
        batch_id = self.store.create_batch([Account(1, "human@example.invalid", "fixture")], "human", DEFAULT_URL)
        run_id = self.store.runs(batch_id)[0]["id"]
        self.store.finish_run(run_id, {"status":"needs-human", "error":"paused", "screenshot":PNG, "result":{}})
        self.store.finish_batch(batch_id)
        response = self.client.post(f"/api/batches/{batch_id}/retry", json={"scope":"human", "run_id":run_id}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.store.run(run_id)["status"], "pending")
        self.assertIsNone(self.store.run(run_id)["error"])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = create_app(Path(self.tmp.name) / "db.sqlite3")
        self.client = self.app.test_client()
        self.store = self.app.extensions["store"]
        page = self.client.get("/").get_data(as_text=True)
        self.headers = {"X-App-Token": re.search(r'name="app-token" content="([^"]+)"', page)[1]}

    def upload(self, content=b"email,password\nfirst@example.invalid,private-password\n", **form):
        return self.client.post("/api/import", data={"file": (io.BytesIO(content), "accounts.csv"), **form}, headers=self.headers)

    def test_account_detail_returns_plaintext_password_and_cookie_directly(self):
        response = self.upload()
        self.assertEqual(response.status_code, 201)
        batch_id = response.json["id"]
        response = self.client.get(f"/api/batches/{batch_id}")
        self.assertNotIn("private-password", response.get_data(as_text=True))
        run_id = response.json["runs"][0]["id"]
        self.assertEqual(self.store.run(run_id)["password"], "private-password")
        self.store.finish_run(run_id, {"status":"success", "cookie_header":"SID=full-session", "result":{}})
        detail = self.client.get(f"/api/runs/{run_id}").json
        self.assertEqual(detail["password"], "private-password")
        self.assertEqual(detail["cookie_header"], "SID=full-session")
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn('id="detail-password" type="text"', page)
        self.assertNotIn('id="reveal-password"', page)
        self.assertNotIn('id="reveal-cookie"', page)
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_invalid_upload_is_atomic_and_post_requires_token(self):
        response = self.upload(b"email,password\nfirst@example.invalid,a\nfirst@example.invalid,b\n")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.store.batches(), [])
        self.assertEqual(self.client.post("/api/import").status_code, 403)
        self.assertEqual(self.client.get("/api/runs/999").status_code, 404)

    def test_api_proxy_filter_routes_to_cached_json_without_external_probes(self):
        service = self.app.extensions["queries"]
        pool = fixture_pool(DEFAULT_URL)
        with patch("jobs.filtered_pool_from_api", return_value=pool) as load, patch("jobs.fresh_pool") as deep:
            response = self.client.post("/api/proxies/filter", json={"campaign":DEFAULT_URL}, headers=self.headers)
            self.assertEqual(response.status_code, 202)
            service.proxy_thread.join(timeout=5)
            load.assert_called_once()
            deep.assert_not_called()
        self.assertIs(service.current_pool, pool)
        self.assertEqual(service.proxy_status()["mode"], "api")
        self.assertEqual(service.proxy_status()["status"], "ready")
        self.assertEqual(self.client.post("/api/proxies/refresh", json={"mode":"invalid"}, headers=self.headers).status_code, 400)
        self.assertIn(b'id="filter-proxies"', self.client.get("/").data)

    def test_default_proxy_refresh_loads_all_regions_without_prechecks(self):
        pool=fixture_pool(DEFAULT_URL)
        for node,country in zip(pool.nodes,("HK","JP","TW")):
            node["screening"]={"country":country,"exitIP":"192.0.2."+str(int(node["id"])+1)}
        service=self.app.extensions["queries"]
        with patch("jobs.filtered_pool_from_api",return_value=pool) as load, patch("jobs.fresh_pool") as deep:
            response=self.client.post("/api/proxies/refresh",json={"campaign":DEFAULT_URL},headers=self.headers)
            self.assertEqual(response.status_code,202)
            service.proxy_thread.join(timeout=5)
            load.assert_called_once()
            deep.assert_not_called()
        self.assertEqual(service.pool_mode,"api")
        self.assertEqual({n["country"] for n in service.proxy_nodes()["nodes"]},{"HK","JP","TW"})

    def test_proxy_refresh_is_async_requires_token_and_coalesces_duplicate_clicks(self):
        service = self.app.extensions["queries"]
        entered, release = threading.Event(), threading.Event()
        calls = []
        def factory(campaign):
            calls.append(campaign)
            entered.set()
            release.wait(timeout=5)
            return fixture_pool(campaign)
        service.pool_factory = factory
        self.assertEqual(self.client.post("/api/proxies/refresh").status_code, 403)
        self.assertEqual(self.client.post("/api/proxies/refresh", json={"campaign":"https://other.invalid"}, headers=self.headers).status_code, 400)
        self.assertEqual(self.client.post("/api/proxies/refresh", json={"campaign":None}, headers=self.headers).status_code, 400)
        response = self.client.post("/api/proxies/refresh", json={"campaign":DEFAULT_URL}, headers=self.headers)
        self.assertEqual(response.status_code, 202)
        try:
            self.assertTrue(entered.wait(timeout=2))
            self.assertEqual(self.client.get("/api/proxies").json["status"], "checking")
            self.assertEqual(self.client.post("/api/proxies/refresh", json={"campaign":DEFAULT_URL}, headers=self.headers).status_code, 202)
            self.assertEqual(calls, [DEFAULT_URL])
        finally:
            release.set()
            service.proxy_thread.join(timeout=5)
        status = self.client.get("/api/proxies").json
        self.assertEqual((status["status"], status["accepted"]), ("ready", 3))
        self.assertNotIn("test-only", str(status))
        self.assertEqual(self.store.batches(), [])

    def test_upload_reports_skipped_rows_and_only_creates_pending_accounts(self):
        response = self.upload(b"email,password,status\nfirst@example.invalid,old,success\nsecond@example.invalid,new,failed\nthird@example.invalid,next,\n")
        self.assertEqual(response.status_code, 201)
        self.assertEqual((response.json["count"], response.json["skipped"]), (2, 1))
        rows = self.store.runs(response.json["id"])
        self.assertEqual([r["email"] for r in rows], ["second@example.invalid", "third@example.invalid"])
        self.assertEqual([r["source_line"] for r in rows], [3, 4])
        self.assertEqual({r["status"] for r in rows}, {"pending"})

    def test_all_success_upload_does_not_create_an_empty_batch(self):
        response = self.upload(b"email,password,status\nfirst@example.invalid,,success\nsecond@example.invalid,, SUCCESS \n")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"id":None, "count":0, "skipped":2, "deferred":0})
        self.assertEqual(self.store.batches(), [])

    def test_exported_excel_can_be_reuploaded_and_skips_successful_accounts(self):
        old_id = self.upload(b"email,password\nfirst@example.invalid,old\nsecond@example.invalid,still-needed\n").json["id"]
        rows = self.store.runs(old_id)
        self.store.finish_run(rows[0]["id"], {"status":"success", "selections":[], "screenshot":PNG, "screenshot_kind":"full-page", "result":{}})
        self.store.finish_run(rows[1]["id"], {"status":"failed", "result":{}})
        self.store.finish_batch(old_id)
        exported = self.client.get(f"/api/batches/{old_id}/export.xlsx")
        response = self.client.post("/api/import", data={"file":(io.BytesIO(exported.data), "results.xlsx")}, headers=self.headers)
        exported.close()
        self.assertEqual(response.status_code, 201)
        self.assertEqual((response.json["count"], response.json["skipped"]), (1,1))
        rows = self.store.runs(response.json["id"])
        self.assertEqual(rows[0]["email"], "second@example.invalid")
        self.assertEqual(self.store.run(rows[0]["id"])["password"], "still-needed")

    def test_continuous_retry_api_requires_boolean_and_existing_pool(self):
        batch_id = self.upload().json["id"]
        endpoint = f"/api/batches/{batch_id}/continuous-retry"
        for value in ({}, [], {"enabled":1}, {"enabled":"true"}):
            self.assertEqual(self.client.post(endpoint, json=value, headers=self.headers).status_code, 400)
        self.assertEqual(self.client.post(endpoint, json={"enabled":True}, headers=self.headers).status_code, 400)
        self.assertEqual(self.store.batch(batch_id)["status"], "draft")
        self.assertEqual(self.store.batch(batch_id)["continuous_retry"], 0)
        self.assertEqual(self.client.post(endpoint, json={"enabled":False}, headers=self.headers).status_code, 200)
        self.assertIn(b'id="continuous-retry-button"', self.client.get("/").data)

    def test_update_running_concurrency_api_validates_and_preserves_other_tasks(self):
        batch_id = self.upload().json["id"]
        service = self.app.extensions["queries"]
        service.active = {batch_id: threading.Event()}
        service.retry_queues[batch_id] = deque()
        endpoint = f"/api/batches/{batch_id}/concurrency"
        for value in (0, -1, 1.5, True, None, ""):
            self.assertEqual(self.client.post(endpoint, json={"concurrency":value}, headers=self.headers).status_code, 400)
        self.assertEqual(self.client.post(endpoint, json=[], headers=self.headers).status_code, 400)
        self.assertEqual(self.client.post(endpoint, json={"concurrency":100}, headers=self.headers).status_code, 200)
        self.assertEqual(self.store.batch(batch_id)["concurrency"], 100)
        other_id = self.upload().json["id"]
        self.assertEqual(self.client.post(f"/api/batches/{other_id}/concurrency", json={"concurrency":2}, headers=self.headers).status_code, 400)
        service.active[batch_id].set()
        self.assertEqual(self.client.post(endpoint, json={"concurrency":2}, headers=self.headers).status_code, 400)
        service.active = {}
        self.assertIn(b'id="apply-concurrency"', self.client.get("/").data)

    @patch("jobs.browser_command", return_value=(["node"], False))
    def test_manual_counts_accept_high_concurrency_and_start_uses_current_setting(self, _):
        service = self.app.extensions["queries"]
        service.pool_factory = fixture_pool
        service.delay = 0
        service.execute = lambda email, password, out, *args, **kwargs: successful_run(out, email)
        content = b"email,password,status\ndone@example.invalid,,success\none@example.invalid,a,failed\ntwo@example.invalid,b,\nthree@example.invalid,c,\n"
        response = self.upload(content, concurrency="32", max_accounts="2")
        self.assertEqual(response.status_code, 201)
        self.assertEqual((response.json["count"], response.json["skipped"], response.json["deferred"]), (2, 1, 1))
        batch_id = response.json["id"]
        self.assertEqual(self.store.batch(batch_id)["concurrency"], 32)
        service.refresh_proxies(DEFAULT_URL)
        service.proxy_thread.join(timeout=5)
        response = self.client.post(f"/api/batches/{batch_id}/start", json={"concurrency":64}, headers=self.headers)
        self.assertEqual(response.status_code, 202)
        list(service.threads.values())[-1].join(timeout=10)
        self.assertFalse(list(service.threads.values())[-1].is_alive())
        self.assertEqual(self.store.batch(batch_id)["concurrency"], 64)
        self.assertEqual(self.store.batch(batch_id)["effective_concurrency"], 2)
        self.assertEqual({row["status"] for row in self.store.runs(batch_id)}, {"success"})

    def test_counts_require_positive_integers_but_account_limit_can_be_empty(self):
        for form in ({"concurrency":"0"}, {"concurrency":"1.5"}, {"max_accounts":"0"}, {"max_accounts":"-1"}):
            with self.subTest(form=form):
                self.assertEqual(self.upload(**form).status_code, 400)
        self.assertEqual(self.store.batches(), [])
        response = self.upload(concurrency="100", max_accounts="")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["deferred"], 0)

    def test_retry_resets_the_same_record_and_discards_old_result(self):
        batch_id = self.upload().json["id"]
        run_id = self.store.runs(batch_id)[0]["id"]
        self.store.finish_run(run_id, {"status":"failed", "screenshot":PNG, "screenshot_kind":"failure",
                                      "error":"fixture failure", "result":{"failureStage":"old-stage"},
                                      "cookie_header":"old-cookie", "cookies":[{"name":"old"}],
                                      "storage_state":{"cookies":[]}, "diagnostic_log":"old-log", "selections":[]})
        with self.store.connect() as db:
            db.execute("UPDATE runs SET progress=?,started_at=?,attempt_count=3 WHERE id=?", ('{"stage":"old-stage"}', "old-time", run_id))
        self.store.finish_batch(batch_id)
        self.assertEqual(self.client.get(f"/api/runs/{run_id}/screenshot").data, PNG)
        response = self.client.post(f"/api/batches/{batch_id}/retry", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["id"], batch_id)
        self.assertEqual(len(self.store.batches()), 1)
        self.assertEqual([r["id"] for r in self.store.runs(batch_id)], [run_id])
        record = self.store.run(run_id)
        self.assertEqual(record["status"], "pending")
        self.assertEqual(record["attempt_count"], 0)
        self.assertEqual(record["password"], "private-password")
        for key in ("stage", "started_at", "finished_at", "progress", "result", "selections", "cookie_header",
                    "cookies", "storage_state", "screenshot", "screenshot_kind", "diagnostic_log", "error"):
            self.assertIsNone(record[key], key)
        self.assertEqual(self.client.get(f"/api/runs/{run_id}/screenshot").status_code, 404)
        self.assertEqual(self.store.batch(batch_id)["status"], "draft")

    def test_delete_task_removes_its_records_and_preserves_other_tasks(self):
        batch_id = self.upload().json["id"]
        other_id = self.upload().json["id"]
        run_id = self.store.runs(batch_id)[0]["id"]
        other_run_id = self.store.runs(other_id)[0]["id"]
        for record_id in (run_id, other_run_id):
            self.store.finish_run(record_id, {"status":"success", "cookie_header":"fixture-cookie",
                                             "screenshot":PNG, "selections":[], "result":{}})
        self.store.finish_batch(batch_id)
        self.store.finish_batch(other_id)
        other_batch = self.store.batch(other_id)
        other_record = self.store.run(other_run_id)
        response = self.client.delete(f"/api/batches/{batch_id}", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"ok":True})
        for path in (f"/api/batches/{batch_id}", f"/api/runs/{run_id}", f"/api/runs/{run_id}/screenshot",
                     f"/api/batches/{batch_id}/export.xlsx"):
            self.assertEqual(self.client.get(path).status_code, 404, path)
        self.assertEqual(self.store.batch(other_id), other_batch)
        self.assertEqual(self.store.run(other_run_id), other_record)
        self.assertEqual([b["id"] for b in self.store.batches()], [other_id])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM runs").fetchone()[0], 1)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_delete_requires_token_and_can_remove_the_last_draft(self):
        batch_id = self.upload().json["id"]
        self.assertEqual(self.client.delete(f"/api/batches/{batch_id}").status_code, 403)
        self.assertEqual(len(self.store.runs(batch_id)), 1)
        self.assertEqual(self.client.delete(f"/api/batches/{batch_id}", headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get("/api/batches").json, {"batches":[]})
        self.assertEqual(self.client.delete(f"/api/batches/{batch_id}", headers=self.headers).status_code, 404)

    def test_delete_rejects_running_or_still_finishing_tasks(self):
        batch_id = self.upload().json["id"]
        service = self.app.extensions["queries"]
        old_rows = self.store.runs(batch_id)
        for status, active in (("running", None), ("cancelled", (batch_id, threading.Event()))):
            with self.store.connect() as db:
                db.execute("UPDATE batches SET status=? WHERE id=?", (status, batch_id))
            service.active = {active[0]: active[1]} if active else {}
            old_batch = self.store.batch(batch_id)
            response = self.client.delete(f"/api/batches/{batch_id}", headers=self.headers)
            self.assertEqual(response.status_code, 400)
            self.assertIn("等待任务结束", response.json["error"])
            self.assertEqual(self.store.batch(batch_id), old_batch)
            self.assertEqual(self.store.runs(batch_id), old_rows)

    def test_delete_allows_an_unrelated_task_to_keep_running(self):
        batch_id = self.upload().json["id"]
        active_id = self.upload().json["id"]
        service = self.app.extensions["queries"]
        service.active = {active_id: threading.Event()}
        with self.store.connect() as db:
            db.execute("UPDATE batches SET status='running' WHERE id=?", (active_id,))
        original = self.store.runs(active_id)
        self.assertEqual(self.client.delete(f"/api/batches/{batch_id}", headers=self.headers).status_code, 200)
        self.assertEqual(self.store.runs(active_id), original)
        self.assertEqual(self.store.batch(active_id)["status"], "running")
        self.assertFalse(service.active[active_id].is_set())

    def test_csv_export_contains_plaintext_password_and_cookie(self):
        batch_id = self.upload().json["id"]
        run_id = self.store.runs(batch_id)[0]["id"]
        self.store.finish_run(run_id, {"status":"success", "cookie_header":"SID=full-session", "result":{}})
        response = self.client.get(f"/api/batches/{batch_id}/export")
        self.assertTrue(response.data.startswith(b"\xef\xbb\xbf"))
        row = list(csv.DictReader(io.StringIO(response.data.decode("utf-8-sig"))))[0]
        self.assertEqual(row["密码"], "private-password")
        self.assertEqual(row["Cookie"], "SID=full-session")

    def test_zip_export_includes_original_images_and_csv_mapping(self):
        batch_id = self.upload().json["id"]
        run_id = self.store.runs(batch_id)[0]["id"]
        self.store.finish_run(run_id, {"status":"success", "screenshot":PNG, "screenshot_kind":"full-page",
                                      "cookie_header":"private-cookie", "selections":[{"name":"A selected show"}], "result":{}})
        response = self.client.get(f"/api/batches/{batch_id}/export.zip")
        self.assertEqual(response.status_code, 200)
        self.assertIn("query-1.zip", response.headers["Content-Disposition"])
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            filename = f"screenshots/account-{run_id:06d}.png"
            self.assertEqual(set(archive.namelist()), {"results.csv", filename})
            self.assertEqual(archive.read(filename), PNG)
            table = archive.read("results.csv").decode("utf-8-sig")
            self.assertIn("first@example.invalid", table)
            self.assertIn(filename, table)
            self.assertIn("整页长截图", table)
            exported = list(csv.DictReader(io.StringIO(table)))[0]
            self.assertEqual(exported["密码"], "private-password")
            self.assertEqual(exported["Cookie"], "private-cookie")
            self.assertIsNone(archive.testzip())
        response.close()

    def test_zip_export_with_missing_images_still_includes_account_result(self):
        batch_id = self.upload().json["id"]
        response = self.client.get(f"/api/batches/{batch_id}/export.zip")
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            self.assertEqual(archive.namelist(), ["results.csv"])
            self.assertIn("无截图", archive.read("results.csv").decode("utf-8-sig"))
        response.close()

    def test_excel_embeds_original_images_in_matching_account_rows(self):
        from openpyxl import load_workbook
        from PIL import Image
        batch_id = self.upload(b"email,password\nfirst@example.invalid,private-password\nsecond@example.invalid,second-password\nthird@example.invalid,third-password\n").json["id"]
        rows = self.store.runs(batch_id)
        tall = io.BytesIO()
        Image.new("RGB", (1440, 5283), "#186553").save(tall, format="PNG")
        original = tall.getvalue()
        self.store.finish_run(rows[0]["id"], {"status":"success", "screenshot":original, "screenshot_kind":"full-page",
                                            "cookie_header":"private-cookie", "selections":[{"name":"Selected show"}], "result":{}})
        self.store.finish_run(rows[1]["id"], {"status":"failed", "screenshot":PNG, "screenshot_kind":"failure",
                                            "error":"=untrusted-formula()", "result":{}})
        response = self.client.get(f"/api/batches/{batch_id}/export.xlsx")
        self.assertEqual(response.status_code, 200)
        self.assertIn("query-1.xlsx", response.headers["Content-Disposition"])
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            media = [name for name in archive.namelist() if name.startswith("xl/media/")]
            self.assertEqual(len(media), 2)
            self.assertEqual(archive.read(media[0]), original)
            self.assertEqual(archive.read(media[1]), PNG)
            xml = archive.read("xl/worksheets/sheet1.xml")
            self.assertIn(b"private-password", xml)
            self.assertIn(b"private-cookie", xml)
            self.assertNotIn(b"<f>", xml)
            self.assertIsNone(archive.testzip())
        book = load_workbook(io.BytesIO(response.data))
        try:
            sheet = book["查询结果"]
            self.assertEqual(sheet["A2"].value, "first@example.invalid")
            self.assertEqual(sheet["A3"].value, "second@example.invalid")
            self.assertEqual(sheet["B2"].value, "private-password")
            self.assertEqual(sheet["B3"].value, "second-password")
            self.assertEqual(sheet["I2"].value, "private-cookie")
            self.assertIsNone(sheet["I3"].value)
            self.assertEqual(sheet["D2"].value, "Selected show")
            self.assertEqual(sheet["E4"].value, "暂无截图")
            self.assertEqual(sheet["F2"].value, "整页长截图")
            self.assertEqual(sheet["F3"].value, "失败时页面")
            self.assertEqual(sheet["G3"].data_type, "s")
            self.assertEqual(sheet.freeze_panes, "C2")
            self.assertTrue(all(not dimension.hidden for dimension in sheet.column_dimensions.values()))
            self.assertEqual([(image.anchor._from.col, image.anchor._from.row) for image in sheet._images], [(4,1), (4,2)])
            self.assertEqual((sheet._images[0].width, sheet._images[0].height), (1440,5283))
            self.assertLessEqual(sheet.row_dimensions[2].height, 409)
            self.assertAlmostEqual(sheet._images[0].anchor.ext.cx / sheet._images[0].anchor.ext.cy, 1440 / 5283, places=5)
        finally:
            book.close()
            response.close()

    def test_exports_preserve_literal_passwords_and_long_cookies(self):
        from openpyxl import load_workbook
        for password in ("001234", " =literal, password ", "=1+1"):
            with self.subTest(password=password):
                content = io.StringIO(newline="")
                writer = csv.writer(content)
                writer.writerow(["email", "password"])
                writer.writerow(["test@example.invalid", password])
                batch_id = self.upload(content.getvalue().encode()).json["id"]
                run_id = self.store.runs(batch_id)[0]["id"]
                cookie = "SID=" + "x" * 33000
                self.store.finish_run(run_id, {"status":"success", "cookie_header":cookie, "result":{}})
                response = self.client.get(f"/api/batches/{batch_id}/export")
                row = list(csv.DictReader(io.StringIO(response.data.decode("utf-8-sig"))))[0]
                self.assertEqual(row["密码"], password)
                self.assertEqual(row["Cookie"], cookie)
                response = self.client.get(f"/api/batches/{batch_id}/export.xlsx")
                book = load_workbook(io.BytesIO(response.data))
                try:
                    sheet = book["查询结果"]
                    self.assertEqual(sheet["B2"].value, password)
                    self.assertEqual(sheet["B2"].data_type, "s")
                    self.assertEqual(sheet["I2"].value + sheet["J2"].value, cookie)
                    self.assertEqual(sheet["J1"].value, "Cookie（续 2）")
                finally:
                    book.close()
                    response.close()


class JobTests(unittest.TestCase):
    def test_live_retry_queues_are_isolated_between_batches(self):
        service = self.service(self.store)
        ids = [self.store.create_batch([account], "retry-queues", DEFAULT_URL)
               for account in self.accounts[:2]]
        run_ids = [self.store.runs(batch_id)[0]["id"] for batch_id in ids]
        for batch_id, run_id in zip(ids, run_ids):
            self.store.finish_run(run_id, {"status":"failed", "result":{}})
            with self.store.connect(write=True) as db:
                db.execute("UPDATE batches SET status='running' WHERE id=?", (batch_id,))
            service.active[batch_id] = threading.Event()
            service.retry_queues[batch_id] = deque()
        service.retry(ids[0], run_id=run_ids[0], start=True)
        self.assertEqual(list(service.retry_queues[ids[0]]), [run_ids[0]])
        self.assertEqual(list(service.retry_queues[ids[1]]), [])
        self.assertEqual(self.store.run_status(run_ids[1]), "failed")
        service.retry(ids[1], run_id=run_ids[1], start=True)
        service.cancel(ids[0])
        self.assertEqual(self.store.run_status(run_ids[0]), "cancelled")
        self.assertEqual(self.store.run_status(run_ids[1]), "pending")
        self.assertFalse(service.active[ids[1]].is_set())

    @patch("jobs.browser_command", return_value=(["node"], False))
    def test_batches_wait_for_shared_proxy_and_shutdown_cancels_all(self, _):
        entered = threading.Event()
        def execute(email, password, out, *args, stop_event, **kwargs):
            entered.set()
            stop_event.wait(5)
            raise InterruptedError()
        service = self.service(self.store, execute=execute, delay=0)
        service.current_pool.nodes = service.current_pool.nodes[:1]
        ids = [self.store.create_batch([account], "shared-proxy", DEFAULT_URL, 1)
               for account in self.accounts[:2]]
        try:
            service.start(ids[0])
            self.assertTrue(entered.wait(5))
            service.start(ids[1])
            self.wait_status(self.store.runs(ids[1])[0]["id"], "running")
            self.assertEqual(len(service.current_pool.leased), 1)
            with service.lock:
                self.assertEqual(set(service.active), set(ids))
        finally:
            service.shutdown()
        self.assertEqual(service.active, {})
        self.assertEqual(service.current_pool.leased, set())
        for batch_id in ids:
            self.assertFalse(service.threads[batch_id].is_alive())
            self.assertEqual(self.store.runs(batch_id)[0]["status"], "cancelled")

    @patch("jobs.browser_command", return_value=(["node"], False))
    def test_api_filtered_pool_supports_parallel_campaigns(self, _):
        entered = [threading.Event(), threading.Event()]
        release = threading.Event()
        def execute(email, password, out, *args, **kwargs):
            entered[int(email[4])].set()
            release.wait(5)
            return successful_run(out, email)
        service = self.service(self.store, execute=execute, delay=0)
        service.refresh_proxies(DEFAULT_URL, mode="api")
        service.proxy_thread.join(5)
        ids = [self.store.create_batch([account], "campaigns", DEFAULT_URL + suffix, 1)
               for account, suffix in zip(self.accounts[:2], ("", "-other"))]
        try:
            for batch_id in ids:
                service.start(batch_id)
            self.assertTrue(all(event.wait(5) for event in entered))
            service.cancel(ids[0])
            self.assertFalse(service.active[ids[1]].is_set())
        finally:
            release.set()
            service.shutdown()

    @patch("jobs.browser_command", return_value=(["node"], False))
    def test_parallel_batches_have_independent_limits_and_cancellation(self, _):
        entered = [threading.Event(), threading.Event()]
        release = threading.Event()
        leased, collision = set(), []
        lock = threading.Lock()

        def execute(email, password, out, *args, proxy, stop_event, **kwargs):
            index = int(email[4])
            with lock:
                if proxy.node["id"] in leased:
                    collision.append(proxy.node["id"])
                leased.add(proxy.node["id"])
            entered[index].set()
            while not release.wait(0.01) and not stop_event.is_set():
                pass
            with lock:
                leased.remove(proxy.node["id"])
            return successful_run(out, email)

        service = self.service(self.store, execute=execute, delay=0)
        first, second = [self.store.create_batch([account], "parallel", DEFAULT_URL, 1)
                         for account in self.accounts[:2]]
        try:
            service.start(first)
            service.start(second)
            self.assertTrue(all(event.wait(5) for event in entered))
            with self.assertRaises(ValueError):
                service.start(first)
            service.set_concurrency(second, 7)
            self.assertEqual(self.store.batch(first)["concurrency"], 1)
            self.assertEqual(self.store.batch(second)["concurrency"], 7)
            service.cancel(first)
            service.threads[first].join(5)
            self.assertNotIn(first, service.active)
            self.assertFalse(service.active[second].is_set())
            self.assertEqual(self.store.batch(second)["status"], "running")
            self.assertEqual(collision, [])
        finally:
            release.set()
            service.shutdown()
        self.assertEqual(self.store.runs(second)[0]["status"], "success")

    @patch("jobs.CONTINUOUS_RETRY_DELAY", 0.01)
    @patch("jobs.browser_command", return_value=(["node"], False))
    def test_retry_and_continuous_retry_can_start_alongside_another_batch(self, _):
        entered, release = threading.Event(), threading.Event()
        def execute(email, password, out, *args, **kwargs):
            if email == self.accounts[0].email:
                entered.set()
                release.wait(5)
            return successful_run(out, email)
        service = self.service(self.store, execute=execute, delay=0)
        ids = [self.store.create_batch([account], "parallel-retry", DEFAULT_URL, 1)
               for account in self.accounts[:3]]
        for batch_id in ids[1:]:
            row = self.store.runs(batch_id)[0]
            self.store.finish_run(row["id"], {"status":"failed", "result":{}})
            self.store.finish_batch(batch_id)
        try:
            service.start(ids[0])
            self.assertTrue(entered.wait(5))
            service.retry(ids[1], scope="failed", start=True)
            service.set_continuous_retry(ids[2], True)
            for batch_id in ids[1:]:
                service.threads[batch_id].join(5)
                self.assertEqual(self.store.runs(batch_id)[0]["status"], "success")
            self.assertIn(ids[0], service.active)
            self.assertEqual(self.store.batch(ids[0])["status"], "running")
        finally:
            release.set()
            service.shutdown()

    def service(self, store, **options):
        service = QueryService(store, **options)
        if options.get("pool_factory"):
            service.refresh_proxies(DEFAULT_URL)
            service.proxy_thread.join(timeout=5)
        else:
            service.current_pool = fixture_pool(None)
            service.pool_campaign = DEFAULT_URL
        return service

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "jobs.sqlite3")
        self.accounts = [Account(i+1, f"user{i}@example.invalid", f"password-{i}") for i in range(6)]

    def wait(self, service):
        list(service.threads.values())[-1].join(timeout=12)
        self.assertFalse(list(service.threads.values())[-1].is_alive())

    def wait_status(self, run_id, status):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.store.run(run_id)["status"] == status:
                return
            time.sleep(0.01)
        self.fail(f"Record {run_id} did not reach {status}")

    @patch("jobs.CONTINUOUS_RETRY_DELAY", 0.01)
    @patch("jobs.browser_command", return_value=(["node"], False))
    def test_continuous_retry_exceeds_three_attempts_and_stops_on_success(self, _):
        calls = []
        def execute(email, password, out, *args, proxy=None, **kwargs):
            calls.append(proxy.node["id"])
            return successful_run(out, email) if len(calls) == 7 else {"authenticated":False, "passwordStatus":403}
        batch_id = self.store.create_batch(self.accounts[:1], "continuous", DEFAULT_URL, 1)
        service = self.service(self.store, execute=execute, delay=0)
        with patch("jobs.fresh_pool") as refresh:
            service.set_continuous_retry(batch_id, True)
            self.wait(service)
            refresh.assert_not_called()
        self.assertEqual(len(calls), 7)
        self.assertTrue(all(a != b for a, b in zip(calls, calls[1:])))
        self.assertEqual(self.store.runs(batch_id)[0]["status"], "success")
        self.assertEqual(self.store.batch(batch_id)["continuous_retry"], 0)
        self.assertEqual(len(self.store.runs(batch_id)), 1)

    @patch("jobs.CONTINUOUS_RETRY_DELAY", 0.01)
    @patch("jobs.browser_command", return_value=(["node"], False))
    def test_continuous_retry_goes_behind_unprocessed_accounts(self, _):
        calls = []
        batch_id = self.store.create_batch(self.accounts[:3], "fair-retry", DEFAULT_URL, 1)
        def execute(email, password, out, *args, **kwargs):
            calls.append(email)
            row = next(r for r in self.store.runs(batch_id) if r["email"] == email)
            self.assertEqual(row["attempt_count"], 1)
            if email == self.accounts[0].email and calls.count(email) == 1:
                return {"authenticated":False, "passwordStatus":403}
            return successful_run(out, email)
        service = self.service(self.store, execute=execute, delay=0)
        service.set_continuous_retry(batch_id, True)
        self.wait(service)
        self.assertEqual(calls, [a.email for a in self.accounts[:3]] + [self.accounts[0].email])
        self.assertEqual({r["status"] for r in self.store.runs(batch_id)}, {"success"})

    @patch("jobs.CONTINUOUS_RETRY_DELAY", 0.01)
    @patch("jobs.browser_command", return_value=(["node"], False))
    def test_continuous_retry_with_only_one_exit_can_take_another_queue_turn(self, _):
        calls = []
        pool = ProxyPool([{"id":"single", "username":"route", "screening":{"exitIP":"192.0.2.1"}}],
                         "127.0.0.1", 10801, "fixture")
        def execute(email, password, out, *args, **kwargs):
            calls.append(email)
            if len(calls) == 1:
                return {"authenticated":False, "passwordStatus":403}
            return successful_run(out, email)
        batch_id = self.store.create_batch(self.accounts[:1], "single-exit-requeue", DEFAULT_URL, 1)
        service = self.service(self.store, execute=execute, pool_factory=lambda _:pool, delay=0)
        service.set_continuous_retry(batch_id, True)
        self.wait(service)
        self.assertEqual(len(calls), 2)
        row = self.store.runs(batch_id)[0]
        self.assertEqual((row["status"], row["attempt_count"]), ("success", 1))
        self.assertEqual(pool.leased, set())

    @patch("jobs.CONTINUOUS_RETRY_DELAY", 10)
    @patch("jobs.browser_command", return_value=(["node"], False))
    def test_disabling_continuous_retry_keeps_failed_result(self, _):
        calls = []
        def execute(email, password, out, *args, **kwargs):
            calls.append(email)
            return {"authenticated":False, "passwordStatus":403}
        batch_id = self.store.create_batch(self.accounts[:1], "disable-continuous", DEFAULT_URL, 1)
        service = self.service(self.store, execute=execute, delay=0)
        service.set_continuous_retry(batch_id, True)
        self.wait_status(self.store.runs(batch_id)[0]["id"], "failed")
        service.set_continuous_retry(batch_id, False)
        self.wait(service)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.store.runs(batch_id)[0]["status"], "failed")
        self.assertEqual(self.store.batch(batch_id)["continuous_retry"], 0)

    @patch("jobs.CONTINUOUS_RETRY_DELAY", 0.01)
    @patch("jobs.browser_command", return_value=(["node"], False))
    def test_continuous_retry_skips_success_and_human_and_uses_original_rows(self, _):
        batch_id = self.store.create_batch(self.accounts[:3], "existing-continuous", DEFAULT_URL, 1)
        ids = [r["id"] for r in self.store.runs(batch_id)]
        for run_id, status in zip(ids, ("success", "needs-human", "failed")):
            self.store.finish_run(run_id, {"status":status, "result":{}, "screenshot":PNG})
        self.store.finish_batch(batch_id)
        preserved = [self.store.run(run_id) for run_id in ids[:2]]
        calls = []
        def execute(email, password, out, *args, **kwargs):
            calls.append(email)
            return {"authenticated":False, "requiresHumanInteraction":True}
        service = self.service(self.store, execute=execute, delay=0)
        service.set_continuous_retry(batch_id, True)
        self.wait(service)
        self.assertEqual(calls, [self.accounts[2].email])
        self.assertEqual([self.store.run(run_id) for run_id in ids[:2]], preserved)
        self.assertEqual(self.store.run(ids[2])["status"], "needs-human")
        self.assertEqual(len(self.store.batches()), 1)

    @patch("jobs.CONTINUOUS_RETRY_DELAY", 10)
    @patch("jobs.browser_command", return_value=(["node"], False))
    def test_stop_query_cancels_continuous_retry_during_cooldown(self, _):
        batch_id = self.store.create_batch(self.accounts[:1], "cancel-continuous", DEFAULT_URL, 1)
        service = self.service(self.store, execute=lambda *args, **kwargs:{"authenticated":False}, delay=0)
        service.set_continuous_retry(batch_id, True)
        self.wait_status(self.store.runs(batch_id)[0]["id"], "failed")
        service.cancel(batch_id)
        self.wait(service)
        self.assertEqual(self.store.batch(batch_id)["status"], "cancelled")
        self.assertEqual(self.store.batch(batch_id)["continuous_retry"], 0)

    @patch("jobs.browser_command", return_value=(["node"], False))
    def test_running_concurrency_can_increase_and_decrease_without_refresh(self, _):
        releases = [threading.Event() for _ in self.accounts]
        started = [threading.Event() for _ in self.accounts]
        def execute(email, password, out, *args, **kwargs):
            index = next(i for i, account in enumerate(self.accounts) if account.email == email)
            started[index].set()
            releases[index].wait(timeout=8)
            return successful_run(out, email)
        batch_id = self.store.create_batch(self.accounts, "resize", DEFAULT_URL, 1)
        service = self.service(self.store, execute=execute, delay=0)
        with patch("jobs.fresh_pool") as refresh:
            service.start(batch_id)
            try:
                self.assertTrue(started[0].wait(timeout=3))
                self.assertFalse(started[1].is_set())
                service.set_concurrency(batch_id, 3)
                self.assertTrue(started[1].wait(timeout=3))
                self.assertTrue(started[2].wait(timeout=3))
                self.assertEqual(self.store.batch(batch_id)["effective_concurrency"], 3)
                service.set_concurrency(batch_id, 1)
                releases[0].set(); releases[1].set()
                self.wait_status(self.store.runs(batch_id)[1]["id"], "success")
                self.assertFalse(started[3].wait(timeout=0.4))
                releases[2].set()
                self.assertTrue(started[3].wait(timeout=3))
                self.assertEqual(self.store.batch(batch_id)["effective_concurrency"], 1)
                self.assertFalse(started[4].is_set())
                refresh.assert_not_called()
            finally:
                for release in releases:
                    release.set()
                self.wait(service)
        self.assertEqual({r["status"] for r in self.store.runs(batch_id)}, {"success"})
        with self.assertRaises(ValueError):
            service.set_concurrency(batch_id, 2)

    def test_missing_pool_retry_preserves_existing_result(self):
        batch_id = self.store.create_batch(self.accounts[:1], "no-pool-retry", DEFAULT_URL)
        run_id = self.store.runs(batch_id)[0]["id"]
        self.store.finish_run(run_id, {"status":"failed", "screenshot":PNG,
            "cookie_header":"fixture-cookie", "result":{"passwordStatus":403}})
        self.store.finish_batch(batch_id)
        original_run, original_batch = self.store.run(run_id), self.store.batch(batch_id)
        service = QueryService(self.store)
        with patch("jobs.fresh_pool") as refresh:
            with self.assertRaisesRegex(ValueError, "请先点击"):
                service.retry(batch_id, start=True)
            refresh.assert_not_called()
        self.assertEqual(self.store.run(run_id), original_run)
        self.assertEqual(self.store.batch(batch_id), original_batch)

    def test_cached_pool_for_other_campaign_cannot_start_query(self):
        batch_id = self.store.create_batch(self.accounts[:1], "other-campaign", DEFAULT_URL)
        service = self.service(self.store)
        service.pool_campaign = DEFAULT_URL + "?different=1"
        with patch("jobs.fresh_pool") as refresh:
            with self.assertRaisesRegex(ValueError, "请先点击"):
                service.start(batch_id)
            refresh.assert_not_called()
        self.assertEqual(self.store.batch(batch_id)["status"], "draft")
        self.assertEqual(self.store.runs(batch_id)[0]["status"], "pending")

    @patch("jobs.browser_command", return_value=(["node"], False))
    def test_only_manual_refresh_fetches_pool_and_queries_and_retries_reuse_it(self, _):
        pools = [ProxyPool([{"id":name, "username":name}], "127.0.0.1", 10801, "fixture") for name in ("old", "new")]
        selected = []
        def execute(email, password, out, *args, proxy=None, **kwargs):
            selected.append(proxy.node["id"])
            return successful_run(out, email) if proxy.node["id"] == "new" else {"authenticated":False}
        batch_id = self.store.create_batch(self.accounts[:1], "fresh", DEFAULT_URL)
        service = QueryService(self.store, execute=execute, delay=0)
        with patch("jobs.fresh_pool", side_effect=pools) as refresh:
            with self.assertRaisesRegex(ValueError, "请先点击"):
                service.start(batch_id)
            self.assertEqual(refresh.call_count, 0)
            self.assertEqual(self.store.batch(batch_id)["status"], "draft")
            service.refresh_proxies(DEFAULT_URL)
            service.proxy_thread.join(timeout=5)
            service.start(batch_id); self.wait(service)
            self.assertEqual(self.store.runs(batch_id)[0]["status"], "failed")
            service.retry(batch_id, start=True); self.wait(service)
            self.assertEqual(refresh.call_count, 1)
            self.assertEqual(selected, ["old", "old"])
            service.refresh_proxies(DEFAULT_URL)
            service.proxy_thread.join(timeout=5)
            service.retry(batch_id, start=True); self.wait(service)
        self.assertEqual(refresh.call_count, 2)
        self.assertEqual(selected, ["old", "old", "new"])
        self.assertEqual(self.store.runs(batch_id)[0]["status"], "success")

    @patch("jobs.browser_command", return_value=(["node"], False))
    def test_manual_refresh_updates_routes_without_interrupting_an_active_account(self, _):
        release = threading.Event()
        refreshes, calls = [], {}
        def factory(campaign):
            label = "old" if not refreshes else "new"
            refreshes.append(label)
            return ProxyPool([{"id":f"{label}-{i}", "username":str(i)} for i in range(3)], "127.0.0.1", 10801, label)
        def execute(email, password, out, *args, proxy=None, **kwargs):
            calls.setdefault(email, []).append(proxy.node["id"])
            if email == self.accounts[0].email:
                release.wait(timeout=8)
                self.assertEqual(proxy.upstream["password"], "old")
            elif len(calls[email]) == 1:
                return {"authenticated":False, "passwordStatus":403}
            return successful_run(out, email)
        batch_id = self.store.create_batch(self.accounts[:2], "live-fresh", DEFAULT_URL, 2)
        ids = [r["id"] for r in self.store.runs(batch_id)]
        service = self.service(self.store, execute=execute, pool_factory=factory, delay=0)
        service.start(batch_id)
        try:
            self.wait_status(ids[1], "failed")
            service.refresh_proxies(DEFAULT_URL)
            service.proxy_thread.join(timeout=5)
            service.retry(batch_id, run_id=ids[1], start=True)
            self.wait_status(ids[1], "success")
            self.assertEqual(refreshes, ["old", "new"])
            self.assertTrue(calls[self.accounts[1].email][-1].startswith("new-"))
            self.assertEqual(self.store.run(ids[0])["status"], "running")
            self.assertEqual(service.proxy_status()["status"], "ready")
        finally:
            release.set()
            self.wait(service)

    @patch("jobs.browser_command", return_value=(["node"], False))
    def test_failed_live_refresh_does_not_query_with_stale_proxies(self, _):
        release = threading.Event()
        refreshes, calls = [], []
        def factory(campaign):
            refreshes.append(1)
            if len(refreshes) > 1:
                raise ValueError("fixture refresh failed")
            return fixture_pool(campaign)
        def execute(email, password, out, *args, **kwargs):
            calls.append(email)
            if email == self.accounts[0].email:
                release.wait(timeout=8)
                return successful_run(out, email)
            return {"authenticated":False, "passwordStatus":403}
        batch_id = self.store.create_batch(self.accounts[:2], "refresh-failure", DEFAULT_URL, 2)
        ids = [r["id"] for r in self.store.runs(batch_id)]
        service = self.service(self.store, execute=execute, pool_factory=factory, delay=0)
        service.start(batch_id)
        try:
            self.wait_status(ids[1], "failed")
            original = self.store.run(ids[1])
            service.refresh_proxies(DEFAULT_URL)
            service.proxy_thread.join(timeout=5)
            with self.assertRaisesRegex(ValueError, "手动刷新"):
                service.retry(batch_id, run_id=ids[1], start=True)
            self.assertEqual(calls.count(self.accounts[1].email), 1)
            self.assertEqual(self.store.run(ids[1]), original)
            self.assertEqual(service.proxy_status()["status"], "failed")
            self.assertEqual(self.store.run(ids[0])["status"], "running")
        finally:
            release.set()
            self.wait(service)

    @patch("jobs.browser_command")
    def test_shutdown_cancels_manual_screening_without_launching_accounts(self, command):
        entered = threading.Event()
        def screening(*args, stop_event=None, **kwargs):
            entered.set()
            stop_event.wait(timeout=5)
            raise InterruptedError("cancelled")
        batch_id = self.store.create_batch(self.accounts[:2], "cancel-screen", DEFAULT_URL)
        service = QueryService(self.store)
        with patch("jobs.fresh_pool", side_effect=screening):
            service.refresh_proxies(DEFAULT_URL)
            self.assertTrue(entered.wait(timeout=2))
            service.shutdown()
        command.assert_not_called()
        self.assertEqual(self.store.batch(batch_id)["status"], "draft")
        self.assertEqual({r["status"] for r in self.store.runs(batch_id)}, {"pending"})
        self.assertEqual(service.proxy_status()["status"], "cancelled")

    @patch("jobs.browser_command", return_value=(["node"], False))
    def test_failed_account_can_retry_while_another_account_keeps_running(self, _):
        release = threading.Event()
        calls = {}
        batch_id = self.store.create_batch(self.accounts[:3], "live-retry", DEFAULT_URL, 2)
        ids = [row["id"] for row in self.store.runs(batch_id)]
        def execute(email, password, out, *args, **kwargs):
            calls[email] = calls.get(email, 0) + 1
            if email == self.accounts[0].email:
                release.wait(timeout=8)
            if email == self.accounts[1].email and calls[email] == 1:
                return {"authenticated":False, "passwordStatus":403}
            return successful_run(out, email)
        service = self.service(self.store, execute=execute, pool_factory=fixture_pool, delay=0)
        service.start(batch_id)
        try:
            self.wait_status(ids[1], "failed")
            self.wait_status(ids[2], "success")
            preserved = self.store.run(ids[2])
            original_thread = list(service.threads.values())[-1]
            self.assertEqual(service.retry(batch_id, run_id=ids[1], start=True, concurrency=20), batch_id)
            self.wait_status(ids[1], "success")
            self.assertEqual(self.store.run(ids[0])["status"], "running")
            self.assertEqual(self.store.batch(batch_id)["status"], "running")
            self.assertEqual(self.store.batch(batch_id)["concurrency"], 2)
            self.assertIs(list(service.threads.values())[-1], original_thread)
            self.assertEqual(self.store.run(ids[2]), preserved)
            self.assertEqual(calls[self.accounts[1].email], 2)
            self.assertEqual(self.store.run(ids[1])["attempt_count"], 1)
        finally:
            release.set()
            self.wait(service)
        self.assertEqual(len(self.store.batches()), 1)
        self.assertEqual([r["id"] for r in self.store.runs(batch_id)], ids)
        self.assertEqual(self.store.batch(batch_id)["status"], "complete")

    def test_live_bulk_retry_skips_success_pending_and_running_and_rejects_duplicates(self):
        batch_id = self.store.create_batch(self.accounts[:5], "live-selection", DEFAULT_URL, 2)
        ids = [row["id"] for row in self.store.runs(batch_id)]
        for run_id, status in zip(ids, ("failed", "pending", "running", "success", "partial")):
            self.store.finish_run(run_id, {"status":status, "cookie_header":"fixture-cookie", "result":{}})
        with self.store.connect() as db:
            db.execute("UPDATE batches SET status='running' WHERE id=?", (batch_id,))
        original = {run_id:self.store.run(run_id) for run_id in ids}
        service = self.service(self.store)
        service.active = {batch_id: threading.Event()}
        service.retry_queues[batch_id] = deque()
        service.retry(batch_id, scope="failed", start=True)
        self.assertEqual(list(service.retry_queues[batch_id]), [ids[0], ids[4]])
        for run_id in (ids[0], ids[4]):
            self.assertEqual(self.store.run(run_id)["status"], "pending")
            self.assertIsNone(self.store.run(run_id)["cookie_header"])
        for run_id in ids[1:4]:
            self.assertEqual(self.store.run(run_id), original[run_id])
        for options in ({"scope":"failed"}, *({"run_id":run_id} for run_id in ids)):
            with self.assertRaises(ValueError):
                service.retry(batch_id, start=True, **options)
        self.assertEqual(list(service.retry_queues[batch_id]), [ids[0], ids[4]])
        service.active[batch_id].set()
        with self.assertRaises(ValueError):
            service.retry(batch_id, start=True)

    @patch("jobs.browser_command", return_value=(["node"], False))
    def test_live_retry_takes_next_available_slot_before_original_pending_accounts(self, _):
        held, release = threading.Event(), threading.Event()
        calls = []
        batch_id = self.store.create_batch(self.accounts[:3], "priority", DEFAULT_URL, 1)
        ids = [row["id"] for row in self.store.runs(batch_id)]
        def execute(email, password, out, *args, **kwargs):
            calls.append(email)
            if email == self.accounts[0].email and calls.count(email) == 1:
                return {"authenticated":False, "passwordStatus":403}
            if email == self.accounts[1].email:
                held.set()
                release.wait(timeout=8)
            return successful_run(out, email)
        service = self.service(self.store, execute=execute, pool_factory=fixture_pool, delay=0)
        service.start(batch_id)
        try:
            self.assertTrue(held.wait(timeout=5))
            service.retry(batch_id, run_id=ids[0], start=True)
            self.assertEqual(self.store.run(ids[0])["stage"], "retry_queued")
            with self.assertRaises(ValueError):
                service.retry(batch_id, run_id=ids[0], start=True)
        finally:
            release.set()
            self.wait(service)
        self.assertEqual(calls, [self.accounts[0].email] + [self.accounts[1].email, self.accounts[0].email, self.accounts[2].email])
        self.assertEqual({r["status"] for r in self.store.runs(batch_id)}, {"success"})

    @patch("jobs.browser_command", return_value=(["node"], False))
    def test_cancellation_also_cancels_live_retry_queue(self, _):
        held = threading.Event()
        calls = []
        batch_id = self.store.create_batch(self.accounts[:3], "cancel-retry", DEFAULT_URL, 1)
        ids = [row["id"] for row in self.store.runs(batch_id)]
        def execute(email, password, out, *args, stop_event=None, **kwargs):
            calls.append(email)
            if email == self.accounts[0].email:
                return {"authenticated":False, "passwordStatus":403}
            held.set()
            stop_event.wait(timeout=8)
            return {"authenticated":False, "stopped":True}
        service = self.service(self.store, execute=execute, pool_factory=fixture_pool, delay=0)
        service.start(batch_id)
        self.assertTrue(held.wait(timeout=5))
        service.retry(batch_id, run_id=ids[0], start=True)
        service.cancel(batch_id)
        self.wait(service)
        self.assertEqual(calls, [self.accounts[0].email] + [self.accounts[1].email])
        self.assertEqual({r["status"] for r in self.store.runs(batch_id)}, {"cancelled"})

    @patch("jobs.browser_command", return_value=(["node"], False))
    def test_retry_at_batch_completion_is_not_lost_or_cleared_by_old_worker(self, _):
        finishing, release_finish = threading.Event(), threading.Event()
        retried, release_retry = threading.Event(), threading.Event()
        calls = []
        batch_id = self.store.create_batch(self.accounts[:1], "completion-race", DEFAULT_URL)
        run_id = self.store.runs(batch_id)[0]["id"]
        original_finish = self.store.finish_batch
        first_finish = [True]
        def finish(*args, **kwargs):
            if first_finish[0]:
                first_finish[0] = False
                finishing.set()
                release_finish.wait(timeout=8)
            original_finish(*args, **kwargs)
        def execute(email, password, out, *args, **kwargs):
            calls.append(email)
            if len(calls) == 1:
                return {"authenticated":False, "passwordStatus":403}
            retried.set()
            release_retry.wait(timeout=8)
            return successful_run(out, email)
        service = self.service(self.store, execute=execute, pool_factory=fixture_pool, delay=0)
        with patch.object(self.store, "finish_batch", side_effect=finish):
            service.start(batch_id)
            old_thread, old_event = list(service.threads.values())[-1], service.active[batch_id]
            self.assertTrue(finishing.wait(timeout=5))
            errors = []
            def retry():
                try: service.retry(batch_id, run_id=run_id, start=True)
                except Exception as error: errors.append(type(error).__name__)
            requester = threading.Thread(target=retry, daemon=True)
            requester.start()
            release_finish.set()
            try:
                requester.join(timeout=5)
                self.assertFalse(requester.is_alive())
                self.assertEqual(errors, [])
                self.assertTrue(retried.wait(timeout=5))
                old_thread.join(timeout=5)
                self.assertIsNotNone(service.active)
                self.assertIsNot(service.active[batch_id], old_event)
            finally:
                release_retry.set()
                self.wait(service)
        self.assertEqual(self.store.run(run_id)["status"], "success")
        self.assertEqual(len(calls), 2)

    @patch("jobs.browser_command", return_value=(["node"], False))
    def test_screenshot_success_skips_all_retries_when_selections_are_incomplete(self, _):
        for record, markets in (("missing", None), ("selected", [])):
            with self.subTest(record=record):
                calls = []
                def execute(email, password, out, *args, **kwargs):
                    calls.append(email)
                    return successful_run(out, email, record=record, markets=markets)
                batch_id = self.store.create_batch(self.accounts[:1], "screenshot", DEFAULT_URL)
                service = self.service(self.store, execute=execute, delay=0)
                service.set_continuous_retry(batch_id, True)
                self.wait(service)
                row = self.store.run(self.store.runs(batch_id)[0]["id"])
                self.assertEqual((row["status"], row["attempt_count"]), ("success", 1))
                self.assertEqual(len(calls), 1)
                self.assertEqual(row["screenshot"], PNG)

    @patch("jobs.browser_command", return_value=(["node"], False))
    def test_manual_requeue_starts_a_new_single_attempt_and_cleans_previous_result(self, _):
        calls, paths = [], []
        batch_id = self.store.create_batch(self.accounts[:1], "retry", DEFAULT_URL)
        run_id = self.store.runs(batch_id)[0]["id"]
        def execute(email, password, out, *args, proxy=None, **kwargs):
            calls.append(proxy.node["id"])
            paths.append(out)
            record = self.store.run(run_id)
            self.assertEqual(record["status"], "running")
            self.assertEqual(record["attempt_count"], 1)
            self.assertIsNone(record["screenshot"])
            if len(calls) == 1:
                return {"authenticated":False, "failureStage":"password_once", "passwordStatus":403}
            return successful_run(out, email)
        service = self.service(self.store, execute=execute, pool_factory=fixture_pool, delay=0)
        service.start(batch_id); self.wait(service)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.store.run(run_id)["status"], "failed")
        service.retry(batch_id, run_id=run_id, start=True); self.wait(service)
        self.assertEqual(len(calls), 2)

        scores = service.current_pool.reputation_status()["reputations"]
        self.assertEqual(next(r["confidence"] for r in scores if r["route"] == "node:" + calls[0]), 9)
        self.assertEqual(next(r["confidence"] for r in scores if r["route"] == "node:" + calls[1]), 10)
        record = self.store.run(run_id)
        self.assertEqual((record["status"], record["attempt_count"]), ("success", 1))
        self.assertEqual(json.loads(record["result"])["proxyNode"]["id"], calls[-1])
        self.assertEqual(len(self.store.batches()), 1)
        self.assertEqual(len(self.store.runs(batch_id)), 1)
        self.assertEqual(len(set(paths)), 2)
        self.assertTrue(all(not path.exists() for path in paths))

    @patch("jobs.browser_command", return_value=(["node"], False))
    def test_partial_screenshot_result_is_kept_without_internal_login_retries(self, _):
        calls = []
        batch_id = self.store.create_batch(self.accounts[:1], "retry", DEFAULT_URL)
        def execute(email, password, out, *args, proxy=None, **kwargs):
            calls.append(proxy.node["id"])
            if len(calls) == 1:
                summary = successful_run(out, email)
                (out / "com-result.png").unlink()
                return summary  # Keep the audited Cookie when the screenshot is missing.
            return {"authenticated":False, "failureStage":"password_once", "passwordStatus":403}
        service = self.service(self.store, execute=execute, pool_factory=fixture_pool, delay=0)
        service.start(batch_id); self.wait(service)
        record = self.store.run(self.store.runs(batch_id)[0]["id"])
        self.assertEqual(len(calls), 1)
        self.assertEqual((record["status"], record["attempt_count"]), ("partial", 1))
        self.assertNotIn("已尝试 3 次", record["error"] or "")

        self.assertIsNotNone(record["cookie_header"])
        self.assertIsNone(record["screenshot"])
        self.assertIsNotNone(record["selections"])
        self.assertEqual(json.loads(record["result"])["attempts"], 1)

    @patch("jobs.browser_command", return_value=(["node"], False))
    def test_runner_exception_finishes_after_one_attempt(self, _):
        calls = []
        def execute(email, password, out, *args, proxy=None, **kwargs):
            calls.append(proxy.node["id"])
            if len(calls) == 1:
                raise RuntimeError("private-credentials-must-not-be-persisted")
            return successful_run(out, email)
        batch_id = self.store.create_batch(self.accounts[:1], "exception", DEFAULT_URL)
        service = self.service(self.store, execute=execute, pool_factory=fixture_pool, delay=0)
        service.start(batch_id); self.wait(service)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("private-credentials", self.store.runs(batch_id)[0]["error"])
        self.assertEqual(self.store.runs(batch_id)[0]["status"], "failed")

    @patch("jobs.browser_command", return_value=(["node"], False))
    def test_single_exit_failure_finishes_without_internal_retry(self, _):
        calls = []
        def pool(_):
            return ProxyPool([{"id":str(i), "username":str(i), "screening":{"exitIP":"192.0.2.1"}} for i in range(2)], "127.0.0.1", 10801, "fixture")
        def execute(*args, proxy=None, **kwargs):
            calls.append(proxy.node["id"])
            return {"authenticated":False, "passwordStatus":403}
        batch_id = self.store.create_batch(self.accounts[:1], "same-exit", DEFAULT_URL)
        service = self.service(self.store, execute=execute, pool_factory=pool, delay=0)
        service.start(batch_id); self.wait(service)
        record = self.store.run(self.store.runs(batch_id)[0]["id"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(record["attempt_count"], 1)
        self.assertIn("403", record["error"])
        self.assertNotIn("retryStopReason", json.loads(record["result"]))

    @patch("jobs.browser_command", return_value=(["node"], False))
    def test_identity_requirement_completes_but_access_pause_fails_once(self, _):
        for flags, expected, attempts in (({"personaVerificationRequired":True}, "success", 1),
                                           ({"requiresHumanInteraction":True, "browsingActivityPaused":True}, "failed", 1)):
            with self.subTest(flags=flags):
                calls = []
                def execute(*args, **kwargs):
                    calls.append(1)
                    return {"authenticated":False, **flags}
                batch_id = self.store.create_batch(self.accounts[:1], "verification", DEFAULT_URL)
                service = self.service(self.store, execute=execute, delay=0)
                service.start(batch_id); self.wait(service)
                self.assertEqual(self.store.runs(batch_id)[0]["status"], expected)
                self.assertEqual(len(calls), attempts)

    @patch("jobs.browser_command", return_value=(["node"], False))
    def test_automatic_retry_does_not_continue_after_human_verification_or_cancellation(self, _):
        for expected in ("needs-human", "cancelled"):
            with self.subTest(status=expected):
                calls = []
                def execute(*args, stop_event=None, **kwargs):
                    calls.append(1)
                    if expected == "cancelled":
                        stop_event.set()
                        return {"authenticated":False}
                    return {"authenticated":False, "requiresHumanInteraction":True}
                batch_id = self.store.create_batch(self.accounts[:1], expected, DEFAULT_URL)
                service = self.service(self.store, execute=execute, pool_factory=fixture_pool, delay=0)
                service.start(batch_id); self.wait(service)
                self.assertEqual(len(calls), 1)
                self.assertEqual(self.store.runs(batch_id)[0]["status"], expected)

    @patch("jobs.browser_command", return_value=(["node"], False))
    def test_concurrent_retries_keep_exclusive_proxies_and_finish_without_deadlock(self, _):
        pool = fixture_pool(None)
        barrier, lock = threading.Barrier(3), threading.Lock()
        calls, active = {}, set()
        def execute(email, password, out, *args, proxy=None, **kwargs):
            node_id = proxy.node["id"]
            with lock:
                self.assertNotIn(node_id, active)
                active.add(node_id)
                calls.setdefault(email, []).append(node_id)
                first = len(calls[email]) == 1
            if first:
                barrier.wait(timeout=5)
            with lock:
                active.remove(node_id)
            return {"authenticated":False, "passwordStatus":403}
        batch_id = self.store.create_batch(self.accounts[:3], "concurrent-retry", DEFAULT_URL, 3)
        service = self.service(self.store, execute=execute, pool_factory=lambda _:pool, delay=0)
        service.start(batch_id); self.wait(service)
        self.assertEqual(len(calls), 3)
        for selected in calls.values():
            self.assertEqual(len(selected), 1)
            self.assertTrue(all(a != b for a, b in zip(selected, selected[1:])))
        self.assertEqual(pool.leased, set())
        self.assertEqual({row["attempt_count"] for row in self.store.runs(batch_id)}, {1})

    @patch("jobs.browser_command", return_value=(["node"], False))
    def test_three_concurrent_accounts_are_isolated_and_temporary_files_removed(self, _):
        barrier = threading.Barrier(3)
        lock = threading.Lock()
        active, peak, paths, leases = [0], [0], [], set()
        def execute(email,password,out,*args,proxy=None,on_progress=None,**kwargs):
            with lock:
                self.assertNotIn(proxy.node["id"], leases)
                leases.add(proxy.node["id"])
                active[0] += 1; peak[0] = max(peak[0], active[0]); paths.append(out)
            barrier.wait(timeout=5)
            self.assertEqual(password, "password-" + email[4])
            on_progress({"stage":"readonly_com_fan", "emailStatus":200})
            summary = successful_run(out, email)
            with lock:
                active[0] -= 1; leases.remove(proxy.node["id"])
            return summary
        service = self.service(self.store, execute=execute, pool_factory=fixture_pool, delay=0)
        batch_id = self.store.create_batch(self.accounts, "test", DEFAULT_URL, 3)
        service.start(batch_id, concurrency=30)
        with self.assertRaises(ValueError): service.start(batch_id)
        self.wait(service)
        self.assertEqual(peak[0], 3)
        self.assertEqual(self.store.batch(batch_id)["concurrency"], 30)
        self.assertEqual(self.store.batch(batch_id)["effective_concurrency"], 3)
        self.assertTrue(all(not p.exists() for p in paths))
        for row in self.store.runs(batch_id):
            detail = self.store.run(row["id"])
            self.assertEqual(row["status"], "success")
            self.assertEqual(detail["cookie_header"], "SID=" + row["email"].split("@")[0])

    @patch("jobs.browser_command", return_value=(["node"], False))
    def test_more_than_six_accounts_can_run_together_when_nodes_are_available(self, _):
        barrier = threading.Barrier(8)
        def execute(email, password, out, *args, **kwargs):
            barrier.wait(timeout=30)
            return successful_run(out, email)
        def eight_nodes(_):
            return ProxyPool([{"id":str(i), "name":f"node-{i}", "username":str(i)} for i in range(8)], "127.0.0.1", 10801, "test")
        accounts = [Account(i + 1, f"parallel{i}@example.invalid", "test") for i in range(8)]
        service = self.service(self.store, execute=execute, pool_factory=eight_nodes, delay=0)
        self.addCleanup(service.shutdown)
        batch_id = self.store.create_batch(accounts, "eight", DEFAULT_URL, 8)
        service.start(batch_id)
        service.threads[batch_id].join(timeout=60)
        self.assertFalse(service.threads[batch_id].is_alive())
        self.assertEqual(self.store.batch(batch_id)["effective_concurrency"], 8)
        self.assertEqual({row["status"] for row in self.store.runs(batch_id)}, {"success"})

    @patch("jobs.browser_command", return_value=(["node"], False))
    def test_cancellation_stops_active_and_pending_accounts(self, _):
        started = threading.Event()
        calls = []
        def execute(email,password,out,*args,stop_event=None,**kwargs):
            calls.append(email); started.set()
            stop_event.wait(timeout=5)
            return {"authenticated":False, "stopped":True}
        service = self.service(self.store, execute=execute, pool_factory=fixture_pool, delay=1)
        batch_id = self.store.create_batch(self.accounts, "test", DEFAULT_URL, 1)
        service.start(batch_id); self.assertTrue(started.wait(timeout=3)); service.cancel(batch_id); self.wait(service)
        self.assertEqual(len(calls), 1)
        self.assertEqual({r["status"] for r in self.store.runs(batch_id)}, {"cancelled"})

    def test_restart_marks_interrupted_jobs_and_leaves_drafts(self):
        running = self.store.create_batch(self.accounts, "old", DEFAULT_URL)
        draft = self.store.create_batch(self.accounts, "new", DEFAULT_URL)
        with self.store.connect() as db:
            db.execute("UPDATE batches SET status='running' WHERE id=?", (running,))
        self.store.recover()
        self.assertEqual({r["status"] for r in self.store.runs(running)}, {"interrupted"})
        self.assertEqual({r["status"] for r in self.store.runs(draft)}, {"pending"})

    @patch("jobs.browser_command", return_value=(["node"], False))
    def test_shutdown_does_not_leave_waiting_accounts_in_a_closed_batch(self, _):
        started = threading.Event()
        def execute(*args, stop_event=None, **kwargs):
            started.set()
            stop_event.wait(timeout=5)
            return {"authenticated": False, "stopped": True}
        service = self.service(self.store, execute=execute, pool_factory=fixture_pool, delay=1)
        batch_id = self.store.create_batch(self.accounts, "shutdown", DEFAULT_URL, 1)
        service.start(batch_id)
        self.assertTrue(started.wait(timeout=3))
        service.shutdown()
        self.assertEqual({r["status"] for r in self.store.runs(batch_id)}, {"cancelled"})
        self.assertEqual(self.store.batch(batch_id)["status"], "cancelled")

    @patch("jobs.browser_command")
    def test_proxy_failure_never_launches_direct_browser(self, command):
        def broken(_): raise ValueError("代理不可用")
        service = self.service(self.store, pool_factory=broken)
        batch_id = self.store.create_batch(self.accounts, "test", DEFAULT_URL)
        with self.assertRaisesRegex(ValueError, "请先点击"):
            service.start(batch_id)
        command.assert_not_called()
        self.assertEqual(self.store.batch(batch_id)["status"], "draft")
        self.assertEqual({r["status"] for r in self.store.runs(batch_id)}, {"pending"})

    @patch("jobs.browser_command", return_value=(["node"], False))
    def test_failed_retry_overwrites_selected_rows_and_preserves_successes(self, _):
        statuses = ["success", "failed", "partial", "needs-human", "interrupted", "cancelled"]
        original_id = self.store.create_batch(self.accounts, "original", DEFAULT_URL, 3)
        for row, status in zip(self.store.runs(original_id), statuses):
            self.store.finish_run(row["id"], {"status":status, "screenshot":PNG, "cookie_header":"old-cookie", "result":{}})
        self.store.finish_batch(original_id)
        original_rows = self.store.runs(original_id)
        untouched = [self.store.run(original_rows[index]["id"]) for index in (0,3,5)]
        calls = []
        def execute(email,password,out,*args,**kwargs):
            calls.append(email)
            return successful_run(out, email)
        service = self.service(self.store, execute=execute, pool_factory=fixture_pool, delay=0)
        returned_id = service.retry(original_id, scope="failed", start=True, concurrency=8)
        self.wait(service)
        self.assertEqual(returned_id, original_id)
        self.assertEqual(self.store.batch(original_id)["concurrency"], 8)
        self.assertEqual(set(calls), {self.accounts[i].email for i in (1,2,4)})
        self.assertEqual(len(self.store.batches()), 1)
        final_rows = self.store.runs(original_id)
        self.assertEqual([row["id"] for row in final_rows], [row["id"] for row in original_rows])
        self.assertEqual([row["status"] for row in final_rows], ["success", "success", "success", "needs-human", "success", "cancelled"])
        for record in untouched:
            self.assertEqual(self.store.run(record["id"]), record)
        for row in (final_rows[i] for i in (1,2,4)):
            self.assertEqual(self.store.run(row["id"])["cookie_header"], "SID=" + row["email"].split("@")[0])

    @patch("jobs.browser_command", return_value=(["node"], False))
    def test_retry_failing_again_does_not_restore_old_cookie_or_screenshot(self, _):
        batch_id = self.store.create_batch(self.accounts[:1], "failed", DEFAULT_URL)
        run_id = self.store.runs(batch_id)[0]["id"]
        self.store.finish_run(run_id, {"status":"partial", "cookie_header":"old-cookie", "screenshot":PNG,
                                      "selections":[{"name":"old-show"}], "error":"old-error", "result":{"old":True}})
        self.store.finish_batch(batch_id)
        service = self.service(self.store, pool_factory=fixture_pool, delay=0,
                               execute=lambda *args, **kwargs: {"authenticated":False, "emailStatus":200,
                                                               "passwordStatus":403, "failureStage":"password_once"})
        self.assertEqual(service.retry(batch_id, scope="failed", start=True), batch_id)
        self.wait(service)
        record = self.store.run(run_id)
        self.assertEqual(record["status"], "failed")
        self.assertIn("403", record["error"])
        self.assertIsNone(record["cookie_header"])
        self.assertIsNone(record["screenshot"])
        self.assertIsNone(record["selections"])
        self.assertNotIn("old", json.loads(record["result"]))
        self.assertEqual(len(self.store.runs(batch_id)), 1)

    def test_database_failure_reports_sqlite_code_without_credentials(self):
        batch_id = self.store.create_batch(self.accounts[:1], "db-failure", DEFAULT_URL)
        service = self.service(self.store)
        error = sqlite3.OperationalError("fixture-secret-password and cookie-value")
        error.sqlite_errorname = "SQLITE_BUSY"
        with patch("jobs.browser_command", side_effect=error), self.assertLogs("jobs", level="ERROR") as captured:
            service.start(batch_id)
            self.wait(service)
        batch = self.store.batch(batch_id)
        self.assertEqual(batch["status"], "failed")
        self.assertIn("SQLITE_BUSY", batch["error"])
        self.assertIn("数据库写入繁忙", batch["error"])
        self.assertEqual(self.store.runs(batch_id)[0]["stage"], "service")
        report = batch["error"] + " ".join(captured.output)
        self.assertNotIn("fixture-secret-password", report)
        self.assertNotIn("cookie-value", report)
        self.assertIn("call sites", report)

    @patch("jobs.browser_command", return_value=(["node"], False))
    def test_worker_database_failure_is_not_retried_as_browser_failure(self, _):
        batch_id = self.store.create_batch(self.accounts[:1], "worker-db-failure", DEFAULT_URL)
        service = self.service(self.store)
        error = sqlite3.OperationalError("fixture-sensitive-sql")
        error.sqlite_errorname = "SQLITE_BUSY"
        with patch.object(self.store, "begin_attempt", side_effect=error) as begin, self.assertLogs("jobs", level="ERROR"):
            service.start(batch_id)
            self.wait(service)
        self.assertEqual(begin.call_count, 1)
        self.assertIn("SQLITE_BUSY", self.store.batch(batch_id)["error"])
        self.assertEqual(self.store.runs(batch_id)[0]["stage"], "service")

    def test_human_status_is_preserved_by_ordinary_retry_paths(self):
        batch_id = self.store.create_batch(self.accounts[:1], "human-skip", DEFAULT_URL)
        run_id = self.store.runs(batch_id)[0]["id"]
        self.store.finish_run(run_id, {"status":"needs-human", "screenshot":PNG, "result":{}})
        self.store.finish_batch(batch_id)
        original = self.store.run(run_id)
        service = self.service(self.store)
        for live in (False, True):
            if live:
                service.active = {batch_id: threading.Event()}
                service.retry_queues[batch_id] = deque()
                with self.store.connect() as db:
                    db.execute("UPDATE batches SET status='running' WHERE id=?", (batch_id,))
            for options in ({"scope":"failed"}, {"scope":"unfinished"}, {"run_id":run_id}):
                with self.assertRaises(ValueError):
                    service.retry(batch_id, start=live, **options)
                self.assertEqual(self.store.run(run_id), original)
        service.active = {}

    def test_explicit_human_retry_resets_only_selected_human_rows(self):
        for live in (False, True):
            for single in (False, True):
                with self.subTest(live=live, single=single):
                    batch_id = self.store.create_batch(self.accounts[:5], "manual-human", DEFAULT_URL)
                    ids = [r["id"] for r in self.store.runs(batch_id)]
                    for run_id, status in zip(ids, ("needs-human", "needs-human", "failed", "success", "running")):
                        self.store.finish_run(run_id, {"status":status, "screenshot":PNG, "error":"old", "result":{}})
                    self.store.finish_batch(batch_id)
                    service = self.service(self.store)
                    if live:
                        service.active[batch_id] = threading.Event()
                        service.retry_queues[batch_id] = deque()
                        with self.store.connect(write=True) as db:
                            db.execute("UPDATE batches SET status='running' WHERE id=?", (batch_id,))
                    untouched = {rid:self.store.run(rid) for rid in (ids[1:] if single else ids[2:])}
                    service.retry(batch_id, run_id=ids[0] if single else None, scope="human", start=live)
                    targets = ids[:1] if single else ids[:2]
                    for rid in targets:
                        row = self.store.run(rid)
                        self.assertEqual(row["status"], "pending")
                        self.assertEqual(row["attempt_count"], 0)
                        self.assertIsNone(row["error"])
                        self.assertIsNone(row["screenshot"])
                    for rid, original in untouched.items():
                        self.assertEqual(self.store.run(rid), original)
                    if live:
                        self.assertEqual(set(service.retry_queues[batch_id]), set(targets))

    def test_single_account_retry_only_resets_the_requested_row(self):
        batch_id = self.store.create_batch(self.accounts[:2], "single", DEFAULT_URL)
        rows = self.store.runs(batch_id)
        for row in rows:
            self.store.finish_run(row["id"], {"status":"failed", "screenshot":PNG, "error":"old", "result":{}})
        self.store.finish_batch(batch_id)
        untouched = self.store.run(rows[1]["id"])
        service = self.service(self.store)
        self.assertEqual(service.retry(batch_id, run_id=rows[0]["id"]), batch_id)
        self.assertEqual(self.store.run(rows[0]["id"])["status"], "pending")
        self.assertEqual(self.store.run(rows[1]["id"]), untouched)

    def test_invalid_retry_target_leaves_the_original_batch_unchanged(self):
        batch_id = self.store.create_batch(self.accounts[:1], "original", DEFAULT_URL)
        run_id = self.store.runs(batch_id)[0]["id"]
        self.store.finish_run(run_id, {"status":"success", "cookie_header":"original", "result":{}})
        self.store.finish_batch(batch_id)
        original_batch = self.store.batch(batch_id)
        original_record = self.store.run(run_id)
        service = self.service(self.store)
        for options in ({"scope":"failed"}, {"run_id":9999}):
            with self.assertRaises(ValueError):
                service.retry(batch_id, concurrency=50, **options)
            self.assertEqual(self.store.batch(batch_id), original_batch)
            self.assertEqual(self.store.run(run_id), original_record)

    @patch("jobs.CONTINUOUS_RETRY_DELAY", 0.01)
    @patch("jobs.browser_command", return_value=(["node"], False))
    def test_terminal_account_issues_stop_continuous_retry_without_penalizing_proxy(self, _):
        evidence = [
            {"emailStatus":404,"emailBusinessError":{"message":"The user is not found"}},
            {"passwordResetRequired":True,"requiresHumanInteraction":True},
            {"passwordFormBeforeSubmit":{"valueLength":7,"submitEnabled":False}},
        ]
        calls = []
        accounts = self.accounts[:3]
        by_email = {account.email:item for account,item in zip(accounts,evidence)}
        def execute(email,password,out,*args,**kwargs):
            calls.append(email)
            Path(out).mkdir(parents=True,exist_ok=True)
            (Path(out)/"failure.png").write_bytes(PNG)
            return {"authenticated":False,**by_email[email]}
        batch_id = self.store.create_batch(accounts,"terminal-issues",DEFAULT_URL,1)
        service = self.service(self.store,execute=execute,delay=0)
        self.addCleanup(service.shutdown)
        service.set_continuous_retry(batch_id,True)
        self.wait(service)
        self.assertEqual(calls,[a.email for a in accounts])
        self.assertEqual({r["status"] for r in self.store.runs(batch_id)},{"success"})
        self.assertEqual(self.store.batch(batch_id)["continuous_retry"],0)
        self.assertEqual([r["verification_requirement"] for r in self.store.runs(batch_id)],
                         ["账号不存在","需要重置密码","密码仅7位，登录按钮不可用，请核对密码"])
        self.assertTrue(all(n["confidence"]==10 for n in service.current_pool.reputation_status(include_nodes=True)["nodes"]))



if __name__ == "__main__":
    unittest.main()
