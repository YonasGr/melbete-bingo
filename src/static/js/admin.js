/* ===========================================================================
   Melbete Bingo — Admin Panel
   All /api/admin/* endpoints, request bodies, and the Telegram TOTP login
   flow are unchanged from the previous implementation — only the rendering
   layer and navigation were rebuilt. See README/DESIGN notes in the repo
   for the endpoint contracts this file depends on.
   =========================================================================== */

const tg = window.Telegram?.WebApp;
tg?.ready();
tg?.expand();

const state = {
  isSuperAdmin: false,
  myTelegramId: "",
  perms: new Set(),
  inTelegram: !!tg?.initData,
  currentDepositFilter: "pending",
  currentWithdrawalFilter: "pending",
  currentGameFilter: "all",
  currentLeaderTab: "depositors",
};
window.state = state;

const TAB_TITLES = {
  dashboard: "Dashboard", deposits: "Deposits", withdrawals: "Withdrawals",
  economy: "Economy", reconciliation: "Reconciliation", games: "Games History", users: "User Directory",
  referrals: "Referrals", bonuses: "Bonuses & Cashback", agents: "Agents", leaderboard: "Leaderboard", broadcast: "Broadcast", payments: "Payment Methods",
  admins: "Admins", settings: "System Settings",
};
const TAB_CONTEXT = {
  dashboard: "Live operations and platform health",
  deposits: "Review incoming money and payment evidence",
  withdrawals: "Process payouts and audit completed requests",
  economy: "Wallet liabilities, revenue, and player ledgers",
  reconciliation: "Daily money check: wallet integrity, deposits and withdrawals",
  games: "Monitor and audit Bingo",
  users: "Search players and manage account access",
  referrals: "Invitation performance and bonus distribution",
  bonuses: "Reward switches, amounts, budgets and cashback rules",
  agents: "Reseller float, commission and player top-ups",
  leaderboard: "Campaign dates, rewards, wagering and channel claims",
  broadcast: "Compose and monitor Telegram announcements",
  payments: "Deposit and withdrawal provider configuration",
  admins: "Administrative roles and access control",
  settings: "Bingo rooms, economy, and timing",
};

/* ===========================================================================
   Small utilities
   =========================================================================== */
function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str == null ? "" : String(str);
  return div.innerHTML;
}

function toast(msg, kind = "info") {
  const el = document.getElementById("admin-toast");
  el.textContent = msg;
  el.className = `toast ${kind}`;
  el.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), 3500);
}

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  const res = await fetch(path, { ...options, headers, credentials: "same-origin" });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || "Request failed");
  return data;
}

function timeAgo(isoString) {
  if (!isoString) return "";
  const diff = Math.floor((Date.now() - new Date(isoString).getTime()) / 1000);
  if (diff < 60) return `${diff}s ago`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

function money(n) { return `${Number(n || 0).toLocaleString()} ETB`; }

const STATUS_PILL = {
  pending:  { cls: "waiting",   label: "⏳ Pending" },
  approved: { cls: "approved",  label: "✅ Approved" },
  rejected: { cls: "terminated", label: "❌ Rejected" },
};

function emptyState(icon, text) {
  return `<div class="empty-state"><div class="es-icon">${icon}</div>${escapeHtml(text)}</div>`;
}

/* ===========================================================================
   Reusable "record card" builder — the one list-item look shared by
   deposits, withdrawals, games, users, payment methods, admins & referrals.
   =========================================================================== */
function recordCard({
  id = "", clickable = false, extraAttrs = "",
  statusHtml = "", title = "", amountHtml = "", meta = [], extraHtml = "",
  detailRows = null, actionsHtml = "", decidedHtml = "",
}) {
  const metaHtml = Array.isArray(meta) ? meta.filter(Boolean).join('<span class="dot">·</span>') : meta;
  const details = detailRows && detailRows.length
    ? `<dl class="rc-detail-grid">${detailRows.map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${v ?? "—"}</dd>`).join("")}</dl>`
    : "";
  return `
    <div class="record-card${clickable ? " clickable" : ""}" ${id ? `data-id="${escapeHtml(id)}"` : ""} ${extraAttrs}>
      <div class="rc-top">
        ${statusHtml}
        <div class="rc-title">${title}</div>
        ${amountHtml}
      </div>
      ${metaHtml ? `<div class="rc-meta">${metaHtml}</div>` : ""}
      ${extraHtml}
      ${details}
      ${decidedHtml}
      ${actionsHtml}
    </div>
  `;
}

function tgCopyChip(tgId) {
  if (!tgId) return "";
  return `<span class="tg-copy" data-action="copy-tg" data-tg="${escapeHtml(tgId)}">TG ${escapeHtml(tgId)} 📋</span>`;
}

/* ===========================================================================
   Login
   =========================================================================== */
// Start now / reset lobby / delete room: super admins, or any role holding "games.manage".
const canManageGames = () => state.isSuperAdmin || state.perms.has("games.manage");
// Terminating a running round (refunds every stake): super admins only.
const canTerminateRound = () => !!state.isSuperAdmin;

async function login(totpCode) {
  if (!state.inTelegram || !tg?.initData) throw new Error("The admin panel can only be opened from Telegram.");
  const me = await api("/api/admin/login", {
    method: "POST",
    headers: { "X-Telegram-Init-Data": tg.initData },
    body: JSON.stringify({ totp_code: totpCode }),
  });
  state.isSuperAdmin = me.is_super_admin;
  state.myTelegramId = String(me.telegram_id || "");
  state.perms = new Set(me.permissions || []);
  document.getElementById("login-screen").classList.add("hidden");
  document.getElementById("admin-shell").classList.remove("hidden");

  const badge = document.getElementById("role-badge");
  badge.textContent = state.isSuperAdmin ? "👑 Super Admin" : "Admin";
  badge.classList.toggle("is-super", state.isSuperAdmin);
  const sidebarRole = document.getElementById("sidebar-role");
  if (sidebarRole) sidebarRole.textContent = state.isSuperAdmin ? "Super administrator" : "Administrator";

  document.querySelectorAll(".super-only").forEach((el) => el.classList.toggle("hidden", !state.isSuperAdmin));

  // Load the visible overview first. Other tabs load lazily on first visit
  // so login doesn't fan out a dozen heavy queries that compete with live
  // games for the same API process.
  await LOADERS.dashboard();
  startLiveTabPolling();
}

async function logout() {
  await fetch("/api/admin/logout", { method: "POST", credentials: "same-origin" });
  location.reload();
}

/* ===========================================================================
   Navigation — bottom tab bar + "More" sheet
   =========================================================================== */
const LIVE_TABS = new Set(["dashboard", "games", "economy"]);
const LIVE_TAB_POLL_MS = 8000;
let liveTabPollTimer = null;

const LOADERS = {
  dashboard: loadOverviewStats,
  deposits: () => loadDeposits(),
  withdrawals: () => loadWithdrawals(),
  economy: loadEconomy,
  reconciliation: () => loadReconciliation(),
  games: () => {
    return loadGames();
  },
  users: () => loadUsers(),
  referrals: loadReferrals,
  bonuses: loadBonuses,
  agents: loadAgents,
  leaderboard: loadLeaderboard,
  payments: loadPaymentMethods,
  broadcast: loadBroadcastStatus,
  admins: () => Promise.all([loadAdmins(), loadSuperAdminPanel()]),
  settings: () => loadSettings(),
};


function startLiveTabPolling() {
  clearInterval(liveTabPollTimer);
  liveTabPollTimer = setInterval(() => {
    if (document.hidden) return;
    const activeTab = getActiveTab();
    if (!LIVE_TABS.has(activeTab)) return;
    LOADERS[activeTab]?.().catch(() => {}); // background refresh - a failed tick just retries next interval
  }, LIVE_TAB_POLL_MS);
}

function getActiveTab() {
  return document.querySelector(".tab-panel.active")?.id?.replace("tab-", "") || "dashboard";
}

function setActiveTab(tab) {
  document.querySelectorAll(".admin-nav-item[data-tab]").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  document.querySelectorAll(".bottom-nav-item[data-tab]").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  document.querySelectorAll(".sheet-tile[data-tab]").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${tab}`));
  document.getElementById("topbar-title").textContent = TAB_TITLES[tab] || "Admin";
  const context = document.getElementById("topbar-context");
  if (context) context.textContent = TAB_CONTEXT[tab] || "Melbete administration";
  window.scrollTo({ top: 0, behavior: "instant" in window ? "instant" : "auto" });
}

const loadedTabs = new Set(["dashboard"]);
function goToTab(tab) {
  setActiveTab(tab);
  closeMoreSheet();
  if (LIVE_TABS.has(tab)) {
    LOADERS[tab]?.().catch((err) => toast(err.message || "Failed to load data", "error"));
    return;
  }
  if (loadedTabs.has(tab)) return;
  loadedTabs.add(tab);
  LOADERS[tab]?.().catch((err) => toast(err.message || "Failed to load data", "error"));
}

function wireNav() {
  document.querySelectorAll(".admin-nav-item[data-tab]").forEach((btn) => {
    btn.addEventListener("click", () => goToTab(btn.dataset.tab));
  });
  document.querySelectorAll(".bottom-nav-item[data-tab]").forEach((btn) => {
    btn.addEventListener("click", () => goToTab(btn.dataset.tab));
  });
  document.querySelectorAll(".sheet-tile[data-tab]").forEach((btn) => {
    btn.addEventListener("click", () => goToTab(btn.dataset.tab));
  });

  const overlay = document.getElementById("sheet-overlay");
  const sheet = document.getElementById("more-sheet");
  const moreBtn = document.getElementById("more-nav-btn");
  moreBtn?.addEventListener("click", () => {
    const isOpen = sheet.classList.contains("open");
    if (isOpen) closeMoreSheet(); else openMoreSheet();
  });
  overlay?.addEventListener("click", closeMoreSheet);
}

function openMoreSheet() {
  document.getElementById("sheet-overlay").classList.add("open");
  document.getElementById("more-sheet").classList.add("open");
  document.getElementById("more-nav-btn").classList.add("active");
}
function closeMoreSheet() {
  document.getElementById("sheet-overlay").classList.remove("open");
  document.getElementById("more-sheet").classList.remove("open");
  if (!Object.keys(TAB_TITLES).slice(0, 4).includes(getActiveTab())) {
    // keep the "More" nav item highlighted only while its sheet is open or
    // its own tab is what's currently on screen
  }
  const active = getActiveTab();
  const inPrimary = ["dashboard", "deposits", "withdrawals", "games"].includes(active);
  document.getElementById("more-nav-btn").classList.toggle("active", !inPrimary);
}

function wireTopbarRefresh() {
  document.getElementById("topbar-refresh")?.addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    const tab = getActiveTab();
    const load = LOADERS[tab];
    if (!load || btn.disabled) return;
    btn.disabled = true;
    btn.classList.add("spinning");
    try {
      await load();
      toast(`${TAB_TITLES[tab] || "Screen"} refreshed`, "success");
    } catch (err) {
      toast(err.message || "Failed to refresh data", "error");
    } finally {
      btn.disabled = false;
      btn.classList.remove("spinning");
    }
  });
}

function wireCommandCenter() {
  document.querySelectorAll("[data-jump-tab]").forEach((button) => {
    button.addEventListener("click", () => goToTab(button.dataset.jumpTab));
  });

  const settingsButtons = [...document.querySelectorAll("[data-settings-view]")];
  const settingsPanels = [...document.querySelectorAll("[data-settings-panel]")];
  settingsButtons.forEach((button) => {
    button.addEventListener("click", () => {
      const view = button.dataset.settingsView;
      settingsButtons.forEach((item) => item.classList.toggle("active", item === button));
      settingsPanels.forEach((panel) => panel.classList.toggle("active", panel.dataset.settingsPanel === view));
      document.querySelector("#tab-settings .page-heading")?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  });
}

/* ===========================================================================
   Super Admin panel
   =========================================================================== */
async function loadSuperAdminPanel() {
  if (!state.isSuperAdmin) return;
  const container = document.getElementById("super-admin-panel");
  if (!container) return;
  try {
    const data = await api("/api/admin/admins");
    const admins = data.admins || [];
    const superAdmins = admins.filter((a) => a.is_super_admin);
    const regularAdmins = admins.filter((a) => !a.is_super_admin);

    let html = `<div class="super-admin-card">
      <div class="super-admin-card-header">
        <div class="super-admin-avatar">👑</div>
        <div>
          <strong>${superAdmins.length} Super Admin${superAdmins.length === 1 ? "" : "s"}</strong>
          <div class="muted small">${superAdmins.map((a) => `ID: ${escapeHtml(a.telegram_id)}`).join(", ")}</div>
        </div>
      </div>`;

    if (regularAdmins.length > 0) {
      html += `<div class="super-admin-transfer-section">
        <label class="muted small" for="promote-select">Promote an admin to Super Admin</label>
        <div class="super-admin-transfer-row">
          <select id="promote-select">
            <option value="">— Select admin —</option>
            ${regularAdmins.map((a) => `<option value="${escapeHtml(a.telegram_id)}">${escapeHtml(a.telegram_id)}</option>`).join("")}
          </select>
          <button id="promote-btn" class="transfer-btn">Promote</button>
        </div>
      </div>`;
    } else {
      html += `<p class="muted small super-admin-transfer-section">Add another admin first, then promote them here.</p>`;
    }
    html += `</div>`;
    container.innerHTML = html;

    document.getElementById("promote-btn")?.addEventListener("click", async () => {
      const select = document.getElementById("promote-select");
      const targetId = select.value;
      if (!targetId) { toast("Select an admin first", "error"); return; }
      const btn = document.getElementById("promote-btn");
      btn.disabled = true;
      btn.textContent = "Promoting…";
      try {
        await api("/api/admin/admins/promote", { method: "POST", body: JSON.stringify({ telegram_id: targetId }) });
        toast("Promoted to Super Admin!", "success");
        await refreshAdminScreens();
      } catch (err) {
        toast(err.message, "error");
        btn.disabled = false;
        btn.textContent = "Promote";
      }
    });
  } catch (err) {
    container.innerHTML = `<p class="error-text">Failed to load: ${escapeHtml(err.message)}</p>`;
  }
}

/* ===========================================================================
   Deposits
   =========================================================================== */
async function loadDeposits(filter = state.currentDepositFilter) {
  state.currentDepositFilter = filter;
  const endpoint = filter === "pending" ? "/api/admin/deposits/pending" : `/api/admin/deposits/history?status=${filter}`;
  const { deposits } = await api(endpoint);
  const list = document.getElementById("deposits-list");
  if (!deposits || !deposits.length) {
    list.innerHTML = emptyState("✅", filter === "pending" ? "No pending deposits." : "No deposit history records found.");
    return;
  }
  list.innerHTML = deposits.map((d) => renderDepositCard(d)).join("");
}

function renderDepositCard(d) {
  const st = STATUS_PILL[d.status] || STATUS_PILL.pending;
  const dateStr = new Date(d.created_at).toLocaleString();
  const meta = [
    `${money(d.amount)} via ${escapeHtml(d.payment_method)}`,
    dateStr,
    d.reviewed_by ? `Admin: ${escapeHtml(d.reviewed_by)}` : "",
    d.note ? `Note: ${escapeHtml(d.note)}` : "",
  ];
  let extraHtml = "";
  if (d.payment_verification_url) {
    extraHtml += `<div class="rc-meta">🔗 <a href="${escapeHtml(d.payment_verification_url)}" target="_blank" rel="noopener">${escapeHtml(d.payment_verification_url)}</a></div>`;
  }
  if (d.sms_text) extraHtml += `<div class="sms-block">${escapeHtml(d.sms_text)}</div>`;
  if (d.screenshot_url) {
    extraHtml += `<a href="${escapeHtml(d.screenshot_url)}" target="_blank" rel="noopener"><img class="record-thumb" src="${escapeHtml(d.screenshot_url)}" alt="Payment screenshot" /></a>`;
  }
  const decidedHtml = d.status && d.status !== "pending"
    ? `<div class="decided-note ${d.status === "approved" ? "approved" : "rejected"}">${d.status === "approved" ? "✅ Approved" : "❌ Rejected"}${d.reviewed_by ? ` by ${escapeHtml(d.reviewed_by)}` : ""}</div>`
    : "";
  const actionsHtml = (!d.status || d.status === "pending")
    ? `<div class="rc-actions data-actions">
        <button class="btn btn-approve" data-action="approve-deposit" data-id="${d.id}">✅ Approve</button>
        <button class="btn btn-reject" data-action="reject-deposit" data-id="${d.id}">❌ Reject</button>
      </div>`
    : "";
  return recordCard({
    id: d.id,
    statusHtml: `<span class="pill ${st.cls}">${st.label}</span>`,
    title: escapeHtml(d.player_name),
    meta,
    extraHtml,
    decidedHtml,
    actionsHtml,
  });
}

/* ===========================================================================
   Withdrawals
   =========================================================================== */
async function loadWithdrawals(filter = state.currentWithdrawalFilter) {
  state.currentWithdrawalFilter = filter;
  const endpoint = filter === "pending" ? "/api/admin/withdrawals/pending" : `/api/admin/withdrawals/history?status=${filter}`;
  const { withdrawals, big_threshold: bigThreshold } = await api(endpoint);
  const list = document.getElementById("withdrawals-list");
  const large = filter === "pending" ? (withdrawals || []).filter((w) => w.is_large) : [];
  const banner = large.length
    ? `<div class="recon-banner recon-review">💸 ${large.length} large withdrawal${large.length === 1 ? "" : "s"} waiting (${money(bigThreshold)} or more). Check the payout account before paying.</div>`
    : "";
  if (!withdrawals || !withdrawals.length) {
    list.innerHTML = emptyState("✅", filter === "pending" ? "No pending withdrawals." : "No withdrawal history records found.");
    return;
  }
  list.innerHTML = banner + withdrawals.map((w) => renderWithdrawalCard(w)).join("");
}

function renderWithdrawalCard(w) {
  const st = STATUS_PILL[w.status] || STATUS_PILL.pending;
  const label = st === STATUS_PILL.approved ? "✅ Paid" : st.label;
  const dateStr = new Date(w.created_at).toLocaleString();
  const meta = [
    `${money(w.amount)} via ${escapeHtml(w.payment_method)} → ${escapeHtml(w.withdrawal_address)}`,
    dateStr,
    w.reviewed_by ? `Admin: ${escapeHtml(w.reviewed_by)}` : "",
    w.note ? `Note: ${escapeHtml(w.note)}` : "",
  ];
  const decidedHtml = w.status && w.status !== "pending"
    ? `<div class="decided-note ${w.status === "approved" ? "approved" : "rejected"}">${w.status === "approved" ? "✅ Paid" : "❌ Rejected"}${w.reviewed_by ? ` by ${escapeHtml(w.reviewed_by)}` : ""}</div>`
    : "";
  const actionsHtml = (!w.status || w.status === "pending")
    ? `<div class="rc-actions data-actions">
        <button class="btn btn-approve" data-action="approve-withdrawal" data-id="${w.id}">✅ Mark Paid</button>
        <button class="btn btn-reject" data-action="reject-withdrawal" data-id="${w.id}">❌ Reject</button>
      </div>`
    : "";
  return recordCard({
    id: w.id,
    statusHtml: `<span class="pill ${st.cls}">${label}</span>${w.is_large ? ' <span class="pill pending">Large</span>' : ""}`,
    title: `${escapeHtml(w.legal_name || w.player_name)} <span class="muted small">(${escapeHtml(w.player_name)})</span>`,
    meta,
    decidedHtml,
    actionsHtml,
  });
}

function wireAuditFilterButtons() {
  document.querySelectorAll("[data-deposit-filter]").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("[data-deposit-filter]").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      loadDeposits(btn.dataset.depositFilter).catch((err) => toast(err.message, "error"));
    });
  });
  document.querySelectorAll("[data-withdrawal-filter]").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("[data-withdrawal-filter]").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      loadWithdrawals(btn.dataset.withdrawalFilter).catch((err) => toast(err.message, "error"));
    });
  });
  document.querySelectorAll("[data-game-filter]").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("[data-game-filter]").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      loadGames(btn.dataset.gameFilter).catch((err) => toast(err.message, "error"));
    });
  });


}

