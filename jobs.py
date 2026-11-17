"""Independent batch schedulers with shared proxy leases and private browser workspaces."""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from collections import deque
from pathlib import Path
import tempfile
import logging
import sqlite3
import traceback
import threading
import time

from proxy_check import fresh_pool, filtered_pool_from_api
from proxy_pool import DEFAULT_PROXY_API, NoAlternativeProxy, ProxyRefreshError
from results import collect, credentials_rejected, has_persona_evidence
from runner import browser_command, run_account
from store import now

MAX_ATTEMPTS = 1
CONTINUOUS_RETRY_DELAY = 3
logger = logging.getLogger(__name__)


def proxy_outcome(data):
    result = data.get("result", {})
    if data["status"] == "cancelled" or credentials_rejected(result) or has_persona_evidence(result):
        return None
    if result.get("verification", {}).get("authenticated"):
        return True
    if result.get("errorType") in ("PlaywrightWebSocketAssertion", "BrowserResultInvalid"):
        return None
    return data["status"] == "success"


class QueryService:
    def __init__(self, store, proxy_api=DEFAULT_PROXY_API, delay=3, execute=None, pool_factory=None):
        self.store = store
        self.proxy_api = proxy_api
        self.delay = delay
        self.execute = execute or run_account
        self.pool_factory = pool_factory
        self.lock = threading.Lock()
        self.active = {}
        self.threads = {}
        self.retry_queues = {}
        self.current_pool = None
        self.pool_campaign = None
        self.pool_mode = "verify"
        self.proxy_thread = None
        self.proxy_stop = threading.Event()
        self.proxy_scan_lock = threading.Lock()
        self.proxy_state_lock = threading.Lock()
        self.proxy_state = {"status":"idle", "total":0, "checked":0, "accepted":0, "updated_at":None, "error":None}

    def proxy_status(self):
        with self.proxy_state_lock:
            status = {**self.proxy_state, "supports_api_filter":True}
        pool = self.current_pool
        return {**status, **(pool.reputation_status() if pool else {})}

    def _proxy_progress(self, **values):
        with self.proxy_state_lock:
            self.proxy_state.update(values)

    def proxy_nodes(self):
        with self.proxy_state_lock:
            state = dict(self.proxy_state)
        pool = self.current_pool
        details = pool.reputation_status(include_nodes=True) if pool else {"nodes":[], "disabled_nodes":0, "available_nodes":0}
        details.pop("reputations", None)
        return {**state, **details, "server_time":time.time()}

    def restore_proxy(self, node_id):
        with self.lock:
            if self.current_pool is None:
                raise ValueError("请先加载代理名单。")
            return self.current_pool.restore(node_id)

    def restore_all_proxies(self):
        with self.lock:
            if self.current_pool is None:
                raise ValueError("请先加载代理名单。")
            return self.current_pool.restore_all()

    def _load_pool(self, campaign, event, mode="verify"):
        while not self.proxy_scan_lock.acquire(timeout=0.2):
            if event.is_set():
                raise InterruptedError("代理检测已取消。")
        try:
            if event.is_set():
                raise InterruptedError("代理检测已取消。")
            self._proxy_progress(status="checking", campaign=campaign, mode=mode, total=0, checked=0, accepted=0, error=None)
            loader = filtered_pool_from_api if mode == "api" else fresh_pool
            pool = self.pool_factory(campaign) if self.pool_factory else loader(
                self.proxy_api, campaign, stop_event=event, on_progress=lambda value:self._proxy_progress(**value))
            if event.is_set():
                raise InterruptedError("代理检测已取消。")
            if not pool.nodes:
                raise ValueError("本次筛选没有可用的代理出口，请更新代理后重新刷新。")
            pool.configure_reputation(self.store)
            total = self.proxy_status()["total"] or len(pool.nodes)
            self._proxy_progress(status="ready", total=total, checked=total, accepted=len(pool.nodes), updated_at=now(), error=None)
            return pool
        except InterruptedError:
            self._proxy_progress(status="cancelled", error="代理检测已取消。")
            raise
        except Exception as error:
            message = str(error) if isinstance(error, ValueError) else f"代理刷新失败（{type(error).__name__}）。"
            self._proxy_progress(status="failed", error=message)
            raise ValueError(message) from None
        finally:
            self.proxy_scan_lock.release()

    def refresh_proxies(self, campaign, *, mode="verify"):
        if mode not in ("api", "verify"):
            raise ValueError("代理筛选模式必须为 api 或 verify。")
        with self.lock:
            if mode == "verify" and any(self.store.batch(batch_id)["campaign"] != campaign
                                        for batch_id in self.active):
                raise ValueError("存在其他活动的运行任务，请使用“加载全部地区代理”，或等这些任务结束后再检测活动代理。")
            self._refresh_proxies_locked(campaign, mode=mode)

    def _refresh_proxies_locked(self, campaign, mode="verify"):
        if self.proxy_thread and self.proxy_thread.is_alive():
            return
        target = self.current_pool
        event = threading.Event()
        self.proxy_stop = event
        if target:
            target.begin_refresh()
        self._proxy_progress(status="checking", campaign=campaign, mode=mode, total=0, checked=0, accepted=0, error=None)

        def refresh():
            try:
                replacement = self._load_pool(campaign, event, mode=mode)
                if target:
                    target.finish_refresh(replacement)
                with self.lock:
                    if not target:
                        self.current_pool = replacement
                    self.pool_campaign = campaign
                    self.pool_mode = mode
            except (ValueError, InterruptedError) as error:
                if target:
                    target.finish_refresh(error=str(error))
        self.proxy_thread = threading.Thread(target=refresh, daemon=True, name="proxy-refresh")
        self.proxy_thread.start()

    def _require_pool_locked(self, campaign, allow_refresh=False):
        if self.proxy_thread and self.proxy_thread.is_alive() and not allow_refresh:
            raise ValueError("代理正在筛选或刷新，请等待完成后再开始查询。")
        pool = self.current_pool
        if pool is None or self.pool_mode != "api" and self.pool_campaign != campaign:
            raise ValueError("请先点击“更新全部地区代理”，加载完成后再开始查询。")
        if not pool.refreshing and (pool.refresh_error or not pool.nodes):
            raise ValueError("当前没有可用的代理，请先手动刷新。")
        return pool

    def start(self, batch_id, concurrency=None):
        with self.lock:
            self._start_locked(batch_id, concurrency)

    def _start_locked(self, batch_id, concurrency=None):
        if batch_id in self.active:
            raise ValueError("该批次正在查询，请勿重复启动。")
        batch = self.store.batch(batch_id)
        if batch["status"] != "draft":
            raise ValueError("该批次已执行过，请使用重试更新原记录。")
        pool = self._require_pool_locked(batch["campaign"])
        if concurrency is not None:
            batch["concurrency"] = concurrency
        event = threading.Event()
        with self.store.connect(write=True) as db:
            db.execute("UPDATE batches SET status='running',concurrency=? WHERE id=?", (batch["concurrency"], batch_id))
        self.active[batch_id] = event
        self.retry_queues[batch_id] = deque()
        thread = threading.Thread(target=self._run, args=(batch, event, pool, self.retry_queues[batch_id]),
                                  daemon=True, name=f"batch-{batch_id}")
        self.threads[batch_id] = thread
        thread.start()

    def set_continuous_retry(self, batch_id, enabled):
        if type(enabled) is not bool:
            raise ValueError("持续重试开关必须是布尔值。")
        with self.lock:
            batch = self.store.batch(batch_id)
            live = batch_id in self.active
            if enabled:
                if live and self.active[batch_id].is_set():
                    raise ValueError("当前任务正在停止，请等待结束。")
                self._require_pool_locked(batch["campaign"], allow_refresh=live)
                if batch["status"] != "draft":
                    with self.store.connect() as db:
                        eligible = db.execute("SELECT 1 FROM runs WHERE batch_id=? AND status IN ('failed','partial','interrupted') LIMIT 1", (batch_id,)).fetchone()
                    if eligible:
                        self._retry_locked(batch_id, None, "continuous", live=live)
                    elif not live:
                        raise ValueError("没有可以持续重试的失败账号。")
            with self.store.connect(write=True) as db:
                db.execute("UPDATE batches SET continuous_retry=? WHERE id=?", (int(enabled), batch_id))
            if enabled and not live:
                self._start_locked(batch_id)

    def set_concurrency(self, batch_id, concurrency):
        if type(concurrency) is not int or concurrency < 1:
            raise ValueError("并发数必须是正整数。")
        with self.lock:
            if batch_id not in self.active or self.active[batch_id].is_set():
                raise ValueError("该任务未在查询中，无法调整运行并发。")
            with self.store.connect(write=True) as db:
                db.execute("UPDATE batches SET concurrency=? WHERE id=?", (concurrency, batch_id))

    def cancel(self, batch_id):
        with self.lock:
            if batch_id not in self.active:
                raise ValueError("该批次没有正在运行的查询。")
            self.active[batch_id].set()
            with self.store.connect(write=True) as db:
                db.execute("UPDATE batches SET continuous_retry=0 WHERE id=?", (batch_id,))
                db.execute("UPDATE runs SET status='cancelled',error='查询已取消。',finished_at=? WHERE batch_id=? AND status='pending'", (now(), batch_id))

    def retry(self, batch_id, run_id=None, scope="unfinished", start=False, concurrency=None):
        with self.lock:
            live = batch_id in self.active
            if live and self.active[batch_id].is_set():
                raise ValueError("当前任务正在停止，请等待停止完成后再重试。")
            if live and not start:
                raise ValueError("运行中的任务请直接启动重试。")
            if start:
                self._require_pool_locked(self.store.batch(batch_id)["campaign"], allow_refresh=live)
            self._retry_locked(batch_id, run_id, scope, concurrency, live=live)
            if start and not live:
                self._start_locked(batch_id)
            return batch_id

    def delete(self, batch_id):
        with self.lock:
            batch = self.store.batch(batch_id)
            if batch["status"] == "running" or batch_id in self.active:
                raise ValueError("请先停止查询，等待任务结束后再删除。")
            with self.store.connect(write=True) as db:
                db.execute("DELETE FROM runs WHERE batch_id=?", (batch_id,))
                db.execute("DELETE FROM batches WHERE id=?", (batch_id,))

    def _retry_locked(self, batch_id, run_id, scope, concurrency=None, live=False):
        batch = self.store.batch(batch_id)
        if batch["status"] == "draft" or batch["status"] == "running" and not live:
            raise ValueError("请先完成或停止当前批次。")
        if scope == "human":
            condition, parameters = "status='needs-human'", (batch_id,)
            if run_id is not None:
                condition += " AND id=?"
                parameters += (run_id,)
        elif run_id is not None:
            condition, parameters = "id=?", (batch_id, run_id)
            if live:
                condition += " AND status NOT IN ('success','pending','running')"
        elif scope == "continuous":
            condition, parameters = "status IN ('failed','partial','interrupted')", (batch_id,)
        elif scope == "failed":
            condition, parameters = "status IN ('failed','partial','interrupted')", (batch_id,)
        else:
            condition, parameters = "status NOT IN ('success','pending','running')" if live else "status<>'success'", (batch_id,)
        if scope != "human":
            condition += " AND status<>'needs-human'"
        # Reset only the selected accounts, atomically with the batch state.
        # Old screenshots and cookies must not appear as this attempt's result.
        with self.store.connect(write=True) as db:
            rows = db.execute(f"""UPDATE runs SET status='pending',stage=?,started_at=NULL,finished_at=NULL,
                progress=NULL,result=NULL,selections=NULL,cookie_header=NULL,cookies=NULL,storage_state=NULL,
                screenshot=NULL,screenshot_kind=NULL,diagnostic_log=NULL,error=NULL,attempt_count=0
                WHERE batch_id=? AND ({condition}) RETURNING id""", ("retry_queued" if live else None, *parameters)).fetchall()
            if not rows:
                raise ValueError("没有需要重试的账号。")
            if not live:
                db.execute("""UPDATE batches SET status='draft',concurrency=?,effective_concurrency=NULL,
                    finished_at=NULL,error=NULL WHERE id=?""", (concurrency or batch["concurrency"], batch_id))
        if live:
            self.retry_queues[batch_id].extend(row["id"] for row in rows)

    def _run(self, batch, event, pool, retry_queue):
        try:
            with self.lock:
                with self.store.connect(write=True) as db:
                    run_ids = [row["id"] for row in db.execute("SELECT id FROM runs WHERE batch_id=? AND status='pending' ORDER BY id", (batch["id"],))]
                    capacity = db.execute("SELECT COUNT(*) FROM runs WHERE batch_id=?", (batch["id"],)).fetchone()[0]
                    workers = min(batch["concurrency"], len(pool.nodes), capacity)
                    db.execute("UPDATE batches SET effective_concurrency=? WHERE id=?", (workers, batch["id"]))
                queued = set(retry_queue)
                run_ids = list(retry_queue) + [run_id for run_id in run_ids if run_id not in queued]
                retry_queue.clear()
                if not workers:
                    self.store.finish_batch(batch["id"], "cancelled" if event.is_set() else "complete")
                    self._release_batch_locked(batch["id"], event)
                    return
            command, xvfb = browser_command("login.cjs", "auto")
            gate = threading.Lock()
            next_start = [0.0]
            previous_proxies = {}

            def worker(run_id):
                if event.is_set():
                    return
                with self.store.connect(write=True) as db:
                    changed = db.execute("UPDATE runs SET status='running',stage='launch',started_at=? WHERE id=? AND status='pending'", (now(), run_id)).rowcount
                if not changed:
                    return
                account = self.store.run(run_id)
                previous, attempts = previous_proxies.pop(run_id, None), 0
                if previous and len(pool.retry_exclusions(previous)) == len(pool.nodes):
                    # A later queue turn can reuse the only exit after it is released.
                    previous = None
                data = {"status":"cancelled", "error":"查询已取消。", "result":{}}
                while attempts < MAX_ATTEMPTS:
                    proxy = None
                    try:
                        if pool.refreshing:
                            self.store.progress(run_id, {"stage":"refresh_proxies"})
                        else:
                            self.store.progress(run_id, {"stage":"wait_proxy"})
                        with pool.lease(previous=previous, stop_event=event, wait=True) as proxy:
                            self.store.progress(run_id, {"stage":"wait_start"})
                            with gate:
                                if event.wait(max(0, next_start[0] - time.monotonic())):
                                    raise InterruptedError("查询已取消。")
                                next_start[0] = time.monotonic() + self.delay
                            attempts += 1
                            previous = proxy
                            self.store.begin_attempt(run_id, attempts, MAX_ATTEMPTS, proxy)
                            with tempfile.TemporaryDirectory(prefix="tm-query-") as temporary:
                                out = Path(temporary) / "browser"
                                try:
                                    summary = self.execute(account["email"], account["password"], out, command, xvfb, batch["campaign"],
                                                           proxy=proxy, stop_event=event,
                                                           on_progress=lambda value: self.store.progress(run_id, value))
                                    data = collect(out, account["email"], summary)
                                except sqlite3.Error:
                                    raise
                                except Exception:
                                    if not event.is_set():
                                        pool.record_outcome(proxy, False)
                                    raise
                                outcome = None if event.is_set() else proxy_outcome(data)
                                reputation = pool.record_outcome(proxy, outcome)
                                if reputation:
                                    data["result"]["proxyReputation"] = reputation
                    except NoAlternativeProxy as error:
                        if not attempts:
                            data = {"status":"failed", "error":None, "result":{"failureStage":"proxy_setup"}}
                        data["error"] = (data.get("error") or "查询未完成。") + " " + str(error)
                        data["result"]["retryStopReason"] = "no_alternative_proxy"
                        break
                    except ProxyRefreshError as error:
                        data = {"status":"failed", "error":str(error), "result":{"failureStage":"refresh_proxies"}}
                        break
                    except InterruptedError:
                        data = {"status":"cancelled", "error":"查询已取消。", "result":{}}
                        break
                    except sqlite3.Error:
                        # A storage failure is a service error, not a browser/login failure.
                        raise
                    except Exception as error:
                        # Exception arguments may contain credentials; retain only the type.
                        data = {"status":"failed", "error":"查询进程异常，详情中保留了异常类型。",
                                "result":{"failureStage":"runner", "errorType":type(error).__name__}}
                        if proxy is None:
                            break
                    if proxy:
                        data["result"]["proxyNode"] = proxy.node
                    if event.is_set():
                        if data["status"] != "success":
                            data["status"], data["error"] = "cancelled", "查询已取消。"
                        break
                    if data["status"] not in ("failed", "partial") or attempts == MAX_ATTEMPTS:
                        break
                    self.store.progress(run_id, {"stage":"retry_wait"})
                data["result"].update(attempts=attempts, maxAttempts=MAX_ATTEMPTS)
                if previous is not None:
                    previous_proxies[run_id] = previous
                self.store.finish_run(run_id, data)
                return run_id if data["status"] in ("failed", "partial") else None

            # Threads are created lazily; the scheduler controls the live limit.
            with ThreadPoolExecutor(max_workers=capacity, thread_name_prefix="query") as executor:
                remaining = deque(run_ids)
                delayed = {}
                futures = set()
                while True:
                    with self.lock:
                        current = self.store.batch(batch["id"])
                        requested = current["concurrency"]
                        if not current["continuous_retry"] or event.is_set():
                            delayed.clear()
                        elif not pool.refreshing and pool.nodes and not pool.refresh_error:
                            for retry_id, ready_at in list(delayed.items()):
                                if time.monotonic() >= ready_at:
                                    # Manual retry may already have claimed the same row.
                                    if self.store.run_status(retry_id) in ("failed", "partial"):
                                        self._retry_locked(batch["id"], retry_id, "continuous", live=True)
                                        retry_queue.remove(retry_id)
                                        remaining.append(retry_id)
                                    del delayed[retry_id]
                        else:
                            # An unusable pool needs a manual refresh, not a busy retry loop.
                            if not pool.refreshing:
                                delayed.clear()
                        # Keep scheduling paused while a manual refresh replaces the pool.
                        if not pool.refreshing and pool.nodes:
                            updated = min(requested, len(pool.nodes), capacity)
                            if updated != workers:
                                workers = updated
                                with self.store.connect(write=True) as db:
                                    db.execute("UPDATE batches SET effective_concurrency=? WHERE id=?", (workers, batch["id"]))
                        while len(futures) < workers and not event.is_set():
                            run_id = retry_queue.popleft() if retry_queue else (remaining.popleft() if remaining else None)
                            if run_id is None:
                                break
                            futures.add(executor.submit(worker, run_id))
                        if not futures and not delayed:
                            # Finishing and accepting a live retry share the same lock.
                            with self.store.connect(write=True) as db:
                                db.execute("UPDATE batches SET continuous_retry=0 WHERE id=?", (batch["id"],))
                            self.store.finish_batch(batch["id"], "cancelled" if event.is_set() else "complete")
                            self._release_batch_locked(batch["id"], event)
                            return
                    if not futures:
                        event.wait(0.2)
                        continue
                    done, futures = wait(futures, timeout=0.2, return_when=FIRST_COMPLETED)
                    for future in done:
                        retry_id = future.result()
                        if retry_id is not None:
                            with self.lock:
                                if not event.is_set() and self.store.batch(batch["id"])["continuous_retry"]:
                                    delayed[retry_id] = time.monotonic() + CONTINUOUS_RETRY_DELAY
        except InterruptedError:
            with self.lock:
                with self.store.connect(write=True) as db:
                    db.execute("UPDATE runs SET status='cancelled',error='查询已取消。',finished_at=? WHERE batch_id=? AND status IN ('pending','running')", (now(), batch["id"]))
                self.store.finish_batch(batch["id"], "cancelled")
        except Exception as error:
            if isinstance(error, sqlite3.Error):
                code = getattr(error, "sqlite_errorname", None) or type(error).__name__
                # SQLite may include supplied values in its error text. Keep the
                # structured code and call sites, never credentials or SQL values.
                labels = {"SQLITE_BUSY":"数据库写入繁忙", "SQLITE_BUSY_SNAPSHOT":"数据库事务发生并发冲突",
                          "SQLITE_LOCKED":"数据库被锁定", "SQLITE_FULL":"数据库或磁盘空间不足",
                          "SQLITE_READONLY":"数据库不可写", "SQLITE_CANTOPEN":"无法打开数据库",
                          "SQLITE_IOERR":"数据库读写失败", "SQLITE_ERROR":"数据库结构或语句错误"}
                message = f"查询服务执行失败：{labels.get(code, '数据库操作失败')}（{code}）。"
            else:
                message = str(error) if isinstance(error, ValueError) else f"查询服务执行失败（{type(error).__name__}）。"
            frames = " > ".join(f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
                                for frame in traceback.extract_tb(error.__traceback__))
            logger.error("Batch %s failed: %s; call sites: %s", batch["id"], message, frames)
            with self.lock:
                with self.store.connect(write=True) as db:
                    db.execute("UPDATE runs SET status='failed',stage='service',error=?,finished_at=? WHERE batch_id=? AND status IN ('pending','running')", (message, now(), batch["id"]))
                with self.store.connect(write=True) as db:
                    db.execute("UPDATE batches SET continuous_retry=0 WHERE id=?", (batch["id"],))
                self.store.finish_batch(batch["id"], "failed", message)
                self._release_batch_locked(batch["id"], event)
        finally:
            with self.lock:
                self._release_batch_locked(batch["id"], event)

    def _release_batch_locked(self, batch_id, event):
        # A completed scheduler must never remove a newer retry of the same batch.
        if self.active.get(batch_id) is event:
            del self.active[batch_id]
            self.retry_queues.pop(batch_id, None)

    def shutdown(self):
        with self.lock:
            self.proxy_stop.set()
            with self.store.connect(write=True) as db:
                db.execute("UPDATE batches SET continuous_retry=0 WHERE continuous_retry<>0")
                for batch_id, event in self.active.items():
                    event.set()
                    db.execute("UPDATE runs SET status='cancelled',error='服务已停止。',finished_at=? WHERE batch_id=? AND status='pending'", (now(), batch_id))
            threads = list(self.threads.values())
        deadline = time.monotonic() + 15
        for thread in threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        if self.proxy_thread:
            self.proxy_thread.join(timeout=max(0, deadline - time.monotonic()))
