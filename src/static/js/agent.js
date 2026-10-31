// Agent dashboard: float, commission, player top-ups. All money moves server-side; this page only
// sends requests (with an Idempotency-Key per action) and renders what the server returns.
const tg = window.Telegram?.WebApp;
tg?.ready?.();
const user = tg?.initDataUnsafe?.user;
const playerId = user ? `tg_${user.id}` : null;
const $ = (id) => document.getElementById(id);
const fmt = (n) => Number(n || 0).toLocaleString();
const idem = {};
const keyFor = (scope) => (idem[scope] ||= `${scope}-${crypto.randomUUID()}`);

function say(text, bad = false) {
  const el = $("msg");
  el.textContent = text || "";
  el.style.color = bad ? "var(--danger)" : "var(--accent)";
}

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (tg?.initData) headers["X-Telegram-Init-Data"] = tg.initData;
  const res = await fetch(path, { ...options, headers });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);
  return data;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

const LEDGER_LABEL = { topup: "Top-up", float_adjust: "Float from house", commission_cashout: "Commission cash-out", referral_commission: "Referral commission (player deposit)", reward_new_player: "New-player reward", reward_retention: "Retention reward" };
const REWARD_LABEL = { new_player: "New player", retention: "Player retention", milestone: "Milestone" };
const CREDIT_TYPES = new Set(["referral_commission", "reward_new_player", "reward_retention", "reward_milestone"]);

function render(me) {
  const a = me.agent;
  $("float").textContent = fmt(a.float_balance);
  $("commission").textContent = fmt(a.commission_balance);
  $("players").textContent = fmt(me.player_count);
  $("rate").textContent = `${me.commission_percent}%`;
  $("status-line").textContent = a.status === "active" ? `Agent code ${a.agent_code}` : "Your agent account is suspended";
  $("link").textContent = me.link || `Agent code: ${a.agent_code}`;
  $("ledger").innerHTML = me.ledger.length
    ? me.ledger.map((e) => {
        const amt = e.type === "commission_cashout" ? -e.commission_delta : CREDIT_TYPES.has(e.type) ? e.commission_delta : e.float_delta;
        const note = e.player_id ? ` · ${esc(e.player_id.replace("tg_", ""))}` : "";
        return `<div class="row"><span>${esc(LEDGER_LABEL[e.type] || e.type)}${note}<br><span class="muted">${new Date(e.created_at).toLocaleString()}</span></span>` +
          `<span class="${amt >= 0 ? "pos" : "neg"}">${amt >= 0 ? "+" : ""}${fmt(amt)}${e.commission_delta > 0 && e.type === "topup" ? `<br><span class="muted">+${fmt(e.commission_delta)} commission</span>` : ""}</span></div>`;
      }).join("")
    : '<p class="muted">Nothing yet.</p>';
}