/* ===========================================================================
   Dashboard overview
   =========================================================================== */
async function loadOverviewStats() {
  try {
    const s = await api("/api/admin/report");
    renderLiveGameBanner(s);

    const box = document.getElementById("overview-stats");
    const stats = [
      { label: "Pending deposits", value: s.pending_deposits, tab: "deposits", action: "Review requests" },
      { label: "Pending withdrawals", value: s.pending_withdrawals, tab: "withdrawals", action: "Process payouts" },
      { label: "Room state", value: s.room_state.toUpperCase() },
      { label: "Players active", value: s.players_in_round },
      { label: "Current pool", value: money(s.prize_pool) },
      { label: "Registered users", value: s.registered_users },
      { label: "Games (last 200)", value: s.games_played_recent },
      { label: "Total payouts", value: money(s.total_paid_out_recent) },
    ];
    box.innerHTML = stats.map(({ label, value, tab, action }) => tab ? `
      <button class="stat-card overview-action" data-overview-tab="${tab}">
        <div class="stat-label">${label}</div>
        <div class="stat-value">${value}</div>
        <span>${action} →</span>
      </button>
    ` : `
      <div class="stat-card">
        <div class="stat-label">${label}</div>
        <div class="stat-value">${value}</div>
      </div>
    `).join("");
    box.querySelectorAll("[data-overview-tab]").forEach((button) => {
      button.addEventListener("click", () => goToTab(button.dataset.overviewTab));
    });

    renderDashboardRecentGames(s.recent_games_list || []);
  } catch (e) {
    toast("Failed to load overview stats", "error");
  }
}




function renderDashboardRecentGames(games) {
  const el = document.getElementById("recent-games-dashboard");
  if (!el) return;
  if (!games.length) { el.innerHTML = ""; return; }
  const STATE_ICONS = { running: "🟢", waiting: "🟡", ended: "⚪" };
  el.innerHTML = `
    <div class="section-title" style="margin-top:0.4rem;">Recent rounds</div>
    <div class="record-list">
      ${games.map((g) => {
        const icon = STATE_ICONS[g.state] || "⚫";
        const winBadge = g.winners_count > 0 ? `<span class="winner-line">🏆 ${g.winners_count} winner${g.winners_count > 1 ? "s" : ""}</span>` : "";
        return recordCard({
          statusHtml: `<span class="pill ${g.state}">${icon} ${g.state}</span>`,
          title: `${g.players_count} player${g.players_count !== 1 ? "s" : ""} · ${money(g.prize_pool)} pool`,
          meta: [g.created_at ? timeAgo(g.created_at) : ""],
          extraHtml: winBadge,
        });
      }).join("")}
    </div>
  `;
}

function renderLiveGameBanner(report) {
  const el = document.getElementById("live-game-banner");
  if (!el) return;
  const isLive = report.room_state === "running";
  el.innerHTML = `
    <div class="live-game-banner${isLive ? "" : " idle"}">
      <div>
        <div class="lg-title"><span class="pill ${report.room_state}">${report.room_state}</span> Live session</div>
        <div class="lg-meta">${report.players_in_round} cartela(s) in play · ${report.called_numbers} number(s) drawn · Pool: ${money(report.prize_pool)}</div>
      </div>
      ${isLive && canTerminateRound() ? `<button class="btn-terminate" id="terminate-live-btn">⛔ Terminate round</button>` : ""}
    </div>
  `;
  document.getElementById("terminate-live-btn")?.addEventListener("click", (e) => terminateGame(report.room_id, e.currentTarget));
}

async function terminateGame(roomId, btn) {
  if (!roomId) return;
  const reason = prompt(
    "Reason for terminating this round (shown in the audit log and to players)?\n" +
    "All charged stakes will be refunded automatically."
  );
  if (reason === null) return;
  if (!confirm("This will immediately end the running round and refund every player. Continue?")) return;

  btn.disabled = true;
  btn.textContent = "Terminating…";
  try {
    await api(`/api/admin/games/${roomId}/terminate`, { method: "POST", body: JSON.stringify({ reason: reason || "No reason given" }) });
    toast("Round terminated and stakes refunded.", "success");
    await Promise.all([loadOverviewStats(), loadGames()]);
  } catch (e) {
    toast(e.message || "Failed to terminate round", "error");
    btn.disabled = false;
    btn.textContent = "⛔ Terminate round";
  }
}

async function resetLobby(roomId, btn) {
  if (!roomId) return;
  const reason = prompt(
    "Reset this lobby? Every cartela reservation is released and the countdown stops.\n" +
    "Nobody has been charged yet, so no refunds are needed. Reason (optional):"
  );
  if (reason === null) return;
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Resetting…";
  try {
    const res = await api(`/api/admin/games/${roomId}/reset-lobby`, { method: "POST", body: JSON.stringify({ reason }) });
    toast(`Lobby reset (${res.released_cartelas} cartela(s) released).`, "success");
    await Promise.all([loadOverviewStats(), loadGames()]);
  } catch (e) {
    toast(e.message || "Failed to reset lobby", "error");
    btn.disabled = false;
    btn.textContent = label;
  }
}

async function forceStartGame(roomId, btn) {
  if (!roomId) return;
  if (!confirm("Start this round now? Every held cartela is charged immediately and players who cannot pay are dropped.")) return;
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Starting…";
  try {
    const res = await api(`/api/admin/games/${roomId}/force-start`, { method: "POST", body: JSON.stringify({}) });
    toast(`Round started: ${res.cartelas} cartela(s), pool ${money(res.prize_pool)}.`, "success");
    await Promise.all([loadOverviewStats(), loadGames()]);
  } catch (e) {
    toast(e.message || "Failed to start round", "error");
    btn.disabled = false;
    btn.textContent = label;
  }
}

async function deleteGame(roomId, btn) {
  if (!roomId) return;
  if (!confirm("Are you sure you want to permanently delete this game room from the database? This will remove the room and all its reserved cards.")) return;

  const label = btn ? btn.textContent : "";
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Deleting…";
  }
  try {
    await api(`/api/admin/games/${roomId}`, { method: "DELETE" });
    toast("Game room permanently deleted from database.", "success");
    closeGameManageModal();
    await Promise.all([loadOverviewStats(), loadGames()]);
  } catch (e) {
    toast(e.message || "Failed to delete game room", "error");
    if (btn) {
      btn.disabled = false;
      btn.textContent = label || "🗑️ Delete room";
    }
  }
}

async function clearAllWaitingGames() {
  if (!confirm("Are you sure you want to permanently delete ALL waiting game rooms from the database? This cannot be undone.")) return;
  const btn = document.getElementById("btn-clear-waiting-rooms");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Deleting…";
  }
  try {
    const res = await api("/api/admin/games/clear-waiting", { method: "POST", body: JSON.stringify({}) });
    toast(`Successfully deleted ${res.deleted_count || 0} waiting room(s) from database.`, "success");
    await Promise.all([loadOverviewStats(), loadGames()]);
  } catch (e) {
    toast(e.message || "Failed to clear waiting rooms", "error");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "🗑️ Delete all waiting";
    }
  }
}

let _selectedGameForManage = null;

async function openGameManageModal(roomId) {
  const modal = document.getElementById("game-manage-modal");
  if (!modal) return;
  try {
    const data = await api(`/api/admin/games/${roomId}`);
    const g = data.room || data;
    _selectedGameForManage = g;

    document.getElementById("game-modal-title").textContent = `🎲 Manage Room ${String(g.id).slice(0, 8).toUpperCase()}`;
    document.getElementById("gm-room-id").value = g.id;
    const metaEl = document.getElementById("gm-room-meta");
    if (metaEl) {
      metaEl.innerHTML = `<strong>ID:</strong> ${escapeHtml(g.id)} · <strong>Track:</strong> ${escapeHtml(g.track)} · <strong>Created:</strong> ${new Date(g.created_at).toLocaleString()}`;
    }

    document.getElementById("gm-state").value = g.state || "waiting";
    document.getElementById("gm-stake").value = g.stake_amount ?? 10;
    document.getElementById("gm-min-cartelas").value = g.min_cartelas_to_start ?? 1;
    document.getElementById("gm-min-players").value = g.min_players_to_start ?? 1;
    document.getElementById("gm-countdown").value = g.countdown_seconds ?? 30;
    document.getElementById("gm-draw-interval").value = g.draw_interval ?? 2;
    document.getElementById("gm-cards-per-player").value = g.cards_per_player ?? 4;
    document.getElementById("gm-prize-pool").value = g.prize_pool ?? 0;
    document.getElementById("gm-cartela-min").value = g.cartela_min ?? 1;
    document.getElementById("gm-cartela-max").value = g.cartela_max ?? 1000;
    document.getElementById("gm-auto-draw").checked = g.auto_draw !== false;

    const delBtn = document.getElementById("gm-delete-btn");
    if (delBtn) {
      delBtn.style.display = (g.state === "waiting" || g.state === "cancelled") ? "inline-flex" : "none";
    }

    modal.classList.remove("hidden");
  } catch (e) {
    toast(e.message || "Failed to load game room details", "error");
  }
}

function closeGameManageModal() {
  document.getElementById("game-manage-modal")?.classList.add("hidden");
}

function wireGameManageModal() {
  document.getElementById("game-modal-close")?.addEventListener("click", closeGameManageModal);
  document.getElementById("gm-cancel-btn")?.addEventListener("click", closeGameManageModal);
  const modal = document.getElementById("game-manage-modal");
  modal?.addEventListener("click", (e) => { if (e.target === modal) closeGameManageModal(); });

  document.getElementById("btn-clear-waiting-rooms")?.addEventListener("click", clearAllWaitingGames);

  document.getElementById("gm-delete-btn")?.addEventListener("click", () => {
    const roomId = document.getElementById("gm-room-id").value;
    if (roomId) deleteGame(roomId, document.getElementById("gm-delete-btn"));
  });

  document.getElementById("game-manage-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const roomId = document.getElementById("gm-room-id").value;
    if (!roomId) return;

    const payload = {
      state: document.getElementById("gm-state").value,
      stake_amount: parseInt(document.getElementById("gm-stake").value, 10),
      min_cartelas_to_start: parseInt(document.getElementById("gm-min-cartelas").value, 10),
      min_players_to_start: parseInt(document.getElementById("gm-min-players").value, 10),
      countdown_seconds: parseInt(document.getElementById("gm-countdown").value, 10),
      draw_interval: parseInt(document.getElementById("gm-draw-interval").value, 10),
      cards_per_player: parseInt(document.getElementById("gm-cards-per-player").value, 10),
      prize_pool: parseInt(document.getElementById("gm-prize-pool").value, 10),
      cartela_min: parseInt(document.getElementById("gm-cartela-min").value, 10),
      cartela_max: parseInt(document.getElementById("gm-cartela-max").value, 10),
      auto_draw: document.getElementById("gm-auto-draw").checked,
    };

    const submitBtn = e.target.querySelector('button[type="submit"]');
    if (submitBtn) {
      submitBtn.disabled = true;
      submitBtn.textContent = "Saving…";
    }

    try {
      await api(`/api/admin/games/${roomId}`, {
        method: "PATCH",
        body: JSON.stringify(payload),
      });
      toast("Game room stats updated successfully.", "success");
      closeGameManageModal();
      await Promise.all([loadOverviewStats(), loadGames()]);
    } catch (err) {
      toast(err.message || "Failed to update game room stats", "error");
    } finally {
      if (submitBtn) {
        submitBtn.disabled = false;
        submitBtn.textContent = "Save changes";
      }
    }
  });
}

/* ===========================================================================
   Games history
   =========================================================================== */
async function loadGames(filter = state.currentGameFilter) {
  state.currentGameFilter = filter;
  const clearBtn = document.getElementById("btn-clear-waiting-rooms");
  if (clearBtn) {
    clearBtn.classList.toggle("hidden", filter !== "waiting" || !canManageGames());
  }
  const endpoint = filter === "all" ? "/api/admin/games?limit=50" : `/api/admin/games?limit=50&state=${filter}`;
  try {
    const { games } = await api(endpoint);
    const list = document.getElementById("games-list");
    if (!games || !games.length) { list.innerHTML = emptyState("🎲", "No games found under this filter."); return; }
    list.innerHTML = games.map((g) => {
      const stateCls = g.terminated ? "terminated" : g.state;
      const endNote = g.terminated ? ` · ${escapeHtml(g.end_reason)}` : "";
      const detailRows = [
        ["Room ID", `<span class="small">${escapeHtml(g.id)}</span>`],
        ["Variant", escapeHtml(g.variant)],
        ["Stake", money(g.stake_amount)],
        ["Cards/player", g.cards_per_player],
        ["Min cartelas", g.min_cartelas_to_start],
        ["Countdown", `${g.countdown_seconds}s`],
        ["Draw interval", `${g.draw_interval}s`],
        ["Draw pool left", g.draw_pool_remaining],
        ["Cartela range", `${g.cartela_min}–${g.cartela_max}`],
        ["Taken cartelas", escapeHtml((g.taken_cartelas || []).join(", ") || "None")],
        ["Updated", new Date(g.updated_at).toLocaleString()],
      ];
      const winners = (g.winners || []).map((w) =>
        `<div class="winner-line">🏆 <strong>${escapeHtml(w.player_name)}</strong> — Cartela #${w.cartela_number} — ${money(w.payout)}</div>`
      ).join("");
      let actionsHtml = "";
      const manageBtn = canManageGames()
        ? `<button class="btn-manage" data-action="manage-game-stats" data-id="${g.id}">⚙️ Manage stats</button>`
        : "";
      if (g.state === "running" && !g.terminated && canTerminateRound()) {
        actionsHtml = `<div class="rc-actions">${manageBtn}<button class="btn-terminate" data-action="terminate-game" data-id="${g.id}">⛔ Terminate round</button></div>`;
      } else if (g.state === "waiting" && canManageGames()) {
        actionsHtml = `<div class="rc-actions">` +
          manageBtn +
          `<button class="btn-terminate" data-action="force-start-game" data-id="${g.id}">▶️ Start now</button>` +
          `<button class="btn-terminate" data-action="reset-lobby" data-id="${g.id}">🔄 Reset lobby</button>` +
          `<button class="btn-delete" data-action="delete-game" data-id="${g.id}">🗑️ Delete room</button></div>`;
      } else if (canManageGames()) {
        actionsHtml = `<div class="rc-actions">${manageBtn}${g.state === "cancelled" ? `<button class="btn-delete" data-action="delete-game" data-id="${g.id}">🗑️ Delete room</button>` : ""}</div>`;
      }
      return recordCard({
        id: g.id,
        statusHtml: `<span class="pill ${stateCls}">${g.terminated ? "terminated" : g.state}</span>`,
        title: `${g.players_count} cartelas · ${money(g.prize_pool)} pool`,
        meta: [`${g.called_numbers_count} numbers drawn`, new Date(g.created_at).toLocaleString() + endNote],
        extraHtml: winners,
        detailRows,
        actionsHtml,
      });
    }).join("");
  } catch (e) {
    toast("Failed to load games", "error");
  }
}



/* ===========================================================================
   Users directory
   =========================================================================== */

let _allUsers = [];       // last unfiltered page - only used to restore the view when search is cleared
let _lastRenderedUsers = []; // whatever is ACTUALLY on screen right now (filtered or not)
let _selectedUserForManage = null;

async function loadUsers(searchQuery = "") {
  try {
    const url = searchQuery.trim()
      ? `/api/admin/users?search=${encodeURIComponent(searchQuery.trim())}`
      : "/api/admin/users?limit=200";
    const { users } = await api(url);
    if (!searchQuery.trim()) _allUsers = users;
    renderUsers(users);

    const search = document.getElementById("users-search");
    if (search && !search._wired) {
      search._wired = true;
      let debounceTimer;
      search.addEventListener("input", () => {
        clearTimeout(debounceTimer);
        const q = search.value.trim();
        if (!q) { renderUsers(_allUsers); return; }
        debounceTimer = setTimeout(() => loadUsers(q), 350);
      });
    }
  } catch (e) {
    toast("Failed to load users", "error");
  }
}

