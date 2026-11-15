"""Import an old batch into SQLite without logging in or deleting source files."""
import argparse
import hashlib
import json
import os
from pathlib import Path

from accounts import parse_accounts
from results import collect
from runner import ROOT
from store import Store


def migrate(directory, account_file, store):
    directory = Path(directory).resolve()
    raw = Path(account_file).read_bytes()
    manifest = json.loads((directory / "batch.json").read_text())
    if hashlib.sha256(raw).hexdigest() != manifest["accountsFileSHA256"]:
        raise ValueError("账号表格与历史批次指纹不符，不能绑定账号。")
    with store.connect() as db:
        prior = db.execute("SELECT id FROM batches WHERE source=?", (str(directory),)).fetchone()
        if prior:
            return prior["id"]
    accounts = parse_accounts(raw, str(account_file), skip_success=False)
    old = json.loads((directory / "summary.json").read_text())
    if len(accounts) != manifest["accountCount"] or len(accounts) != len(old["accounts"]):
        raise ValueError("历史批次的账号数量不一致。")
    # Validate and read everything before creating a durable batch.
    imported = []
    for account, row in zip(accounts, old["accounts"]):
        if account.source_line != row["sourceLine"]:
            raise ValueError("历史批次的表格行号不一致。")
        out = (directory / row["outputDirectory"]).resolve()
        if not out.is_relative_to(directory):
            raise ValueError("历史记录包含无效路径。")
        summary = {**row, "authenticated": row["status"] == "success"}
        log = out / "browser-private.log"
        if log.is_file() and "FFPage._onWebSocketOpened" in log.read_text(errors="replace"):
            summary.update(failureStage="browser_driver", errorType="PlaywrightWebSocketAssertion", childExitCode=1)
            try:
                progress = json.loads((out / "progress.json").read_text())
                summary.update(lastStage=progress.get("stage"), emailStatus=progress.get("emailStatus"), passwordStatus=progress.get("passwordStatus"))
            except (ValueError, OSError):
                pass
        imported.append(collect(out, account.email, summary))
    batch_id = store.create_batch(accounts, "历史 · " + directory.name, manifest["startURL"], 3, source=str(directory))
    for row, data in zip(store.runs(batch_id), imported):
        store.finish_run(row["id"], data)
    with store.connect(write=True) as db:
        db.execute("UPDATE batches SET created_at=? WHERE id=?", (manifest["createdAtUTC"], batch_id))
        for row, data in zip(store.runs(batch_id), imported):
            result = data["result"]
            db.execute("UPDATE runs SET started_at=?,finished_at=? WHERE id=?", (result.get("startedAtUTC"), result.get("finishedAtUTC") or old["updatedAtUTC"], row["id"]))
    store.finish_batch(batch_id)
    return batch_id


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--accounts", type=Path, required=True)
    parser.add_argument("--database", type=Path, default=ROOT / "data/ticketmaster.sqlite3")
    args = parser.parse_args()
    batch_id = migrate(args.directory, args.accounts, Store(args.database))
    print(f"历史批次已导入 SQLite，批次 ID：{batch_id}")
