"""Serial background Excel exports; private files expire after one hour."""
import logging
from pathlib import Path
import secrets
import shutil
import tempfile
import threading
import time

from exports import ExportSizeError, results_workbook

logger = logging.getLogger(__name__)
EXPORT_TTL = 3600


class ExportService:
    def __init__(self, store):
        self.store = store
        self.condition = threading.Condition()
        self.jobs = {}
        self.thread = None
        self.stopping = False

    def start(self, batch_id):
        self.store.batch(batch_id)
        with self.condition:
            if self.stopping:
                raise ValueError("服务正在停止。")
            for job in self.jobs.values():
                if job["batch_id"] == batch_id and job["status"] in ("queued", "building"):
                    return self._public(job)
            job_id = secrets.token_urlsafe(24)
            self.jobs[job_id] = dict(id=job_id, batch_id=batch_id, status="queued", completed=0,
                                     total=0, parts=0, error=None, directory=None, expires=None)
            if not self.thread or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._work, daemon=True, name="excel-export")
                self.thread.start()
            self.condition.notify_all()
            return self._public(self.jobs[job_id])

    @staticmethod
    def _public(job):
        return {key: job[key] for key in ("id", "batch_id", "status", "completed", "total", "parts", "error", "filename", "bytes") if key in job}

    def status(self, job_id):
        with self.condition:
            job = self.jobs.get(job_id)
            if not job or job["expires"] and time.monotonic() >= job["expires"]:
                raise LookupError("导出文件已过期，请重新导出。")
            return self._public(job)

    def download(self, job_id):
        with self.condition:
            info = self.status(job_id)
            if info["status"] != "ready":
                raise ValueError("导出文件尚未准备好。")
            job = self.jobs[job_id]
            # Allow time for a download that starts near expiry.
            job["expires"] = time.monotonic() + EXPORT_TTL
            return Path(job["directory"]) / job["filename"]

    def _update(self, job, **values):
        with self.condition:
            if self.stopping:
                raise InterruptedError()
            job.update(values)

    def _build(self, job):
        rows = self.store.runs(job["batch_id"], include_credentials=True)
        with self.condition:
            if self.stopping:
                raise InterruptedError()
            directory = Path(tempfile.mkdtemp(prefix="tm-export-"))
            job.update(directory=str(directory), total=len(rows), parts=1)
        result = directory / f"query-{job['batch_id']}.xlsx"
        workbook = results_workbook(self.store, job["batch_id"], rows=rows, destination=result,
                                    on_progress=lambda count: self._update(job, completed=count))
        workbook.close()
        self._update(job, status="ready", filename=result.name, bytes=result.stat().st_size,
                     expires=time.monotonic() + EXPORT_TTL)

    def _work(self):
        try:
            while True:
                with self.condition:
                    for job_id, job in list(self.jobs.items()):
                        if job["expires"] and time.monotonic() >= job["expires"]:
                            if job["directory"]:
                                shutil.rmtree(job["directory"], ignore_errors=True)
                            del self.jobs[job_id]
                    if self.stopping:
                        return
                    job = next((job for job in self.jobs.values() if job["status"] == "queued"), None)
                    if job is None:
                        self.condition.wait(timeout=60)
                        continue
                    job["status"] = "building"
                try:
                    self._build(job)
                except Exception as error:
                    # Database/file errors may contain private values or paths.
                    logger.error("Excel export failed (%s)", type(error).__name__)
                    with self.condition:
                        if job["directory"]:
                            shutil.rmtree(job["directory"], ignore_errors=True)
                        job.update(status="failed", error=str(error) if isinstance(error, ExportSizeError) else f"导出失败（{type(error).__name__}），请重试。",
                                   expires=time.monotonic() + EXPORT_TTL)
        finally:
            with self.condition:
                for job in self.jobs.values():
                    if job["directory"]:
                        shutil.rmtree(job["directory"], ignore_errors=True)
                self.jobs.clear()

    def shutdown(self):
        with self.condition:
            self.stopping = True
            self.condition.notify_all()
        if self.thread:
            self.thread.join(timeout=15)
