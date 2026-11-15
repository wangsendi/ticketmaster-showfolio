"""Local account query workbench."""
import argparse
import io
import json
import os
from pathlib import Path
import secrets
import signal
from urllib.parse import urlsplit

from flask import Flask, abort, jsonify, render_template, request, send_file
from werkzeug.exceptions import HTTPException

from accounts import parse_import
from exports import result_bundle, results_csv, results_workbook
from export_jobs import ExportService
from jobs import MAX_ATTEMPTS, QueryService
from runner import DEFAULT_URL, ROOT
from store import Store


def validate_campaign(value):
    url = urlsplit(value)
    if (url.scheme != "https" or url.netloc != "signup.ticketmaster.com" or url.query or url.fragment
            or url.path in ("", "/")):
        raise ValueError("活动地址必须是 https://signup.ticketmaster.com/ 下的活动页面。")
    return value


def positive_count(value, label, optional=False):
    if optional and (value is None or isinstance(value, str) and not value.strip()):
        return None
    try:
        if type(value) not in (str, int):
            raise ValueError
        number = int(value)
        if number < 1:
            raise ValueError
        return number
    except (ValueError, TypeError):
        raise ValueError(f"{label}必须是正整数。") from None


def create_app(database=None, service_options=None, recover=False):
    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=51 * 1024 * 1024)
    store = Store(database or ROOT / "data/ticketmaster.sqlite3")
    if recover:
        store.recover()
    service = QueryService(store, **(service_options or {}))
    exporter = ExportService(store)
    app.extensions.update(store=store, queries=service, exports=exporter)
    token = secrets.token_urlsafe(32)

    @app.before_request
    def protect_local_writes():
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            if not secrets.compare_digest(request.headers.get("X-App-Token", ""), token):
                abort(403, "请刷新网页后重试。")

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        return response

    @app.errorhandler(ValueError)
    def invalid(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(LookupError)
    def missing(error):
        return jsonify(error=str(error)), 404

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error="文件过大，Excel 最多 50 MB，CSV/TSV/TXT 最多 5 MB。" if error.code == 413 else error.description), error.code

    @app.get("/")
    def index():
        return render_template("index.html", token=token, campaign=DEFAULT_URL, max_attempts=MAX_ATTEMPTS)

    @app.get("/api/batches")
    def batches():
        return jsonify(batches=store.batches())

    @app.get("/api/proxies")
    def proxy_status():
        return jsonify(**service.proxy_status(), supports_background_exports=True)

    @app.post("/api/proxies/refresh")
    @app.post("/api/proxies/filter")
    def refresh_proxies():
        value = request.get_json(silent=True) or {}
        if not isinstance(value, dict) or not isinstance(value.get("campaign", DEFAULT_URL), str):
            raise ValueError("代理刷新参数格式无效。")
        service.refresh_proxies(validate_campaign(value.get("campaign", DEFAULT_URL).strip()), mode="api" if request.path.endswith("/filter") else value.get("mode", "verify"))
        return jsonify(ok=True), 202

    @app.post("/api/import")
    def upload():
        file = request.files.get("file")
        if file is None or not file.filename:
            raise ValueError("请先选择账号表格。")
        concurrency = positive_count(request.form.get("concurrency", "3"), "并发数")
        max_accounts = positive_count(request.form.get("max_accounts"), "账号数量", optional=True)
        campaign = validate_campaign(request.form.get("campaign", DEFAULT_URL).strip())
        imported = parse_import(file.read(), file.filename, max_accounts=max_accounts)
        if not imported.accounts:
            return jsonify(id=None, count=0, skipped=imported.skipped_count, deferred=imported.deferred_count)
        batch_id = store.create_batch(imported.accounts, Path(file.filename).name[:160], campaign, concurrency)
        return jsonify(id=batch_id, count=len(imported.accounts), skipped=imported.skipped_count, deferred=imported.deferred_count), 201

    @app.get("/api/batches/<int:batch_id>")
    def batch(batch_id):
        metadata = store.batch(batch_id)
        metadata.pop("source", None)
        return jsonify(batch=metadata, runs=store.runs(batch_id))

    @app.post("/api/batches/<int:batch_id>/start")
    def start(batch_id):
        value = request.get_json(silent=True) or {}
        if not isinstance(value, dict):
            raise ValueError("查询参数格式无效。")
        service.start(batch_id, concurrency=positive_count(value.get("concurrency"), "并发数", optional=True))
        return jsonify(ok=True), 202

    @app.post("/api/batches/<int:batch_id>/continuous-retry")
    def continuous_retry(batch_id):
        value = request.get_json(silent=True)
        if not isinstance(value, dict) or type(value.get("enabled")) is not bool:
            raise ValueError("持续重试开关必须是布尔值。")
        service.set_continuous_retry(batch_id, value["enabled"])
        return jsonify(ok=True)

    @app.post("/api/batches/<int:batch_id>/concurrency")
    def update_concurrency(batch_id):
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise ValueError("查询参数格式无效。")
        service.set_concurrency(batch_id, positive_count(value.get("concurrency"), "并发数"))
        return jsonify(ok=True)

    @app.delete("/api/batches/<int:batch_id>")
    def delete_batch(batch_id):
        service.delete(batch_id)
        return jsonify(ok=True)

    @app.post("/api/batches/<int:batch_id>/cancel")
    def cancel(batch_id):
        service.cancel(batch_id)
        return jsonify(ok=True)

    @app.post("/api/batches/<int:batch_id>/retry")
    def retry(batch_id):
        value = request.get_json(silent=True) or {}
        if not isinstance(value, dict):
            raise ValueError("重试参数格式无效。")
        run_id = value.get("run_id")
        if run_id is not None and type(run_id) is not int:
            raise ValueError("账号记录 ID 无效。")
        start = value.get("start", False)
        scope = value.get("scope", "unfinished")
        if type(start) is not bool or scope not in ("failed", "unfinished"):
            raise ValueError("重试参数格式无效。")
        concurrency = positive_count(value.get("concurrency"), "并发数", optional=True)
        return jsonify(id=service.retry(batch_id, run_id, scope=scope, start=start, concurrency=concurrency)), 202 if start else 200

    @app.get("/api/runs/<int:run_id>")
    def detail(run_id):
        row = store.run(run_id)
        for key in ("cookies", "storage_state", "diagnostic_log"):
            row.pop(key)
        row["has_screenshot"] = row.pop("screenshot") is not None
        for key in ("progress", "result", "selections"):
            row[key] = json.loads(row[key]) if row[key] else None
        return jsonify(row)

    @app.get("/api/runs/<int:run_id>/screenshot")
    def screenshot(run_id):
        row = store.run(run_id)
        if row["screenshot"] is None:
            abort(404, "本次查询没有截图。")
        return send_file(io.BytesIO(row["screenshot"]), mimetype="image/png", download_name=f"account-{run_id}.png",
                         as_attachment=request.args.get("download") == "1")

    @app.get("/api/batches/<int:batch_id>/export")
    def export(batch_id):
        return send_file(io.BytesIO(results_csv(store.runs(batch_id, include_credentials=True))), mimetype="text/csv", as_attachment=True,
                         download_name=f"query-{batch_id}.csv")

    @app.get("/api/batches/<int:batch_id>/export.zip")
    def export_bundle(batch_id):
        archive = result_bundle(store, batch_id)
        response = send_file(archive, mimetype="application/zip", as_attachment=True,
                             download_name=f"query-{batch_id}.zip")
        response.call_on_close(archive.close)
        return response

    @app.get("/api/batches/<int:batch_id>/export.xlsx")
    def export_excel(batch_id):
        workbook = results_workbook(store, batch_id)
        response = send_file(workbook, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             as_attachment=True, download_name=f"query-{batch_id}.xlsx")
        response.call_on_close(workbook.close)
        return response

    @app.post("/api/batches/<int:batch_id>/exports")
    def create_export(batch_id):
        return jsonify(exporter.start(batch_id)), 202

    @app.get("/api/exports/<job_id>")
    def export_status(job_id):
        return jsonify(exporter.status(job_id))

    @app.get("/api/exports/<job_id>/download")
    def download_export(job_id):
        path = exporter.download(job_id)
        return send_file(path, as_attachment=True, download_name=path.name, conditional=True)

    @app.get("/template.csv")
    def template():
        return send_file(io.BytesIO("邮箱,密码\n".encode("utf-8-sig")), mimetype="text/csv", as_attachment=True,
                         download_name="accounts-template.csv")

    return app


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8088)
    parser.add_argument("--database", type=Path, default=ROOT / "data/ticketmaster.sqlite3")
    args = parser.parse_args()
    args.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # A second server must not mistake live jobs for interrupted ones.
    import fcntl
    lock = open(str(args.database) + ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.exit(1, "这个数据库已有 Web 服务运行。\n")
    app = create_app(args.database, recover=True)
    from waitress import serve
    print(f"账号查询工作台：http://{args.host}:{args.port}", flush=True)
    def stop_server(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop_server)
    try:
        serve(app, host=args.host, port=args.port, threads=8)
    finally:
        app.extensions["exports"].shutdown()
        app.extensions["queries"].shutdown()
        lock.close()


if __name__ == "__main__":
    main()
