"use strict";
const $ = id => document.getElementById(id);
const PAGE_SIZE = 100;
const failedStatuses = new Set(["failed", "partial", "interrupted"]);
const token = document.querySelector('meta[name="app-token"]').content;
const state = { batches: [], batch: null, runs: [], filter: "all", page: 1, detail: null, refreshing: false, proxies: null, exports: {} };
const statusNames = { draft: "待开始", pending: "等待查询", running: "查询中", success: "已完成", partial: "结果不完整", failed: "查询失败", "needs-human": "需人工验证", cancelled: "已停止", interrupted: "已中断", complete: "已结束" };
const stageNames = { refresh_proxies: "更新代理名单", wait_proxy: "等待空闲出口 IP", wait_start: "等待启动浏览器", retry_queued: "等待重试", retry_wait: "等待更换代理重试", setup: "准备代理", service: "查询服务异常", launch: "启动浏览器", proxy_setup: "连接代理", campaign: "打开活动", official_sign_in: "打开登录", wait_email: "等待邮箱页面", wait_recaptcha_before_email: "等待验证加载", email_once: "提交邮箱", password_once: "提交密码", wait_recaptcha_initialization: "等待验证加载", wait_exchange: "等待登录回调", wait_com_return: "返回活动页面", readonly_com_fan: "读取报名记录", capture_page: "生成整页截图", browser_driver: "浏览器驱动崩溃", browser_process: "浏览器进程异常", complete: "查询结束", runner: "查询进程异常", timeout: "查询超时" };
const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
const date = value => value ? new Date(value).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }) : "—";
const badge = status => `<span class="badge ${esc(status)}">${esc(statusNames[status] || status)}</span>`;
let toastTimer;
function toast(message) { $("toast").textContent = message; $("toast").hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $("toast").hidden = true, 4500); }
async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { "X-App-Token": token, ...options.headers } });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || "请求失败，请稍后重试。");
  return value;
}
async function action(button, fn) {
  button.disabled = true;
  try { await fn(); } catch (error) { toast(error.message || "无法连接查询服务。"); } finally { button.disabled = false; render(); }
}
function renderBatches() {
  $("history-count").textContent = state.batches.length;
  $("batch-list").innerHTML = state.batches.length ? state.batches.map(b => `<button class="batch-item ${state.batch?.id === b.id ? "selected" : ""}" data-batch="${b.id}"><span class="batch-name">${esc(b.name)}</span><span class="batch-summary"><span>${date(b.created_at)} · ${b.total} 个账号</span><span>${statusNames[b.status]}</span></span></button>`).join("") : '<p class="quiet-empty">还没有导入记录</p>';
}
function render() {
  renderBatches();
  renderProxies();
  const batch = state.batch;
  $("apply-concurrency").hidden = batch?.status !== "running";
  if (!batch) {
    $("batch-label").textContent = "QUERY RESULTS";
    $("batch-meta").textContent = "导入表格后，从这里开始查询";
    for (const name of ["start", "cancel", "retry", "human-retry", "continuous-retry", "resume", "export", "delete"]) $(`${name}-button`).hidden = true;
    for (const name of ["total", "success", "running", "failed"]) $(`${name}-stat`).textContent = "0";
    $("progress-bar").style.width = "0%";
    $("batch-error").hidden = true;
    $("live-label").textContent = "查询结果自动保存";
    renderRows();
    return;
  }
  const all = state.runs;
  const successes = all.filter(row => row.status === "success").length;
  const pending = all.filter(row => ["pending", "running"].includes(row.status)).length;
  const attention = all.length - successes - pending;
  $("batch-label").textContent = `QUERY ${String(batch.id).padStart(3, "0")} / ${statusNames[batch.status] || batch.status}`;
  const concurrencyText = `并发上限 ${batch.concurrency}` + (batch.effective_concurrency != null && batch.effective_concurrency !== batch.concurrency ? `，实际 ${batch.effective_concurrency}` : "");
  $("batch-meta").textContent = `${date(batch.created_at)} 导入 · ${concurrencyText} · ${new URL(batch.campaign).pathname.slice(1)}`;
  $("total-stat").textContent = all.length;
  $("success-stat").textContent = successes;
  $("running-stat").textContent = pending;
  $("failed-stat").textContent = attention;
  $("progress-bar").style.width = `${all.length ? ((all.length - pending) / all.length) * 100 : 0}%`;
  $("start-button").hidden = batch.status !== "draft";
  $("cancel-button").hidden = batch.status !== "running";
  const failedCount = all.filter(row => failedStatuses.has(row.status)).length;
  const humanCount = all.filter(row => row.status === "needs-human").length;
  $("human-retry-button").hidden = humanCount === 0;
  $("human-retry-button").disabled = batch.status === "draft";
  $("human-retry-button").textContent = `重试需人工处理 (${humanCount})`;
  $("retry-button").hidden = false;
  $("retry-button").disabled = batch.status === "draft" || failedCount === 0;
  $("continuous-retry-button").hidden = false;
  $("continuous-retry-button").disabled = batch.status !== "running" && batch.status !== "draft" && !all.some(row => ["failed", "partial", "interrupted"].includes(row.status));
  $("continuous-retry-button").textContent = batch.continuous_retry ? "关闭持续重试" : "持续重试失败账号";
  $("continuous-retry-button").setAttribute("aria-pressed", String(!!batch.continuous_retry));
  $("continuous-retry-button").title = "开启后失败账号持续重新排队；成功、需要人工验证的账号不再自动重试";
  $("retry-button").textContent = `重试失败账号 (${failedCount})`;
  $("retry-button").title = failedCount ? "失败账号优先加入重试队列，覆盖旧结果，无需等待整批结束" : "当前批次没有失败账号";
  $("resume-button").hidden = ["draft", "running"].includes(batch.status) || !all.some(row => row.status === "cancelled");
  $("export-button").hidden = false;
  $("export-button").href = `/api/batches/${batch.id}/export.xlsx`;
  const exporting = state.exports[batch.id];
  $("export-button").setAttribute("aria-disabled", String(!!exporting));
  $("export-button").textContent = exporting
    ? exporting.status === "queued" ? "导出排队中…" : exporting.total && exporting.completed < exporting.total
      ? `准备导出 ${exporting.completed} / ${exporting.total}` : "正在生成 Excel…"
    : "导出 Excel（含截图）";
  $("delete-button").hidden = false;
  $("delete-button").disabled = batch.status === "running";
  $("delete-button").title = batch.status === "running" ? "请先停止查询，等待任务结束后再删除" : "删除当前任务及其账号、Cookie 和截图";
  $("batch-error").textContent = batch.error || "";
  $("batch-error").hidden = !batch.error;
  $("live-label").textContent = batch.status === "running" ? "每 2 秒更新 · 结果自动保存" : "查询结果已保存在本机";
  renderRows();
  renderDetailActions();
}
function renderProxies() {
  const proxy = state.proxies;
  if (!proxy) return;
  const checking = proxy.status === "checking";
  if ($("filter-proxies")) {
    $("filter-proxies").hidden = true;
    $("filter-proxies").disabled = checking;
    $("filter-proxies").textContent = checking && proxy.mode === "api" ? "正在筛选…" : "加载全部地区代理";
  }
  $("refresh-proxies").disabled = checking;
  $("refresh-proxies").textContent = checking ? "正在加载…" : "更新全部地区代理";
  $("proxy-status").textContent = checking ? (proxy.mode === "api" ? "读取全部地区节点" : "检测代理可用性") : proxy.status === "ready" ? `${proxy.mode === "api" ? "全部地区节点" : "可用节点"} ${proxy.accepted} 个` : proxy.status === "failed" ? "刷新失败" : proxy.status === "cancelled" ? "检测已取消" : "尚未刷新";
  $("proxy-progress").textContent = checking ? `已检测 ${proxy.checked} / ${proxy.total || "…"}，通过 ${proxy.accepted} 个。` : proxy.error || (proxy.updated_at ? `${date(proxy.updated_at)} 更新${proxy.mode === "api" ? "（接口 IP 结果）" : ""}；查询和重试复用此名单。` : "先加载代理名单；不限地区，查询和重试复用名单。");
  if (!checking && proxy.disabled_nodes) $("proxy-progress").textContent += ` ${proxy.disabled_nodes} 个节点已禁用，${proxy.available_nodes} 个可用；请到代理池页面手动恢复。`;
  $("proxy-progress").classList.toggle("error-message", proxy.status === "failed");
}
function renderDetailActions() {
  const row = state.detail;
  if (!row) return;
  $("rerun-button").hidden = state.batch?.status === "draft" || ["pending", "running"].includes(row.status) || state.batch?.status === "running" && row.status === "success";
  $("rerun-button").textContent = row.status === "needs-human" ? "清除人工处理状态并重试" : "重新查询";
}
function renderRows() {
  const search = $("search").value.trim().toLowerCase();
  const matching = state.runs.filter(row => row.email.toLowerCase().includes(search));
  const category = row => failedStatuses.has(row.status) ? "failed" : row.status;
  const counts = { all: matching.length };
  for (const row of matching) {
    const key = category(row);
    counts[key] = (counts[key] || 0) + 1;
  }
  document.querySelectorAll("[data-filter-count]").forEach(counter => {
    counter.textContent = counts[counter.dataset.filterCount] || 0;
  });
  const rows = matching.filter(row => state.filter === "all" || category(row) === state.filter);
  const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  state.page = Math.min(state.page, pages);
  const offset = (state.page - 1) * PAGE_SIZE;
  const visibleRows = rows.slice(offset, offset + PAGE_SIZE);
  $("pagination").hidden = pages <= 1;
  $("previous-page").disabled = state.page <= 1;
  $("next-page").disabled = state.page >= pages;
  $("page-number").textContent = `${state.page} / ${pages}`;
  $("account-rows").innerHTML = visibleRows.map((row) => {
    let selected = '<span class="muted">—</span>';
    if (row.verification_requirement) {
      selected = `<span>${esc(row.verification_requirement)}</span>`;
    } else if (row.selections !== null) {
      selected = row.selections.length ? `<strong>${row.selections.length} 场</strong><span class="selection-preview">${esc(row.selections.map(v => v.city || v.name).join(" · "))}</span>` : '<span class="muted">未选择场次</span>';
    }
    return `<tr><td class="row-number">${String(row.source_line).padStart(2,"0")}</td><td><button class="email-button" data-detail="${row.id}">${esc(row.email)}</button><span class="account-date">${row.finished_at ? date(row.finished_at) : "尚未完成查询"}</span></td><td>${badge(row.status)}${row.attempt_count ? `<span class="stage-label">本轮尝试 ${row.attempt_count} 次</span>` : ""}${row.status === "running" || row.stage === "retry_queued" || row.error ? `<span class="stage-label" title="${esc(row.error || stageNames[row.stage] || row.stage)}">${esc(stageNames[row.stage] || row.error || "准备查询")}</span>` : ""}${failedStatuses.has(row.status) && state.batch?.status !== "draft" ? `<button class="text-link row-retry" data-retry="${row.id}" aria-label="重试 ${esc(row.email)}">重试</button>` : ""}</td><td>${selected}</td><td class="last-col"><button class="detail-link" data-detail="${row.id}" aria-label="查看 ${esc(row.email)} 详情">↗</button></td></tr>`;
  }).join("");
  $("table-scroll").hidden = !rows.length;
  $("empty-state").hidden = !!rows.length;
  $("empty-title").textContent = state.batch ? "没有匹配的账号" : "从一张表格开始";
  $("empty-description").textContent = state.batch ? "换个邮箱关键词，或切换到全部账号。" : "导入邮箱和密码，场次与截图会整理在这里。";
  $("result-count").textContent = state.batch ? `显示 ${rows.length ? offset + 1 : 0}–${offset + visibleRows.length} / ${rows.length} 个账号` : "尚未导入账号";
}
async function refresh(batchId) {
  if (state.refreshing) { state.pendingRefresh = true; if (batchId) state.pendingBatchId = batchId; return; }
  state.refreshing = true;
  try {
    const [batches, proxies] = await Promise.all([api("/api/batches"), api("/api/proxies").catch(() => null)]);
    state.batches = batches.batches;
    state.proxies = proxies;
    const requested = batchId || state.batch?.id;
    const id = state.batches.some(batch => batch.id === requested) ? requested : state.batches[0]?.id;
    if (id) {
      const data = await api(`/api/batches/${id}`);
      if (state.batch?.id !== data.batch.id) state.page = 1;
      state.batch = data.batch;
      state.runs = data.runs;
    } else {
      state.batch = null;
      state.runs = [];
      state.page = 1;
    }
    render();
    if (state.detail && $("detail-dialog").open) {
      const current = state.runs.find(row => row.id === state.detail.id);
      if (!current) { clearAccountFields(); $("detail-dialog").close(); }
      else if (current.status !== state.detail.status || current.stage !== state.detail.stage || current.attempt_count !== state.detail.attempt_count) await openDetail(current.id);
    }
  } finally {
    state.refreshing = false;
    if (state.pendingRefresh) { const next = state.pendingBatchId; state.pendingBatchId = null; state.pendingRefresh = false; await refresh(next); }
  }
}
function clearAccountFields() { $("detail-password").value = ""; $("detail-cookie").value = ""; }
async function openDetail(id) {
  const data = await api(`/api/runs/${id}`);
  state.detail = data;
  $("detail-password").value = data.password;
  $("detail-cookie").value = data.cookie_header || "";
  $("detail-title").textContent = data.email;
  $("detail-state").innerHTML = badge(data.status);
  $("detail-events").innerHTML = data.selections === null ? '<p class="muted">尚未取得场次记录。</p>' : data.selections.length ? data.selections.map(v => `<div class="event-item"><span class="event-date">${esc(v.date || "日期未解析")}</span><div class="event-name">${esc(v.city ? [v.city,v.state].filter(Boolean).join(", ") : v.name)}</div><div class="event-venue">${esc(v.venue || v.id)}</div></div>`).join("") : '<p class="muted">这个账号在该活动中未选择场次。</p>';
  const result = data.result || {}, progress = data.progress || {};
  if (result.verificationRequirement) $("detail-events").innerHTML = `<p>${esc(result.verificationRequirement)}</p>`;
  const fields = [["查询时间", date(data.finished_at || data.started_at)], ["最后阶段", stageNames[data.stage] || data.stage || "等待查询"], ["代理节点", result.proxyNode?.name || "—"], ["邮箱 / 密码", `${result.emailStatus || progress.emailStatus || "—"} / ${result.passwordStatus || progress.passwordStatus || "—"}`]];
  if (data.attempt_count) fields.push(["尝试次数", `${data.attempt_count}`]);
  if (result.proxyNode?.screening?.exitIP) fields.push(["出口 IP（筛选时）", result.proxyNode.screening.exitIP]);
  if (result.proxyReputation) fields.push(["代理信誉（本次结束时）", `${result.proxyReputation.confidence} / 10`]);
  if (result.errorType) fields.push(["异常类型", result.errorType]);
  if (result.lastStage) fields.push(["崩溃前阶段", stageNames[result.lastStage] || result.lastStage]);
  $("detail-metadata").innerHTML = fields.map(([label,value])=>`<dt>${esc(label)}</dt><dd>${esc(value)}</dd>`).join("");
  $("detail-error").textContent = data.error || "";
  $("detail-error").hidden = !data.error;
  renderDetailActions();
  $("download-shot").hidden = !data.has_screenshot;
  $("download-shot").href = `/api/runs/${id}/screenshot?download=1`;
  $("screenshot-kind").textContent = !data.has_screenshot ? "" : result.credentialsRejected ? "账号密码错误页面" : result.verificationRequirement ? "验证要求页面" : data.screenshot_kind === "full-page" ? `整页长截图${result.screenshotHeight ? ` · ${result.screenshotWidth} × ${result.screenshotHeight}` : ""}` : data.screenshot_kind === "legacy-viewport" ? "历史截图 · 仅包含当时第一屏" : "失败时页面";
  $("screenshot-content").innerHTML = data.has_screenshot ? `<a href="/api/runs/${id}/screenshot" target="_blank" rel="noopener" aria-label="打开原始页面截图"><img src="/api/runs/${id}/screenshot" alt="${esc(data.email)} 的查询页面截图"></a>` : '<div class="empty-state"><h3>暂无页面截图</h3><p>完成查询后，整页截图会出现在这里。</p></div>';
  if (!$("detail-dialog").open) $("detail-dialog").showModal();
}
function readCount(id, optional = false) {
  const input = $(id);
  if (optional && input.value.trim() === "") return null;
  const count = input.valueAsNumber;
  if (!input.checkValidity() || !Number.isSafeInteger(count) || count < 1) {
    input.reportValidity();
    throw new Error(id === "concurrency" ? "并发数必须是正整数。" : "账号数量必须是正整数，留空表示不限。");
  }
  return count;
}
for (const id of ["concurrency", "max-accounts"]) {
  try {
    const saved = localStorage.getItem(`tm-query-${id}`);
    if (saved === "" && id === "max-accounts" || saved && Number.isSafeInteger(Number(saved)) && Number(saved) > 0) $(id).value = saved;
  } catch {}
  $(id).addEventListener("change", () => {
    try { localStorage.setItem(`tm-query-${id}`, String(readCount(id, id === "max-accounts") ?? "")); } catch {}
  });
}
$("file").addEventListener("change", () => { $("file-label").textContent = $("file").files[0]?.name || "选择或拖入表格"; $("import-error").hidden = true; $("import-notice").hidden = true; });
for (const name of ["dragenter","dragover"]) $("drop-zone").addEventListener(name, () => $("drop-zone").classList.add("dragging"));
for (const name of ["dragleave","drop"]) $("drop-zone").addEventListener(name, () => $("drop-zone").classList.remove("dragging"));
$("import-form").addEventListener("submit", async event => {
  event.preventDefault();
  $("import-button").disabled = true; $("import-error").hidden = true; $("import-notice").hidden = true;
  try {
    const form = new FormData(event.target);
    form.set("concurrency", String(readCount("concurrency")));
    form.set("max_accounts", readCount("max-accounts", true) ?? "");
    const data = await api("/api/import", { method: "POST", body: form });
    const skipped = data.skipped || 0;
    const deferred = data.deferred || 0;
    const message = data.count
      ? `已导入 ${data.count} 个账号${skipped ? `，跳过 ${skipped} 个已成功或需人工验证的账号` : ""}${deferred ? `，${deferred} 个超出本次数量未导入` : ""}。点击“开始查询”运行。`
      : `已跳过 ${skipped} 个已成功或需人工验证的账号，本次没有需要查询的账号，未创建新批次。`;
    if (data.id) await refresh(data.id);
    $("import-notice").textContent = message;
    $("import-notice").hidden = false;
    toast(message);
  }
  catch (error) { $("import-error").textContent = error.message; $("import-error").hidden = false; }
  finally { $("import-button").disabled = false; }
});
$("batch-list").addEventListener("click", event => { const button = event.target.closest("[data-batch]"); if (button) refresh(Number(button.dataset.batch)).catch(error => toast(error.message)); });
$("export-button").addEventListener("click", async event => {
  if (!state.proxies?.supports_background_exports) return;
  event.preventDefault();
  const batchId = state.batch?.id;
  if (!batchId || state.exports[batchId]) return;
  state.exports[batchId] = {status:"queued"};
  render();
  try {
    let job = await api(`/api/batches/${batchId}/exports`, {method:"POST"});
    while (!["ready", "failed"].includes(job.status)) {
      state.exports[batchId] = job;
      render();
      await new Promise(resolve => setTimeout(resolve, 1000));
      job = await api(`/api/exports/${job.id}`);
    }
    if (job.status === "failed") throw new Error(job.error);
    const link = document.createElement("a");
    link.href = `/api/exports/${job.id}/download`;
    link.download = job.filename;
    document.body.append(link);
    link.click();
    link.remove();
    toast(`Excel 已生成（${(job.bytes / 1000000).toFixed(1)} MB），开始下载。全部账号与截图均在同一个文件中。`);
  } catch (error) { toast(error.message || "导出失败，请重试。"); }
  finally { delete state.exports[batchId]; render(); }
});
$("account-rows").addEventListener("click", event => {
  const retryButton = event.target.closest("[data-retry]");
  if (retryButton) { action(retryButton, () => retry(Number(retryButton.dataset.retry))); return; }
  const button = event.target.closest("[data-detail]");
  if (button) openDetail(Number(button.dataset.detail)).catch(error => toast(error.message));
});
document.querySelectorAll("[data-filter]").forEach(button => button.addEventListener("click", () => { state.filter = button.dataset.filter; state.page = 1; document.querySelectorAll("[data-filter]").forEach(other => { other.classList.toggle("active", other === button); other.setAttribute("aria-pressed", String(other === button)); }); renderRows(); }));
$("search").addEventListener("input", () => { state.page = 1; renderRows(); });
$("previous-page").addEventListener("click", () => { state.page--; renderRows(); });
$("next-page").addEventListener("click", () => { state.page++; renderRows(); });
$("filter-proxies")?.addEventListener("click", event => action(event.target, async () => {
  const campaign = state.batch?.campaign || $("campaign").value.trim();
  await api("/api/proxies/filter", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({campaign})});
  await refresh();
  toast("按接口现有 IP 记录加载全部地区节点，完成后查询和重试复用此名单。");
}));
$("refresh-proxies").addEventListener("click", event => action(event.target, async () => {
  const campaign = state.batch?.campaign || $("campaign").value.trim();
  await api("/api/proxies/refresh", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({campaign, mode:"api"})});
  await refresh();
  toast("正在加载全部地区代理，不按国家或活动页进行预筛选。");
}));
$("apply-concurrency").addEventListener("click", event => action(event.target, async () => {
  await api(`/api/batches/${state.batch.id}/concurrency`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({concurrency:readCount("concurrency")})});
  toast("并发上限已更新；调低时会等待正在查询的账号完成。");
  await refresh();
}));
$("start-button").addEventListener("click", event => action(event.target, async () => { await api(`/api/batches/${state.batch.id}/start`, { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify({concurrency:readCount("concurrency")}) }); await refresh(); }));
$("cancel-button").addEventListener("click", event => action(event.target, async () => { await api(`/api/batches/${state.batch.id}/cancel`, { method: "POST" }); toast("正在停止浏览器，已完成的结果会保留。"); await refresh(); }));
$("delete-button").addEventListener("click", event => action(event.target, async () => {
  const batch = state.batch;
  if (!batch || !window.confirm(`删除任务“${batch.name}”及其中 ${state.runs.length} 个账号的记录？\n账号、密码、Cookie 和截图会一起删除，无法恢复。`)) return;
  await api(`/api/batches/${batch.id}`, { method: "DELETE" });
  clearAccountFields();
  $("detail-dialog").close();
  state.batch = null;
  state.runs = [];
  state.page = 1;
  await refresh();
  toast("任务及其账号记录、Cookie 和截图已删除。");
}));
async function retry(runId, scope = "failed") {
  if (runId && state.runs.find(row => row.id === runId)?.status === "needs-human") scope = "human";
  const running = state.batch.status === "running";
  const data = await api(`/api/batches/${state.batch.id}/retry`, { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify({ start: true, scope, ...(!running ? {concurrency:readCount("concurrency")} : {}), ...(runId ? {run_id:runId} : {}) }) });
  $("detail-dialog").close();
  await refresh(data.id);
  toast(running ? "已优先加入重试队列，使用现有代理执行。" : "已在原记录中开始重试，使用现有代理。");
}
$("continuous-retry-button").addEventListener("click", event => action(event.target, async () => {
  const enabled = !state.batch.continuous_retry;
  await api(`/api/batches/${state.batch.id}/continuous-retry`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({enabled})});
  await refresh();
  toast(enabled ? "已开启持续重试，失败账号将自动重新排队。" : "已关闭持续重试，当前查询会继续完成。");
}));
$("retry-button").addEventListener("click", event => action(event.target, () => retry()));
$("human-retry-button").addEventListener("click", event => action(event.target, () => retry(null, "human")));
$("resume-button").addEventListener("click", event => action(event.target, () => retry(null, "unfinished")));
$("rerun-button").addEventListener("click", event => action(event.target, () => retry(state.detail.id)));
$("close-detail").addEventListener("click", () => { clearAccountFields(); $("detail-dialog").close(); });
$("detail-dialog").addEventListener("close", () => { state.detail = null; clearAccountFields(); });
async function copyAccountField(id, label) {
  const field = $(id), value = field.value;
  if (!value) throw new Error(`本次查询没有可复制的 ${label}。`);
  try { await navigator.clipboard.writeText(value); toast(`${label} 已复制。`); }
  catch { field.select(); toast(`请使用 Ctrl+C 或 ⌘C 复制已选中的 ${label}。`); }
}
$("copy-password").addEventListener("click", event => action(event.target, () => copyAccountField("detail-password", "密码")));
$("copy-cookie").addEventListener("click", event => action(event.target, () => copyAccountField("detail-cookie", "Cookie")));
refresh().catch(error => toast(error.message));
setInterval(() => { if (!document.hidden) refresh().catch(() => { $("live-label").textContent = "连接中断，正在重连…"; }); }, 2000);
