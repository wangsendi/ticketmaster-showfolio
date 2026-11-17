"use strict";
const $ = id => document.getElementById(id);
const token = document.querySelector('meta[name="app-token"]').content;
const campaign = document.querySelector('meta[name="campaign"]').content;
const PAGE_SIZE = 50;
const labels = { available: "可分配", "in-use": "使用中", disabled: "已禁用" };
const countries = { US: "美国", SG: "新加坡", AU: "澳大利亚", HK: "香港", TW: "台湾", JP: "日本", NL: "荷兰", IT: "意大利", ES: "西班牙" };
const state = { data: null, filter: "all", search: "", page: 1, loading: false, updating: false, restoring: false };
const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
async function api(url, options = {}) {
  const response = await fetch(url, { ...options, headers: { "X-App-Token": token, ...options.headers } });
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || "读取代理失败，请稍后重试。");
  return value;
}
function showError(message = "") {
  $("proxy-page-error").textContent = message;
  $("proxy-page-error").hidden = !message;
}
function render() {
  const data = state.data;
  if (!data) return;
  const nodes = data.nodes;
  const checking = data.status === "checking" || state.updating;
  $("update-proxies").disabled = checking;
  $("update-proxies").textContent = checking ? "正在更新…" : "更新代理名单";
  $("total-nodes").textContent = nodes.length;
  $("available-nodes").textContent = nodes.filter(node => node.status === "available").length;
  $("busy-nodes").textContent = nodes.filter(node => node.status === "in-use").length;
  $("disabled-nodes").textContent = nodes.filter(node => node.status === "disabled").length;
  $("restore-all-proxies").disabled = state.restoring || !nodes.some(node => node.status === "disabled");
  $("restore-all-proxies").textContent = state.restoring ? "正在恢复…" : "一键恢复全部禁用节点";
  $("sync-status").textContent = checking ? `正在更新名单 · 已读取 ${data.checked || 0} / ${data.total || "…"}` : `已同步 ${new Date().toLocaleTimeString("zh-CN", {hour12:false})}`;
  const matched = nodes.filter(node => `${node.name} ${node.id} ${node.exit_ip || ""} ${countries[node.country] || node.country || ""}`.toLowerCase().includes(state.search));
  const counts = { all: matched.length, available: 0, "in-use": 0, disabled: 0 };
  for (const node of matched) counts[node.status]++;
  document.querySelectorAll("[data-count]").forEach(el => { el.textContent = counts[el.dataset.count] || 0; });
  document.querySelectorAll("[data-status]").forEach(button => {
    const selected = button.dataset.status === state.filter;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
  const filtered = matched.filter(node => state.filter === "all" || node.status === state.filter)
    .sort((a, b) => a.confidence - b.confidence || a.name.localeCompare(b.name, "zh-CN"));
  const pages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  state.page = Math.min(state.page, pages);
  const offset = (state.page - 1) * PAGE_SIZE;
  $("node-rows").innerHTML = filtered.slice(offset, offset + PAGE_SIZE).map(node => `<tr>
    <td data-label="节点"><span class="proxy-node-name">${esc(node.name)}</span>${node.name !== node.id ? `<span class="proxy-node-id">${esc(node.id)}</span>` : ""}</td>
    <td data-label="出口 IP"><span class="proxy-ip">${esc(node.exit_ip || "未取得")}</span></td>
    <td data-label="地区">${esc(countries[node.country] || node.country || "—")}</td>
    <td data-label="信誉分"><span class="proxy-score ${node.confidence === 0 ? "zero" : node.confidence <= 5 ? "low" : ""}"><strong>${node.confidence}</strong><span class="muted">/ 10</span><meter class="proxy-score-meter" min="0" max="10" value="${node.confidence}" aria-label="信誉分 ${node.confidence} / 10"></meter></span></td>
    <td data-label="状态"><span class="badge proxy-status ${esc(node.status)}">${esc(labels[node.status])}</span></td>
    <td data-label="操作">${node.status === "disabled" ? `<button type="button" class="secondary restore-proxy" data-restore="${esc(node.id)}" ${state.restoring ? "disabled" : ""}>手动恢复</button>` : '<span class="muted">—</span>'}</td>
  </tr>`).join("");
  $("proxy-table").hidden = filtered.length === 0;
  $("proxy-empty").hidden = filtered.length !== 0;
  $("empty-title").textContent = nodes.length ? "没有符合条件的代理" : checking ? "正在读取代理名单" : "尚未加载代理名单";
  $("empty-message").textContent = nodes.length ? "换个节点关键词、出口 IP，或切换状态筛选。" : checking ? "读取完成后，节点、出口 IP 和信誉分会出现在这里。" : "点击“更新代理名单”，加载全部地区已有 IP 记录的节点。";
  $("node-count").textContent = `共 ${nodes.length} 个节点` + (filtered.length ? ` · 当前显示 ${offset + 1}–${Math.min(offset + PAGE_SIZE, filtered.length)} / ${filtered.length}` : "");
  $("proxy-pagination").hidden = pages <= 1;
  $("previous-nodes").disabled = state.page <= 1;
  $("next-nodes").disabled = state.page >= pages;
  $("node-page").textContent = `${state.page} / ${pages}`;
}
async function refresh() {
  if (state.loading) return;
  state.loading = true;
  try {
    state.data = await api("/api/proxies/nodes");
    showError(state.data.error || "");
    render();
  } catch (error) {
    showError(error.message || "无法连接代理服务。");
    $("sync-status").textContent = "同步失败，等待重试";
  } finally { state.loading = false; }
}
$("node-search").addEventListener("input", event => { state.search = event.target.value.trim().toLowerCase(); state.page = 1; render(); });
document.querySelectorAll("[data-status]").forEach(button => button.addEventListener("click", () => { state.filter = button.dataset.status; state.page = 1; render(); }));
$("previous-nodes").addEventListener("click", () => { state.page--; render(); });
$("next-nodes").addEventListener("click", () => { state.page++; render(); });
$("update-proxies").addEventListener("click", async () => {
  state.updating = true;
  $("update-proxies").disabled = true;
  $("update-proxies").textContent = "正在更新…";
  showError();
  try {
    await api("/api/proxies/filter", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({ campaign }) });
    await refresh();
  } catch (error) { showError(error.message || "更新代理名单失败。"); }
  finally {
    state.updating = false;
    render();
    $("update-proxies").disabled = state.data?.status === "checking";
    if (!state.data) $("update-proxies").textContent = "更新代理名单";
  }
});
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
setInterval(() => { if (!document.hidden) refresh(); }, 3000);
refresh();

$("node-rows").addEventListener("click", async event => {
  const button = event.target.closest("[data-restore]");
  if (!button || state.restoring) return;
  state.restoring = true;
  const nodeId = button.dataset.restore;
  render();
  button.disabled = true;
  button.textContent = "正在恢复…";
  showError();
  try {
    await api("/api/proxies/restore", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({ node_id:nodeId }) });
    await refresh();
  } catch (error) { showError(error.message || "恢复代理失败。"); }
  finally { state.restoring = false; render(); }
});

$("restore-all-proxies").addEventListener("click", async () => {
  if (state.restoring) return;
  state.restoring = true;
  render();
  showError();
  try {
    await api("/api/proxies/restore-all", { method:"POST" });
    await refresh();
  } catch (error) { showError(error.message || "恢复代理失败。"); }
  finally { state.restoring = false; render(); }
});
