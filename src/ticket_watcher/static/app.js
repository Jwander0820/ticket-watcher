"use strict";
const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
const ticketLabels = {AVAILABLE:"有票", SOLD_OUT:"售完", UNKNOWN:"未知", UPCOMING:"尚未開賣", TEMPORARILY_UNAVAILABLE:"暫無票券・線索", PAUSED:"停售", ENDED:"已結束"};
const noticeLabels = {PENDING:"待送", INFLIGHT:"傳送中", SENT:"已送達", PARTIAL:"部分送達", CANCELLED:"已取消", EXPIRED:"已過期", FAILED:"傳送失敗", DISABLED:"不通知", NOT_REQUIRED:"不需通知"};
const eventLabels = {RELEASE:"偵測到可購票", RELEASE_HINT:"發現釋票線索", SYSTEM:"系統通知", ERROR:"查詢異常", STATE_CHANGE:"票況變化"};
let state = null, view = "targets", settingsDirty = false, refreshing = false, refreshAgain = false, serviceChanging = false, toastTimer;
const checking = new Set();
const renderedViews = new Map();
function renderChanged(name, value, draw) {
  const signature = JSON.stringify(value);
  if (renderedViews.get(name) === signature) return;
  draw(); renderedViews.set(name, signature);
}
const reasonLabels = {NOT_DUE:"尚未到例行查詢時間", TARGET_BACKOFF:"前次查詢異常，等待退避期限", TARGET_DISABLED_OR_STOPPED:"監控已停用或到期", TARGET_PAUSED:"目標已暫停，需人工解除", PLATFORM_PAUSED:"平台已暫停，需人工處理", PLATFORM_BUSY_OR_BACKOFF:"平台正在查詢或等待限流期限", NETWORK:"網路異常", RATE_LIMITED:"平台限制頻率", BLOCKED:"平台拒絕存取", PARSE:"資料解析異常", UNSUPPORTED:"不支援的資料來源", INTERNAL_ERROR:"程序異常", CANCELLED:"服務重新載入或停止"};
Object.assign(reasonLabels, {SHOW_STARTED:"選定場次均已開始，已自動停止", QUERY_SUPERSEDED:"查詢已過期或被接手，舊結果未寫入"});
const effectiveStop = (target) => target.state?.effective_stop_at || target.stop_at;
const isStopped = (target) => effectiveStop(target) && new Date(effectiveStop(target)) <= new Date();
const dateText = (value) => value ? new Date(value).toLocaleString("zh-TW", {month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false}) : "尚無紀錄";
const channelName = (id) => state.channels.find((channel) => channel.id === id)?.name || "未指定";
const targetChannels = (target) => target.channel_ids?.length ? target.channel_ids : [target.channel_id || "default"];
function fillChannels(container, name, selected, helpId) {
  container.innerHTML = state.channels.map((c) => {
    const id = `${name}-${c.id}`;
    return `<label class="checkbox channel-option" for="${esc(id)}"><input id="${esc(id)}" name="${name}" type="checkbox" value="${esc(c.id)}" aria-describedby="${helpId}" ${selected.includes(c.id) ? "checked" : ""}><span>${esc(c.name)}${c.configured ? "" : "（尚未連接）"}</span></label>`;
  }).join("");
}
function selectedChannels(form, name, container) {
  const selected = new FormData(form).getAll(name);
  if (!selected.length) {
    container.querySelector("input")?.focus();
    throw new Error("請至少勾選一個通知頻道。");
  }
  return selected;
}
function toast(message) {
  clearTimeout(toastTimer); $("#toast").textContent = message; $("#toast").hidden = false;
  toastTimer = setTimeout(() => { $("#toast").hidden = true; }, 5500);
}
async function api(path, {method="GET", body}={}) {
  const response = await fetch(path, {method, headers: method === "GET" ? {} : {"Content-Type":"application/json", "X-CSRF-Token": state.csrf}, body: body === undefined ? undefined : JSON.stringify(body), cache:"no-store"});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "操作失敗，請稍後再試。");
  return result;
}
function badge(status, label) {
  const tone = ["AVAILABLE","SENT"].includes(status) ? "available" : ["TEMPORARILY_UNAVAILABLE","RELEASE_HINT"].includes(status) ? "hint" : ["ERROR","FAILED"].includes(status) ? "error" : "";
  return `<span class="badge ${tone}">${esc(label || ticketLabels[status] || noticeLabels[status] || status)}</span>`;
}
function renderTargets() {
  const enabled = state.targets.filter((t) => t.enabled && !isStopped(t)).length;
  $("#overview").innerHTML = `<div class="metric"><span>啟用監控</span><strong>${enabled}</strong><small>個目標</small></div><div class="metric"><span>全部場次</span><strong>${state.targets.length}</strong><small>個目標</small></div><div class="metric"><span>等待通知</span><strong>${state.health.pending_notifications ?? "—"}</strong><small>筆事件</small></div>`;
  $("#target-list").innerHTML = state.targets.length ? state.targets.map((t) => {
    const s = t.state, stopped = isStopped(t), on = t.enabled && !stopped && !state.ui_paused;
    const label = !t.enabled ? "已停用" : stopped ? (s.auto_stop_at === effectiveStop(t) ? "已停止" : "已到期") : state.ui_paused ? "服務暫停" : s.paused_reason ? "已暫停" : "監控中";
    const summary = Object.entries(s.summary || {}).map(([status,count]) => badge(status, `${ticketLabels[status] || status} ${count}`)).join("") || badge("UNKNOWN","尚未查詢");
    const unknown = (s.current_observation?.UNKNOWN || 0) > 0;
    const stopText = effectiveStop(t) ? dateText(effectiveStop(t)) : t.auto_stop ? "待取得可靠場次時間" : "未設定";
    return `<article class="ticket"><div class="ticket-stub ${on && !s.paused_reason ? "on" : ""}"><span>${t.url.includes("/order/") ? "票區／票種" : "活動場次"}</span><strong>${label}</strong></div><div class="ticket-main"><div class="ticket-heading"><h3 class="ticket-title">${esc(t.name)}</h3><a class="ticket-link" href="${esc(t.url)}" target="_blank" rel="noreferrer">購票頁 ↗</a></div><div class="badge-group" aria-label="最後有效票況">${summary}${stopped && s.auto_stop_at === effectiveStop(t) ? badge("ENDED","演出已開始") : ""}${unknown ? badge("ERROR","本次票況不完整") : ""}${s.mode === "ACTIVE" ? badge("AVAILABLE","快速模式") : ""}</div><dl class="ticket-details"><div><dt>上次完整觀測</dt><dd>${dateText(s.observed_at)}</dd></div><div><dt>下次預定查詢</dt><dd>${on ? dateText(s.next_allowed_at) : "—"}</dd></div><div><dt>Discord 通知頻道</dt><dd>${esc(targetChannels(t).map(channelName).join("、"))}</dd></div><div><dt>停止監控時間</dt><dd>${esc(stopText)}${s.stopped_session_count ? ` · ${s.stopped_session_count} 場已開始` : ""}</dd></div></dl><div class="ticket-actions"><button class="quiet" data-action="edit-target" data-id="${t.id}">編輯</button><button class="quiet" data-action="toggle-target" data-id="${t.id}">${t.enabled ? "停用" : "啟用"}</button><button class="quiet" data-action="check" data-id="${t.id}" ${!on ? "disabled" : ""}>查詢一次</button>${s.paused_reason ? `<button class="quiet" data-action="resume" data-id="${t.id}">解除暫停</button>` : ""}<button class="quiet danger" data-action="delete-target" data-id="${t.id}">刪除</button></div></div></article>`;
  }).join("") : `<div class="empty"><div class="empty-ticket" aria-hidden="true"></div><h2>留意下一張好票</h2><p>新增 TicketPlus 活動或場次網址。先設定監控與通知頻道，再開始追蹤票況。</p><button class="primary" data-action="add-target">＋ 新增第一個監控</button></div>`;
}
function renderChannels() {
  $("#channel-list").innerHTML = state.channels.map((c) => {
    const isDefault = c.id === "default";
    const description = isDefault ? c.source === "ui" ? "使用 UI 儲存的 Webhook；清除後改用環境變數（若有設定）。" : "可直接編輯 Webhook。目前沿用環境變數（若有設定）。" : "Webhook 網址已遮蔽；頻道是否可送達，請傳送測試確認。";
    return `<article class="channel-card"><div><h2># ${esc(c.name)} ${badge(c.configured ? "AVAILABLE" : "UNKNOWN", c.configured ? "已設定" : "未設定")}</h2><p>${description}</p></div><div class="channel-actions"><button class="quiet" data-action="edit-channel" data-id="${c.id}" aria-label="編輯${esc(c.name)}">編輯</button><button class="secondary" data-action="test-channel" data-id="${c.id}" ${!c.configured || state.ui_paused ? "disabled" : ""}>傳送測試</button>${!isDefault ? `<button class="quiet danger" data-action="delete-channel" data-id="${c.id}">刪除</button>` : c.source === "ui" ? '<button class="quiet" data-action="clear-default" data-id="default">清除 UI 設定</button>' : ""}</div></article>`;
  }).join("");
}
function renderEvents() {
  $("#event-list").innerHTML = state.events.events.length ? state.events.events.map((e) => `<article class="event-row"><time datetime="${esc(e.created_at)}">${dateText(e.created_at)}</time><div><h2>${esc(eventLabels[e.kind] || e.kind)} · ${esc(state.targets.find((t) => t.id === e.target_id)?.name || e.payload.event_name || "系統")}</h2><p>${esc(e.payload.message || e.payload.code || (e.payload.changes_total != null ? `${e.payload.changes_total} 個項目變化` : ""))}</p>${(e.deliveries || []).map((d) => `<p class="delivery-result">${esc(channelName(d.channel_id))} ${badge(d.status)}${d.message_id ? `<small>訊息 ${esc(d.message_id)}</small>` : ""}</p>`).join("")}</div><div>${e.notification_status ? badge(e.notification_status) : badge("UNKNOWN","僅記錄")}</div></article>`).join("") : `<div class="empty"><h2>還沒有事件</h2><p>啟用監控後，票況變化與通知結果會顯示在這裡。</p></div>`;
}
function renderQueryLogs() {
  const logs = state.query_logs || {entries:[]};
  $("#log-error").hidden = !logs.error; $("#log-error").textContent = logs.error || "";
  $("#query-log-list").innerHTML = logs.entries.length ? logs.entries.map((entry) => {
    const name = state.targets.find((t) => t.id === entry.target_id)?.name || (entry.target_id ? "已移除的監控" : "單次公開查詢");
    const label = entry.status === "COMPLETED" ? entry.complete ? "查詢成功" : "票況不完整" : {DEFERRED:"等待中",INTERRUPTED:"已中斷",FAILED:"查詢失敗",UNSUPPORTED:"不支援"}[entry.status] || entry.status;
    const mode = {scheduled:"自動監控",manual:"手動查詢",check:"排程檢查",query:"單次查詢"}[entry.mode] || entry.mode;
    const summary = Object.entries(entry.summary).map(([status,count]) => `${ticketLabels[status] || status} ${count}`).join("・");
    const details = [summary,reasonLabels[entry.reason] || entry.reason,entry.release ? "偵測到可購票" : "",entry.hint ? "發現釋票線索" : ""].filter(Boolean).join("・");
    const time = new Date(entry.time).toLocaleString("zh-TW",{hour12:false});
    return `<article class="event-row"><time datetime="${esc(entry.time)}">${esc(time)}</time><div><h2>${esc(name)} · ${esc(mode)}</h2><p>${esc(details || "本次未發送外部查票請求")}</p><small>${entry.requests} 次請求 · ${(entry.duration_ms/1000).toFixed(1)} 秒${entry.next_at ? ` · 下次預定 ${dateText(entry.next_at)}` : ""}</small></div><div>${badge(entry.status === "COMPLETED" && entry.complete ? "AVAILABLE" : entry.status === "DEFERRED" ? "UNKNOWN" : "ERROR",label)}</div></article>`;
  }).join("") : '<div class="empty"><h2>還沒有查詢紀錄</h2><p>從此版本開始記錄。完成自動或手動查詢後，這裡會顯示結果。</p></div>';
}
function fillSettings() {
  if (settingsDirty) return;
  const s = state.settings, form = $("#settings-form");
  const values = {normal_min:s.polling.normal_interval_seconds[0], normal_max:s.polling.normal_interval_seconds[1], active_min:s.polling.active_interval_seconds[0], active_max:s.polling.active_interval_seconds[1], active_window:s.polling.active_window_seconds, exit_checks:s.polling.exit_active_after_no_available_checks, request_gap:s.http.min_request_gap_seconds, timeout:s.http.timeout_seconds, notification_ttl:s.notifications.delivery_ttl_seconds};
  Object.entries(values).forEach(([name,value]) => { form.elements[name].value = value; });
  form.elements.system_alerts.checked = s.notifications.system_alerts_enabled;
  form.elements.worker_alerts.checked = s.notifications.worker_alerts_enabled;
  fillChannels($("#worker-channels"), "worker_channels", s.notifications.worker_alert_channel_ids || [s.notifications.worker_alert_channel_id], "worker-channel-help");
  form.dataset.revision = state.revision;
}
function render() {
  $("#service-state").textContent = state.ui_paused ? "Ⅱ 此服務已暫停" : state.runner_error ? "監控自動恢復中" : state.runner_active ? "● 監控服務運作中" : "服務待命";
  $("#service-state").classList.toggle("paused", state.ui_paused);
  $("#service-paused").hidden = !state.ui_paused;
  const serviceButton = $("#service-toggle");
  serviceButton.dataset.action = state.ui_paused ? "resume-service" : "pause-service";
  serviceButton.textContent = serviceChanging ? "切換中…" : state.ui_paused ? "恢復運作" : "暫停運作";
  serviceButton.className = state.ui_paused ? "primary" : "secondary";
  serviceButton.disabled = serviceChanging;
  const warning = state.runner_error || (state.external_changes ? "設定檔已在外部變更，請重啟 UI 以載入。" : "");
  $("#global-error").textContent = warning; $("#global-error").hidden = !warning;
  $("#platform-warning").hidden = !state.health.platform_paused;
  $("#platform-warning").innerHTML = state.health.platform_paused ? `TicketPlus 平台已暫停查詢。確認存取問題處理完成後，再解除暫停。<button class="secondary" data-action="resume-platform">解除平台暫停</button>` : "";
  if (view === "targets") {
    renderChanged(view, [state.targets, state.channels, state.health.pending_notifications, state.targets.map(isStopped), state.ui_paused], renderTargets);
    document.querySelectorAll('[data-action="check"]').forEach((button) => {
      const target = state.targets.find((item) => item.id === button.dataset.id), busy = checking.has(button.dataset.id);
      button.disabled = busy || state.ui_paused || !target.enabled || isStopped(target);
      const label = busy ? "查詢中…" : "查詢一次";
      if (button.textContent !== label) button.textContent = label;
    });
  } else if (view === "channels") renderChanged(view, [state.channels, state.ui_paused], renderChannels);
  else if (view === "events" && state.events) renderChanged(view, [state.events, state.targets], renderEvents);
  else if (view === "logs" && state.query_logs) renderChanged(view, [state.query_logs, state.targets], renderQueryLogs);
  else if (view === "settings" && !settingsDirty) renderChanged(view, [state.settings, state.channels, state.revision], fillSettings);
  document.querySelectorAll('[data-action="resume"], [data-action="resume-platform"]').forEach((button) => { button.disabled = state.ui_paused; });
  $("#last-refresh").textContent = `上次更新 ${new Date().toLocaleTimeString("zh-TW",{hour12:false})} · 每 30 秒更新狀態`;
}
async function refresh() {
  if (refreshing) { refreshAgain = true; return; }
  refreshing = true;
  const requestedView = view, previousState = state;
  try {
    const update = await api(`/api/state?view=${requestedView}`);
    // A completed save wins over an earlier in-flight status request.
    if (state === previousState) { state = {...state, ...update}; render(); }
    else refreshAgain = true;
  }
  catch (error) { $("#global-error").textContent = `無法更新控制台：${error.message}`; $("#global-error").hidden = false; $("#service-state").textContent = "連線中斷"; }
  finally {
    refreshing = false;
    if (refreshAgain || view !== requestedView) { refreshAgain = false; void refresh(); }
  }
}
function navigate(next) {
  view = next;
  document.querySelectorAll(".view").forEach((section) => { section.hidden = section.id !== `${view}-view`; });
  document.querySelectorAll("[data-view]").forEach((button) => { if (button.dataset.view === view) button.setAttribute("aria-current","page"); else button.removeAttribute("aria-current"); });
  if (state) render();
  void refresh();
}
function resetForm(form, id="") {
  form.reset(); form.dataset.id = id; form.dataset.revision = state.revision;
  form.querySelector(".form-error").hidden = true;
  form.querySelectorAll("[aria-invalid]").forEach((input) => input.removeAttribute("aria-invalid"));
}
function openTarget(id) {
  const form = $("#target-form"), target = state.targets.find((t) => t.id === id);
  resetForm(form,id || ""); $("#target-title").textContent = target ? "編輯監控" : "新增監控";
  fillChannels($("#target-channels"), "channel_ids", target ? targetChannels(target) : [state.channels.find((c) => c.configured)?.id || "default"], "channel-help");
  if (target) {
    ["name","url"].forEach((key) => { form.elements[key].value = target[key]; });
    form.elements.enabled.checked = target.enabled;
    form.elements.auto_stop.checked = target.auto_stop !== false;
    form.elements.session_ids.value = target.session_ids.join(", "); form.elements.item_ids.value = target.item_ids.join(", ");
    if (target.stop_at) { const date = new Date(target.stop_at); form.elements.stop_at.value = new Date(date - date.getTimezoneOffset()*60000).toISOString().slice(0,16); }
  }
  $("#target-dialog").showModal();
}
function openChannel(id) {
  const form = $("#channel-form"), channel = state.channels.find((c) => c.id === id);
  resetForm(form,id || ""); $("#channel-title").textContent = channel ? "編輯頻道" : "新增頻道";
  if (channel) form.elements.name.value = channel.name;
  form.elements.name.readOnly = id === "default";
  form.elements.webhook_url.required = !channel?.configured; form.elements.webhook_url.type = "password";
  $("#reveal-webhook").textContent = "顯示"; $("#reveal-webhook").setAttribute("aria-pressed","false");
  $("#channel-dialog").showModal();
}
function confirmAction(text, accept="確認") {
  const dialog = $("#confirm-dialog"); $("#confirm-text").textContent = text; $("#confirm-accept").textContent = accept;
  return new Promise((resolve) => {
    dialog.returnValue = "cancel";
    $("#confirm-accept").onclick = () => dialog.close("accept");
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "accept"), {once:true});
    dialog.showModal();
  });
}
function targetValue(target) { const {name,url,enabled,session_ids,item_ids,stop_at,auto_stop} = target; return {name,url,enabled,session_ids,item_ids,stop_at,channel_ids:targetChannels(target),auto_stop}; }
async function action(button) {
  const kind = button.dataset.action, id = button.dataset.id;
  if (kind === "add-target" || kind === "edit-target") return openTarget(id);
  if (kind === "add-channel" || kind === "edit-channel") return openChannel(id);
  if (kind === "refresh") return refresh();
  const revision = state.revision;
  if (kind === "pause-service" || kind === "resume-service") {
    serviceChanging = true; render();
    try {
      state = await api(`/api/actions/${kind}`, {method:"POST", body:{revision}});
      toast(state.ui_paused ? "此服務已暫停，設定與紀錄保留。" : "已恢復運作，將依排程查票與處理通知。");
    } finally { serviceChanging = false; render(); }
    return;
  }
  if (kind === "clear-default") {
    if (!await confirmAction("清除 UI 儲存的預設 Webhook？之後會沿用環境變數；若環境變數也未設定，預設頻道將無法送出通知。", "清除設定")) return;
    state = await api("/api/channels/default", {method:"DELETE",body:{revision}}); render(); toast("已清除 UI 預設頻道設定"); return;
  }
  if (kind.startsWith("delete-")) {
    const target = kind === "delete-target", item = (target ? state.targets : state.channels).find((x) => x.id === id);
    if (!await confirmAction(`刪除「${item.name}」？${target ? "將停止此目標的監控，既有事件紀錄會保留。" : "使用中的頻道需先從監控移除。"}`, "刪除")) return;
    state = await api(`/api/${target ? "targets" : "channels"}/${id}`, {method:"DELETE",body:{revision}}); render(); toast("已刪除"); return;
  }
  if (kind === "toggle-target") {
    const target = state.targets.find((t) => t.id === id);
    state = await api(`/api/targets/${id}`, {method:"PUT",body:{revision,value:{...targetValue(target),enabled:!target.enabled}}}); render(); toast(target.enabled ? "已停用監控" : "已啟用監控"); return;
  }
  if (kind === "test-channel" && !await confirmAction(`傳送一則測試訊息到「${channelName(id)}」？`, "傳送測試")) return;
  if (kind.startsWith("resume") && !await confirmAction("確認存取或資料來源問題已處理完成？解除暫停後仍會遵守原有等待期限。", "解除暫停")) return;
  const endpoint = kind === "resume-platform" ? "resume" : kind;
  const result = await api(`/api/actions/${endpoint}`, {method:"POST", body:{revision, target_id:id, channel_id:id, platform:kind === "resume-platform"}});
  if (kind === "test-channel") toast(`測試通知：${noticeLabels[result.notification.status] || result.notification.status}`);
  else if (kind === "check") toast(result.execution_status === "DEFERRED" ? `${reasonLabels[result.reason] || "目前尚不能查詢"}${result.next_allowed_at ? `；等待至 ${dateText(result.next_allowed_at)}` : ""}` : result.execution_status === "COMPLETED" ? result.complete ? "查詢完成，已更新票況。" : "本次票況不完整，已保留最後有效票況。" : result.error?.message || "查詢失敗，已保留最後有效票況。");
  else toast("已解除暫停，等待排程查詢。");
  await refresh();
}
document.addEventListener("click", async (event) => {
  const button = event.target.closest("button"); if (!button) return;
  if (button.dataset.close) return $(`#${button.dataset.close}`).close();
  if (button.dataset.view) return navigate(button.dataset.view);
  if (!button.dataset.action || !state) return;
  const checkingId = button.dataset.action === "check" ? button.dataset.id : null;
  if (checkingId) { if (checking.has(checkingId)) return; checking.add(checkingId); button.textContent = "查詢中…"; }
  button.disabled = true;
  try { await action(button); } catch (error) { toast(error.message); } finally {
    button.disabled = false;
    if (checkingId) checking.delete(checkingId);
    render();
  }
});
$("#reveal-webhook").addEventListener("click", () => {
  const input = $("#webhook-url"), showing = input.type === "password";
  input.type = showing ? "text" : "password"; $("#reveal-webhook").textContent = showing ? "隱藏" : "顯示"; $("#reveal-webhook").setAttribute("aria-pressed",String(showing));
});
$("#channel-dialog").addEventListener("close", () => { $("#webhook-url").value = ""; });
$("#settings-form").addEventListener("input", () => { settingsDirty = true; });
const ids = (value) => value.split(/[\s,，]+/).filter(Boolean);
document.querySelectorAll("form").forEach((form) => form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = form.querySelector('[type="submit"]'), errorBox = form.querySelector(".form-error"), revision = form.dataset.revision;
  button.disabled = true; errorBox.hidden = true;
  try {
    let path, value, message;
    const f = form.elements;
    if (form.id === "target-form") {
      value = {name:f.name.value.trim(),url:f.url.value.trim(),channel_ids:selectedChannels(form,"channel_ids",$("#target-channels")),enabled:f.enabled.checked,auto_stop:f.auto_stop.checked,session_ids:ids(f.session_ids.value),item_ids:ids(f.item_ids.value),stop_at:f.stop_at.value ? new Date(f.stop_at.value).toISOString() : null};
      path = "/api/targets"; message = "已儲存監控";
    } else if (form.id === "channel-form") {
      value = {name:f.name.value.trim(),webhook_url:f.webhook_url.value.trim()}; path = "/api/channels"; message = "已儲存頻道";
    } else {
      const n = (name) => Number(f[name].value);
      if (n("normal_max") < n("normal_min") || n("active_max") < n("active_min")) throw new Error("間隔上限不可小於下限。");
      value = {polling:{normal_interval_seconds:[n("normal_min"),n("normal_max")],active_interval_seconds:[n("active_min"),n("active_max")],active_window_seconds:n("active_window"),exit_active_after_no_available_checks:n("exit_checks")},http:{min_request_gap_seconds:n("request_gap"),timeout_seconds:n("timeout")},notifications:{delivery_ttl_seconds:n("notification_ttl"),system_alerts_enabled:f.system_alerts.checked,worker_alerts_enabled:f.worker_alerts.checked,worker_alert_channel_ids:selectedChannels(form,"worker_channels",$("#worker-channels"))}};
      path = "/api/settings"; message = "已儲存並套用設定";
    }
    if (form.dataset.id) path += `/${form.dataset.id}`;
    state = await api(path, {method:form.dataset.id ? "PUT" : "POST", body:{revision,value}});
    if (form.id === "settings-form") settingsDirty = false;
    form.closest("dialog")?.close(); render(); toast(message);
  } catch (error) { errorBox.textContent = error.message; errorBox.hidden = false; errorBox.focus(); }
  finally { button.disabled = false; }
}));
document.addEventListener("blur", (event) => {
  if (event.target.matches?.("input,select") && CSS.supports("selector(:user-invalid)")) {
    if (event.target.matches(":user-invalid")) event.target.setAttribute("aria-invalid","true");
    else event.target.removeAttribute("aria-invalid");
  }
}, true);
document.addEventListener("input", (event) => { if (event.target.matches?.("input,select") && event.target.validity.valid) event.target.removeAttribute("aria-invalid"); });
refresh();
document.addEventListener("visibilitychange", () => { if (!document.hidden) void refresh(); });
setInterval(() => { if (!document.hidden) void refresh(); }, 30000);
