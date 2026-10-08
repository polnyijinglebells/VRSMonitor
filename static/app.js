const $ = (selector) => document.querySelector(selector);
let lastStatuses = new Map();
let activeDate = "";
let currentReceivers = [];
let notificationEvents = [];
let viewMode = localStorage.getItem("vrs-view") || "table";
let statusFilter = localStorage.getItem("vrs-filter") || "all";
let sortMode = localStorage.getItem("vrs-sort") || "online_first";

function fmtDuration(seconds) {
  seconds = Math.max(0, Number(seconds || 0));
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor(seconds % 86400 / 3600);
  const minutes = Math.floor(seconds % 3600 / 60);
  const secs = Math.floor(seconds % 60);
  return `${days ? `${days} д ` : ""}${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
}

const fmtNumber = (number) => new Intl.NumberFormat("ru-RU").format(number || 0);
const fmtTime = (timestamp) => timestamp ? new Date(timestamp * 1000).toLocaleString("ru-RU") : "—";
const escapeHtml = (value) => String(value ?? "").replace(/[&<>'"]/g, char => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
}[char]));

async function api(url, options) {
  const response = await fetch(url, options);
  const data = response.headers.get("content-type")?.includes("json") ? await response.json() : null;
  if (!response.ok) throw new Error(data?.error || `HTTP ${response.status}`);
  return data;
}

function statusTitle(status) {
  return status === "online" ? "Работает" : status === "offline" ? "Отключён" : "Ожидание данных";
}

function notifyChange(receiver) {
  const previous = lastStatuses.get(receiver.id);
  if (previous && previous !== receiver.status) {
    const message = receiver.status === "online" ? "Приёмник снова подключён" : "Приёмник отключён";
    showToast(receiver.name, message, receiver.status);
    if ("Notification" in window && Notification.permission === "granted") {
      new Notification(`VRS: ${receiver.name}`, {body: message});
    }
  }
  lastStatuses.set(receiver.id, receiver.status);
}

function showToast(receiverName, message, status) {
  const toast = document.createElement("div");
  toast.className = `toast ${status}`;
  toast.innerHTML = `<i class="toast-dot"></i><div><strong>${escapeHtml(receiverName)}</strong><span>${escapeHtml(message)}</span></div><button class="toast-close" type="button">×</button>`;
  toast.querySelector("button").addEventListener("click", () => toast.remove());
  $("#toastContainer").prepend(toast);
  setTimeout(() => toast.remove(), 12000);
}

function renderNotificationCenter() {
  const list = $("#notificationsList");
  if (!notificationEvents.length) {
    list.innerHTML = '<div class="empty">Уведомлений пока нет</div>';
    return;
  }
  list.innerHTML = notificationEvents.map(event => {
    const offline = event.status === "offline";
    return `<article class="notification-item ${event.status}">
      <div class="notification-icon">${offline ? "!" : "✓"}</div>
      <div class="notification-copy"><strong>${escapeHtml(event.receiver_name)} — ${offline ? "отключён" : "подключён"}</strong><span>${escapeHtml(event.reason || (offline ? "Поток данных остановлен" : "Поток данных восстановлен"))}</span></div>
      <time class="notification-time">${fmtTime(event.at)}</time>
    </article>`;
  }).join("");
}

function updateNotificationBadge() {
  const lastRead = Number(localStorage.getItem("vrs-notifications-read") || 0);
  const unread = notificationEvents.filter(event => event.id > lastRead).length;
  const badge = $("#notificationBadge");
  badge.textContent = unread > 99 ? "99+" : unread;
  badge.classList.toggle("hidden", unread === 0);
}

function openNotificationCenter() {
  renderNotificationCenter();
  $("#notificationsDialog").showModal();
  if (notificationEvents.length) {
    localStorage.setItem("vrs-notifications-read", Math.max(...notificationEvents.map(event => event.id)));
  }
  updateNotificationBadge();
}

function visibleReceivers() {
  const statusRank = {online: 0, unknown: 1, offline: 2};
  const list = currentReceivers.filter(receiver => statusFilter === "all" || receiver.status === statusFilter);
  return list.sort((a, b) => {
    if (sortMode === "online_first") return statusRank[a.status] - statusRank[b.status] || a.name.localeCompare(b.name, "ru");
    if (sortMode === "offline_first") return statusRank[b.status] - statusRank[a.status] || a.name.localeCompare(b.name, "ru");
    if (sortMode === "packet_rate") return b.packet_rate - a.packet_rate || a.name.localeCompare(b.name, "ru");
    if (sortMode === "packets") return b.packets - a.packets || a.name.localeCompare(b.name, "ru");
    return a.name.localeCompare(b.name, "ru");
  });
}

function renderCard(receiver) {
  const durationLabel = receiver.status === "online" ? "Непрерывно работает" : receiver.status === "offline" ? "Отключён уже" : "Состояние уточняется";
  const error = receiver.last_error ? `<p class="card-error" title="${escapeHtml(receiver.last_error)}">${escapeHtml(receiver.last_error)}</p>` : "";
  return `<article class="receiver-card ${receiver.status}">
    <div class="card-head"><div class="identity"><i class="status-dot"></i><div><h3>${escapeHtml(receiver.name)}</h3><span class="status-text">${statusTitle(receiver.status)}</span></div></div><span class="feed-id">FEED ${receiver.feed_id}</span></div>
    <div class="duration"><span>${durationLabel}</span><strong data-duration="${receiver.continuous_seconds}">${fmtDuration(receiver.continuous_seconds)}</strong></div>
    <div class="metrics">
      <div class="metric flow-metric"><span>Поток данных</span><strong>${fmtNumber(receiver.packet_rate)} пак/мин</strong></div>
      <div class="metric"><span>Пакетов за сутки</span><strong>${fmtNumber(receiver.packets)}</strong></div>
      <div class="metric"><span>Работа за сутки</span><strong>${fmtDuration(receiver.online_seconds)}</strong></div>
      <div class="metric"><span>Отключений</span><strong>${receiver.outages}</strong></div>
      <div class="metric"><span>Последний пакет</span><strong>${receiver.last_packet_at ? new Date(receiver.last_packet_at * 1000).toLocaleTimeString("ru-RU") : "—"}</strong></div>
    </div>${error}
  </article>`;
}

function renderTable(receivers) {
  const rows = receivers.map(receiver => `<tr class="${receiver.status}">
    <td class="state-cell"><i class="status-dot"></i>${statusTitle(receiver.status)}</td>
    <td><span class="receiver-name">${escapeHtml(receiver.name)}</span><br><span class="feed-id">FEED ${receiver.feed_id}</span></td>
    <td class="flow ${receiver.packet_rate ? "" : "zero"}">${fmtNumber(receiver.packet_rate)} пак/мин</td>
    <td data-duration="${receiver.continuous_seconds}">${fmtDuration(receiver.continuous_seconds)}</td>
    <td>${fmtNumber(receiver.packets)}</td>
    <td>${fmtDuration(receiver.online_seconds)}</td>
    <td>${receiver.outages}</td>
    <td>${fmtTime(receiver.last_packet_at)}</td>
    <td class="error-cell" title="${escapeHtml(receiver.last_error || "")}">${escapeHtml(receiver.last_error || "—")}</td>
  </tr>`).join("");
  return `<div class="receiver-table-wrap"><table class="receiver-table">
    <thead><tr><th>Состояние</th><th>Приёмник</th><th>Поток данных</th><th>Непрерывно</th><th>Пакетов за сутки</th><th>Работа за сутки</th><th>Отключений</th><th>Последний пакет</th><th>Ошибка</th></tr></thead>
    <tbody>${rows}</tbody>
  </table></div>`;
}

function renderReceivers() {
  const container = $("#receiverGrid");
  const receivers = visibleReceivers();
  container.className = `receiver-list ${viewMode}-mode`;
  $("#tableViewBtn").classList.toggle("active", viewMode === "table");
  $("#cardViewBtn").classList.toggle("active", viewMode === "card");
  if (!receivers.length) {
    container.innerHTML = '<div class="panel empty">Нет приёмников, соответствующих выбранному фильтру.</div>';
    return;
  }
  container.innerHTML = viewMode === "table" ? renderTable(receivers) : receivers.map(renderCard).join("");
}

async function refresh() {
  try {
    const suffix = activeDate ? `?date=${encodeURIComponent(activeDate)}` : "";
    const [data, events, notifications] = await Promise.all([
      api(`/api/status${suffix}`), api(`/api/events${suffix}`), api("/api/notifications?limit=200")
    ]);
    $("#alert").classList.add("hidden");
    if (!activeDate) {
      activeDate = data.shift_date;
      $("#dateSelect").value = activeDate;
    }
    data.receivers.forEach(notifyChange);
    currentReceivers = data.receivers;
    notificationEvents = notifications;
    updateNotificationBadge();
    $("#onlineCount").textContent = data.receivers.filter(receiver => receiver.status === "online").length;
    $("#offlineCount").textContent = data.receivers.filter(receiver => receiver.status === "offline").length;
    $("#packetCount").textContent = fmtNumber(data.receivers.reduce((total, receiver) => total + receiver.packets, 0));
    $("#shiftLabel").textContent = `${fmtTime(data.shift_start)} — ${fmtTime(data.shift_end)}`;
    $("#csvBtn").href = `/api/report.csv?date=${encodeURIComponent(activeDate)}`;
    renderReceivers();
    $("#eventCount").textContent = `${events.length} событий`;
    $("#eventsBody").innerHTML = events.length ? events.map(event => `<tr><td>${fmtTime(event.at)}</td><td>${escapeHtml(event.receiver_name)}</td><td class="event-${event.status}">${event.status === "online" ? "Подключён" : "Отключён"}</td><td>${escapeHtml(event.reason || "—")}</td></tr>`).join("") : '<tr><td colspan="4" class="empty">За выбранные сутки событий нет</td></tr>';
  } catch (error) {
    $("#alert").textContent = `Не удалось обновить данные: ${error.message}`;
    $("#alert").classList.remove("hidden");
  }
}

async function openSettings() {
  try {
    const config = await api("/api/settings");
    const form = $("#settingsForm");
    Object.entries(config).forEach(([key, value]) => {
      if (form.elements[key]) form.elements[key].value = value ?? "";
    });
    $("#settingsDialog").showModal();
  } catch (error) {
    alert(error.message);
  }
}

function setView(mode) {
  viewMode = mode;
  localStorage.setItem("vrs-view", mode);
  renderReceivers();
}

$("#statusFilter").value = statusFilter;
$("#sortSelect").value = sortMode;
$("#statusFilter").addEventListener("change", event => {
  statusFilter = event.target.value;
  localStorage.setItem("vrs-filter", statusFilter);
  renderReceivers();
});
$("#sortSelect").addEventListener("change", event => {
  sortMode = event.target.value;
  localStorage.setItem("vrs-sort", sortMode);
  renderReceivers();
});
$("#tableViewBtn").addEventListener("click", () => setView("table"));
$("#cardViewBtn").addEventListener("click", () => setView("card"));
$("#settingsBtn").addEventListener("click", openSettings);
document.querySelectorAll("[data-close]").forEach(button => button.addEventListener("click", () => button.closest("dialog").close()));
$("#settingsForm").addEventListener("submit", async event => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(event.target).entries());
  for (const key of ["poll_seconds", "offline_after_seconds", "shift_hour"]) values[key] = Number(values[key]);
  try {
    await api("/api/settings", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(values)});
    $("#settingsDialog").close();
    activeDate = "";
    await refresh();
  } catch (error) {
    alert(`Настройки не сохранены: ${error.message}`);
  }
});
$("#checkBtn").addEventListener("click", async () => {
  await api("/api/check", {method: "POST"});
  setTimeout(refresh, 900);
});
$("#notifyBtn").addEventListener("click", openNotificationCenter);
$("#enableBrowserNotifications").addEventListener("click", async () => {
  if (!("Notification" in window)) return alert("Браузер не поддерживает уведомления");
  const result = await Notification.requestPermission();
  alert(result === "granted" ? "Уведомления в браузере включены" : "Браузер не разрешил уведомления");
});
$("#dateSelect").addEventListener("change", event => {
  activeDate = event.target.value;
  refresh();
});
$("#todayBtn").addEventListener("click", () => {
  activeDate = "";
  refresh();
});
$("#printBtn").addEventListener("click", () => window.print());

setInterval(() => {
  $("#clock").textContent = new Date().toLocaleString("ru-RU");
  document.querySelectorAll("[data-duration]").forEach(element => {
    element.dataset.duration = Number(element.dataset.duration) + 1;
    element.textContent = fmtDuration(element.dataset.duration);
  });
}, 1000);
setInterval(refresh, 15000);
refresh();