function renderUsers(users) {
  // Always keep this in sync with what's actually rendered - search
  // results included - so Manage buttons for those rows can be found.
  _lastRenderedUsers = users || [];
  const list = document.getElementById("users-list");
  if (!users || !users.length) { list.innerHTML = emptyState("👥", "No registered users yet."); return; }
  list.innerHTML = users.map((u) => {
    const banPill = u.is_banned ? `<span class="pill terminated">Suspended</span>` : `<span class="pill approved">Active</span>`;
    const meta = [
      u.phone_number || "No phone",
      `Joined ${new Date(u.created_at).toLocaleDateString()}`,
    ];
    const balances = `<div class="rc-meta">
      <span class="rc-amount pos">Real ${money(u.balance)}</span> ·
      <span class="rc-amount" style="color:var(--gold)">Won ${money(u.won_balance)}</span> ·
      <span class="rc-amount" style="color:var(--accent)">Bonus ${money(u.bonus_balance)}</span>
    </div>`;
    return recordCard({
      id: u.id,
      statusHtml: banPill,
      title: `${escapeHtml(u.display_name)} ${tgCopyChip(u.telegram_id)}`,
      meta,
      extraHtml: balances,
      actionsHtml: `<div class="rc-actions"><button class="btn btn-ghost" data-action="manage-user" data-id="${u.id}">⚙️ Manage</button></div>`,
    });
  }).join("");
}

function findUserById(id) {
  return _lastRenderedUsers.find((u) => u.id === id) || _allUsers.find((u) => u.id === id);
}

function openUserManageModal(user) {
  _selectedUserForManage = user;
  const modal = document.getElementById("user-manage-modal");
  if (!modal) return;
  document.getElementById("user-modal-title").textContent = `👤 ${user.display_name}`;
  document.getElementById("um-reset-player-id").value = user.id;
  switchUserModalTab("overview");
  renderUserManageOverview(user);
  populateUserResetForm(user);
  modal.classList.remove("hidden");
}
function closeUserManageModal() { document.getElementById("user-manage-modal")?.classList.add("hidden"); }

function kv(key, valueHtml) {
  return `<div class="kv"><span class="kv-key">${escapeHtml(key)}</span><span class="kv-val">${valueHtml}</span></div>`;
}

function renderUserManageOverview(u) {
  const container = document.getElementById("um-player-details");
  if (!container) return;
  const accountSummary = Object.entries(u.withdrawal_accounts || {}).map(([m, addr]) => `${m}: ${addr}`).join(", ");
  const banPill = u.is_banned
    ? `<span class="pill terminated">Suspended${u.ban_reason ? ` — ${escapeHtml(u.ban_reason)}` : ""}</span>`
    : `<span class="pill approved">Active</span>`;

  container.innerHTML = [
    kv("Telegram ID", tgCopyChip(u.telegram_id)),
    kv("Player ID", `<span class="small">${escapeHtml(u.id)}</span>`),
    kv("Phone", escapeHtml(u.phone_number || "Not set")),
    kv("Status", banPill),
    kv("Registration", escapeHtml(u.registration_state)),
    kv("Real balance", `<span style="color:var(--pos)">${money(u.balance)}</span>`),
    kv("Won balance", `<span style="color:var(--gold)">${money(u.won_balance)}</span>`),
    kv("Bonus balance", `<span style="color:var(--accent)">${money(u.bonus_balance)}</span>`),
    kv("Lifetime deposited", money(u.lifetime_deposited)),
    kv("Auto bingo", u.auto_bingo_enabled ? "Enabled" : "Off"),
    kv("Referrals made", `${u.referral_count} invites`),
    kv("Referred by", escapeHtml(u.referred_by || "None")),
    kv("Withdrawal accounts", escapeHtml(accountSummary || "None set")),
    kv("Joined", new Date(u.created_at).toLocaleString()),
  ].join("");

  const actions = document.getElementById("um-action-buttons");
  if (!actions) return;
  actions.innerHTML = "";

  if (state.isSuperAdmin) {
    const banBtn = document.createElement("button");
    banBtn.className = u.is_banned ? "btn btn-approve" : "btn btn-reject";
    banBtn.textContent = u.is_banned ? "✅ Lift suspension" : "🚫 Suspend account";
    banBtn.addEventListener("click", async () => {
      if (u.is_banned) {
        await setUserBan(u.id, false, null, banBtn);
        u.is_banned = false;
      } else {
        const reason = prompt('Reason for suspending this user? (e.g. "fake deposit screenshot")');
        if (reason === null) return;
        await setUserBan(u.id, true, reason || "Suspended by admin", banBtn);
        u.is_banned = true;
        u.ban_reason = reason || "Suspended by admin";
      }
      renderUserManageOverview(u);
    });
    actions.appendChild(banBtn);
  }

  const resetTabBtn = document.createElement("button");
  resetTabBtn.className = "primary-btn";
  resetTabBtn.textContent = "💰 Adjust balances";
  resetTabBtn.addEventListener("click", () => switchUserModalTab("reset-balance"));
  actions.appendChild(resetTabBtn);

  const depHistBtn = document.createElement("button");
  depHistBtn.className = "btn btn-ghost";
  depHistBtn.textContent = "💳 Deposit history";
  depHistBtn.addEventListener("click", () => switchUserModalTab("deposits"));
  actions.appendChild(depHistBtn);

  const wdHistBtn = document.createElement("button");
  wdHistBtn.className = "btn btn-ghost";
  wdHistBtn.textContent = "🏧 Withdrawal history";
  wdHistBtn.addEventListener("click", () => switchUserModalTab("withdrawals"));
  actions.appendChild(wdHistBtn);
}

function populateUserResetForm(u) {
  const cur = document.getElementById("um-current-balances");
  if (cur) cur.textContent = `Now: real ${money(u.balance)} · winnings ${money(u.won_balance)} · bonus ${money(u.bonus_balance)}`;
  document.getElementById("um-adjust-kind").value = "real";
  document.getElementById("um-adjust-delta").value = "";
  document.getElementById("um-reset-note").value = "";
}

function switchUserModalTab(tabName) {
  document.querySelectorAll(".user-modal-tab").forEach((t) => t.classList.toggle("active", t.dataset.userTab === tabName));
  document.querySelectorAll(".um-tab-panel").forEach((p) => p.classList.toggle("active", p.id === `um-tab-${tabName}`));
  if (tabName === "deposits" && _selectedUserForManage) loadUserDepositHistory(_selectedUserForManage.id);
  else if (tabName === "withdrawals" && _selectedUserForManage) loadUserWithdrawalHistory(_selectedUserForManage.id);
}

async function loadUserDepositHistory(playerId) {
  const container = document.getElementById("um-deposits-list");
  if (!container) return;
  container.innerHTML = `<p class="muted">Loading…</p>`;
  try {
    const data = await api(`/api/admin/economy/user/${encodeURIComponent(playerId)}`);
    const topups = (data.transactions || []).filter((t) => t.type.startsWith("top_up"));
    if (!topups.length) { container.innerHTML = emptyState("💳", "No deposit records for this player."); return; }
    container.innerHTML = topups.map((d) => {
      const isApp = d.type === "top_up_approved";
      const isRej = d.type === "top_up_rejected";
      const pill = isApp ? `<span class="pill approved">✅ Approved</span>` : (isRej ? `<span class="pill terminated">❌ Rejected</span>` : `<span class="pill waiting">⏳ Pending</span>`);
      const date = d.created_at ? new Date(d.created_at).toLocaleString() : "—";
      return recordCard({
        statusHtml: pill,
        title: `<span class="rc-amount pos">+${money(d.amount)}</span> via ${escapeHtml(d.payment_method || "Payment")}`,
        meta: [date, d.admin_id ? `Admin: ${escapeHtml(d.admin_id)}` : "", d.note ? escapeHtml(d.note) : ""],
      });
    }).join("");
  } catch (e) {
    container.innerHTML = `<p class="muted">Failed to load deposit history: ${escapeHtml(e.message)}</p>`;
  }
}

async function loadUserWithdrawalHistory(playerId) {
  const container = document.getElementById("um-withdrawals-list");
  if (!container) return;
  container.innerHTML = `<p class="muted">Loading…</p>`;
  try {
    const data = await api(`/api/admin/economy/user/${encodeURIComponent(playerId)}`);
    const withdrawals = (data.transactions || []).filter((t) => t.type.startsWith("withdrawal"));
    if (!withdrawals.length) { container.innerHTML = emptyState("🏧", "No withdrawal records for this player."); return; }
    container.innerHTML = withdrawals.map((w) => {
      const isApp = w.type === "withdrawal_approved";
      const isRej = w.type === "withdrawal_rejected";
      const pill = isApp ? `<span class="pill approved">✅ Paid</span>` : (isRej ? `<span class="pill terminated">❌ Rejected</span>` : `<span class="pill waiting">⏳ Pending</span>`);
      const date = w.created_at ? new Date(w.created_at).toLocaleString() : "—";
      return recordCard({
        statusHtml: pill,
        title: `<span class="rc-amount neg">−${money(Math.abs(w.amount))}</span> via ${escapeHtml(w.payment_method || "Payment")}`,
        meta: [date, w.admin_id ? `Admin: ${escapeHtml(w.admin_id)}` : "", w.note ? escapeHtml(w.note) : ""],
      });
    }).join("");
  } catch (e) {
    container.innerHTML = `<p class="muted">Failed to load withdrawal history: ${escapeHtml(e.message)}</p>`;
  }
}

async function setUserBan(playerId, banned, reason, btn) {
  btn.disabled = true;
  try {
    if (banned) {
      await api(`/api/admin/users/${playerId}/ban`, { method: "POST", body: JSON.stringify({ reason }) });
      toast("User suspended.", "success");
    } else {
      await api(`/api/admin/users/${playerId}/unban`, { method: "POST" });
      toast("User unbanned.", "success");
    }
    await loadUsers();
  } catch (e) {
    toast(e.message || "Failed to update user", "error");
    btn.disabled = false;
  }
}

function wireUserManageModal() {
  document.getElementById("user-modal-close")?.addEventListener("click", closeUserManageModal);
  const modal = document.getElementById("user-manage-modal");
  modal?.addEventListener("click", (e) => { if (e.target === modal) closeUserManageModal(); });
  document.querySelectorAll(".user-modal-tab").forEach((tab) => {
    tab.addEventListener("click", () => switchUserModalTab(tab.dataset.userTab));
  });

  document.getElementById("um-reset-form")?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const playerId = document.getElementById("um-reset-player-id").value;
    const kind = document.getElementById("um-adjust-kind").value;
    const delta = parseInt(document.getElementById("um-adjust-delta").value, 10);
    const note = document.getElementById("um-reset-note").value.trim();
    const submitBtn = e.target.querySelector('button[type="submit"]');
    if (submitBtn) submitBtn.disabled = true;
    try {
      if (!Number.isInteger(delta) || delta === 0) throw new Error("Enter a non-zero whole amount (negative to remove).");
      if (note.length < 3) throw new Error("A reason of at least 3 characters is required.");
      const res = await api(`/api/admin/users/${encodeURIComponent(playerId)}/wallet-adjust`, {
        method: "POST",
        body: JSON.stringify({ kind, delta, reason: note }),
      });
      toast("Wallet adjusted.", "success");
      if (_selectedUserForManage && _selectedUserForManage.id === playerId) {
        _selectedUserForManage.balance = res.balance;
        _selectedUserForManage.won_balance = res.won_balance;
        _selectedUserForManage.bonus_balance = res.bonus_balance;
        renderUserManageOverview(_selectedUserForManage);
        populateUserResetForm(_selectedUserForManage);
      }
      await loadUsers();
      switchUserModalTab("overview");
    } catch (err) {
      toast(err.message || "Failed to adjust wallet", "error");
    } finally {
      if (submitBtn) submitBtn.disabled = false;
    }
  });
}

/* ===========================================================================
   Payment methods
   =========================================================================== */
let _paymentMethods = [];

async function loadPaymentMethods() {
  const { methods } = await api("/api/admin/payment-methods");
  _paymentMethods = methods;
  const list = document.getElementById("payment-methods-list");
  if (!methods.length) { list.innerHTML = emptyState("⚙️", "No payment methods configured yet."); return; }
  list.innerHTML = methods.map((m) => recordCard({
    id: m.id,
    statusHtml: `<span class="pill ${m.active ? "approved" : "neutral"}">${m.active ? "Active" : "Inactive"}</span>`,
    title: `${escapeHtml(m.name)} <span class="muted small">(${escapeHtml(m.key)})</span>`,
    meta: [
      `${escapeHtml(m.account_info)} · ${escapeHtml(m.account_holder || "No holder set")}`,
      `Deposit ${m.supports_deposit ? "✓" : "✗"}`,
      `Withdrawal ${m.supports_withdrawal ? "✓" : "✗"}`,
    ],
    actionsHtml: `<div class="rc-actions">
      <button class="btn btn-approve" data-action="edit-method" data-id="${m.id}">Edit</button>
      <button class="btn btn-delete" data-action="delete-method" data-id="${m.id}">Delete</button>
    </div>`,
  })).join("");
}

document.getElementById("payment-method-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/admin/payment-methods", {
      method: "POST",
      body: JSON.stringify({
        id: document.getElementById("pm-id").value || undefined,
        key: document.getElementById("pm-key").value.trim(),
        name: document.getElementById("pm-name").value.trim(),
        account_info: document.getElementById("pm-account").value.trim(),
        account_holder: document.getElementById("pm-holder").value.trim() || null,
        instructions: document.getElementById("pm-instructions").value.trim() || null,
        active: document.getElementById("pm-active").checked,
        supports_deposit: document.getElementById("pm-deposit").checked,
        supports_withdrawal: document.getElementById("pm-withdrawal").checked,
      }),
    });
    toast("Payment method saved", "success");
    e.target.reset();
    document.getElementById("pm-id").value = "";
    await loadPaymentMethods();
  } catch (err) {
    toast(err.message, "error");
  }
});

/* ===========================================================================
   Admins
   =========================================================================== */
let _adminRoles = [];

function adminRoleLabel(a) {
  if (a.is_super_admin) return "Super Admin";
  const r = _adminRoles.find((x) => x.key === a.role);
  return r ? r.label : (a.role || "Admin");
}

function adminRoleOptions(selected) {
  return _adminRoles.map((r) => `<option value="${escapeHtml(r.key)}"${r.key === selected ? " selected" : ""}>${escapeHtml(r.label)}</option>`).join("");
}

async function loadAdmins() {
  const { admins, roles } = await api("/api/admin/admins");
  _adminRoles = roles || [];
  const addRole = document.getElementById("new-admin-role");
  if (addRole && !addRole.options.length) addRole.innerHTML = adminRoleOptions("admin");
  const list = document.getElementById("admins-list");
  if (!admins.length) { list.innerHTML = emptyState("🛡️", "No admins yet."); return; }
  list.innerHTML = admins.map((a) => {
    const badges = [a.is_you ? "You" : "", a.is_owner ? "Owner" : ""].filter(Boolean).map((t) => ` <span class="pill">${t}</span>`).join("");
    let actions = "";
    if (state.isSuperAdmin && !a.is_you && !a.is_owner) {
      if (a.is_super_admin) {
        actions = `<div class="rc-actions">
          <select data-role-select aria-label="Role after demotion">${adminRoleOptions("admin")}</select>
          <button class="btn btn-reject" data-action="demote-admin" data-id="${escapeHtml(a.telegram_id)}">Demote</button>
          <button class="btn btn-delete" data-action="remove-admin" data-id="${escapeHtml(a.telegram_id)}">Remove</button>
        </div>`;
      } else {
        actions = `<div class="rc-actions">
          <select data-role-select aria-label="Role">${adminRoleOptions(a.role)}</select>
          <button class="btn" data-action="set-admin-role" data-id="${escapeHtml(a.telegram_id)}">Save role</button>
          <button class="btn btn-approve" data-action="promote-admin" data-id="${escapeHtml(a.telegram_id)}">Make super admin</button>
          <button class="btn btn-delete" data-action="remove-admin" data-id="${escapeHtml(a.telegram_id)}">Remove</button>
        </div>`;
      }
    }
    return recordCard({
      id: a.telegram_id,
      title: `${escapeHtml(a.telegram_id)} ${a.is_super_admin ? "👑" : ""}${badges}`,
      meta: [adminRoleLabel(a), a.added_by ? `Added by ${escapeHtml(a.added_by)}` : ""],
      actionsHtml: actions,
    });
  }).join("");
}

async function refreshAdminScreens() {
  await Promise.all([loadAdmins(), loadSuperAdminPanel()]);
}

async function runAdminAction(btn, fn) {
  btn.disabled = true;
  try { await fn(); } catch (err) { toast(err.message, "error"); } finally { btn.disabled = false; }
}

async function adminStaffAction(action, id, btn) {
  const card = btn.closest(".record-card");
  const role = card?.querySelector("select[data-role-select]")?.value || "admin";
  if (action === "promote-admin") {
    if (!window.confirm(`Make ${id} a super admin? They will be able to manage staff, settings and money.`)) return;
    await runAdminAction(btn, async () => {
      await api("/api/admin/admins/promote", { method: "POST", body: JSON.stringify({ telegram_id: id }) });
      toast("Promoted to Super Admin", "success"); await refreshAdminScreens();
    });
  } else if (action === "demote-admin") {
    if (!window.confirm(`Demote ${id} from super admin to a regular role? They keep panel access with that role.`)) return;
    await runAdminAction(btn, async () => {
      await api(`/api/admin/admins/${encodeURIComponent(id)}/demote`, { method: "POST", body: JSON.stringify({ role }) });
      toast("Demoted", "success"); await refreshAdminScreens();
    });
  } else if (action === "set-admin-role") {
    await runAdminAction(btn, async () => {
      await api(`/api/admin/admins/${encodeURIComponent(id)}/role`, { method: "PUT", body: JSON.stringify({ role }) });
      toast("Role updated", "success"); await refreshAdminScreens();
    });
  } else if (action === "remove-admin") {
    if (!window.confirm(`Remove ${id} from the staff? They lose panel access immediately.`)) return;
    await runAdminAction(btn, async () => {
      await api(`/api/admin/admins/${encodeURIComponent(id)}`, { method: "DELETE" });
      toast("Admin removed", "success"); await refreshAdminScreens();
    });
  }
}

