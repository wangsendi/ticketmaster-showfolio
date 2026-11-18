"""SQLite is the only durable store for accounts, queries and screenshots."""
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dumps(value):
    return json.dumps(value, ensure_ascii=False) if value is not None else None


class Store:
    def __init__(self, path):
        self._write_lock = threading.RLock()
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS batches (
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL, campaign TEXT NOT NULL,
                    concurrency INTEGER NOT NULL, effective_concurrency INTEGER, status TEXT NOT NULL DEFAULT 'draft',
                    created_at TEXT NOT NULL, finished_at TEXT, error TEXT,
                    source TEXT UNIQUE
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES batches(id),
                    source_line INTEGER NOT NULL, email TEXT NOT NULL, password TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', stage TEXT, started_at TEXT,
                    finished_at TEXT, progress TEXT, result TEXT, selections TEXT,
                    cookie_header TEXT, cookies TEXT, storage_state TEXT,
                    screenshot BLOB, screenshot_kind TEXT, diagnostic_log TEXT, error TEXT,
                    UNIQUE(batch_id, email COLLATE NOCASE)
                );
                CREATE INDEX IF NOT EXISTS runs_batch ON runs(batch_id, id);
                CREATE INDEX IF NOT EXISTS runs_batch_status ON runs(batch_id, status);
                CREATE TABLE IF NOT EXISTS proxy_reputations (
                    route_key TEXT PRIMARY KEY,
                    confidence INTEGER NOT NULL,
                    disabled_until REAL NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
            """)
            if "effective_concurrency" not in {row["name"] for row in db.execute("PRAGMA table_info(batches)")}:
                db.execute("ALTER TABLE batches ADD COLUMN effective_concurrency INTEGER")
            if "continuous_retry" not in {row["name"] for row in db.execute("PRAGMA table_info(batches)")}:
                db.execute("ALTER TABLE batches ADD COLUMN continuous_retry INTEGER NOT NULL DEFAULT 0")
            if "attempt_count" not in {row["name"] for row in db.execute("PRAGMA table_info(runs)")}:
                db.execute("ALTER TABLE runs ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0")
            # Keep the list projection separate from large result JSON and screenshots.
            # Triggers cover retries, normalization and all other result updates.
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_requirements'").fetchone():
                db.execute("""CREATE TABLE run_requirements (
                    run_id INTEGER PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
                    requirement TEXT NOT NULL
                )""")
                db.execute("""INSERT INTO run_requirements
                    SELECT id,json_extract(result,'$.verificationRequirement') FROM runs
                    WHERE json_extract(result,'$.verificationRequirement') IS NOT NULL""")
            db.executescript("""
                CREATE TRIGGER IF NOT EXISTS runs_requirement_insert AFTER INSERT ON runs
                WHEN json_extract(NEW.result,'$.verificationRequirement') IS NOT NULL
                BEGIN
                    INSERT INTO run_requirements VALUES(NEW.id,json_extract(NEW.result,'$.verificationRequirement'));
                END;
                CREATE TRIGGER IF NOT EXISTS runs_requirement_update AFTER UPDATE OF result ON runs
                BEGIN
                    DELETE FROM run_requirements WHERE run_id=NEW.id;
                    INSERT INTO run_requirements
                        SELECT NEW.id,json_extract(NEW.result,'$.verificationRequirement')
                        WHERE json_extract(NEW.result,'$.verificationRequirement') IS NOT NULL;
                END;
            """)
            db.execute("UPDATE proxy_reputations SET confidence=0,disabled_until=-1 WHERE confidence=0 OR disabled_until<>0")
        self.path.chmod(0o600)
        self.normalize_verification_results()
        self.normalize_credentials_results()

    def normalize_verification_results(self):
        from results import verification_requirement, verification_status
        with self.connect(write=True) as db:
            rows = db.execute("SELECT id,status,result FROM runs WHERE status='needs-human' OR (status='success' AND json_extract(result,'$.verificationRequirement')='未进行身份验证')").fetchall()
            for row in rows:
                result = json.loads(row["result"] or "{}")
                requirement = verification_requirement(result)
                if not requirement:
                    if row["status"] == "success":
                        result.pop("verificationRequirement", None)
                        db.execute("UPDATE runs SET status='failed',stage=?,error=?,result=? WHERE id=? AND status='success'",
                                   (result.get("failureStage") or "wait_exchange", "登录回调未完成，请重试。", dumps(result), row["id"]))
                    continue
                result["verificationRequirement"] = requirement
                status = verification_status(result)
                db.execute("UPDATE runs SET status=?,stage=?,error=CASE WHEN ? THEN NULL ELSE error END,result=? WHERE id=? AND status IN ('needs-human','success')",
                           (status, "complete" if status == "success" else result.get("failureStage"), status == "success", dumps(result), row["id"]))

    def normalize_credentials_results(self):
        from results import account_issue, credentials_rejected
        with self.connect(write=True) as db:
            rows = db.execute("SELECT id,result FROM runs WHERE status IN ('failed','partial','needs-human')").fetchall()
            for row in rows:
                result = json.loads(row["result"] or "{}")
                issue = account_issue(result)
                rejected = credentials_rejected(result)
                if result.get("authenticated") or not (issue or rejected):
                    continue
                if issue:
                    result["accountIssue"], result["verificationRequirement"] = issue
                else:
                    result["credentialsRejected"] = True
                    result["verificationRequirement"] = "账号密码错误"
                db.execute("UPDATE runs SET status='success',stage='complete',error=NULL,result=? WHERE id=? AND status IN ('failed','partial','needs-human')",
                           (dumps(result), row["id"]))

    def proxy_reputations(self):
        with self.connect() as db:
            return {row["route_key"]:{"confidence":row["confidence"], "disabled_until":row["disabled_until"]}
                    for row in db.execute("SELECT route_key,confidence,disabled_until FROM proxy_reputations")}

    def save_proxy_reputation(self, route_key, confidence, disabled_until):
        with self.connect(write=True) as db:
            db.execute("""INSERT INTO proxy_reputations(route_key,confidence,disabled_until,updated_at)
                VALUES(?,?,?,?) ON CONFLICT(route_key) DO UPDATE SET confidence=excluded.confidence,
                disabled_until=excluded.disabled_until,updated_at=excluded.updated_at""",
                       (route_key, confidence, disabled_until, now()))

    @contextmanager
    def connect(self, *, write=False):
        # SQLite permits only one writer. Queue local writes before opening a
        # transaction; WAL readers can still proceed independently.
        with self._write_lock if write else nullcontext():
            db = sqlite3.connect(self.path, timeout=30)
            db.row_factory = sqlite3.Row
            try:
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("PRAGMA busy_timeout=30000")
                with db:
                    if write:
                        db.execute("BEGIN IMMEDIATE")
                    yield db
            finally:
                db.close()

    def recover(self):
        with self.connect(write=True) as db:
            db.execute("""UPDATE runs SET status='interrupted', error='服务停止导致查询中断，可手动重试。', finished_at=?
                WHERE batch_id IN (SELECT id FROM batches WHERE status='running') AND status IN ('pending','running')""", (now(),))
            db.execute("UPDATE batches SET status='interrupted', continuous_retry=0, finished_at=? WHERE status='running'", (now(),))

    def create_batch(self, accounts, name, campaign, concurrency=3, source=None):
        with self.connect(write=True) as db:
            batch_id = db.execute("INSERT INTO batches(name,campaign,concurrency,created_at,source) VALUES(?,?,?,?,?)",
                                  (name, campaign, concurrency, now(), source)).lastrowid
            db.executemany("INSERT INTO runs(batch_id,source_line,email,password) VALUES(?,?,?,?)",
                           [(batch_id, a.source_line, a.email, a.password) for a in accounts])
        return batch_id

    def batches(self):
        with self.connect() as db:
            rows = db.execute("""SELECT b.*, count(r.id) total,
                sum(r.status='success') success, sum(r.status IN ('failed','needs-human','partial','interrupted')) failed,
                sum(r.status='running') running, sum(r.status='pending') pending,
                sum(r.status='cancelled') cancelled
                FROM batches b LEFT JOIN runs r ON r.batch_id=b.id GROUP BY b.id ORDER BY b.id DESC""").fetchall()
        return [{k: row[k] for k in row.keys() if k != "source"} for row in rows]

    def batch(self, batch_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise LookupError("批次不存在。")
        return dict(row)

    def runs(self, batch_id, include_credentials=False):
        self.batch(batch_id)
        credentials = ",r.password,r.cookie_header" if include_credentials else ""
        with self.connect() as db:
            rows = db.execute(f"""SELECT r.id,r.batch_id,r.source_line,r.email,r.status,r.stage,r.started_at,r.finished_at,r.selections,r.error,r.attempt_count,
                r.screenshot IS NOT NULL has_screenshot, r.cookie_header IS NOT NULL has_cookie,
                q.requirement AS verification_requirement{credentials}
                FROM runs r LEFT JOIN run_requirements q ON q.run_id=r.id
                WHERE r.batch_id=? ORDER BY r.id""", (batch_id,)).fetchall()
        return [{**dict(row), "selections": json.loads(row["selections"]) if row["selections"] else None} for row in rows]

    def run(self, run_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise LookupError("账号查询记录不存在。")
        return dict(row)

    def run_status(self, run_id):
        with self.connect() as db:
            row = db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise LookupError("账号查询记录不存在。")
        return row["status"]

    def progress(self, run_id, progress):
        safe = {key: progress.get(key) for key in ("stage", "emailStatus", "passwordStatus", "exchangeCOMObserved", "requiresHumanInteraction")}
        with self.connect(write=True) as db:
            db.execute("UPDATE runs SET stage=?,progress=? WHERE id=? AND status='running'", (safe["stage"], dumps(safe), run_id))

    def begin_attempt(self, run_id, attempt, max_attempts, proxy):
        with self.connect(write=True) as db:
            db.execute("""UPDATE runs SET attempt_count=?,stage='launch',finished_at=NULL,progress=NULL,result=?,
                selections=NULL,cookie_header=NULL,cookies=NULL,storage_state=NULL,screenshot=NULL,
                screenshot_kind=NULL,diagnostic_log=NULL,error=NULL WHERE id=? AND status='running'""",
                (attempt, dumps({"attempts":attempt, "maxAttempts":max_attempts, "proxyNode":proxy.node}), run_id))

    def finish_run(self, run_id, data):
        result = data.get("result", {})
        # Serialization needs no database lock; other completed accounts can save meanwhile.
        values = (data["status"], "complete" if data["status"] == "success" else result.get("failureStage") or "complete",
                  now(), dumps(result), dumps(data.get("selections")), data.get("cookie_header"),
                  dumps(data.get("cookies")), dumps(data.get("storage_state")), data.get("screenshot"),
                  data.get("screenshot_kind"), data.get("diagnostic_log"), data.get("error"), run_id)
        with self.connect(write=True) as db:
            db.execute("""UPDATE runs SET status=?,stage=?,finished_at=?,result=?,selections=?,cookie_header=?,
                cookies=?,storage_state=?,screenshot=?,screenshot_kind=?,diagnostic_log=?,error=? WHERE id=?""",
                values)

    def finish_batch(self, batch_id, status="complete", error=None):
        with self.connect(write=True) as db:
            db.execute("UPDATE batches SET status=?,finished_at=?,error=?,continuous_retry=0 WHERE id=?", (status, now(), error, batch_id))
