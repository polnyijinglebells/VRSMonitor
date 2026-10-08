const $ = (s) => document.querySelector(s);
let lastStatuses = new Map();
let activeDate = "";

function fmtDuration(seconds) {
  seconds = Math.max(0, Number(seconds || 0));
  const d = Math.floor(seconds / 86400), h = Math.floor(seconds % 86400 / 3600);
  const m = Math.floor(seconds % 3600 / 60), s = Math.floor(seconds % 60);
  return `${d ? d + " д " : ""}${String(h).padStart(2,"0")}:${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}`;
}
const fmtNumber = (n) => new Intl.NumberFormat("ru-RU").format(n || 0);
const fmtTime = (ts) => ts ? new Date(ts * 1000).toLocaleString("ru-RU") : "—";
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c]));

async function api(url, options) {
  const response = await fetch(url, options);
  const data = response.headers.get("content-type")?.includes("json") ? await response.json() : null;
  if (!response.ok) throw new Error(data?.error || `HTTP ${response.status}`);
  return data;
}

function notifyChange(receiver) {
  const before = lastStatuses.get(receiver.id);
  if (before && before !== receiver.status && Notification.permission === "granted") {
    new Notification(`VRS: ${receiver.name}`, {body: receiver.status === "online" ? "Приёмник снова работает" : "Приёмник отключён"});
  }
  lastStatuses.set(receiver.id, receiver.status);
}

function renderReceivers(receivers) {
  const grid = $("#receiverGrid");
  if (!receivers.length) {
    grid.innerHTML = '<div class="panel empty">Приёмники пока не найдены. Проверьте адрес VRS в настройках.</div>';
    return;
  }
  grid.innerHTML = receivers.map(r => {
    notifyChange(r);
    const title = r.status === "online" ? "Работает" : r.status === "offline" ? "Отключён" : "Ожидание данных";
    const durationLabel = r.status === "online" ? "Непрерывно работает" : r.status === "offline" ? "Отключён уже" : "Состояние уточняется";
    const error = r.last_error ? `<p class="card-error" title="${escapeHtml(r.last_error)}">${escapeHtml(r.last_error)}</p>` : "";
    return `<article class="receiver-card ${r.status}">
      <div class="card-head"><div class="identity"><i class="status-dot"></i><div><h3>${escapeHtml(r.name)}</h3><span class="status-text">${title}</span></div></div><span class="feed-id">FEED ${r.feed_id}</span></div>
      <div class="duration"><span>${durationLabel}</span><strong data-duration="${r.continuous_seconds}">${fmtDuration(r.continuous_seconds)}</strong></div>
      <div class="metrics"><div class="metric"><span>Пакетов за сутки</span><strong>${fmtNumber(r.packets)}</strong></div><div class="metric"><span>Работа за сутки</span><strong>${fmtDuration(r.online_seconds)}</strong></div><div class="metric"><span>Отключений</span><strong>${r.outages}</strong></div><div class="metric"><span>Последний пакет</span><strong>${r.last_packet_at ? new Date(r.last_packet_at*1000).toLocaleTimeString("ru-RU") : "—"}</strong></div></div>${error}
    </article>`;
  }).join("");
}

async function refresh() {
  try {
    const suffix = activeDate ? `?date=${encodeURIComponent(activeDate)}` : "";
    const [data, events] = await Promise.all([api(`/api/status${suffix}`), api(`/api/events${suffix}`)]);
    $("#alert").classList.add("hidden");
    if (!activeDate) { activeDate = data.shift_date; $("#dateSelect").value = activeDate; }
    $("#onlineCount").textContent = data.receivers.filter(r => r.status === "online").length;
    $("#offlineCount").textContent = data.receivers.filter(r => r.status === "offline").length;
    $("#packetCount").textContent = fmtNumber(data.receivers.reduce((n,r) => n + r.packets, 0));
    $("#shiftLabel").textContent = `${fmtTime(data.shift_start)} — ${fmtTime(data.shift_end)}`;
    $("#csvBtn").href = `/api/report.csv?date=${encodeURIComponent(activeDate)}`;
    renderReceivers(data.receivers);
    $("#eventCount").textContent = `${events.length} событий`;
    $("#eventsBody").innerHTML = events.length ? events.map(e => `<tr><td>${fmtTime(e.at)}</td><td>${escapeHtml(e.receiver_name)}</td><td class="event-${e.status}">${e.status === "online" ? "Подключён" : "Отключён"}</td><td>${escapeHtml(e.reason || "—")}</td></tr>`).join("") : '<tr><td colspan="4" class="empty">За выбранные сутки событий нет</td></tr>';
  } catch (error) {
    $("#alert").textContent = `Не удалось обновить данные: ${error.message}`;
    $("#alert").classList.remove("hidden");
  }
}

async function openSettings() {
  try {
    const cfg = await api("/api/settings");
    const form = $("#settingsForm");
    Object.entries(cfg).forEach(([key,value]) => { if (form.elements[key]) form.elements[key].value = value ?? ""; });
    $("#settingsDialog").showModal();
  } catch (e) { alert(e.message); }
}

$("#settingsBtn").addEventListener("click", openSettings);
document.querySelectorAll("[data-close]").forEach(b => b.addEventListener("click", () => $("#settingsDialog").close()));
$("#settingsForm").addEventListener("submit", async e => {
  e.preventDefault();
  const values = Object.fromEntries(new FormData(e.target).entries());
  for (const key of ["poll_seconds","offline_after_seconds","shift_hour"]) values[key] = Number(values[key]);
  try {
    await api("/api/settings", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(values)});
    $("#settingsDialog").close(); activeDate = ""; await refresh();
  } catch (err) { alert(`Настройки не сохранены: ${err.message}`); }
});
$("#checkBtn").addEventListener("click", async () => { await api("/api/check", {method:"POST"}); setTimeout(refresh, 900); });
$("#notifyBtn").addEventListener("click", async () => {
  if (!("Notification" in window)) return alert("Браузер не поддерживает уведомления");
  const result = await Notification.requestPermission();
  alert(result === "granted" ? "Уведомления в браузере включены" : "Браузер не разрешил уведомления");
});
$("#dateSelect").addEventListener("change", e => { activeDate = e.target.value; refresh(); });
$("#todayBtn").addEventListener("click", () => { activeDate = ""; refresh(); });
$("#printBtn").addEventListener("click", () => window.print());
setInterval(() => { $("#clock").textContent = new Date().toLocaleString("ru-RU"); document.querySelectorAll("[data-duration]").forEach(el => { el.dataset.duration = Number(el.dataset.duration)+1; el.textContent=fmtDuration(el.dataset.duration); }); }, 1000);
setInterval(refresh, 15000);
refresh();