document.getElementById("add-admin-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = document.getElementById("new-admin-id").value.trim();
  const role = document.getElementById("new-admin-role")?.value || "admin";
  try {
    await api("/api/admin/admins", { method: "POST", body: JSON.stringify({ telegram_id: id, role }) });
    toast("Admin added", "success");
    e.target.reset();
    await refreshAdminScreens();
  } catch (err) {
    toast(err.message, "error");
  }
});

/* ===========================================================================
   Agents (resellers)
   =========================================================================== */
async function loadAgents() {
  const list = document.getElementById("agents-list");
  loadAgentRewards();  // independent section: must load even when there are no agents yet
  loadAgentApplications();
  try {
    const { agents } = await api("/api/admin/agents");
    if (!agents.length) { list.innerHTML = emptyState("⚑", "No agents yet."); return; }
    list.innerHTML = agents.map((a) => recordCard({
      id: a.id,
      title: `${escapeHtml(a.player_id.replace("tg_", ""))} <span class="muted small">${escapeHtml(a.agent_code)}</span>`,
      statusHtml: `<span class="status-pill">${a.status === "active" ? "Active" : "Suspended"}</span>`,
      meta: [`Float ${money(a.float_balance)}`, `Commission ${money(a.commission_balance)} (${a.commission_percent}%)`, `${a.player_count} players`, a.tier_label ? `Tier ${escapeHtml(a.tier_label)} ${a.tier_bps / 100}%` : "", a.rewards_enabled === false ? "Rewards off" : "Rewards on"],
      extraHtml: `<div id="agent-ledger-${escapeHtml(a.id)}" class="muted small"></div>`,
      actionsHtml: `<div class="rc-actions">
        <button class="btn" data-action="agent-float" data-id="${a.id}">Adjust float</button>
        <button class="btn" data-action="agent-ledger" data-id="${a.id}">Ledger</button>
        <button class="btn" data-action="agent-commission" data-id="${a.id}" data-rate="${a.commission_percent}">Edit commission</button>
        <button class="btn" data-action="agent-rewards" data-id="${a.id}" data-on="${a.rewards_enabled === false ? "0" : "1"}">${a.rewards_enabled === false ? "Turn rewards on" : "Turn rewards off"}</button>
        <button class="btn btn-delete" data-action="${a.status === "active" ? "agent-suspend" : "agent-activate"}" data-id="${a.id}">${a.status === "active" ? "Suspend" : "Activate"}</button>
      </div>`,
    })).join("");
  } catch (err) {
    list.innerHTML = `<p class="muted">Could not load agents: ${escapeHtml(err.message)}</p>`;
  }
}

async function loadAgentApplications() {
  const list = document.getElementById("agent-applications");
  const count = document.getElementById("agent-applications-count");
  if (!list) return;
  try {
    const { applications } = await api("/api/admin/agent-applications?status=pending");
    if (count) count.textContent = applications.length ? String(applications.length) : "";
    if (!applications.length) { list.innerHTML = emptyState("⚑", "No requests waiting."); return; }
    list.innerHTML = applications.map((a) => recordCard({
      id: a.id,
      title: `${escapeHtml(a.display_name || a.telegram_id)} <span class="muted small">${escapeHtml(a.telegram_id)}</span>`,
      statusHtml: `<span class="status-pill">Pending</span>`,
      meta: [a.phone_number ? escapeHtml(a.phone_number) : "", `Deposited ${money(a.lifetime_deposited)}`, a.is_banned ? "Banned" : "", escapeHtml(new Date(a.created_at).toLocaleString())],
      extraHtml: (a.channel_link
          ? `<div class="small"><strong>Telegram:</strong> <a href="${escapeHtml(a.channel_link)}" target="_blank" rel="noopener noreferrer">${escapeHtml(a.channel_link)}</a></div>`
          : `<div class="muted small">No Telegram link submitted (older request)</div>`) +
        (a.note ? `<div class="muted small">“${escapeHtml(a.note)}”</div>` : ""),
      actionsHtml: `<div class="rc-actions">
        <button class="btn btn-approve" data-action="agent-app-approve" data-id="${a.id}">Approve</button>
        <button class="btn btn-delete" data-action="agent-app-reject" data-id="${a.id}">Reject</button>
      </div>`,
    })).join("");
  } catch (err) {
    list.innerHTML = `<p class="muted">Could not load requests: ${escapeHtml(err.message)}</p>`;
  }
}

async function reviewAgentApplication(id, approve) {
  let body = {};
  if (approve) {
    const raw = window.prompt("Commission % for this agent (0 to 30):", "3");
    if (raw === null) return false;
    const pct = Number(raw);
    if (raw.trim() === "" || !Number.isFinite(pct) || pct < 0 || pct > 30) { toast("Commission must be a number from 0 to 30", "error"); return false; }
    body = { commission_percent: pct };
  } else {
    const note = window.prompt("Reason shown to the player (optional):", "");
    if (note === null) return false;
    body = { note: note.trim() };
  }
  await api(`/api/admin/agent-applications/${id}/${approve ? "approve" : "reject"}`, { method: "POST", body: JSON.stringify(body) });
  toast(approve ? "Agent approved" : "Request rejected", "success");
  await loadAgents();
  refreshAttention();
  return true;
}

async function editAgentCommission(id, current) {
  const raw = window.prompt("New base commission % (0 to 30). Monthly tiers can still pay more:", String(current ?? 3));
  if (raw === null) return;
  const pct = Number(raw);
  if (raw.trim() === "" || !Number.isFinite(pct) || pct < 0 || pct > 30) { toast("Commission must be a number from 0 to 30", "error"); return; }
  await api(`/api/admin/agents/${id}/commission`, { method: "PUT", body: JSON.stringify({ commission_percent: pct }) });
  toast("Commission updated", "success");
  await loadAgents();
}

// Register-an-agent: find the player by name, phone or Telegram ID instead of typing a raw ID.
let agentSearchTimer = null;
document.getElementById("new-agent-search")?.addEventListener("input", (e) => {
  clearTimeout(agentSearchTimer);
  const q = e.target.value.trim();
  const box = document.getElementById("new-agent-results");
  if (q.length < 2) { box.innerHTML = ""; return; }
  agentSearchTimer = setTimeout(async () => {
    try {
      const { users } = await api(`/api/admin/users?search=${encodeURIComponent(q)}`);
      const rows = users.filter((u) => !u.is_banned).slice(0, 6);
      box.innerHTML = rows.length
        ? rows.map((u) => `<button type="button" class="player-pick" data-tg="${escapeHtml(u.telegram_id)}"><b>${escapeHtml(u.display_name || u.telegram_id)}</b><span class="muted small">${escapeHtml(u.telegram_id)}${u.phone_number ? " · " + escapeHtml(u.phone_number) : ""}</span></button>`).join("")
        : `<p class="muted small">No matching player. They must open the bot once first.</p>`;
    } catch (err) { box.innerHTML = `<p class="muted small">${escapeHtml(err.message)}</p>`; }
  }, 300);
});
document.getElementById("new-agent-results")?.addEventListener("click", (e) => {
  const btn = e.target.closest(".player-pick");
  if (!btn) return;
  document.getElementById("new-agent-id").value = btn.dataset.tg;
  document.getElementById("new-agent-results").innerHTML = `<p class="muted small">Selected ${escapeHtml(btn.querySelector("b").textContent)} (${escapeHtml(btn.dataset.tg)})</p>`;
});

document.getElementById("add-agent-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/admin/agents", { method: "POST", body: JSON.stringify({
      telegram_id: document.getElementById("new-agent-id").value.trim(),
      commission_percent: document.getElementById("new-agent-commission").value.trim(),
    }) });
    toast("Agent registered", "success");
    e.target.reset();
    document.getElementById("new-agent-commission").value = "3";
    await loadAgents();
  } catch (err) {
    toast(err.message, "error");
  }
});

async function adjustAgentFloat(id) {
  const raw = window.prompt("Float change in ETB (positive funds the agent, negative takes float back):");
  if (raw === null) return;
  const amount = Number(raw);
  if (!Number.isInteger(amount) || amount === 0) { toast("Enter a non-zero whole number", "error"); return; }
  const note = window.prompt("Reference / note (e.g. Telebirr transaction ID):") || "";
  if (!window.confirm(`${amount > 0 ? "Add" : "Remove"} ${Math.abs(amount)} ETB ${amount > 0 ? "to" : "from"} this agent's float?`)) return;
  const key = `agent-float-${id}-${crypto.randomUUID()}`;
  await api(`/api/admin/agents/${id}/float`, { method: "POST", headers: { "Idempotency-Key": key }, body: JSON.stringify({ amount, note }) });
  toast("Float updated", "success");
  await loadAgents();
}

async function toggleAgentLedger(id) {
  const box = document.getElementById(`agent-ledger-${id}`);
  if (!box) return;
  if (box.dataset.open === "1") { box.innerHTML = ""; box.dataset.open = ""; return; }
  const { ledger } = await api(`/api/admin/agents/${id}/ledger`);
  box.dataset.open = "1";
  box.innerHTML = ledger.length
    ? ledger.map((e) => `<div>${new Date(e.created_at).toLocaleString()} · ${escapeHtml(e.type)} · float ${e.float_delta >= 0 ? "+" : ""}${e.float_delta} → ${e.float_after} · commission ${e.commission_delta >= 0 ? "+" : ""}${e.commission_delta}${e.player_id ? ` · ${escapeHtml(e.player_id.replace("tg_", ""))}` : ""}${e.note ? ` · ${escapeHtml(e.note)}` : ""}</div>`).join("")
    : "No ledger entries yet.";
}

/* ---- Abuse review: related accounts ------------------------------------------------------------- */
const ABUSE_GROUPS = [
  ["shared_ip", "Same connection", "Several accounts made money requests from one connection"],
  ["duplicate_phone", "Same phone number", "Registered before the one-account-per-phone rule"],
  ["shared_payout_account", "Same payout account", "Different players withdrew to the same account"],
];

document.getElementById("abuse-run")?.addEventListener("click", async () => {
  const box = document.getElementById("abuse-report");
  box.textContent = "Checking…";
  try {
    const r = await api("/api/admin/abuse");
    const html = ABUSE_GROUPS.map(([key, title, hint]) => {
      const clusters = r[key] || [];
      if (!clusters.length) return `<div><strong>${title}</strong>: nothing found</div>`;
      return `<div style="margin-top:10px"><strong>${title} (${clusters.length})</strong> <span class="muted">${hint}</span>` +
        clusters.map((c) => `<div style="margin:6px 0;padding:8px;border:1px solid var(--line, #ddd);border-radius:8px">` +
          `<div class="muted">${escapeHtml(c.key)}</div>` +
          c.players.map((p) => `<div>${escapeHtml(p.display_name)} <span class="muted">${escapeHtml(String(p.id).replace("tg_", ""))}</span>` +
            (p.is_banned ? ' <em>banned</em>' : ` <button class="btn btn-delete" data-abuse-ban="${escapeHtml(p.id)}">Ban</button>`) + `</div>`).join("") +
          `</div>`).join("") + `</div>`;
    }).join("");
    box.innerHTML = html;
  } catch (err) { box.textContent = ""; toast(err.message, "error"); }
});

document.getElementById("abuse-report")?.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-abuse-ban]");
  if (!btn) return;
  const id = btn.dataset.abuseBan;
  if (!window.confirm("Ban this player? They will not be able to play, deposit or withdraw.")) return;
  btn.disabled = true;
  try {
    await api(`/api/admin/users/${encodeURIComponent(id)}/ban`, { method: "POST", body: JSON.stringify({ reason: "Related accounts (abuse review)" }) });
    btn.replaceWith(Object.assign(document.createElement("em"), { textContent: "banned" }));
  } catch (err) { btn.disabled = false; toast(err.message, "error"); }
});

/* ---- Deposit review: match a provider statement against pending deposits ---------------------- */
function stmtPayload() {
  return {
    statement: document.getElementById("stmt-text").value,
    ref_column: document.getElementById("stmt-ref").value.trim(),
    amount_column: document.getElementById("stmt-amt").value.trim(),
  };
}

document.getElementById("stmt-run")?.addEventListener("click", async () => {
  const out = document.getElementById("stmt-result");
  const approveBtn = document.getElementById("stmt-approve");
  approveBtn.classList.add("hidden");
  out.textContent = "Matching…";
  try {
    const r = await api("/api/admin/deposits/reconcile", { method: "POST", body: JSON.stringify(stmtPayload()) });
    const line = (label, items, fmt) => items.length
      ? `<div><strong>${label} (${items.length})</strong>${items.slice(0, 20).map((x) => `<div>${fmt(x)}</div>`).join("")}</div>` : "";
    const who = (x) => escapeHtml(String(x.player_id || "").replace("tg_", ""));
    out.innerHTML = `<div>Columns used: ${escapeHtml(r.columns.reference)} / ${escapeHtml(r.columns.amount)} · ${r.statement_rows} credits · ${r.pending_deposits} pending deposits</div>` +
      line("✅ Matched - safe to approve", r.matched, (x) => `${who(x)} · ${money(x.amount)} · ${escapeHtml(x.ref)}`) +
      line("⚠ Amount differs - check by hand", r.amount_mismatch, (x) => `${who(x)} claims ${money(x.amount)}, statement shows ${money(x.statement_amount)} (${escapeHtml(x.ref)})`) +
      line("⚠ Same payment claimed twice", r.duplicate_claim, (x) => `${who(x)} · ${money(x.amount)} · ${escapeHtml(x.ref)}`) +
      line("No matching payment found", r.unmatched, (x) => `${who(x)} · ${money(x.amount)}`) +
      line("Money received, nobody has asked for it yet", r.unclaimed, (x) => `${escapeHtml(x.ref)} · ${money(x.amount)}`);
    if (r.matched.length) {
      approveBtn.textContent = `Approve ${r.matched.length} matched`;
      approveBtn.classList.remove("hidden");
    }
  } catch (err) { out.textContent = ""; toast(err.message, "error"); }
});

document.getElementById("stmt-approve")?.addEventListener("click", async (e) => {
  if (!window.confirm("Approve every deposit marked as matched? Each one is credited to the player's wallet.")) return;
  e.target.disabled = true;
  try {
    const r = await api("/api/admin/deposits/approve-matched", { method: "POST", body: JSON.stringify(stmtPayload()) });
    toast(`${r.approved} deposits approved${r.failed.length ? `, ${r.failed.length} failed` : ""}`, r.failed.length ? "error" : "success");
    document.getElementById("stmt-result").textContent = "";
    e.target.classList.add("hidden");
    await loadDeposits();
  } catch (err) { toast(err.message, "error"); } finally { e.target.disabled = false; }
});

/* ---- Agent rewards: rules, tiers, held rewards, abuse flags ---------------------------------- */
function tierRow(t = {}) {
  const row = document.createElement("div");
  row.className = "tier-row";
  row.style.cssText = "display:grid;grid-template-columns:1.2fr 1.3fr 0.9fr auto;gap:6px;margin-bottom:6px;align-items:center";
  row.innerHTML = `<input class="t-label" placeholder="Name" maxlength="20" value="${escapeHtml(t.label || "")}">
    <input class="t-volume" inputmode="numeric" placeholder="Monthly ETB" value="${t.min_monthly_volume ?? ""}">
    <input class="t-pct" inputmode="decimal" placeholder="%" value="${t.commission_percent ?? ""}">
    <button type="button" class="btn btn-delete t-remove" aria-label="Remove tier">×</button>`;
  row.querySelector(".t-remove").addEventListener("click", () => row.remove());
  return row;
}

async function loadAgentRewards() {
  const flags = document.getElementById("reward-flags");
  const list = document.getElementById("rewards-list");
  if (!flags || !list) return;
  try {
    const d = await api("/api/admin/agent-rewards");
    const s = d.settings;
    document.getElementById("rs-enabled").checked = !!s.enabled;
    document.getElementById("rs-bounty").value = s.new_player_bounty;
    document.getElementById("rs-min-deposit").value = s.min_first_deposit;
    document.getElementById("rs-rounds").value = s.min_rounds_played;
    document.getElementById("rs-hold").value = s.hold_days;
    document.getElementById("rs-retention").value = s.retention_bonus;
    document.getElementById("rs-retention-days").value = s.retention_days;
    document.getElementById("rs-cap").value = s.daily_payout_cap;
    const rows = document.getElementById("tiers-rows");
    rows.replaceChildren(...d.tiers.map(tierRow));
    flags.innerHTML = d.flags.length
      ? d.flags.map((f) => `<div class="muted small">⚠ ${escapeHtml(f.agent_code)} topped up ${escapeHtml(f.player_id.replace("tg_", ""))} ${f.topups}× (${money(f.total)}) in 24h</div>`).join("")
      : '<p class="muted small">Nothing suspicious in the last 24 hours.</p>';
    list.innerHTML = d.rewards.length
      ? d.rewards.map((r) => recordCard({
          id: r.id,
          title: `${escapeHtml(r.agent_code)} · ${escapeHtml(r.type.replace("_", " "))} <span class="muted small">${escapeHtml((r.player_id || "").replace("tg_", ""))}</span>`,
          statusHtml: `<span class="status-pill">${r.status === "pending" ? "On hold" : escapeHtml(r.status)}</span>`,
          amountHtml: `<div class="rc-amount">${money(r.amount)}</div>`,
          meta: [r.status === "pending" ? `Pays after ${new Date(r.release_at).toLocaleDateString()}` : new Date(r.created_at).toLocaleDateString(), r.note ? escapeHtml(r.note) : ""],
          actionsHtml: r.status === "pending" ? `<div class="rc-actions"><button class="btn btn-delete" data-action="reward-cancel" data-id="${r.id}">Cancel reward</button></div>` : "",
        })).join("")
      : emptyState("★", "No rewards yet.");
  } catch (err) {
    list.innerHTML = `<p class="muted">Could not load rewards: ${escapeHtml(err.message)}</p>`;
  }
}

