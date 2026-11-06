// In-app notification inbox: a bell with an unread badge in the hub header, and a bottom sheet
// listing the player's notifications (deposit/withdrawal results, wins, bonuses, agent messages).
// The server stores them (see src/services/player_notifications.py); this file only displays them.
(() => {
  const tg = window.Telegram?.WebApp;
  const user = tg?.initDataUnsafe?.user;
  if (!user) return;  // outside Telegram there is no verified identity to show an inbox for
  const playerId = `tg_${user.id}`;
  const POLL_MS = 20000;
  const ICONS = {
    deposit_approved: "✅", deposit_rejected: "❌", withdrawal_approved: "💸", withdrawal_rejected: "↩️",
    win: "🏆", deposit_received: "📥", withdrawal_received: "⏳", low_balance: "👛", bonus_granted: "🎁", agent_topup_received: "💰", referral_reward: "🤝", agent_commission_earned: "💼",
  };

  async function api(path, options = {}) {
    const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
    if (tg?.initData) headers["X-Telegram-Init-Data"] = tg.initData;
    const res = await fetch(path, { ...options, headers });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  }

  function timeAgo(iso) {
    const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return new Date(iso).toLocaleDateString();
  }

  // ---- build the UI once the hub header exists
  let badge, sheet, listEl, olderBtn, emptyEl;
  let oldest = null;

  function setBadge(n) {
    if (!badge) return;
    badge.textContent = n > 99 ? "99+" : String(n);
    badge.classList.toggle("hidden", n <= 0);
    // Mirror the unread count onto the footer's Profile tab.
    const tabBadge = document.getElementById("hub-badge-profile");
    if (tabBadge) { tabBadge.textContent = n > 99 ? "99+" : String(n); tabBadge.classList.toggle("hidden", n <= 0); }
  }

  async function refreshCount() {
    try {
      // pollNotice (melbete-core.js) returns the unread count and shows a banner for a fresh message.
      const unread_count = typeof pollNotice === "function"
        ? await pollNotice(playerId, () => openSheet())
        : (await api(`/api/notifications/unread-count?player_id=${encodeURIComponent(playerId)}`)).unread_count;
      setBadge(unread_count);
    } catch { /* offline or not logged in yet: try again next tick */ }
  }

  function renderItem(n, isNew) {
    const li = document.createElement("li");
    li.className = "inbox-item" + (isNew ? " is-new" : "");
    const icon = document.createElement("span");
    icon.className = "inbox-icon";
    icon.textContent = ICONS[n.type] || "🔔";
    const text = document.createElement("div");
    text.className = "inbox-text";
    const title = document.createElement("strong");
    title.textContent = n.title;
    const body = document.createElement("p");
    body.textContent = n.body;
    const when = document.createElement("time");
    when.textContent = timeAgo(n.created_at);
    text.append(title, body, when);
    li.append(icon, text);
    return li;
  }

  async function load(more) {
    const q = new URLSearchParams({ player_id: playerId, limit: "30" });
    if (more && oldest) q.set("before", oldest);
    const { notifications } = await api(`/api/notifications?${q}`);
    if (!more) listEl.replaceChildren();
    notifications.forEach((n) => listEl.append(renderItem(n, !n.read)));
    if (notifications.length) oldest = notifications[notifications.length - 1].created_at;
    olderBtn.classList.toggle("hidden", notifications.length < 30);
    emptyEl.classList.toggle("hidden", listEl.children.length > 0);
  }

  async function openSheet() {
    sheet.classList.remove("hidden");
    document.body.classList.add("inbox-open");
    try {
      await load(false);
      await api("/api/notifications/read", { method: "POST", body: JSON.stringify({ player_id: playerId }) });
      setBadge(0);
    } catch {
      emptyEl.textContent = "Could not load notifications. Try again.";
      emptyEl.classList.remove("hidden");
    }
  }

  function closeSheet() {
    sheet.classList.add("hidden");
    document.body.classList.remove("inbox-open");
  }

  function build() {
    const profileBtn = document.getElementById("header-profile-btn");
    if (!profileBtn || document.getElementById("inbox-btn")) return !!document.getElementById("inbox-btn");
    const actions = document.createElement("div");
    actions.className = "topbar-actions";
    profileBtn.parentNode.insertBefore(actions, profileBtn);

    const btn = document.createElement("button");
    btn.id = "inbox-btn";
    btn.type = "button";
    btn.className = "header-profile-btn inbox-btn";
    btn.setAttribute("aria-label", "Notifications");
    btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M6 9a6 6 0 1112 0c0 5 2 6.5 2 6.5H4S6 14 6 9zM10 19a2 2 0 004 0" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';
    badge = document.createElement("span");
    badge.className = "inbox-badge hidden";
    btn.append(badge);
    actions.append(btn, profileBtn);

    sheet = document.createElement("div");
    sheet.id = "inbox-sheet";
    sheet.className = "inbox-sheet hidden";
    sheet.innerHTML =
      '<div class="inbox-backdrop" data-close></div>' +
      '<section class="inbox-panel" role="dialog" aria-modal="true" aria-label="Notifications">' +
      '<header><h2>Notifications</h2><button type="button" class="inbox-close" data-close aria-label="Close">×</button></header>' +
      '<p class="inbox-empty hidden">No notifications yet.</p><ul class="inbox-list"></ul>' +
      '<button type="button" class="inbox-older hidden">Show older</button>' +
      '<label class="inbox-promos"><input type="checkbox" id="inbox-promos-toggle" checked> Promotional messages (offers, reminders)</label></section>';
    document.body.append(sheet);
    listEl = sheet.querySelector(".inbox-list");
    olderBtn = sheet.querySelector(".inbox-older");
    emptyEl = sheet.querySelector(".inbox-empty");
    sheet.addEventListener("click", (e) => { if (e.target.closest("[data-close]")) closeSheet(); });
    olderBtn.addEventListener("click", () => load(true).catch(() => {}));
    btn.addEventListener("click", openSheet);
    const toggle = sheet.querySelector("#inbox-promos-toggle");
    toggle.addEventListener("change", async () => {
      const want = toggle.checked;
      try {
        await api("/api/notifications/preferences", { method: "POST", body: JSON.stringify({ player_id: playerId, promos: want }) });
      } catch { toggle.checked = !want; }  // the server did not accept it: show the real state
    });
    api(`/api/notifications/preferences?player_id=${encodeURIComponent(playerId)}`)
      .then((r) => { toggle.checked = !!r.promos; }).catch(() => {});
    return true;
  }

  // The hub header is created hidden and revealed after login; wait for it, then start polling.
  let tries = 0;
  const wait = setInterval(() => {
    if (build() || ++tries > 60) {
      clearInterval(wait);
      refreshCount();
      setInterval(() => { if (!document.hidden) refreshCount(); }, POLL_MS);
      document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshCount(); });
    }
  }, 500);
})();
