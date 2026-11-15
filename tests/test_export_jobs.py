import io
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

from accounts import Account
from export_jobs import ExportService
from helpers import PNG
from runner import DEFAULT_URL
from store import Store
from web import create_app


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Store(Path(self.temporary.name) / "test.sqlite3")
        self.service = ExportService(self.store)
        self.addCleanup(self.service.shutdown)

    def batch(self, count=1):
        batch_id = self.store.create_batch([Account(i + 1, f"test{i}@example.invalid", f"=password-{i}")
                                           for i in range(count)], "export", DEFAULT_URL)
        for row in self.store.runs(batch_id):
            self.store.finish_run(row["id"], {"status":"success", "screenshot":PNG,
                "screenshot_kind":"full-page", "cookie_header":"SID=" + "x" * 33000, "result":{}})
        return batch_id

    def wait(self, job_id):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            job = self.service.status(job_id)
            if job["status"] in ("ready", "failed"):
                return job
            time.sleep(0.01)
        self.fail("Export did not finish")

    def test_large_export_is_one_excel_with_all_credentials_and_original_images(self):
        from openpyxl import load_workbook
        batch_id = self.batch(105)
        job = self.wait(self.service.start(batch_id)["id"])
        self.assertEqual((job["status"], job["parts"], job["completed"]), ("ready", 1, 105))
        path = self.service.download(job["id"])
        self.assertEqual(path.suffix, ".xlsx")
        with zipfile.ZipFile(path) as workbook:
            media = [name for name in workbook.namelist() if name.startswith("xl/media/")]
            self.assertEqual(len(media), 105)
            for image in media:
                self.assertEqual(workbook.getinfo(image).compress_type, zipfile.ZIP_STORED)
                self.assertEqual(workbook.read(image), PNG)
        book = load_workbook(path)
        try:
            rows = list(book.active.iter_rows(min_row=2))
            self.assertEqual([row[0].value for row in rows], [f"test{i}@example.invalid" for i in range(105)])
            self.assertEqual(len(book.active._images), 105)
            for row in rows:
                self.assertTrue(row[1].value.startswith("=password-"))
                self.assertEqual(row[1].data_type, "s")
                self.assertEqual(row[8].value + row[9].value, "SID=" + "x" * 33000)
        finally:
            book.close()

    def test_browser_ansi_and_forbidden_xml_characters_do_not_break_export(self):
        from openpyxl import load_workbook
        batch_id = self.batch()
        run_id = self.store.runs(batch_id)[0]["id"]
        error = "locator.click: \x1b[2mTimeout\x1b[22m\nNUL=\x00 end=\uffff"
        self.store.finish_run(run_id, {"status":"failed", "error":error, "result":{}})
        with self.store.connect(write=True) as db:
            db.execute("UPDATE runs SET password=?,cookie_header=? WHERE id=?", (" space\t=line\n", "SID=one\x0bvalue", run_id))
        job = self.wait(self.service.start(batch_id)["id"])
        self.assertEqual(job["status"], "ready")
        book = load_workbook(self.service.download(job["id"]))
        try:
            sheet = book.active
            self.assertEqual(sheet["G2"].value, "locator.click: Timeout\nNUL=\\u0000 end=\\uffff")
            self.assertEqual(sheet["B2"].value, " space\t=line\n")
            self.assertEqual(sheet["I2"].value, "SID=one\\u000bvalue")
            self.assertEqual(self.store.run(run_id)["error"], error)
        finally:
            book.close()

    def test_excel_compresses_large_image_and_keeps_database_original(self):
        from PIL import Image
        from openpyxl import load_workbook
        batch_id = self.batch()
        run_id = self.store.runs(batch_id)[0]["id"]
        original = io.BytesIO()
        with Image.effect_noise((1600, 2400), 35).convert("RGB") as image:
            image.save(original, format="PNG")
        raw = original.getvalue()
        self.store.finish_run(run_id, {"status":"success", "screenshot":raw, "screenshot_kind":"full-page", "result":{}})
        job = self.wait(self.service.start(batch_id)["id"])
        self.assertEqual(job["status"], "ready")
        path = self.service.download(job["id"])
        with zipfile.ZipFile(path) as workbook:
            media = [name for name in workbook.namelist() if name.startswith("xl/media/")]
            self.assertEqual(len(media), 1)
            compressed = workbook.read(media[0])
            self.assertLess(len(compressed), len(raw) * 0.5)
            with Image.open(io.BytesIO(compressed)) as image:
                self.assertEqual(image.format, "JPEG")
                self.assertEqual(image.size, (1440, 2160))
        book = load_workbook(path)
        try:
            self.assertEqual(len(book.active._images), 1)
        finally:
            book.close()
        self.assertEqual(self.store.run(run_id)["screenshot"], raw)

    def test_size_budget_preserves_every_account_and_whole_page(self):
        from PIL import Image
        from exports import results_workbook
        batch_id = self.batch(12)
        buffer = io.BytesIO()
        with Image.effect_noise((800, 2200), 35).convert("RGB") as image:
            image.save(buffer, format="PNG")
        with self.store.connect(write=True) as db:
            db.execute("UPDATE runs SET screenshot=? WHERE batch_id=?", (buffer.getvalue(), batch_id))
        limit = 200_000
        workbook = results_workbook(self.store, batch_id, max_bytes=limit)
        try:
            self.assertLess(workbook.seek(0, 2), limit)
            with zipfile.ZipFile(workbook) as archive:
                media = [name for name in archive.namelist() if name.startswith("xl/media/")]
                self.assertEqual(len(media), 12)
                xml = archive.read("xl/worksheets/sheet1.xml")
                for index in range(12):
                    self.assertIn(f"test{index}@example.invalid".encode(), xml)
                for name in media:
                    with Image.open(io.BytesIO(archive.read(name))) as image:
                        self.assertAlmostEqual(image.width / image.height, 800 / 2200, delta=0.01)
        finally:
            workbook.close()

    def test_size_budget_never_returns_oversized_text_only_excel(self):
        from exports import ExportSizeError, results_workbook
        batch_id = self.batch()
        with self.store.connect(write=True) as db:
            db.execute("UPDATE runs SET screenshot=NULL WHERE batch_id=?", (batch_id,))
        with self.assertRaises(ExportSizeError):
            results_workbook(self.store, batch_id, max_bytes=1000)

    def test_duplicate_clicks_share_work_and_other_jobs_queue(self):
        entered, release = threading.Event(), threading.Event()
        original = self.service._build
        def build(job):
            entered.set()
            release.wait(5)
            original(job)
        with patch.object(self.service, "_build", side_effect=build):
            first_batch, second_batch = self.batch(), self.batch()
            first = self.service.start(first_batch)
            self.assertTrue(entered.wait(5))
            try:
                self.assertEqual(self.service.start(first_batch)["id"], first["id"])
                second = self.service.start(second_batch)
                self.assertEqual(second["status"], "queued")
                with self.assertRaises(ValueError):
                    self.service.download(first["id"])
            finally:
                release.set()
            self.assertEqual(self.wait(first["id"])["status"], "ready")
            self.assertEqual(self.wait(second["id"])["status"], "ready")

    def test_failed_export_cleans_files_and_next_job_can_run(self):
        with patch("export_jobs.results_workbook", side_effect=OSError("private-secret")):
            with self.assertLogs("export_jobs", level="ERROR") as logs:
                job = self.wait(self.service.start(self.batch())["id"])
        self.assertEqual(job["status"], "failed")
        self.assertNotIn("private-secret", job["error"] + str(logs.output))
        self.assertFalse(Path(self.service.jobs[job["id"]]["directory"]).exists())
        self.assertEqual(self.wait(self.service.start(self.batch())["id"])["status"], "ready")

    def test_download_supports_ranges_and_expiry(self):
        import re
        app = create_app(self.store.path)
        service = app.extensions["exports"]
        self.addCleanup(service.shutdown)
        client = app.test_client()
        token = re.search(rb'name="app-token" content="([^"]+)"', client.get("/").data).group(1).decode()
        batch_id = self.batch()
        endpoint = f"/api/batches/{batch_id}/exports"
        self.assertEqual(client.post(endpoint).status_code, 403)
        response = client.post(endpoint, headers={"X-App-Token":token})
        self.assertEqual(response.status_code, 202)
        self.service.shutdown()
        self.service = service
        job = self.wait(response.json["id"])
        self.assertEqual(job["status"], "ready")
        url = f"/api/exports/{job['id']}/download"
        response = client.get(url, headers={"Range":"bytes=0-31"})
        try:
            self.assertEqual(response.status_code, 206)
            self.assertEqual(len(response.data), 32)
            self.assertEqual(response.headers["Content-Length"], "32")
        finally:
            response.close()
        path = service.download(job["id"])
        service.jobs[job["id"]]["expires"] = time.monotonic() - 1
        self.assertEqual(client.get(url).status_code, 404)
        with service.condition:
            service.condition.notify_all()
        deadline = time.monotonic() + 2
        while path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(path.exists())