document.getElementById("add-tier-btn")?.addEventListener("click", () => {
  document.getElementById("tiers-rows").append(tierRow());
});

document.getElementById("tiers-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const tiers = [...document.querySelectorAll("#tiers-rows .tier-row")].map((r) => ({
    label: r.querySelector(".t-label").value.trim(),
    min_monthly_volume: Number(r.querySelector(".t-volume").value),
    commission_percent: r.querySelector(".t-pct").value.trim(),
  }));
  try {
    await api("/api/admin/agent-rewards/tiers", { method: "PUT", body: JSON.stringify({ tiers }) });
    toast("Tiers saved", "success");
    await loadAgentRewards();
  } catch (err) { toast(err.message, "error"); }
});

document.getElementById("reward-settings-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const num = (id) => Number(document.getElementById(id).value);
  try {
    await api("/api/admin/agent-rewards/settings", { method: "PUT", body: JSON.stringify({
      enabled: document.getElementById("rs-enabled").checked,
      new_player_bounty: num("rs-bounty"), min_first_deposit: num("rs-min-deposit"), min_rounds_played: num("rs-rounds"),
      hold_days: num("rs-hold"), retention_bonus: num("rs-retention"), retention_days: num("rs-retention-days"),
      daily_payout_cap: num("rs-cap"),
    }) });
    toast("Reward rules saved", "success");
    await loadAgentRewards();
  } catch (err) { toast(err.message, "error"); }
});

/* ===========================================================================
   Settings
   =========================================================================== */
// The 50 ETB and Bonus room fields are no longer in the page; skip any control that is missing
// so Settings still loads and saves instead of throwing on a null element.
function setValue(id, v) { const el = document.getElementById(id); if (el) el.value = v; }
function setChecked(id, v) { const el = document.getElementById(id); if (el) el.checked = v; }
function optNum(id) { const el = document.getElementById(id); return el ? Number(el.value) : undefined; }
function optBool(id) { const el = document.getElementById(id); return el ? el.checked : undefined; }

async function loadSettings() {
  try {
    const s = await api("/api/admin/settings");
    document.getElementById("s-stake").value = s.stake_amount;
    document.getElementById("s-bonus").value = s.free_credit_amount;
    document.getElementById("s-referral-bonus").value = s.referral_bonus;
    document.getElementById("s-referral-cap").value = s.max_referrals_per_user;
    document.getElementById("s-countdown").value = s.lobby_countdown_seconds;
    document.getElementById("s-draw-interval").value = s.draw_interval;
    document.getElementById("s-min-dep").value = s.min_deposit;
    document.getElementById("s-max-dep").value = s.max_deposit;
    document.getElementById("s-min-wd").value = s.min_withdrawal;
    document.getElementById("s-max-wd").value = s.max_withdrawal;
    document.getElementById("s-payout").value = s.payout_ratio;
    document.getElementById("s-cards-per-player").value = s.cards_per_player;
    document.getElementById("s-min-players").value = s.min_cartelas_to_start;
    document.getElementById("s-cartela-min").value = s.cartela_min;
    document.getElementById("s-cartela-max").value = s.cartela_max;
    document.getElementById("s-timeout").value = s.game_inactivity_timeout_minutes;
    document.getElementById("s-support-contact").value = s.support_contact || "@melbete_support_1";
    document.getElementById("s-deposits-enabled").checked = s.deposits_enabled !== false;
    document.getElementById("s-bot-enabled-real-10").checked = !!s.bot_enabled_real_10;
    setChecked("s-bot-enabled-real-50", !!s.bot_enabled_real_50);
    setChecked("s-bot-enabled-bonus", !!s.bot_enabled_bonus);
    document.getElementById("s-max-bots-real-10").value = s.max_bots_real_10 || 10;
    setValue("s-max-bots-real-50", s.max_bots_real_50 || 10);
    setValue("s-max-bots-bonus", s.max_bots_bonus || 10);
    setValue("s-stake-real-50", s.stake_amount_real_50 || 50);
    setValue("s-stake-bonus", s.stake_amount_bonus || 10);
  } catch (err) {
    toast("Failed to load System Settings", "error");
  }
}

// Game controls: [input id, label, min, max]. Checked here so a typo is caught before it is sent.
const GAME_CONTROL_RULES = [
  ["s-stake", "Stake per cartela", 1, 100000],
  ["s-stake-real-50", "50 ETB room stake", 1, 100000],
  ["s-stake-bonus", "Bonus room stake", 1, 100000],
  ["s-countdown", "Lobby countdown", 5, 300],
  ["s-draw-interval", "Draw interval", 1, 30],
  ["s-cards-per-player", "Max cartelas per player", 1, 4],
  ["s-min-players", "Minimum to start", 1, 500],
];

function validateGameControls() {
  for (const [id, label, lo, hi] of GAME_CONTROL_RULES) {
    const el = document.getElementById(id);
    if (!el) continue;
    const raw = String(el.value).trim();
    const n = Number(raw);
    if (raw === "" || !Number.isInteger(n) || n < lo || n > hi) {
      el.focus();
      return `${label} must be a whole number between ${lo} and ${hi}.`;
    }
  }
  return "";
}

const LOBBY_FIELD_NAMES = {
  draw_interval: "draw interval", countdown_seconds: "countdown", min_cartelas_to_start: "minimum to start",
  stake_amount: "stake", cards_per_player: "cartelas per player", cartela_min: "cartela range", cartela_max: "cartela range",
};
function lobbySyncMessage(sync) {
  if (!sync) return "";
  const names = (list) => [...new Set((list || []).map((k) => LOBBY_FIELD_NAMES[k] || k))].join(", ");
  const parts = [];
  if (sync.applied?.length) parts.push(`Applied to the open lobby: ${names(sync.applied)}.`);
  if (sync.deferred?.length) parts.push(`From the next round (players already hold cartelas): ${names(sync.deferred)}.`);
  return parts.join(" ");
}

document.getElementById("settings-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const problem = validateGameControls();
  if (problem) { toast(problem, "error"); return; }
  try {
    const saved = await api("/api/admin/settings", {
      method: "PUT",
      body: JSON.stringify({
        stake_amount: Number(document.getElementById("s-stake").value),
        stake_amount_real_10: Number(document.getElementById("s-stake").value),
        stake_amount_real_50: optNum("s-stake-real-50"),
        stake_amount_bonus: optNum("s-stake-bonus"),
        free_credit_amount: Number(document.getElementById("s-bonus").value),
        referral_bonus: Number(document.getElementById("s-referral-bonus").value),
        max_referrals_per_user: Number(document.getElementById("s-referral-cap").value),
        lobby_countdown_seconds: Number(document.getElementById("s-countdown").value),
        draw_interval: Number(document.getElementById("s-draw-interval").value),
        min_deposit: Number(document.getElementById("s-min-dep").value),
        max_deposit: Number(document.getElementById("s-max-dep").value),
        min_withdrawal: Number(document.getElementById("s-min-wd").value),
        max_withdrawal: Number(document.getElementById("s-max-wd").value),
        payout_ratio: Number(document.getElementById("s-payout").value),
        cards_per_player: Number(document.getElementById("s-cards-per-player").value),
        min_cartelas_to_start: Number(document.getElementById("s-min-players").value),
        cartela_min: Number(document.getElementById("s-cartela-min").value),
        cartela_max: Number(document.getElementById("s-cartela-max").value),
        game_inactivity_timeout_minutes: Number(document.getElementById("s-timeout").value),
        support_contact: document.getElementById("s-support-contact").value.trim(),
        deposits_enabled: document.getElementById("s-deposits-enabled").checked,
        bot_enabled_real_10: document.getElementById("s-bot-enabled-real-10").checked,
        bot_enabled_real_50: optBool("s-bot-enabled-real-50"),
        bot_enabled_bonus: optBool("s-bot-enabled-bonus"),
        max_bots_real_10: Number(document.getElementById("s-max-bots-real-10").value),
        max_bots_real_50: optNum("s-max-bots-real-50"),
        max_bots_bonus: optNum("s-max-bots-bonus"),
      }),
    });
    toast(`Settings saved. ${lobbySyncMessage(saved?.lobby_sync)}`.trim(), "success");
    await loadSettings();
  } catch (err) {
    toast(err.message, "error");
  }
});

/* ===========================================================================
   Broadcast
   =========================================================================== */
const MAX_BROADCAST_IMAGE_BYTES = 5_000_000;
let broadcastPhotoDataUrl = "";

function clearBroadcastPhoto() {
  const input = document.getElementById("broadcast-photo");
  const preview = document.getElementById("broadcast-image-preview");
  const image = document.getElementById("broadcast-photo-preview");
  const name = document.getElementById("broadcast-photo-name");
  broadcastPhotoDataUrl = "";
  if (input) input.value = "";
  if (image) image.removeAttribute("src");
  preview?.classList.add("hidden");
  if (name) name.textContent = "JPEG, PNG, or WebP · up to 5 MB";
}

document.getElementById("broadcast-photo")?.addEventListener("change", (event) => {
  const file = event.target.files?.[0];
  if (!file) return clearBroadcastPhoto();
  if (!new Set(["image/jpeg", "image/png", "image/webp"]).has(file.type)) {
    clearBroadcastPhoto();
    return toast("Choose a JPEG, PNG, or WebP image", "error");
  }
  if (file.size > MAX_BROADCAST_IMAGE_BYTES) {
    clearBroadcastPhoto();
    return toast("Choose an image smaller than 5 MB", "error");
  }
  const reader = new FileReader();
  reader.addEventListener("load", () => {
    if (document.getElementById("broadcast-photo")?.files?.[0] !== file) return;
    broadcastPhotoDataUrl = String(reader.result || "");
    document.getElementById("broadcast-photo-preview").src = broadcastPhotoDataUrl;
    document.getElementById("broadcast-image-preview").classList.remove("hidden");
    document.getElementById("broadcast-photo-name").textContent = `${file.name} · ${(file.size / 1024 / 1024).toFixed(1)} MB`;
  });
  reader.addEventListener("error", () => {
    if (document.getElementById("broadcast-photo")?.files?.[0] !== file) return;
    clearBroadcastPhoto();
    toast("The image could not be read. Please choose another one.", "error");
  });
  reader.readAsDataURL(file);
});

let broadcastPollInterval = null;

async function loadBroadcastStatus() {
  const monitor = document.getElementById("broadcast-live-monitor");
  const badge = document.getElementById("broadcast-status-badge");
  const stats = document.getElementById("broadcast-monitor-stats");
  const progress = document.getElementById("broadcast-progress-bar");
  const subtitle = document.getElementById("broadcast-monitor-subtitle");
  const cancelBtn = document.getElementById("broadcast-cancel-btn");
  const dismissBtn = document.getElementById("broadcast-dismiss-btn");
  const submitBtn = document.getElementById("broadcast-submit");

  try {
    const data = await api("/api/admin/broadcasts/status");
    if (!monitor) return;

    if (data.status === "running" || data.status === "cancelling") {
      monitor.classList.remove("hidden");
      if (submitBtn) submitBtn.disabled = true;
      if (dismissBtn) dismissBtn.classList.add("hidden");
      if (cancelBtn) {
        cancelBtn.classList.remove("hidden");
        cancelBtn.disabled = false; // Always keep active so admin can force-reset if needed
      }

      const pct = data.total_users > 0 ? Math.min(100, Math.round((data.sent_users / data.total_users) * 100)) : 0;
      if (progress) progress.style.width = `${pct}%`;

      if (data.status === "cancelling") {
        if (badge) {
          badge.textContent = "Cancelling…";
          badge.className = "pill terminated";
        }
        if (subtitle) subtitle.textContent = "Broadcast cancellation requested. Halting delivery…";
      } else {
        if (badge) {
          badge.textContent = "Running";
          badge.className = "pill running";
        }
        if (subtitle) subtitle.textContent = "Transmission in progress…";
      }

      if (stats) {
        stats.textContent = `⏳ ${data.sent_users} / ${data.total_users} processed (${pct}%) — Delivered: ${data.delivered_users}, Failed: ${data.failed_users}`;
      }

      if (!broadcastPollInterval) {
        broadcastPollInterval = setInterval(loadBroadcastStatus, 1500);
      }
    } else {
      if (broadcastPollInterval) {
        clearInterval(broadcastPollInterval);
        broadcastPollInterval = null;
      }

      if (data.status === "completed" || data.status === "cancelled" || data.status === "failed") {
        monitor.classList.remove("hidden");
        if (submitBtn) submitBtn.disabled = false;
        if (cancelBtn) cancelBtn.classList.add("hidden");
        if (dismissBtn) dismissBtn.classList.remove("hidden");

        const pct = data.total_users > 0 ? Math.min(100, Math.round((data.sent_users / data.total_users) * 100)) : 0;
        if (progress) progress.style.width = `${pct}%`;

        if (data.status === "completed") {
          if (badge) { badge.textContent = "Completed"; badge.className = "pill approved"; }
          if (subtitle) subtitle.textContent = "Broadcast finished successfully.";
          if (stats) stats.textContent = `✅ Delivered: ${data.delivered_users} / ${data.total_users}. Unavailable: ${data.failed_users}.`;
        } else if (data.status === "cancelled") {
          if (badge) { badge.textContent = "Cancelled"; badge.className = "pill cancelled"; }
          if (subtitle) subtitle.textContent = "Broadcast was cancelled by admin.";
          if (stats) stats.textContent = `⛔ Delivered ${data.delivered_users} / ${data.sent_users} before cancellation.`;
        } else {
          if (badge) { badge.textContent = "Failed"; badge.className = "pill rejected"; }
          if (subtitle) subtitle.textContent = "Broadcast failed during execution.";
          if (stats) stats.textContent = `❌ Processed ${data.sent_users} / ${data.total_users} before failure.`;
        }
      } else {
        // Idle
        monitor.classList.add("hidden");
        if (submitBtn) submitBtn.disabled = false;
      }
    }
  } catch (err) {
    console.error("Failed to load broadcast status:", err);
  }
}

document.getElementById("broadcast-cancel-btn")?.addEventListener("click", async () => {
  const cancelBtn = document.getElementById("broadcast-cancel-btn");
  if (cancelBtn) cancelBtn.disabled = true;
  try {
    await api("/api/admin/broadcasts/cancel", { method: "POST" });
    toast("Broadcast cancelled and state reset", "info");
    await loadBroadcastStatus();
  } catch (err) {
    toast(err.message || "Failed to cancel broadcast", "error");
    if (cancelBtn) cancelBtn.disabled = false;
  }
});

document.getElementById("broadcast-dismiss-btn")?.addEventListener("click", () => {
  const monitor = document.getElementById("broadcast-live-monitor");
  if (monitor) monitor.classList.add("hidden");
  const submitBtn = document.getElementById("broadcast-submit");
  if (submitBtn) submitBtn.disabled = false;
});

document.getElementById("broadcast-photo-remove")?.addEventListener("click", clearBroadcastPhoto);

document.getElementById("broadcast-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const submit = document.getElementById("broadcast-submit");
  const result = document.getElementById("broadcast-result");
  const buttons = [1, 2, 3].map((n) => ({
    text: document.getElementById(`broadcast-button-${n}-text`).value.trim(),
    url: document.getElementById(`broadcast-button-${n}-url`).value.trim(),
  })).filter((b) => b.text || b.url);
  submit.disabled = true;
  result.textContent = "Starting broadcast…";
  try {
    await api("/api/admin/broadcasts", {
      method: "POST",
      body: JSON.stringify({ text: document.getElementById("broadcast-text").value.trim(), photo_base64: broadcastPhotoDataUrl, buttons }),
    });

    e.target.reset();
    clearBroadcastPhoto();
    result.textContent = "";
    toast("Broadcast started in background", "success");
    await loadBroadcastStatus();
  } catch (err) {
    result.textContent = "";
    toast(err.message, "error");
    submit.disabled = false;
  }
});

/* ===========================================================================
   Economy
   =========================================================================== */
/* ===========================================================================
   Reconciliation screen + CSV export (deposits / withdrawals)
   =========================================================================== */
function etbNum(n) { return Number(n || 0).toLocaleString(); }

function reconFindings(title, hint, rows, cols) {
  if (!rows || !rows.length) return "";
  const head = cols.map((c) => `<th>${escapeHtml(c[1])}</th>`).join("");
  const body = rows.map((r) => `<tr>${cols.map((c) => `<td>${escapeHtml(r[c[0]])}</td>`).join("")}</tr>`).join("");
  return `<article class="work-panel recon-finding"><div class="panel-heading"><div><h3>${escapeHtml(title)}</h3><p>${escapeHtml(hint)}</p></div></div>
    <div class="table-scroll"><table class="data-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div></article>`;
}

