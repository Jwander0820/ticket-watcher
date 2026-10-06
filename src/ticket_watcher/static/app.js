"use strict";
const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
const ticketLabels = {AVAILABLE:"有票", SOLD_OUT:"售完", UNKNOWN:"未知", UPCOMING:"尚未開賣", TEMPORARILY_UNAVAILABLE:"暫無票券・線索", PAUSED:"停售", ENDED:"已結束"};
const noticeLabels = {PENDING:"待送", INFLIGHT:"傳送中", SENT:"已送達", CANCELLED:"已取消", EXPIRED:"已過期", FAILED:"傳送失敗", DISABLED:"不通知", NOT_REQUIRED:"不需通知"};
const eventLabels = {RELEASE:"偵測到可購票", RELEASE_HINT:"發現釋票線索", SYSTEM:"系統通知", ERROR:"查詢異常", STATE_CHANGE:"票況變化"};
let state = null, view = "targets", settingsDirty = false, refreshing = false, toastTimer;
const dateText = (value) => value ? new Date(value).toLocaleString("zh-TW", {month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false}) : "尚無紀錄";
const channelName = (id) => state.channels.find((channel) => channel.id === id)?.name || "未指定";
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
  const enabled = state.targets.filter((t) => t.enabled && (!t.stop_at || new Date(t.stop_at) > new Date())).length;
  $("#overview").innerHTML = `<div class="metric"><span>啟用監控</span><strong>${enabled}</strong><small>個目標</small></div><div class="metric"><span>全部場次</span><strong>${state.targets.length}</strong><small>個目標</small></div><div class="metric"><span>等待通知</span><strong>${state.health.pending_notifications}</strong><small>筆事件</small></div>`;
  $("#target-list").innerHTML = state.targets.length ? state.targets.map((t) => {
    const s = t.state, stopped = t.stop_at && new Date(t.stop_at) <= new Date(), on = t.enabled && !stopped;
    const label = !t.enabled ? "已停用" : stopped ? "已到期" : s.paused_reason ? "已暫停" : "監控中";
    const summary = Object.entries(s.summary || {}).map(([status,count]) => badge(status, `${ticketLabels[status] || status} ${count}`)).join("") || badge("UNKNOWN","尚未查詢");
    const unknown = (s.current_observation?.UNKNOWN || 0) > 0;
    return `<article class="ticket"><div class="ticket-stub ${on && !s.paused_reason ? "on" : ""}"><span>${t.url.includes("/order/") ? "票區／票種" : "活動場次"}</span><strong>${label}</strong></div><div class="ticket-main"><div class="ticket-heading"><h3 class="ticket-title">${esc(t.name)}</h3><a class="ticket-link" href="${esc(t.url)}" target="_blank" rel="noreferrer">購票頁 ↗</a></div><div class="badge-group" aria-label="最後有效票況">${summary}${unknown ? badge("ERROR","本次票況不完整") : ""}${s.mode === "ACTIVE" ? badge("AVAILABLE","快速模式") : ""}</div><dl class="ticket-details"><div><dt>上次完整觀測</dt><dd>${dateText(s.observed_at)}</dd></div><div><dt>下次預定查詢</dt><dd>${on ? dateText(s.next_allowed_at) : "—"}</dd></div><div><dt>Discord 通知頻道</dt><dd>${esc(channelName(t.channel_id))}</dd></div></dl><div class="ticket-actions"><button class="quiet" data-action="edit-target" data-id="${t.id}">編輯</button><button class="quiet" data-action="toggle-target" data-id="${t.id}">${t.enabled ? "停用" : "啟用"}</button><button class="quiet" data-action="check" data-id="${t.id}" ${!on ? "disabled" : ""}>查詢一次</button>${s.paused_reason ? `<button class="quiet" data-action="resume" data-id="${t.id}">解除暫停</button>` : ""}<button class="quiet danger" data-action="delete-target" data-id="${t.id}">刪除</button></div></div></article>`;
  }).join("") : `<div class="empty"><div class="empty-ticket" aria-hidden="true"></div><h2>留意下一張好票</h2><p>新增 TicketPlus 活動或場次網址。先設定監控與通知頻道，再開始追蹤票況。</p><button class="primary" data-action="add-target">＋ 新增第一個監控</button></div>`;
}
function renderChannels() {
  $("#channel-list").innerHTML = state.channels.map((c) => `<article class="channel-card"><div><h2># ${esc(c.name)} ${badge(c.configured ? "AVAILABLE" : "UNKNOWN", c.configured ? "已設定" : "未設定")}</h2><p>${c.readonly ? "沿用啟動程序的 DISCORD_WEBHOOK_URL 設定。" : "Webhook 網址已遮蔽；頻道是否可送達，請傳送測試確認。"}</p></div><div class="channel-actions">${!c.readonly ? `<button class="quiet" data-action="edit-channel" data-id="${c.id}">編輯</button>` : ""}<button class="secondary" data-action="test-channel" data-id="${c.id}" ${!c.configured ? "disabled" : ""}>傳送測試</button>${!c.readonly ? `<button class="quiet danger" data-action="delete-channel" data-id="${c.id}">刪除</button>` : ""}</div></article>`).join("");
}
function renderEvents() {
  $("#event-list").innerHTML = state.events.events.length ? state.events.events.map((e) => `<article class="event-row"><time datetime="${esc(e.created_at)}">${dateText(e.created_at)}</time><div><h2>${esc(eventLabels[e.kind] || e.kind)} · ${esc(state.targets.find((t) => t.id === e.target_id)?.name || e.payload.event_name || "系統")}</h2><p>${esc(e.payload.message || e.payload.code || (e.payload.changes_total != null ? `${e.payload.changes_total} 個項目變化` : ""))}</p>${e.message_id ? `<small>訊息 ${esc(e.message_id)}</small>` : ""}</div><div>${e.notification_status ? badge(e.notification_status) : badge("UNKNOWN","僅記錄")}</div></article>`).join("") : `<div class="empty"><h2>還沒有事件</h2><p>啟用監控後，票況變化與通知結果會顯示在這裡。</p></div>`;
}
function fillSettings() {
  if (settingsDirty) return;
  const s = state.settings, form = $("#settings-form");
  const values = {normal_min:s.polling.normal_interval_seconds[0], normal_max:s.polling.normal_interval_seconds[1], active_min:s.polling.active_interval_seconds[0], active_max:s.polling.active_interval_seconds[1], active_window:s.polling.active_window_seconds, exit_checks:s.polling.exit_active_after_no_available_checks, request_gap:s.http.min_request_gap_seconds, timeout:s.http.timeout_seconds, notification_ttl:s.notifications.delivery_ttl_seconds};
  Object.entries(values).forEach(([name,value]) => { form.elements[name].value = value; });
  form.elements.system_alerts.checked = s.notifications.system_alerts_enabled;
  form.dataset.revision = state.revision;
}
function render() {
  $("#service-state").textContent = state.runner_error ? "監控已停止" : state.runner_active ? "● 監控服務運作中" : "服務待命";
  const warning = state.runner_error || (state.external_changes ? "設定檔已在外部變更，請重啟 UI 以載入。" : "");
  $("#global-error").textContent = warning; $("#global-error").hidden = !warning;
  $("#platform-warning").hidden = !state.health.platform_paused;
  $("#platform-warning").innerHTML = state.health.platform_paused ? `TicketPlus 平台已暫停查詢。確認存取問題處理完成後，再解除暫停。<button class="secondary" data-action="resume-platform">解除平台暫停</button>` : "";
  renderTargets(); renderChannels(); renderEvents(); fillSettings();
  $("#last-refresh").textContent = `上次更新 ${new Date().toLocaleTimeString("zh-TW",{hour12:false})} · 每 10 秒更新狀態`;
}
async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try { state = await api("/api/state"); render(); }
  catch (error) { $("#global-error").textContent = `無法更新控制台：${error.message}`; $("#global-error").hidden = false; $("#service-state").textContent = "連線中斷"; }
  finally { refreshing = false; }
}
function navigate(next) {
  view = next;
  document.querySelectorAll(".view").forEach((section) => { section.hidden = section.id !== `${view}-view`; });
  document.querySelectorAll("[data-view]").forEach((button) => { if (button.dataset.view === view) button.setAttribute("aria-current","page"); else button.removeAttribute("aria-current"); });
}
function resetForm(form, id="") {
  form.reset(); form.dataset.id = id; form.dataset.revision = state.revision;
  form.querySelector(".form-error").hidden = true;
  form.querySelectorAll("[aria-invalid]").forEach((input) => input.removeAttribute("aria-invalid"));
}
function openTarget(id) {
  const form = $("#target-form"), target = state.targets.find((t) => t.id === id);
  resetForm(form,id || ""); $("#target-title").textContent = target ? "編輯監控" : "新增監控";
  $("#target-channel").innerHTML = state.channels.map((c) => `<option value="${c.id}">${esc(c.name)}${c.configured ? "" : "（尚未連接）"}</option>`).join("");
  if (target) {
    ["name","url","channel_id"].forEach((key) => { form.elements[key].value = target[key]; });
    form.elements.enabled.checked = target.enabled;
    form.elements.session_ids.value = target.session_ids.join(", "); form.elements.item_ids.value = target.item_ids.join(", ");
    if (target.stop_at) { const date = new Date(target.stop_at); form.elements.stop_at.value = new Date(date - date.getTimezoneOffset()*60000).toISOString().slice(0,16); }
  } else if (state.channels.some((c) => c.configured)) form.elements.channel_id.value = state.channels.find((c) => c.configured).id;
  $("#target-dialog").showModal();
}
function openChannel(id) {
  const form = $("#channel-form"), channel = state.channels.find((c) => c.id === id);
  resetForm(form,id || ""); $("#channel-title").textContent = channel ? "編輯頻道" : "新增頻道";
  if (channel) form.elements.name.value = channel.name;
  form.elements.webhook_url.required = !channel; form.elements.webhook_url.type = "password";
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
function targetValue(target) { const {name,url,enabled,session_ids,item_ids,stop_at,channel_id} = target; return {name,url,enabled,session_ids,item_ids,stop_at,channel_id}; }
async function action(button) {
  const kind = button.dataset.action, id = button.dataset.id;
  if (kind === "add-target" || kind === "edit-target") return openTarget(id);
  if (kind === "add-channel" || kind === "edit-channel") return openChannel(id);
  if (kind === "refresh") return refresh();
  const revision = state.revision;
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
  else if (kind === "check") toast(result.execution_status === "DEFERRED" ? `目前尚不能查詢；下次可查：${dateText(result.next_allowed_at)}` : result.execution_status === "COMPLETED" ? "查詢完成，已更新票況。" : result.error?.message || "查詢失敗，已保留最後有效票況。");
  else toast("已解除暫停，等待排程查詢。");
  await refresh();
}
document.addEventListener("click", async (event) => {
  const button = event.target.closest("button"); if (!button) return;
  if (button.dataset.close) return $(`#${button.dataset.close}`).close();
  if (button.dataset.view) return navigate(button.dataset.view);
  if (!button.dataset.action || !state) return;
  button.disabled = true;
  try { await action(button); } catch (error) { toast(error.message); } finally { button.disabled = false; }
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
      value = {name:f.name.value.trim(),url:f.url.value.trim(),channel_id:f.channel_id.value,enabled:f.enabled.checked,session_ids:ids(f.session_ids.value),item_ids:ids(f.item_ids.value),stop_at:f.stop_at.value ? new Date(f.stop_at.value).toISOString() : null};
      path = "/api/targets"; message = "已儲存監控";
    } else if (form.id === "channel-form") {
      value = {name:f.name.value.trim(),webhook_url:f.webhook_url.value.trim()}; path = "/api/channels"; message = "已儲存頻道";
    } else {
      const n = (name) => Number(f[name].value);
      if (n("normal_max") < n("normal_min") || n("active_max") < n("active_min")) throw new Error("間隔上限不可小於下限。");
      value = {polling:{normal_interval_seconds:[n("normal_min"),n("normal_max")],active_interval_seconds:[n("active_min"),n("active_max")],active_window_seconds:n("active_window"),exit_active_after_no_available_checks:n("exit_checks")},http:{min_request_gap_seconds:n("request_gap"),timeout_seconds:n("timeout")},notifications:{delivery_ttl_seconds:n("notification_ttl"),system_alerts_enabled:f.system_alerts.checked}};
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
setInterval(() => { if (!document.hidden) refresh(); }, 10000);