function renderRewards(r) {
  const card = $("rewards-card");
  if (!r) { card.classList.add("hidden"); $("rewards-list-card").classList.add("hidden"); $("board-card").classList.add("hidden"); return; }
  [card, $("rewards-list-card"), $("board-card")].forEach((el) => el.classList.remove("hidden"));
  $("tier-badge").textContent = r.tier.label || "Starter";
  $("tier-line").textContent = `This month: ${fmt(r.monthly_volume)} ETB moved · you earn ${r.effective_percent}% per top-up`;
  const nxt = r.next_tier;
  const top = r.tiers.length ? r.tiers[r.tiers.length - 1].min_monthly_volume : 0;
  const cur = r.tiers.filter((t) => t.min_monthly_volume <= r.monthly_volume).pop();
  const from = cur ? cur.min_monthly_volume : 0;
  const to = nxt ? r.monthly_volume + nxt.remaining : top;
  const pct = nxt ? Math.max(0, Math.min(100, ((r.monthly_volume - from) / Math.max(1, to - from)) * 100)) : 100;
  $("tier-bar").style.width = `${pct}%`;
  $("tier-next").textContent = nxt
    ? `${fmt(nxt.remaining)} ETB more this month to reach ${nxt.label} (${nxt.percent}%)`
    : (r.tiers.length ? "You are on the top tier. Great work!" : "");
  $("rw-pending").textContent = fmt(r.totals.pending);
  $("rw-paid").textContent = fmt(r.totals.paid);
  const u = r.rules;
  $("rw-rules").textContent = r.enabled
    ? `New player: ${fmt(u.new_player_bounty)} ETB when a player you brought makes a first deposit of at least ${fmt(u.min_first_deposit)} ETB and plays ${u.min_rounds_played} rounds. ` +
      `Rewards are held ${u.hold_days} days before they are paid. Retention: ${fmt(u.retention_bonus)} ETB if that player is still playing after ${u.retention_days} days. ` +
      `Your tier is based on this month's volume and resets each month.`
    : "Player rewards are currently switched off. Tier rates still apply.";
  $("rw-list").innerHTML = r.rewards.length
    ? r.rewards.map((x) => `<div class="row"><span>${esc(REWARD_LABEL[x.type] || x.type)}<br><span class="muted">${x.status === "pending" ? "Pays after " + new Date(x.release_at).toLocaleDateString() : new Date(x.created_at).toLocaleDateString()}</span></span>` +
        `<span style="text-align:right"><strong>${fmt(x.amount)}</strong><br><span class="pill ${esc(x.status)}">${x.status === "pending" ? "on hold" : esc(x.status)}</span></span></div>`).join("")
    : '<p class="muted">No rewards yet. Bring a new player and they appear here.</p>';
  $("board").innerHTML = r.leaderboard.length
    ? r.leaderboard.map((b) => `<div class="row ${b.is_me ? "me" : ""}"><span>#${b.rank} ${b.is_me ? "You" : esc(b.agent_code)}</span><span>${fmt(b.volume)} ETB</span></div>`).join("")
    : '<p class="muted">No activity this week yet.</p>';
}

async function loadPlayers() {
  const { players } = await api(`/api/agent/players?player_id=${encodeURIComponent(playerId)}`);
  $("player-list").innerHTML = players.length
    ? players.map((p) => `<div class="row"><span>${esc(p.display_name)}</span><span class="muted">${esc(p.telegram_id)}</span></div>`).join("")
    : '<p class="muted">No players linked yet. Share your invite link or top someone up.</p>';
}

async function refresh() {
  const me = await api(`/api/agent/me?player_id=${encodeURIComponent(playerId)}`);
  if (!me.is_agent) { $("not-agent").classList.remove("hidden"); $("agent-view").classList.add("hidden"); return; }
  $("not-agent").classList.add("hidden");
  $("agent-view").classList.remove("hidden");
  render(me);
  renderRewards(me.rewards);
  await loadPlayers();
}

async function act(btn, scope, path, body, okText) {
  btn.disabled = true;
  say("");
  try {
    const r = await api(path, { method: "POST", headers: { "Idempotency-Key": keyFor(scope) }, body: JSON.stringify({ player_id: playerId, ...body }) });
    delete idem[scope];  // done: the next attempt is a new action
    say(okText(r));
    await refresh();
    return true;
  } catch (e) {
    if (!/in progress|409/.test(e.message)) delete idem[scope];  // clean rejection: allow a fresh attempt
    say(e.message, true);
    return false;
  } finally {
    btn.disabled = false;
  }
}

$("t-btn").addEventListener("click", async () => {
  const telegram_id = $("t-id").value.trim();
  const amount = Number($("t-amount").value);
  if (!Number.isInteger(amount)) return say("Enter a whole amount", true);
  const scope = `agent-topup-${telegram_id}-${amount}`;
  const ok = await act($("t-btn"), scope, "/api/agent/topup", { telegram_id, amount },
    (r) => `Topped up ${r.player_name} with ${fmt(r.amount)} ETB (commission +${fmt(r.commission)})`);
  if (ok) { $("t-amount").value = ""; }
});

$("c-btn").addEventListener("click", async () => {
  const amount = Number($("c-amount").value);
  if (!Number.isInteger(amount)) return say("Enter a whole amount", true);
  const ok = await act($("c-btn"), `agent-cashout-${amount}`, "/api/agent/commission/cashout", { amount },
    (r) => `Moved ${fmt(r.amount)} ETB commission to your wallet`);
  if (ok) $("c-amount").value = "";
});

$("copy-btn").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText($("link").textContent); say("Link copied"); } catch { say("Copy failed - select the link and copy it", true); }
});

if (!playerId) {
  say("Open this page from the Telegram bot.", true);
} else {
  refresh().catch((e) => say(e.message, true));
}