async function loadReconciliation() {
  const body = document.getElementById("recon-body");
  const days = document.getElementById("recon-days")?.value || 14;
  let data;
  try {
    data = await api(`/api/admin/reconciliation?days=${encodeURIComponent(days)}`);
  } catch (e) {
    body.innerHTML = emptyState("🔒", e.message || "Could not load the reconciliation");
    return;
  }
  const r = data.report, t = r.totals, p = r.pending;
  const critical = r.negative.length + r.won_over_balance.length;
  document.getElementById("recon-stamp").textContent = `Live read ${data.generated_at} (Ethiopia time)`;

  const banner = critical
    ? `<div class="recon-banner recon-alert">🚨 ${critical} wallet${critical === 1 ? "" : "s"} with impossible balances. Check them before approving more payouts.</div>`
    : `<div class="recon-banner recon-ok">✅ No balance problems found.</div>` +
      (data.needs_review ? `<div class="recon-banner recon-review">${r.deposit_ledger.length} wallet(s) where lifetime deposits differ from approved top-ups - review below.</div>` : "");

  const cards = [
    { label: "Player money (real)", value: money(t.real_total), accent: "var(--pos)" },
    { label: "Withdrawable (won)", value: money(t.withdrawable), accent: "var(--gold)" },
    { label: "Bonus (not withdrawable)", value: money(t.bonus_total), accent: "var(--accent)" },
    { label: "Wallets", value: etbNum(t.wallets), accent: "var(--text)" },
    { label: "Deposits waiting", value: etbNum(p.topups), accent: p.topups ? "var(--warn)" : "var(--pos)" },
    { label: "Withdrawals waiting", value: `${etbNum(p.withdrawals)} (${money(p.withdrawal_amount)})`, accent: p.withdrawals ? "var(--warn)" : "var(--pos)" },
    { label: "Pending over 24h", value: etbNum(p.stale), accent: p.stale ? "var(--neg)" : "var(--pos)" },
  ].map((k) => `<div class="stat-card" style="border-left-color:${k.accent};"><div class="stat-label">${k.label}</div><div class="stat-value" style="color:${k.accent};font-size:1rem;">${k.value}</div></div>`).join("");

  const sum = (key) => data.daily.reduce((a, d) => a + Number(d[key] || 0), 0);
  const rows = data.daily.map((d) => `<tr>
      <td>${escapeHtml(d.day)}${d.day === data.today ? " <span class=\"pill info\">today</span>" : ""}</td>
      <td>${etbNum(d.deposits_approved)} / ${etbNum(d.deposits_approved_etb)}</td><td>${etbNum(d.deposits_rejected)}</td>
      <td>${etbNum(d.withdrawals_paid)} / ${etbNum(d.withdrawals_paid_etb)}</td><td>${etbNum(d.withdrawals_rejected)}</td>
      <td>${etbNum(d.deposits_pending)} / ${etbNum(d.withdrawals_pending)}</td>
      <td style="font-weight:800;color:${d.net_cash_etb >= 0 ? "var(--pos)" : "var(--neg)"};">${etbNum(d.net_cash_etb)}</td></tr>`).join("");
  const net = sum("deposits_approved_etb") - sum("withdrawals_paid_etb");
  const daily = data.daily.length ? `<article class="work-panel"><div class="panel-heading"><div><h3>Deposits and withdrawals by day</h3><p>A request counts on the day it was resolved (money moved). ETB shown after the count. Net cash = deposits approved - withdrawals paid.</p></div></div>
    <div class="table-scroll"><table class="data-table"><thead><tr><th>Day</th><th>Deposits OK (n / ETB)</th><th>Deposits rejected</th><th>Withdrawals paid (n / ETB)</th><th>Withdrawals rejected</th><th>Still pending (dep / wd)</th><th>Net cash ETB</th></tr></thead>
    <tbody>${rows}</tbody>
    <tfoot><tr><th>Total</th><th>${etbNum(sum("deposits_approved"))} / ${etbNum(sum("deposits_approved_etb"))}</th><th>${etbNum(sum("deposits_rejected"))}</th><th>${etbNum(sum("withdrawals_paid"))} / ${etbNum(sum("withdrawals_paid_etb"))}</th><th>${etbNum(sum("withdrawals_rejected"))}</th><th></th><th>${etbNum(net)}</th></tr></tfoot></table></div></article>`
    : emptyState("📭", "No deposits or withdrawals in this period.");

  body.innerHTML = banner + `<div class="stat-grid report-grid">${cards}</div>` +
    reconFindings("Negative balances", "A wallet should never go below zero.", r.negative, [["player_id", "Player"], ["balance", "Balance"], ["won_balance", "Won"], ["bonus_balance", "Bonus"]]) +
    reconFindings("Withdrawable more than balance", "Won winnings can never exceed the real balance.", r.won_over_balance, [["player_id", "Player"], ["balance", "Balance"], ["won_balance", "Won"]]) +
    reconFindings("Deposits that do not match the ledger", "Lifetime deposited on the wallet differs from the sum of approved top-ups.", r.deposit_ledger, [["player_id", "Player"], ["wallet_total", "Wallet says"], ["ledger_total", "Ledger says"]]) +
    daily +
    `<p class="muted small" style="margin-top:.8rem;">This is a live read. The 03:00 job re-reads after 60 seconds and ignores anything that was only a transaction in flight, so re-check a finding before acting on it.</p>`;
}

function etDate(offsetDays = 0) {
  return new Date(Date.now() + 3 * 3600 * 1000 + offsetDays * 86400000).toISOString().slice(0, 10);
}

async function downloadFinanceCsv(kind) {
  const qs = new URLSearchParams({
    status: document.getElementById(`${kind}-export-status`).value,
    start: document.getElementById(`${kind}-export-start`).value,
    end: document.getElementById(`${kind}-export-end`).value,
  });
  const btn = document.getElementById(`${kind}-export-btn`);
  btn.disabled = true;
  try {
    const res = await fetch(`/api/admin/${kind}s/export.csv?${qs}`, { credentials: "same-origin" });
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      throw new Error(d.detail || "Export failed");
    }
    const name = /filename="([^"]+)"/.exec(res.headers.get("Content-Disposition") || "")?.[1] || `${kind}s.csv`;
    const url = URL.createObjectURL(await res.blob());
    const a = Object.assign(document.createElement("a"), { href: url, download: name });
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
    toast(`Exported ${name}`, "success");
  } catch (e) {
    toast(e.message || "Export failed", "error");
  } finally {
    btn.disabled = false;
  }
}

function wireFinanceTools() {
  for (const kind of ["deposit", "withdrawal"]) {
    document.getElementById(`${kind}-export-start`).value = etDate(-29);
    document.getElementById(`${kind}-export-end`).value = etDate(0);
    document.getElementById(`${kind}-export-btn`).addEventListener("click", () => downloadFinanceCsv(kind));
  }
  document.getElementById("recon-refresh")?.addEventListener("click", () => loadReconciliation());
  document.getElementById("recon-days")?.addEventListener("change", () => loadReconciliation());
}

async function loadEconomy() {
  try {
    const data = await api("/api/admin/economy");
    const s = data.summary;

    // Live wallet balances — the 3 numbers admins check most often
    document.getElementById("economy-wallets").innerHTML = [
      { label: "💰 Real balance", value: money(s.total_real_balance), accent: "var(--pos)" },
      { label: "🎁 Bonus balance", value: money(s.total_bonus_balance), accent: "var(--accent)" },
      { label: "🏆 Won balance", value: money(s.total_won_balance), accent: "var(--gold)" },
    ].map((k) => `
      <div class="stat-card hero" style="border-left-color:${k.accent};">
        <div class="stat-label">${k.label}</div>
        <div class="stat-value" style="color:${k.accent};">${k.value}</div>
      </div>
    `).join("");

    // Revenue trend
    const chartEl = document.getElementById("economy-chart");
    if (data.time_series.length) {
      const ts = data.time_series;
      const maxVal = Math.max(...ts.map((d) => d.deposited + d.stakes), 1);
      chartEl.innerHTML = `
        <div class="economy-chart-inner">
          ${ts.map((d) => {
            const depH = Math.round((d.deposited / maxVal) * 100);
            const stakeH = Math.round((d.stakes / maxVal) * 100);
            const payH = Math.round((d.payouts / maxVal) * 100);
            const day = d.day.slice(5);
            return `<div class="chart-col" title="${d.day} — deposited ${d.deposited}, stakes ${d.stakes}, payouts ${d.payouts}, net ${d.net}">
              <div class="chart-bars">
                <div class="chart-bar dep" style="height:${depH}%;"></div>
                <div class="chart-bar stake" style="height:${stakeH}%;"></div>
                <div class="chart-bar pay" style="height:${payH}%;"></div>
              </div>
              <div class="chart-label">${day}</div>
            </div>`;
          }).join("")}
        </div>
        <div class="chart-legend">
          <span><span class="legend-dot dep"></span>Deposited</span>
          <span><span class="legend-dot stake"></span>Stakes</span>
          <span><span class="legend-dot pay"></span>Payouts</span>
        </div>
      `;
    } else {
      chartEl.innerHTML = `<p class="muted" style="padding:0.5rem 0;">No transaction data for the last 30 days yet.</p>`;
    }

    // Lifetime totals, grouped logically
    document.getElementById("economy-summary").innerHTML = [
      { label: "Total deposited", value: money(s.total_deposited), accent: "var(--pos)" },
      { label: "Total withdrawn", value: money(s.total_withdrawn), accent: "var(--warn)" },
      { label: "Gross stake revenue", value: money(s.total_stakes_collected), accent: "var(--info)" },
      { label: "Total payouts", value: money(s.total_payouts), accent: "var(--gold)" },
      { label: "Net house margin", value: money(s.net_house_margin), accent: s.net_house_margin >= 0 ? "var(--pos)" : "var(--neg)" },
      { label: "Total refunds", value: money(s.total_refunds), accent: "var(--info)" },
      { label: "Free credit issued", value: money(s.total_free_credit_issued), accent: "var(--accent)" },
      { label: "Referral bonuses", value: money(s.total_referral_bonuses), accent: "var(--pos)" },
      { label: "Active wallets", value: s.active_wallets.toLocaleString(), accent: "var(--text)" },
      { label: "Deposits made", value: `${s.deposit_count} txns`, accent: "var(--pos)" },
      { label: "Withdrawals made", value: `${s.withdrawal_count} txns`, accent: "var(--warn)" },
      { label: "Stakes collected", value: `${s.stake_count} txns`, accent: "var(--info)" },
    ].map((k) => `
      <div class="stat-card" style="border-left-color:${k.accent};">
        <div class="stat-label">${k.label}</div>
        <div class="stat-value" style="color:${k.accent};font-size:1rem;">${k.value}</div>
      </div>
    `).join("");

    renderLeaderList("depositors", data.top_depositors);
    renderLeaderList("winners", data.top_winners);
    wireLeaderToggle();
    wireEconomySearch();

  } catch (e) {
    toast(`Failed to load economy data: ${e.message}`, "error");
  }
}




function renderLeaderList(kind, rows) {
  const el = document.getElementById(`economy-top-${kind}`);
  if (!el) return;
  if (!rows.length) { el.innerHTML = emptyState(kind === "depositors" ? "💳" : "🏆", `No ${kind === "depositors" ? "deposit" : "win"} data yet.`); return; }
  el.innerHTML = rows.map((p, i) => {
    const isDep = kind === "depositors";
    const amount = isDep ? p.total_deposited : p.total_won;
    const count = isDep ? p.deposit_count : p.win_count;
    return recordCard({
      clickable: true,
      extraAttrs: `data-action="load-user-economy" data-id="${escapeHtml(p.player_id)}"`,
      statusHtml: `<span class="leader-rank">${i + 1}</span>`,
      title: `${escapeHtml(p.display_name)} ${tgCopyChip(p.telegram_id)}`,
      meta: [`${money(amount)}`, `${count} ${isDep ? "deposits" : "wins"}`],
    });
  }).join("");
}

function wireLeaderToggle() {
  document.querySelectorAll("[data-leader-tab]").forEach((btn) => {
    if (btn._wired) return;
    btn._wired = true;
    btn.addEventListener("click", () => {
      state.currentLeaderTab = btn.dataset.leaderTab;
      document.querySelectorAll("[data-leader-tab]").forEach((b) => b.classList.toggle("active", b === btn));
      document.getElementById("economy-top-depositors").classList.toggle("hidden", state.currentLeaderTab !== "depositors");
      document.getElementById("economy-top-winners").classList.toggle("hidden", state.currentLeaderTab !== "winners");
    });
  });
}

function wireEconomySearch() {
  const searchBtn = document.getElementById("economy-user-load-btn");
  const searchInput = document.getElementById("economy-user-search");
  if (searchBtn && searchInput && !searchBtn._wired) {
    searchBtn._wired = true;
    searchBtn.addEventListener("click", () => { const q = searchInput.value.trim(); if (q) loadUserEconomy(q); });
    searchInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { const q = searchInput.value.trim(); if (q) loadUserEconomy(q); }
    });
  }
}

const TRANSACTION_TYPE_LABELS = {
  top_up_pending:      { label: "Deposit (pending)", icon: "⏳", color: "var(--warn)" },
  top_up_approved:     { label: "Deposit",            icon: "💳", color: "var(--pos)" },
  top_up_rejected:     { label: "Deposit rejected",   icon: "🚫", color: "var(--neg)" },
  withdrawal_pending:  { label: "Withdrawal (pending)", icon: "⏳", color: "var(--warn)" },
  withdrawal_approved: { label: "Withdrawal",         icon: "🏧", color: "var(--neg)" },
  withdrawal_rejected: { label: "Withdrawal rejected", icon: "🚫", color: "var(--neg)" },
  game_stake:          { label: "Game stake",         icon: "🎯", color: "var(--info)" },
  stake_refund:        { label: "Stake refund",       icon: "↩️", color: "var(--info)" },
  win_payout:          { label: "Win payout",         icon: "🏆", color: "var(--gold)" },
  free_credit:         { label: "Free credit",        icon: "🎁", color: "var(--accent)" },
  referral_bonus:      { label: "Referral bonus",     icon: "🤝", color: "var(--pos)" },
  admin_adjustment:    { label: "Admin adjustment",   icon: "🛠️", color: "var(--text-dim)" },
  account_link_merge:  { label: "Account merge",      icon: "🔗", color: "var(--text-dim)" },
};

async function loadUserEconomy(playerIdOrName) {
  const histEl = document.getElementById("economy-user-history");
  if (!histEl) return;
  histEl.innerHTML = `<p class="muted">Loading…</p>`;
  const searchInput = document.getElementById("economy-user-search");
  if (searchInput && searchInput.value !== playerIdOrName) searchInput.value = playerIdOrName;

  try {
    const data = await api(`/api/admin/economy/user/${encodeURIComponent(playerIdOrName)}`);
    const txns = data.transactions || [];
    if (!txns.length) {
      histEl.innerHTML = `<div class="section-card">${emptyState("🔍", `No transactions found for ${escapeHtml(data.display_name)}.`)}</div>`;
      return;
    }
    const positiveTypes = new Set(["top_up_approved", "win_payout", "free_credit", "referral_bonus", "stake_refund"]);
    histEl.innerHTML = `
      <div class="section-card">
        <div class="section-title">${escapeHtml(data.display_name)} ${tgCopyChip(data.telegram_id)}</div>
        <div>${txns.map((t) => {
          const meta = TRANSACTION_TYPE_LABELS[t.type] || { label: t.type, icon: "•", color: "var(--text-dim)" };
          const isPos = positiveTypes.has(t.type);
          const amt = `${isPos ? "+" : "−"}${Math.abs(t.amount).toLocaleString()} ETB`;
          const when = t.created_at ? new Date(t.created_at).toLocaleString() : "—";
          return `
            <div class="ledger-row">
              <div class="ledger-icon" style="background:color-mix(in srgb, ${meta.color} 18%, transparent); color:${meta.color};">${meta.icon}</div>
              <div class="ledger-body">
                <div class="ledger-type">${escapeHtml(meta.label)}${t.note ? ` <span class="muted small">— ${escapeHtml(t.note)}</span>` : ""}</div>
                <div class="ledger-time">${when}${t.payment_method ? ` · ${escapeHtml(t.payment_method)}` : ""}</div>
              </div>
              <div class="ledger-amount" style="color:${isPos ? "var(--pos)" : "var(--neg)"};">${amt}</div>
            </div>
          `;
        }).join("")}</div>
      </div>
    `;
  } catch (e) {
    histEl.innerHTML = `<p class="muted">Could not load history: ${escapeHtml(e.message)}</p>`;
  }
}

/* ===========================================================================
   Referrals
   =========================================================================== */
async function loadReferrals() {
  try {
    const data = await api("/api/admin/referrals");
    document.getElementById("referrals-summary").innerHTML = [
      { label: "Total referrers", value: data.total_referrers },
      { label: "Total referred", value: data.total_referred_users },
      { label: "Bonus per invite", value: money(data.bonus_per_referral) },
      { label: "Max per user", value: data.max_referrals_per_user },
    ].map((s) => `<div class="stat-card"><div class="stat-label">${s.label}</div><div class="stat-value">${s.value}</div></div>`).join("");

    const list = document.getElementById("referrals-list");
    if (!data.referrals.length) { list.innerHTML = emptyState("🤝", "No referrals yet."); return; }
    list.innerHTML = data.referrals.map((r) => {
      const subList = (r.referred_users || []).length
        ? `<ul class="referral-sub-list">${r.referred_users.map((u) => {
            const status = u.awarded ? `✅ +${u.bonus_amount} ETB` : "⏳ over cap — not paid";
            return `<li>${escapeHtml(u.display_name)} — joined ${new Date(u.joined_at).toLocaleDateString()} (${status})</li>`;
          }).join("")}</ul>`
        : "";
      return recordCard({
        title: `${escapeHtml(r.referrer_name)} <span class="muted small">(ID ${escapeHtml(r.referrer_telegram_id)})</span>`,
        meta: [`${r.total_referrals}/${data.max_referrals_per_user} referrals made`],
        extraHtml: subList,
      });
    }).join("");
  } catch (err) {
    document.getElementById("referrals-list").innerHTML = `<p class="muted">Could not load referrals: ${escapeHtml(err.message)}</p>`;
  }
}

/* ===========================================================================
   Row action delegation — one listener for every card button in the app
   =========================================================================== */
document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-action]");
  if (!btn) return;
  const { action, id, tg: tgId } = btn.dataset;

  if (action === "copy-tg") {
    e.stopPropagation();
    if (tgId) {
      navigator.clipboard.writeText(String(tgId))
        .then(() => toast(`Copied Telegram ID: ${tgId}`, "success"))
        .catch(() => toast(`Telegram ID: ${tgId}`, "info"));
    }
    return;
  }
  if (action === "load-user-economy") { loadUserEconomy(id); return; }
  if (action === "manage-user") { const u = findUserById(id); if (u) openUserManageModal(u); return; }
  if (action === "terminate-game") { terminateGame(id, btn); return; }
  if (["promote-admin", "demote-admin", "set-admin-role", "remove-admin"].includes(action)) { adminStaffAction(action, id, btn); return; }
  if (action === "manage-game-stats") { openGameManageModal(id); return; }
  if (action === "delete-game") { deleteGame(id, btn); return; }
  if (action === "reset-lobby") { resetLobby(id, btn); return; }
  if (action === "force-start-game") { forceStartGame(id, btn); return; }
  if (action === "edit-method") {
    const m = _paymentMethods.find((x) => x.id === id);
    if (m) {
      document.getElementById("pm-id").value = m.id;
      document.getElementById("pm-key").value = m.key;
      document.getElementById("pm-name").value = m.name;
      document.getElementById("pm-account").value = m.account_info;
      document.getElementById("pm-holder").value = m.account_holder || "";
      document.getElementById("pm-instructions").value = m.instructions || "";
      document.getElementById("pm-active").checked = m.active;
      document.getElementById("pm-deposit").checked = m.supports_deposit;
      document.getElementById("pm-withdrawal").checked = m.supports_withdrawal;
      document.getElementById("tab-payments").scrollIntoView({ behavior: "smooth" });
    }
    return;
  }

  // Approve/reject actions disable the card's buttons immediately to
  // prevent a double-submit, then replace them with a decided-state note.
  const card = btn.closest(".record-card");
  const actionsDiv = card?.querySelector(".rc-actions");
  if (actionsDiv) actionsDiv.querySelectorAll("button").forEach((b) => { b.disabled = true; });

  try {
    if (action === "approve-deposit") {
      await api(`/api/admin/deposits/${id}/approve`, { method: "POST" });
      toast("Deposit approved ✅", "success");
      await loadDeposits();
    }
    if (action === "reject-deposit") {
      await api(`/api/admin/deposits/${id}/reject`, { method: "POST", body: "{}" });
      toast("Deposit rejected", "success");
      await loadDeposits();
    }
    if (action === "approve-withdrawal") {
      await api(`/api/admin/withdrawals/${id}/approve`, { method: "POST" });
      toast("Withdrawal marked paid ✅", "success");
      await loadWithdrawals();
    }
    if (action === "reject-withdrawal") {
      await api(`/api/admin/withdrawals/${id}/reject`, { method: "POST", body: "{}" });
      toast("Withdrawal rejected & refunded", "success");
      await loadWithdrawals();
    }
    if (action === "delete-method") { await api(`/api/admin/payment-methods/${id}`, { method: "DELETE" }); await loadPaymentMethods(); toast("Payment method deleted", "success"); }
    if (action === "agent-float")    { await adjustAgentFloat(id); if (actionsDiv) actionsDiv.querySelectorAll("button").forEach((b) => { b.disabled = false; }); }
    if (action === "agent-ledger")   { await toggleAgentLedger(id); if (actionsDiv) actionsDiv.querySelectorAll("button").forEach((b) => { b.disabled = false; }); }
    if (action === "reward-cancel")  {
      if (!window.confirm("Cancel this reward? The agent will not be paid for it.")) { if (actionsDiv) actionsDiv.querySelectorAll("button").forEach((b) => { b.disabled = false; }); return; }
      await api(`/api/admin/agent-rewards/${id}/cancel`, { method: "POST", body: "{}" }); toast("Reward cancelled", "success"); await loadAgentRewards();
    }
    if (action && action.startsWith("lb-")) { await leaderboardAction(action, id, btn, actionsDiv); }
    if (action === "agent-app-approve" || action === "agent-app-reject") {
      const done = await reviewAgentApplication(id, action === "agent-app-approve");
      if (!done && actionsDiv) actionsDiv.querySelectorAll("button").forEach((b) => { b.disabled = false; });
    }
    if (action === "agent-commission") { await editAgentCommission(id, Number(btn.dataset.rate)); if (actionsDiv) actionsDiv.querySelectorAll("button").forEach((b) => { b.disabled = false; }); }
    if (action === "agent-rewards") {
      const turnOn = btn.dataset.on === "0";
      await api(`/api/admin/agents/${id}/rewards`, { method: "PUT", body: JSON.stringify({ enabled: turnOn }) });
      toast(turnOn ? "Rewards turned on for this agent" : "Rewards turned off for this agent", "success");
      await loadAgents();
    }
    if (action === "agent-suspend")  { await api(`/api/admin/agents/${id}/suspend`, { method: "POST", body: "{}" }); toast("Agent suspended", "success"); await loadAgents(); }
    if (action === "agent-activate") { await api(`/api/admin/agents/${id}/activate`, { method: "POST", body: "{}" }); toast("Agent activated", "success"); await loadAgents(); }
  } catch (err) {
    if (actionsDiv) actionsDiv.querySelectorAll("button").forEach((b) => { b.disabled = false; });
    toast(err.message, "error");
  }
});

/* ===========================================================================
   Bootstrap
   =========================================================================== */
async function main() {
  wireNav();
  wireGlobalSearch();
  wireTopbarRefresh();
  wireAuditFilterButtons();
  wireFinanceTools();
  wireUserManageModal();
  wireGameManageModal();
  wireCommandCenter();
  document.getElementById("logout-btn")?.addEventListener("click", logout);
  document.getElementById("sidebar-logout-btn")?.addEventListener("click", logout);

  if (state.inTelegram) {
    const loginScreen = document.getElementById("login-screen");
    loginScreen.innerHTML = "";
    const card = document.createElement("div");
    card.className = "panel-card";
    const h2 = document.createElement("h2");
    h2.textContent = "🔐 Admin verification";
    const p = document.createElement("p");
    p.className = "muted";
    p.textContent = "Enter the current code from your authenticator app.";
    const form = document.createElement("form");
    form.className = "login-form";
    form.noValidate = true;
    const field = document.createElement("div");
    field.className = "field";
    const label = document.createElement("label");
    label.htmlFor = "totp-code";
    label.textContent = "Authenticator code";
    const input = document.createElement("input");
    input.id = "totp-code";
    input.type = "text"; input.inputMode = "numeric"; input.autocomplete = "one-time-code";
    input.maxLength = 6; input.placeholder = "123456"; input.pattern = "[0-9]{6}"; input.required = true;
    input.setAttribute("aria-describedby", "totp-error");
    const button = document.createElement("button");
    button.type = "submit"; button.className = "primary-btn"; button.textContent = "Verify and continue";
    const error = document.createElement("p"); error.id = "totp-error"; error.className = "error-text"; error.setAttribute("role", "alert");
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const code = input.value.trim();
      if (!/^\d{6}$/.test(code)) {
        error.textContent = "Enter the 6-digit code from your authenticator app.";
        input.focus();
        return;
      }
      error.textContent = "";
      button.disabled = true;
      try { await login(code); }
      catch (err) { error.textContent = err.message; button.disabled = false; }
    });
    field.append(label, input);
    form.append(field, button, error);
    card.append(h2, p, form);
    loginScreen.append(card);
    return;
  }

  document.getElementById("login-screen").innerHTML = "<div class=\"panel-card\"><h2>🔒 Telegram required</h2><p class=\"muted\">For security, the admin panel is available only from the official Telegram bot.</p></div>";
}

main();


/* ===========================================================================
   Leaderboard campaigns
   =========================================================================== */
const LB_PHASE = { scheduled: "Scheduled", active: "Running", ended: "Ended - awaiting your decision", distributed: "Rewards paid", rejected: "Rejected", cancelled: "Cancelled" };
let lbCampaigns = [];

async function loadLeaderboard() {
  const list = document.getElementById("lb-list");
  try {
    const { campaigns } = await api("/api/admin/leaderboard/campaigns");
    lbCampaigns = campaigns;
    if (!campaigns.length) { list.innerHTML = emptyState("★", "No campaigns yet. Create the first one."); return; }
    list.innerHTML = campaigns.map((c) => {
      const open = c.phase === "scheduled" || c.phase === "active";
      const extendable = open || c.phase === "ended";
      return recordCard({
        id: c.id,
        title: escapeHtml(c.name),
        statusHtml: `<span class="status-pill">${escapeHtml(LB_PHASE[c.phase] || c.phase)}</span>`,
        meta: [`${escapeHtml(c.starts_local.replace("T", " "))} → ${escapeHtml(c.ends_local.replace("T", " "))} (${escapeHtml(c.timezone)})`, `${money(c.budget_etb)} · ${c.winners_count} winners`, `${c.points_per_cartela} pts/cartela · ${c.wagering_multiplier}× wagering`, c.channel_enabled ? `Channel @${escapeHtml(c.channel_username)} +${c.channel_points}` : "No channel reward"],
        actionsHtml: `<div class="rc-actions">
          <button class="btn" data-action="lb-detail" data-id="${c.id}">Standings &amp; rewards</button>
          ${extendable ? `<button class="btn" data-action="lb-extend" data-id="${c.id}">Extend end date</button>` : ""}
          ${open ? `<button class="btn" data-action="lb-edit" data-id="${c.id}">Edit</button><button class="btn btn-delete" data-action="lb-cancel" data-id="${c.id}">Cancel campaign</button>` : ""}
        </div>`,
      });
    }).join("");
  } catch (err) { list.innerHTML = `<p class="muted">Could not load campaigns: ${escapeHtml(err.message)}</p>`; }
}

function lbFormValues() {
  const num = (id) => document.getElementById(id).value.trim();
  return {
    name: num("lb-name"), timezone: num("lb-tz"), starts_at: num("lb-start"), ends_at: num("lb-end"),
    budget_etb: Number(num("lb-budget")), winners_count: Number(num("lb-winners")), points_per_cartela: Number(num("lb-points")),
    wagering_multiplier: Number(num("lb-wager")), channel_enabled: document.getElementById("lb-channel-on").checked,
    channel_username: num("lb-channel"), channel_points: Number(num("lb-channel-points")),
  };
}

function lbResetForm() {
  document.getElementById("lb-form").reset();
  document.getElementById("lb-id").value = "";
  document.getElementById("lb-tz").value = "Africa/Addis_Ababa";
  document.getElementById("lb-budget").value = "100000"; document.getElementById("lb-winners").value = "250";
  document.getElementById("lb-points").value = "10"; document.getElementById("lb-wager").value = "37"; document.getElementById("lb-channel-points").value = "50";
  document.getElementById("lb-form-title").textContent = "New campaign";
  document.getElementById("lb-save").textContent = "Create campaign";
  document.getElementById("lb-reset").classList.add("hidden");
  ["lb-budget", "lb-winners", "lb-points", "lb-wager", "lb-start"].forEach((id) => { document.getElementById(id).disabled = false; });
}

function lbEdit(id) {
  const c = lbCampaigns.find((x) => x.id === id);
  if (!c) return;
  document.getElementById("lb-id").value = c.id;
  document.getElementById("lb-name").value = c.name; document.getElementById("lb-tz").value = c.timezone;
  document.getElementById("lb-start").value = c.starts_local; document.getElementById("lb-end").value = c.ends_local;
  document.getElementById("lb-budget").value = c.budget_etb; document.getElementById("lb-winners").value = c.winners_count;
  document.getElementById("lb-points").value = c.points_per_cartela; document.getElementById("lb-wager").value = c.wagering_multiplier;
  document.getElementById("lb-channel-on").checked = !!c.channel_enabled; document.getElementById("lb-channel").value = c.channel_username ? `@${c.channel_username}` : "";
  document.getElementById("lb-channel-points").value = c.channel_points;
  const running = c.phase === "active";   // once points are being earned, only name / end / channel reward can change
  ["lb-budget", "lb-winners", "lb-points", "lb-wager", "lb-start"].forEach((f) => { document.getElementById(f).disabled = running; });
  document.getElementById("lb-form-title").textContent = running ? "Edit running campaign" : "Edit campaign";
  document.getElementById("lb-save").textContent = "Save changes";
  document.getElementById("lb-reset").classList.remove("hidden");
  document.getElementById("lb-form").scrollIntoView({ behavior: "smooth", block: "start" });
}

document.getElementById("lb-reset")?.addEventListener("click", lbResetForm);
document.getElementById("lb-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = document.getElementById("lb-id").value;
  const body = lbFormValues();
  if (id) { ["budget_etb", "winners_count", "points_per_cartela", "wagering_multiplier", "starts_at"].forEach((k) => { if (document.getElementById({ budget_etb: "lb-budget", winners_count: "lb-winners", points_per_cartela: "lb-points", wagering_multiplier: "lb-wager", starts_at: "lb-start" }[k]).disabled) delete body[k]; }); }
  try {
    await api(id ? `/api/admin/leaderboard/campaigns/${id}` : "/api/admin/leaderboard/campaigns", { method: id ? "PUT" : "POST", body: JSON.stringify(body) });
    toast(id ? "Campaign updated" : "Campaign created", "success");
    lbResetForm();
    await loadLeaderboard();
  } catch (err) { toast(err.message, "error"); }
});

async function lbOpenDetail(id) {
  const panel = document.getElementById("lb-detail-panel"), box = document.getElementById("lb-detail");
  panel.classList.remove("hidden");
  box.innerHTML = '<div class="loader" style="margin:1rem auto"></div>';
  try {
    const d = await api(`/api/admin/leaderboard/campaigns/${id}`);
    const c = d.campaign;
    document.getElementById("lb-detail-title").textContent = c.name;
    document.getElementById("lb-detail-sub").textContent = `${LB_PHASE[c.phase] || c.phase} · ${d.participants} players with points`;
    const rows = (arr, cols) => arr.length ? `<div class="table-scroll"><table class="data-table"><thead><tr>${cols.map((x) => `<th>${x[0]}</th>`).join("")}</tr></thead><tbody>${arr.map((r) => `<tr>${cols.map((x) => `<td>${x[1](r)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>` : '<p class="muted small">Nothing yet.</p>';
    let decision = "";
    if (c.phase === "ended") {
      decision = `<div class="work-panel"><h4>Reward distribution</h4>
        <p class="muted small">If you approve, the top ${c.winners_count} share ${money(c.budget_etb)} in proportion to their points (${money(d.preview_total)} for the current standings). Rewards go to bonus balance and unlock after ${c.wagering_multiplier}× wagering. Nothing is paid unless you approve.</p>
        <div class="rc-actions"><button class="btn" data-action="lb-extend" data-id="${c.id}">Extend end date</button><button class="btn btn-approve" data-action="lb-approve" data-id="${c.id}">Approve &amp; distribute</button><button class="btn btn-delete" data-action="lb-reject" data-id="${c.id}">Reject (pay nothing)</button></div></div>`;
    } else if (c.phase === "distributed") {
      decision = `<p class="muted small">Approved ${c.decided_at ? new Date(c.decided_at).toLocaleString() : ""} · ${money(c.total_distributed)} distributed.</p>`;
    } else if (c.phase === "rejected") {
      decision = `<p class="muted small">Rejected${c.decision_note ? `: ${escapeHtml(c.decision_note)}` : ""}. No rewards were paid.</p>`;
    }
    const money0 = (n) => money(n);
    box.innerHTML = `${decision}
      <h4>Standings (top ${d.standings.length})</h4>
      ${rows(d.standings, [["#", (r) => r.rank], ["Player", (r) => escapeHtml(r.name || r.player_id)], ["Cartela", (r) => r.cartela_points], ["Channel", (r) => r.channel_points], ["Total", (r) => `<b>${r.points}</b>`]])}
      ${d.rewards.length ? `<h4>Rewards</h4>${rows(d.rewards, [["#", (r) => r.rank], ["Player", (r) => escapeHtml(r.name || r.player_id)], ["Reward", (r) => money0(r.amount)], ["Wagered", (r) => `${r.wagered}/${r.wager_required}`], ["Status", (r) => escapeHtml(r.status)]])}` : (d.preview.length ? `<h4>Reward preview</h4>${rows(d.preview, [["#", (r) => r.rank], ["Player", (r) => escapeHtml(r.name || r.player_id)], ["Points", (r) => r.points], ["Reward", (r) => money0(r.amount)]])}` : "")}
      <h4>Channel reward claims</h4>
      ${rows(d.channel_claims, [["Player", (r) => escapeHtml(r.display_name || r.player_id)], ["Points", (r) => r.points], ["When", (r) => new Date(r.created_at).toLocaleString()], ["", (r) => r.revoked ? "Revoked" : `<button class="btn btn-delete" data-action="lb-revoke" data-id="${r.id}">Revoke</button>`]])}
      ${d.failed_attempts.length ? `<h4>Unsuccessful verifications</h4>${rows(d.failed_attempts, [["Player", (r) => escapeHtml(r.display_name || r.player_id)], ["Result", (r) => escapeHtml(r.result)], ["Detail", (r) => escapeHtml(r.detail || "")], ["When", (r) => new Date(r.created_at).toLocaleString()]])}` : ""}`;
    box.dataset.campaign = id;
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (err) { box.innerHTML = `<p class="muted">${escapeHtml(err.message)}</p>`; }
}

async function leaderboardAction(action, id, btn, actionsDiv) {
  const reenable = () => { if (actionsDiv) actionsDiv.querySelectorAll("button").forEach((b) => { b.disabled = false; }); };
  if (action === "lb-edit") { lbEdit(id); return reenable(); }
  if (action === "lb-detail") { await lbOpenDetail(id); return reenable(); }
  if (action === "lb-extend") {
    const c = lbCampaigns.find((x) => x.id === id);
    const raw = window.prompt(`New end date and time (${c ? c.timezone : "campaign timezone"}), later than ${c ? c.ends_local.replace("T", " ") : "the current end"}.\nFormat: 2026-12-05 21:00`, c ? c.ends_local.replace("T", " ") : "");
    if (raw === null) return reenable();
    try {
      await api(`/api/admin/leaderboard/campaigns/${id}/extend`, { method: "POST", body: JSON.stringify({ ends_at: raw.trim().replace(" ", "T") }) });
      toast("End date extended", "success");
    } catch (err) { toast(err.message, "error"); reenable(); return; }
    await loadLeaderboard();
    if (document.getElementById("lb-detail")?.dataset.campaign === id) await lbOpenDetail(id);
    return;
  }
  if (action === "lb-cancel") {
    if (!window.confirm("Cancel this campaign? Points stop being counted and nothing is paid.")) return reenable();
    await api(`/api/admin/leaderboard/campaigns/${id}/cancel`, { method: "POST", body: "{}" });
    toast("Campaign cancelled", "success"); return loadLeaderboard();
  }
  if (action === "lb-approve") {
    if (!window.confirm("Approve and pay the leaderboard rewards now? This cannot be undone.")) return reenable();
    const r = await api(`/api/admin/leaderboard/campaigns/${id}/approve`, { method: "POST", body: "{}" });
    toast(`Distributed ${r.total_distributed} ETB to ${r.winners} winners`, "success");
    refreshAttention();
    await loadLeaderboard(); return lbOpenDetail(id);
  }
  if (action === "lb-reject") {
    const note = window.prompt("Reason (optional). No rewards will be paid:", "");
    if (note === null) return reenable();
    await api(`/api/admin/leaderboard/campaigns/${id}/reject`, { method: "POST", body: JSON.stringify({ note: note.trim() }) });
    toast("Distribution rejected", "success");
    refreshAttention();
    await loadLeaderboard(); return lbOpenDetail(document.getElementById("lb-detail").dataset.campaign || id);
  }
  if (action === "lb-revoke") {
    if (!window.confirm("Revoke these channel points? The player cannot claim again in this campaign.")) return reenable();
    await api(`/api/admin/leaderboard/points/${id}/revoke`, { method: "POST", body: "{}" });
    toast("Claim revoked", "success"); return lbOpenDetail(document.getElementById("lb-detail").dataset.campaign);
  }
}


/* ===========================================================================
   Attention badges: red counts on tabs that have something waiting for a decision
   =========================================================================== */
function setTabBadge(tab, count) {
  document.querySelectorAll(`[data-tab="${tab}"]`).forEach((el) => {
    let b = el.querySelector(".attn-badge");
    if (!count) { b?.remove(); return; }
    if (!b) { b = document.createElement("span"); b.className = "attn-badge"; b.setAttribute("aria-label", "waiting for review"); el.appendChild(b); }
    b.textContent = count > 99 ? "99+" : String(count);
  });
}

async function refreshAttention() {
  if (!state.isSuperAdmin) return;
  try {
    const a = await api("/api/admin/attention");
    setTabBadge("leaderboard", a.leaderboard);
    setTabBadge("agents", a.agents);
  } catch (e) { /* badges are a convenience; try again next tick */ }
}
setInterval(() => { if (!document.hidden) refreshAttention(); }, 30000);
setTimeout(refreshAttention, 4000);

/* ===========================================================================
   Global search (top bar): players, phone numbers and deposit / withdrawal records
   =========================================================================== */
function wireGlobalSearch() {
  const input = document.getElementById("global-search");
  const panel = document.getElementById("global-search-results");
  if (!input || !panel || input._wired) return;
  input._wired = true;
  let timer = 0, seq = 0, last = null;

  const close = () => panel.classList.add("hidden");
  const reveal = (tab) => { loadedTabs.add(tab); setActiveTab(tab); closeMoreSheet(); close(); };
  const banner = (q, reload) => {
    const el = document.createElement("div");
    el.className = "recon-banner recon-review gs-banner";
    el.innerHTML = `<span>Showing the search result for “${escapeHtml(q)}”.</span> <button type="button" class="secondary-btn gs-showall">Show all</button>`;
    el.querySelector("button").addEventListener("click", () => reload().catch((e) => toast(e.message, "error")));
    return el;
  };

  const open = {
    player(p, q) {
      reveal("users");
      const box = document.getElementById("users-search");
      const term = p.display_name || p.telegram_id || q;
      if (box) box.value = term;
      loadUsers(term);
    },
    deposit(d, q) {
      reveal("deposits");
      const list = document.getElementById("deposits-list");
      list.innerHTML = renderDepositCard(d);
      list.prepend(banner(q, () => loadDeposits()));
      list.querySelector(".record-card")?.scrollIntoView({ block: "center", behavior: "smooth" });
    },
    withdrawal(w, q) {
      reveal("withdrawals");
      const list = document.getElementById("withdrawals-list");
      list.innerHTML = renderWithdrawalCard(w);
      list.prepend(banner(q, () => loadWithdrawals()));
      list.querySelector(".record-card")?.scrollIntoView({ block: "center", behavior: "smooth" });
    },
  };

  const row = (icon, title, sub, onPick) => {
    const b = document.createElement("button");
    b.type = "button"; b.className = "gs-item"; b.setAttribute("role", "option");
    b.innerHTML = `<span class="gs-icon" aria-hidden="true">${icon}</span><span class="gs-text"><strong>${title}</strong><small>${sub}</small></span>`;
    b.addEventListener("click", onPick);
    return b;
  };
  const group = (label) => { const h = document.createElement("div"); h.className = "gs-group"; h.textContent = label; return h; };

  function render(data) {
    last = data;
    const q = data.query;
    panel.replaceChildren();
    const status = (s) => ({ approved: "✅", rejected: "❌", pending: "⏳" }[s] || "");
    if (data.players.length) {
      panel.append(group("Players"));
      data.players.forEach((p) => panel.append(row("👤",
        `${escapeHtml(p.display_name || "Player")}${p.is_banned ? " · banned" : ""}`,
        [p.phone_number, p.telegram_id && `TG ${p.telegram_id}`, `${money(p.balance)} + ${money(p.bonus_balance)} bonus`].filter(Boolean).map(escapeHtml).join(" · "),
        () => open.player(p, q))));
    }
    if (data.deposits.length) {
      panel.append(group("Deposits"));
      data.deposits.forEach((d) => panel.append(row("↓",
        `${status(d.status)} ${money(d.amount)} · ${escapeHtml(d.player_name)}`,
        `${escapeHtml(d.payment_method || "")} · ${new Date(d.created_at).toLocaleString()} · ${escapeHtml(String(d.id).slice(0, 8))}`,
        () => open.deposit(d, q))));
    }
    if (data.withdrawals.length) {
      panel.append(group("Withdrawals"));
      data.withdrawals.forEach((w) => panel.append(row("↑",
        `${status(w.status)} ${money(w.amount)} · ${escapeHtml(w.legal_name || w.player_name)}`,
        `${escapeHtml(w.payment_method || "")} → ${escapeHtml(w.withdrawal_address || "")} · ${new Date(w.created_at).toLocaleString()}`,
        () => open.withdrawal(w, q))));
    }
    if (!panel.children.length) {
      const none = document.createElement("div");
      none.className = "gs-empty"; none.textContent = `Nothing found for “${q}”.`;
      panel.append(none);
    }
    panel.classList.remove("hidden");
  }

  async function run() {
    const q = input.value.trim();
    if (q.length < 2) { close(); return; }
    const mine = ++seq;
    try {
      const data = await api(`/api/admin/search?q=${encodeURIComponent(q)}`);
      if (mine === seq) render(data);   // ignore answers to older keystrokes
    } catch (e) {
      if (mine === seq) { panel.replaceChildren(Object.assign(document.createElement("div"), { className: "gs-empty", textContent: e.message || "Search failed" })); panel.classList.remove("hidden"); }
    }
  }

  input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(run, 300); });
  input.addEventListener("focus", () => { if (last && input.value.trim().length >= 2) panel.classList.remove("hidden"); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { close(); input.blur(); }
    if (e.key === "Enter") { e.preventDefault(); clearTimeout(timer); const first = panel.querySelector(".gs-item"); if (first && !panel.classList.contains("hidden")) first.click(); else run(); }
  });
  document.addEventListener("click", (e) => { if (!e.target.closest(".global-search")) close(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || "") && !document.activeElement?.isContentEditable) {
      e.preventDefault(); input.focus(); input.select();
    }
  });
}

/* ===========================================================================
   Bonuses & cashback (super admin): campaign switches, amounts, budgets, meters, cashback player rules
   =========================================================================== */
const BONUS_META = {
  cashback: { icon: "💸", name: "Daily cashback", pct: true, note: "A player who deposits and does not withdraw that day gets this percentage of the day's deposits back as bonus credit." },
  first_deposit: { icon: "🎁", name: "First deposit bonus", pct: true, note: "Percentage of a player's first approved deposit." },
  welcome: { icon: "👋", name: "Welcome bonus", note: "One-time credit when a player claims it." },
  daily: { icon: "📅", name: "Daily reward", note: "Credit a player can claim once a day." },
  referral: { icon: "🤝", name: "Referral reward", note: "Paid to the inviter when a friend finishes sign-up." },
};
const BONUS_ORDER = ["cashback", "first_deposit", "welcome", "daily", "referral"];

function bonusMeter(label, used, budget) {
  const pct = budget > 0 ? Math.min(100, Math.round((used * 100) / budget)) : 0;
  const cls = budget > 0 && used >= budget ? "full" : pct >= 80 ? "warn" : "ok";
  return `<div class="bm"><div class="bm-top"><span>${label}</span><strong>${money(used)} / ${money(budget)}</strong></div>
    <div class="bm-bar ${cls}"><i style="width:${pct}%"></i></div>${cls === "warn" ? '<small class="bm-flag">Over 80% used</small>' : cls === "full" ? '<small class="bm-flag">Budget used up: new rewards are paused</small>' : ""}</div>`;
}

function bonusCard(c) {
  const m = BONUS_META[c.reward_type] || { icon: "★", name: c.reward_type, note: "" };
  const unit = m.pct ? "%" : " ETB";
  const field = (key, label, value, help) => `<label class="bf"><span>${label}</span><input type="number" min="0" step="1" inputmode="numeric" data-field="${key}" data-orig="${value}" value="${value}">${help ? `<small>${help}</small>` : ""}</label>`;
  return `<article class="bonus-card" data-reward="${c.reward_type}">
    <header><div><h3>${m.icon} ${escapeHtml(m.name)}</h3><p class="muted small">${escapeHtml(m.note)}</p></div>
      <button type="button" class="bc-toggle ${c.enabled ? "on" : "off"}" data-act="toggle" aria-pressed="${c.enabled}">${c.enabled ? "On" : "Off"}</button></header>
    ${bonusMeter("Given today", c.today_etb, c.daily_budget_etb)}
    ${bonusMeter("Given in total", c.total_etb, c.total_budget_etb)}
    <p class="muted small">${c.today_count} reward${c.today_count === 1 ? "" : "s"} paid today</p>
    <div class="bf-grid">
      ${field("amount_etb", m.pct ? "Percentage" : "ETB per reward", c.amount_etb, m.pct ? "1 to 100" : "")}
      ${field("daily_budget_etb", "Daily budget (ETB)", c.daily_budget_etb)}
      ${field("total_budget_etb", "Total budget (ETB)", c.total_budget_etb)}
      ${c.reward_type === "cashback" ? field("per_player_daily_max_etb", "Max cashback per player per day (ETB)", c.per_player_daily_max_etb || 0, "0 means no limit") : field("per_player_cap", c.reward_type === "referral" ? "Max rewards per player" : "Rewards per player", c.per_player_cap)}
    </div>
    <div class="bf-save"><input type="text" data-field="reason" maxlength="200" placeholder="Reason for the change (at least 8 characters)"><button type="button" class="primary-btn" data-act="save">Save changes</button></div>
  </article>`;
}

async function loadBonuses() {
  const root = document.getElementById("bonuses-root");
  if (!root) return;
  const [data, rules] = await Promise.all([api("/api/admin/bonus/campaigns"), api("/api/admin/bonus/cashback/players")]);
  const byType = Object.fromEntries(data.campaigns.map((c) => [c.reward_type, c]));
  const cards = BONUS_ORDER.filter((k) => byType[k]).map((k) => bonusCard(byType[k]));
  const rulesHtml = rules.rules.length ? rules.rules.map((r) => `<li class="cr-row" data-player="${escapeHtml(r.player_id)}">
      <span><strong>${escapeHtml(r.display_name || r.player_id)}</strong>${r.phone_number ? ` · ${escapeHtml(r.phone_number)}` : ""}<small>${r.excluded ? "No cashback" : `Max ${money(r.daily_cap_etb)} per day`} · ${escapeHtml(r.reason || "")}</small></span>
      <button type="button" class="secondary-btn" data-act="rule-remove">Remove</button></li>`).join("") : '<li class="muted small">No player rules yet.</li>';
  root.innerHTML = `
    <article class="bonus-card bonus-global"><header><div><h3>All bonuses</h3><p class="muted small">One switch for every reward below, including cashback.</p></div>
      <button type="button" class="bc-toggle ${data.globally_enabled ? "on" : "off"}" data-act="global" aria-pressed="${data.globally_enabled}">${data.globally_enabled ? "On" : "Paused"}</button></header>
      <div class="bf-save"><input type="text" data-field="reason" maxlength="200" placeholder="Reason (needed to change this switch)"></div></article>
    ${cards[0] || ""}
    <article class="bonus-card" id="cashback-rules"><header><div><h3>💸 Cashback player rules</h3><p class="muted small">Keep a player out of cashback, or give them their own daily limit.</p></div></header>
      <div class="bf-save"><input type="search" id="cr-search" placeholder="Find a player: name, phone or Telegram ID" autocomplete="off"></div>
      <div id="cr-results" class="cr-results"></div>
      <div id="cr-form" class="cr-form hidden"></div>
      <h4 class="small">Current rules</h4><ul class="cr-list">${rulesHtml}</ul></article>
    ${cards.slice(1).join("")}`;
  wireBonuses(root);
}

function wireBonuses(root) {
  if (root._wired) return;
  root._wired = true;
  const reasonOf = (scope) => {
    const v = (scope.querySelector('[data-field="reason"]')?.value || "").trim();
    if (v.length < 8) { toast("Please write a reason of at least 8 characters.", "error"); scope.querySelector('[data-field="reason"]')?.focus(); return null; }
    return v;
  };
  const done = (msg) => { toast(msg, "success"); loadBonuses().catch((e) => toast(e.message, "error")); };

  root.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-act]");
    if (!btn) return;
    const card = btn.closest(".bonus-card");
    const act = btn.dataset.act;
    try {
      if (act === "global") {
        const reason = reasonOf(card); if (!reason) return;
        await api("/api/admin/bonus/global", { method: "PUT", body: JSON.stringify({ enabled: btn.getAttribute("aria-pressed") !== "true", reason }) });
        done("Saved");
      } else if (act === "toggle") {
        const reason = reasonOf(card); if (!reason) return;
        await api(`/api/admin/bonus/campaigns/${card.dataset.reward}`, { method: "PUT", body: JSON.stringify({ enabled: btn.getAttribute("aria-pressed") !== "true", reason }) });
        done("Saved");
      } else if (act === "save") {
        const reason = reasonOf(card); if (!reason) return;
        const body = { reason };
        card.querySelectorAll("input[data-orig]").forEach((i) => { if (i.value !== i.dataset.orig) body[i.dataset.field] = Number(i.value); });
        if (Object.keys(body).length === 1) { toast("Nothing changed.", "info"); return; }
        await api(`/api/admin/bonus/campaigns/${card.dataset.reward}`, { method: "PUT", body: JSON.stringify(body) });
        done("Saved");
      } else if (act === "rule-remove") {
        const reason = (window.prompt("Reason for removing this rule (at least 8 characters):") || "").trim();
        if (reason.length < 8) { toast("A reason of at least 8 characters is required.", "error"); return; }
        await api(`/api/admin/bonus/cashback/players/${encodeURIComponent(btn.closest(".cr-row").dataset.player)}`, { method: "PUT", body: JSON.stringify({ excluded: false, daily_cap_etb: null, reason }) });
        done("Rule removed");
      } else if (act === "rule-pick") {
        const form = document.getElementById("cr-form");
        form.dataset.player = btn.dataset.player;
        form.innerHTML = `<p><strong>${escapeHtml(btn.dataset.name)}</strong></p>
          <label class="bf-check"><input type="checkbox" id="cr-excluded"> No cashback for this player</label>
          <label class="bf"><span>Or their own max cashback per day (ETB)</span><input type="number" min="1" step="1" inputmode="numeric" id="cr-cap" placeholder="empty = campaign default"></label>
          <div class="bf-save"><input type="text" data-field="reason" maxlength="200" placeholder="Reason (at least 8 characters)"><button type="button" class="primary-btn" data-act="rule-save">Save rule</button></div>`;
        form.classList.remove("hidden");
        document.getElementById("cr-results").replaceChildren();
      } else if (act === "rule-save") {
        const form = document.getElementById("cr-form");
        const reason = reasonOf(form); if (!reason) return;
        const capRaw = document.getElementById("cr-cap").value.trim();
        await api(`/api/admin/bonus/cashback/players/${encodeURIComponent(form.dataset.player)}`, { method: "PUT", body: JSON.stringify({ excluded: document.getElementById("cr-excluded").checked, daily_cap_etb: capRaw ? Number(capRaw) : null, reason }) });
        done("Rule saved");
      }
    } catch (err) { toast(err.message || "Could not save", "error"); }
  });

  let timer = 0, seq = 0;
  root.addEventListener("input", (e) => {
    if (e.target.id !== "cr-search") return;
    clearTimeout(timer);
    const q = e.target.value.trim();
    const out = document.getElementById("cr-results");
    if (q.length < 2) { out.replaceChildren(); return; }
    timer = setTimeout(async () => {
      const mine = ++seq;
      try {
        const data = await api(`/api/admin/search?q=${encodeURIComponent(q)}`);
        if (mine !== seq) return;
        out.innerHTML = data.players.length ? data.players.map((p) => `<button type="button" class="gs-item" data-act="rule-pick" data-player="${escapeHtml(p.id)}" data-name="${escapeHtml(p.display_name || p.id)}">
            <span class="gs-icon">👤</span><span class="gs-text"><strong>${escapeHtml(p.display_name || "Player")}</strong><small>${[p.phone_number, p.telegram_id && `TG ${p.telegram_id}`].filter(Boolean).map(escapeHtml).join(" · ")}</small></span></button>`).join("")
          : '<p class="muted small">No players found.</p>';
      } catch (err) { /* ignore: search box shows nothing */ }
    }, 300);
  });
}
