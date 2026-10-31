// Melbete Bingo - player lobby
//
// This page is the shared entry point for Bingo and the ONE place
// the profile/wallet (deposit, withdraw, transaction history, referrals)
// lives alongside the Bingo lobby as shared account-level functionality.
// The player-facing game selection contains Bingo only.
//
// The wallet/profile code below is ported directly from app.js (Bingo's
// former all-in-one page) rather than rewritten from scratch - it's
// tested, working, real-money-handling code, and this move is a relocate,
// not a rebuild.

const tg = window.Telegram?.WebApp;
tg?.ready();
tg?.expand();

const API = "";

const state = {
  playerId: null,
  telegramId: null,
  displayName: "Player",
  supportContact: "@melbete_support_1",
  paymentMethods: [],
  withdrawAccounts: {},
  selectedDepositMethod: null,
  selectedWithdrawMethod: null,
  depositScreenshotDataUrl: null,
};

// ---------------------------------------------------------------------
// Identity
// ---------------------------------------------------------------------
function initIdentity() {
  const user = tg?.initDataUnsafe?.user;
  if (user) {
    state.telegramId = String(user.id);
    state.playerId = `tg_${user.id}`;
    state.displayName = [user.first_name, user.last_name].filter(Boolean).join(" ") || user.username || "Player";
    return;
  }
  throw new Error("For your security, this must be opened from the official Telegram bot.");
}

// ---------------------------------------------------------------------
// View + toast helpers
// ---------------------------------------------------------------------
function showView(id) {
  document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
  document.getElementById(id)?.classList.add("active");
}

function toast(msg, kind = "info") {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.className = `toast ${kind}`;
  el.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), 3500);
}

// One Idempotency-Key per user action, reused on retry (see app.js for the full note).
const _idemKeys = {};
function idemKey(scope) {
  if (!_idemKeys[scope]) _idemKeys[scope] = `${scope}-${crypto.randomUUID()}`;
  return _idemKeys[scope];
}
function clearIdemKey(scope) { delete _idemKeys[scope]; }

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (tg?.initData) headers["X-Telegram-Init-Data"] = tg.initData;
  const res = await fetch(`${API}${path}`, { ...options, headers });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.detail || `Request failed (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return data;
}

function isIdentityError(e) {
  return e && e.status === 403;
}

function openModal(id) { document.getElementById(id).classList.remove("hidden"); }
function closeModal(id) { document.getElementById(id).classList.add("hidden"); }

// ---------------------------------------------------------------------
// Wallet / header
// ---------------------------------------------------------------------
// Live balance chip on the floating footer: bumps when the amount changes.
function updateFooterChip(balance) {
  const val = document.getElementById("hub-chip-value");
  if (!val) return;
  const next = Number(balance).toLocaleString();
  if (val.textContent !== next) {
    val.textContent = next;
    const chip = document.getElementById("hub-balance-chip");
    chip?.classList.remove("bump"); void chip?.offsetWidth; chip?.classList.add("bump");
  }
}

// Green pulse on the centre Deposit button while a deposit is under review.
function setDepositDot(pending) {
  document.getElementById("hub-dot-deposit")?.classList.toggle("hidden", !pending);
  document.getElementById("hub-tab-deposit")?.classList.toggle("has-dot", !!pending);
}

async function refreshWallet() {
  try {
    const wallet = await api(`/api/wallet/${state.playerId}`);
    document.getElementById("balance-real").textContent = wallet.balance;
    updateFooterChip(wallet.balance);
    refreshDepositTimeline();   // not awaited; has its own error handling
  } catch (e) { /* non-fatal - header just won't update this tick */ }
}

// ---------------------------------------------------------------------
// Deposit timeline: Submitted -> Reviewing -> Credited.
// Driven by the player's latest top_up_* transaction (status pending /
// completed / rejected). Display only - the admin still approves deposits.
// ---------------------------------------------------------------------
const DEPOSIT_TIMELINE = { lastFetch: 0, lastStatus: null, busy: false };
const DT_HIDE_AFTER_MS = 24 * 60 * 60 * 1000;   // finished deposits fade away after a day

function dtDismissed() { try { return localStorage.getItem("melbete_dt_dismissed") || ""; } catch (e) { return ""; } }
function dtDismiss(id) { try { localStorage.setItem("melbete_dt_dismissed", id); } catch (e) { /* storage may be blocked */ } }

function dtAgo(iso) {
  const mins = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
  if (mins < 1) return mbT("ago_now");
  if (mins < 60) return mbT("ago_min", { n: mins });
  const h = Math.round(mins / 60);
  return h < 24 ? mbT("ago_hr", { n: h }) : mbT("ago_day", { n: Math.round(h / 24) });
}

async function refreshDepositTimeline() {
  const D = DEPOSIT_TIMELINE;
  const box = document.getElementById("deposit-timeline");
  if (!box || !state.playerId || D.busy) return;
  // Poll quickly while a deposit is being reviewed, lazily otherwise.
  const wait = D.lastStatus === "pending" ? 7000 : 60000;
  if (Date.now() - D.lastFetch < wait) return;
  D.busy = true;
  try {
    const res = await api(`/api/wallet/${state.playerId}/transactions?limit=10`);
    D.lastFetch = Date.now();
    const t = (res.transactions || [])
      .filter((x) => /^(top_up|withdrawal)/.test(String(x.type)))
      .sort((a, b) => new Date(b.created_at) - new Date(a.created_at))[0];
    D.lastStatus = t ? t.status : null;
    setDepositDot(!!t && t.status === "pending" && String(t.type).startsWith("top_up"));
    renderDepositTimeline(box, t);
  } catch (e) { /* non-fatal - keep whatever is showing */ }
  finally { D.busy = false; }
}

function renderDepositTimeline(box, t) {
  const finished = t && t.status !== "pending";
  const stale = t && finished && Date.now() - new Date(t.created_at).getTime() > DT_HIDE_AFTER_MS;
  if (!t || stale || dtDismissed() === t.id) { box.classList.add("hidden"); box.innerHTML = ""; return; }

  const credited = t.status === "completed";
  const rejected = t.status === "rejected";
  const isWd = String(t.type).startsWith("withdrawal");
  // [label, state] - deposit: Requested > Reviewing > Credited; withdrawal: Requested > Reviewing > Paid
  const lastLabel = isWd ? (rejected ? "tr_refunded" : "tr_paid") : (rejected ? "tr_notcredited" : "tr_credited");
  const steps = [
    ["tr_requested", "done"],
    ["tr_reviewing", credited || rejected ? "done" : "now"],
    [lastLabel, credited ? "done" : rejected ? "fail" : "todo"],
  ];
  const noteKey = (isWd ? "tr_note_wd_" : "tr_note_dep_") + (credited ? "done" : rejected ? "fail" : "pending");
  const note = mbT(noteKey);

  box.innerHTML = "";
  const head = document.createElement("div");
  head.className = "dt-head";
  const logoUrl = getMethodLogo({ key: t.payment_method, name: t.payment_method });
  if (logoUrl) {
    const img = document.createElement("img");
    img.className = "dt-logo"; img.src = logoUrl; img.alt = "";
    head.appendChild(img);
  }
  const title = document.createElement("div");
  title.className = "dt-title";
  const strong = document.createElement("strong");
  strong.textContent = `${Math.abs(t.amount)} ${mbT("chip_etb")}`;
  const when = document.createElement("span");
  when.textContent = dtAgo(t.created_at);
  title.append(strong, when);
  head.appendChild(title);
  const kindChip = document.createElement("span");
  kindChip.className = "dt-kind"; kindChip.textContent = mbT(isWd ? "tr_wd" : "tr_dep");
  head.appendChild(kindChip);
  if (finished) {
    const x = document.createElement("button");
    x.type = "button"; x.className = "dt-close"; x.setAttribute("aria-label", "Dismiss");
    x.textContent = "\u00d7";
    x.addEventListener("click", () => { dtDismiss(t.id); box.classList.add("hidden"); });
    head.appendChild(x);
  }
  box.appendChild(head);

  const ol = document.createElement("ol");
  ol.className = "dt-steps";
  steps.forEach(([labelKey, st], i) => {
    const li = document.createElement("li");
    li.className = `dt-step ${st}`;
    if (st === "now") li.setAttribute("aria-current", "step");
    const dot = document.createElement("span");
    dot.className = "dt-dot"; dot.setAttribute("aria-hidden", "true");
    dot.textContent = st === "done" ? "\u2713" : st === "fail" ? "!" : String(i + 1);
    const l = document.createElement("span");
    l.className = "dt-label"; l.textContent = mbT(labelKey);
    li.append(dot, l);
    ol.appendChild(li);
  });
  box.appendChild(ol);

  const p = document.createElement("p");
  p.className = "dt-note"; p.textContent = note;
  box.appendChild(p);
  box.classList.remove("hidden");
}

// ---------------------------------------------------------------------
// Promotions panel (footer "Promos" tab + floating Cashback icon).
// Driven by /api/bonus/summary; shows every live campaign with its real status.
// Cashback appears as soon as a "cashback" campaign exists in the summary;
// until then it is shown as "coming soon".
// ---------------------------------------------------------------------
const PROMO_META = {
  welcome: { icon: "🎁", title: "Welcome gift", detail: "Free credit for Bingo cartelas." },
  daily: { icon: "📅", title: "Daily bonus", detail: "Come back every day for a new reward." },
  first_deposit: { icon: "💰", title: "First deposit bonus", detail: "Added automatically when your first deposit is approved." },
  cashback: { icon: "💸", title: "Cashback", detail: "Get a share of your play back as bonus credit." },
};

function claimableCount(data) {
  const r = (data && data.rewards) || {};
  return ["welcome", "daily"].filter((k) => r[k] && r[k].status === "available").length;
}

function setPromoBadges(count) {
  const b = document.getElementById("hub-badge-promos");
  if (b) { b.textContent = String(count); b.classList.toggle("hidden", !(count > 0)); }
  document.getElementById("cashback-dot")?.classList.toggle("hidden", !(count > 0));
}

async function refreshPromoBadge() {
  if (!state.playerId || document.hidden) return;
  try {
    const data = await api(`/api/bonus/summary/${encodeURIComponent(state.playerId)}`);
    setPromoBadges(claimableCount(data));
  } catch (e) { /* badge just stays as it was */ }
}

function promoCard(kind, info, data) {
  const meta = PROMO_META[kind] || { icon: "🎁", title: kind, detail: "" };
  const card = document.createElement("div");
  card.className = "promo-card";
  card.dataset.promo = kind;
  const ico = document.createElement("span");
  ico.className = "promo-card-icon";
  ico.textContent = meta.icon;
  const body = document.createElement("div");
  body.className = "promo-card-body";
  const title = document.createElement("strong");
  const value = info
    ? (kind === "first_deposit" ? ` · +${info.amount_etb}%` : ` · ${info.amount_etb} ETB`)
    : "";
  title.textContent = meta.title + value;
  const detail = document.createElement("span");
  detail.textContent = meta.detail;
  body.append(title, detail);
  const action = document.createElement("div");
  action.className = "promo-card-action";

  const tag = (text, cls) => {
    const t = document.createElement("span");
    t.className = `promo-state ${cls || ""}`;
    t.textContent = text;
    return t;
  };
  const btn = (label, fn) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "promo-card-btn";
    b.textContent = label;
    b.addEventListener("click", () => fn(b));
    return b;
  };

  if (!info) {
    action.append(tag("Coming soon", "soon"));
  } else if (info.status === "available") {
    action.append(btn("Claim", (b) => claimPromoReward(kind, b)));
  } else if (info.status === "automatic") {
    action.append(btn("Deposit", () => { closeModal("promo-modal"); setHubTab("deposit"); openDepositModal(); }));
  } else if (info.status === "claimed") {
    action.append(tag(kind === "daily" ? "Come back tomorrow" : "Claimed ✓", "done"));
  } else {
    action.append(tag("Not active", "paused"));
  }
  card.append(ico, body, action);
  return card;
}

function renderPromoList(data) {
  const list = document.getElementById("promo-list");
  if (!list) return;
  const bal = document.getElementById("promo-bonus-balance");
  if (bal) bal.textContent = data.bonus_balance ?? 0;
  const rewards = data.rewards || {};
  list.innerHTML = "";
  ["welcome", "daily", "first_deposit"].forEach((k) => { if (rewards[k]) list.appendChild(promoCard(k, rewards[k], data)); });
  // Cashback: real campaign if the backend provides one, otherwise a clear placeholder.
  list.appendChild(promoCard("cashback", rewards.cashback || null, data));

  const ref = document.createElement("div");
  ref.className = "promo-card";
  ref.dataset.promo = "referral";
  const ico = document.createElement("span");
  ico.className = "promo-card-icon";
  ico.textContent = "🤝";
  const body = document.createElement("div");
  body.className = "promo-card-body";
  const t = document.createElement("strong");
  t.textContent = "Refer & Earn";
  const d = document.createElement("span");
  const ok = data.referrals ? data.referrals.successful : 0;
  d.textContent = `Invite friends and earn bonus credit · ${ok} successful referral${ok === 1 ? "" : "s"}`;
  body.append(t, d);
  const act = document.createElement("div");
  act.className = "promo-card-action";
  const b = document.createElement("button");
  b.type = "button";
  b.className = "promo-card-btn";
  b.textContent = "Invite";
  b.addEventListener("click", () => { closeModal("promo-modal"); setHubTab("refer"); openReferralModal(); });
  act.appendChild(b);
  ref.append(ico, body, act);
  list.appendChild(ref);
}

async function claimPromoReward(kind, btn) {
  btn.disabled = true;
  try {
    const key = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : `claim-${state.playerId}-${kind}-${Date.now()}`;
    const res = await api(`/api/bonus/${kind}/claim/${encodeURIComponent(state.playerId)}`, { method: "POST", body: JSON.stringify({ idempotency_key: key }) });
    toast(res.status === "granted" ? `Bonus credited: ${res.amount_etb} ETB` : "Reward already processed", "success");
    await refreshWallet();
    renderPromoBanner();
    await openPromoModal(null, true);
  } catch (e) {
    toast(typeof e.message === "string" ? e.message : "Unable to claim this reward", "error");
    btn.disabled = false;
  }
}

async function openPromoModal(focus, keepOpen) {
  if (!keepOpen) { setHubTab("promos"); openModal("promo-modal"); }
  const list = document.getElementById("promo-list");
  try {
    const data = await api(`/api/bonus/summary/${encodeURIComponent(state.playerId)}`);
    renderPromoList(data);
    setPromoBadges(claimableCount(data));
  } catch (e) {
    if (list) list.innerHTML = '<p class="muted small">Promotions are unavailable right now. Please try again shortly.</p>';
  }
  if (focus) list?.querySelector(`[data-promo="${focus}"]`)?.scrollIntoView({ block: "center" });
}

// ---------------------------------------------------------------------
// Promo banner - driven by the real bonus campaigns (/api/bonus/summary).
// Shows at most one offer; renders nothing when no campaign is active.
// ---------------------------------------------------------------------
async function renderPromoBanner() {
  const old = document.getElementById("promo-banner");
  try {
    const data = await api(`/api/bonus/summary/${encodeURIComponent(state.playerId)}`);
    const r = data.rewards || {};
    let offer = null;
    if (r.welcome && r.welcome.status === "available") {
      offer = { kind: "welcome", tag: "Welcome gift", title: `Claim ${r.welcome.amount_etb} ETB bonus`, detail: "Free credit for Bingo cartelas.", cta: "Claim" };
    } else if (r.first_deposit && r.first_deposit.status === "automatic") {
      offer = { kind: "deposit", tag: "First deposit", title: `+${r.first_deposit.amount_etb}% bonus`, detail: "Added automatically when your first deposit is approved.", cta: "Deposit" };
    } else if (r.daily && r.daily.status === "available") {
      offer = { kind: "daily", tag: "Daily bonus", title: `Claim ${r.daily.amount_etb} ETB today`, detail: "Come back every day for a new reward.", cta: "Claim" };
    }
    if (!offer) { old?.remove(); return; }
    const el = old || document.createElement("section");
    el.id = "promo-banner";
    el.className = "promo-banner";
    el.setAttribute("aria-label", "Promotion");
    el.innerHTML = `<div class="promo-copy"><span class="promo-tag">${offer.tag}</span>` +
      `<strong class="promo-title">${offer.title}</strong><span class="promo-detail">${offer.detail}</span></div>` +
      `<button type="button" class="promo-cta">${offer.cta}</button>`;
    if (!old) document.getElementById("cartela-quick-picker")?.before(el);
    el.querySelector(".promo-cta").addEventListener("click", async (ev) => {
      if (offer.kind === "deposit") { setHubTab("deposit"); openDepositModal(); return; }
      const btn = ev.currentTarget;
      btn.disabled = true;
      try {
        const key = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : `claim-${state.playerId}-${offer.kind}-${Date.now()}`;
        const res = await api(`/api/bonus/${offer.kind}/claim/${encodeURIComponent(state.playerId)}`, { method: "POST", body: JSON.stringify({ idempotency_key: key }) });
        if (typeof toast === "function") toast(res.status === "granted" ? `Bonus credited: ${res.amount_etb} ETB` : "Reward already processed", "success");
        await refreshWallet();
        renderPromoBanner();
      } catch (e) {
        if (typeof toast === "function") toast(typeof e.message === "string" ? e.message : "Unable to claim this reward", "error");
        btn.disabled = false;
      }
    });
  } catch (e) { old?.remove(); /* banner is optional - never block the hub */ }
}

// ---------------------------------------------------------------------
// Quick cartela picker (home) - reserve a cartela without opening the room.
// Uses the same /api/lobby + select-cartela endpoints as the bingo room.
// ---------------------------------------------------------------------
let quickPickBusy = false;

function secondsLeft(room) {
  if (!room.countdown_started_at) return null;
  const left = room.countdown_seconds - (Date.parse(room.server_time) - Date.parse(room.countdown_started_at)) / 1000;
  return Math.max(0, Math.round(left));
}

async function pickQuickCartela(room, n) {
  if (quickPickBusy) return;
  quickPickBusy = true;
  document.querySelectorAll("#cartela-quick-picker button").forEach((b) => { b.disabled = true; });
  try {
    await api(`/api/session/${room.id}/select-cartela`, {
      method: "POST",
      body: JSON.stringify({ player_id: state.playerId, cartela_number: n }),
    });
    window.location.href = GAME_ROUTES.bingo;
  } catch (e) {
    quickPickBusy = false;
    toast(e.message, "error");
    if (e.status === 402) { setHubTab("deposit"); openDepositModal(); }
    renderCartelaPicker();
  }
}

async function renderCartelaPicker() {
  const box = document.getElementById("cartela-quick-picker");
  if (!box || quickPickBusy) return;
  try {
    const lobby = await api("/api/lobby");
    const room = Object.values(lobby)[0];
    if (!room) { box.classList.add("hidden"); document.body.classList.remove("has-quick-picker"); return; }
    box.classList.remove("hidden");
    const left = secondsLeft(room);
    const head = document.createElement("div");
    head.className = "qp-head";
    head.innerHTML = '<div><h2>Pick your cartela</h2><p class="qp-sub"></p></div>';
    const sub = head.querySelector(".qp-sub");
    if (room.state !== "waiting") {
      sub.textContent = "A round is in progress.";
      const watch = document.createElement("button");
      watch.type = "button"; watch.className = "qp-link"; watch.textContent = "Watch live";
      watch.addEventListener("click", () => { window.location.href = GAME_ROUTES.bingo; });
      box.replaceChildren(head, watch);
      document.body.classList.remove("has-quick-picker");
      return;
    }
    sub.textContent = `Stake ${room.stake_amount} ETB` + (left !== null ? ` - starts in ${left}s` : " - round starts when a cartela is picked");
    const taken = new Set(room.taken_cartelas || []);
    const free = [];
    for (let n = room.cartela_min || 1; n <= (room.cartela_max || 500); n++) if (!taken.has(n)) free.push(n);
    const random = document.createElement("button");
    random.type = "button"; random.className = "qp-random"; random.textContent = "Pick for me";
    random.disabled = free.length === 0;
    random.addEventListener("click", () => pickQuickCartela(room, free[Math.floor(Math.random() * free.length)]));
    head.appendChild(random);
    const legend = document.createElement("p");
    legend.className = "qp-legend";
    legend.innerHTML = `<span><i class="qp-dot free"></i>Free ${free.length}</span><span><i class="qp-dot taken"></i>Taken ${taken.size}</span>`;
    const grid = document.createElement("div");
    grid.className = "qp-grid";
    for (let n = room.cartela_min || 1; n <= (room.cartela_max || 500); n++) {
      const b = document.createElement("button");
      b.type = "button"; b.className = "qp-chip"; b.textContent = n;
      b.setAttribute("aria-label", `Cartela ${n}`);
      if (taken.has(n)) { b.disabled = true; b.classList.add("taken"); b.setAttribute("aria-label", `Cartela ${n}, taken`); }
      else b.addEventListener("click", () => pickQuickCartela(room, n));
      grid.appendChild(b);
    }
    box.replaceChildren(head, legend, grid);
    document.body.classList.add("has-quick-picker");
  } catch (e) { box.classList.add("hidden"); document.body.classList.remove("has-quick-picker"); /* optional - never block the hub */ }
}

async function loadPaymentMethods() {
  const data = await api("/api/payment-methods");
  state.paymentMethods = data.methods;
}

// ---------------------------------------------------------------------
// Game selection cards
// ---------------------------------------------------------------------
const GAME_ROUTES = { bingo: "/bingo" };

function wireGameCards() {
  document.querySelectorAll(".game-card[data-game]").forEach((card) => {
    card.addEventListener("click", () => {
      const game = card.dataset.game;
      const url = GAME_ROUTES[game];
      if (url) window.location.href = url;
    });
  });
}

// ---------------------------------------------------------------------
// Deposit modal
// ---------------------------------------------------------------------
function getMethodLogo(method) {
  const key = (method?.key || "").toLowerCase();
  const name = (method?.name || "").toLowerCase();
  if (key.includes("tele") || name.includes("tele")) return "/static/img/payment/telebirr.png";
  if (key.includes("cbe") || name.includes("cbe")) return "/static/img/payment/cbebirr.png";
  return null;
}

/** Builds a method-card button (logo + name) used by both the deposit and
 * withdraw method pickers. */
function buildMethodCard(method, label, onSelect) {
  const btn = document.createElement("button");
  btn.className = "method-card";
  btn.type = "button";

  const logoUrl = getMethodLogo(method);
  if (logoUrl) {
    const img = document.createElement("img");
    img.className = "method-card-logo";
    img.src = logoUrl;
    img.alt = label;
    btn.appendChild(img);
  } else {
    const placeholder = document.createElement("span");
    placeholder.className = "method-card-logo placeholder";
    placeholder.textContent = "💳";
    btn.appendChild(placeholder);
  }

  const nameSpan = document.createElement("span");
  nameSpan.textContent = label;
  btn.appendChild(nameSpan);

  const check = document.createElement("span");
  check.className = "method-card-check";
  check.textContent = "✓";
  btn.appendChild(check);

  btn.addEventListener("click", onSelect);
  return btn;
}

/** Any element with data-copy="<value>" becomes tap-to-copy with a
 * "Copied!" confirmation toast - used for every payment account number
 * shown anywhere in the app. */
function wireCopyableElements(root) {
  root.querySelectorAll("[data-copy]").forEach((el) => {
    el.addEventListener("click", async () => {
      const value = el.dataset.copy;
      try {
        await navigator.clipboard.writeText(value);
      } catch (e) {
        const tmp = document.createElement("textarea");
        tmp.value = value;
        tmp.style.position = "fixed";
        tmp.style.opacity = "0";
        document.body.appendChild(tmp);
        tmp.select();
        try { document.execCommand("copy"); } catch (e2) { /* give up silently */ }
        document.body.removeChild(tmp);
      }
      toast("Copied!", "success");
    });
  });
}

async function openDepositModal() {
  try {
    await loadPaymentMethods();
  } catch (e) {
    toast(mbT("tt_methods_fail"), "error");
    return;
  }
  const list = document.getElementById("deposit-methods");
  list.innerHTML = "";
  const methods = state.paymentMethods.filter((m) => m.supports_deposit);

  if (methods.length === 0) {
    const p = document.createElement("p");
    p.className = "muted small";
    p.textContent = mbT("tt_no_dep_methods");
    list.appendChild(p);
  } else {
    const groups = {};
    methods.forEach((m) => {
      if (!groups[m.name]) groups[m.name] = [];
      groups[m.name].push(m);
    });

    Object.keys(groups).sort().forEach((name) => {
      const group = groups[name];
      const btn = buildMethodCard(group[0], name, () => {
        document.querySelectorAll("#deposit-methods .method-card").forEach((b) => b.classList.remove("selected"));
        btn.classList.add("selected");
        if (group.length === 1) {
          selectDepositMethod(group[0].key);
        } else {
          showDepositAccountSelector(name, group);
        }
      });
      list.appendChild(btn);
    });
  }
  document.getElementById("deposit-account-info").classList.add("hidden");
  document.getElementById("deposit-amount").value = "";
  if (document.getElementById("deposit-sms-text")) {
    const smsEl = document.getElementById("deposit-sms-text");
    smsEl.value = "";
    autoGrowTextarea(smsEl);
  }
  document.getElementById("deposit-screenshot").value = "";
  document.getElementById("deposit-screenshot-preview").classList.add("hidden");
  state.depositScreenshotDataUrl = null;
  try {
    const limits = await api(`/api/wallet/${state.playerId}/limits`);
    document.getElementById("deposit-limits").textContent = mbT("dep_limits", { min: limits.min_deposit, max: limits.max_deposit });
  } catch (e) { /* non-fatal */ }
  openModal("deposit-modal");
}

function selectDepositMethod(key) {
  state.selectedDepositMethod = key;
  const method = state.paymentMethods.find((m) => m.key === key);
  const box = document.getElementById("deposit-account-info");
  if (method) {
    box.innerHTML = "";

    const logoUrl = getMethodLogo(method);
    if (logoUrl) {
      const img = document.createElement("img");
      img.className = "account-info-logo";
      img.src = logoUrl;
      img.alt = method.name;
      box.appendChild(img);
    }

    const body = document.createElement("div");
    body.className = "account-info-body";

    const nameStrong = document.createElement("span");
    nameStrong.className = "account-info-name";
    nameStrong.textContent = method.name;
    body.appendChild(nameStrong);

    const row1 = document.createElement("div");
    row1.className = "account-info-row";
    row1.appendChild(document.createTextNode(mbT("dep_holder", { name: method.account_holder || mbT("dep_holder_none") })));
    body.appendChild(row1);

    const row2 = document.createElement("div");
    row2.className = "account-info-row account-number-row";
    row2.appendChild(melbeteAccountCopy(method.account_info, mbT("copy"), mbT("copied")));
    body.appendChild(row2);

    if (method.instructions) {
      const instrSpan = document.createElement("div");
      instrSpan.className = "muted small";
      instrSpan.style.marginTop = "4px";
      instrSpan.textContent = method.instructions;
      body.appendChild(instrSpan);
    }

    box.appendChild(body);
    box.classList.remove("hidden");
    wireCopyableElements(box);
  }
}

function showDepositAccountSelector(name, accounts) {
  const box = document.getElementById("deposit-account-info");
  box.innerHTML = "";
  box.classList.remove("hidden");
  box.style.display = "block";

  const title = document.createElement("p");
  title.className = "small muted";
  title.style.marginBottom = "8px";
  title.textContent = mbT("dep_pick_account", { name });
  box.appendChild(title);

  const grid = document.createElement("div");
  grid.style.display = "grid";
  grid.style.gap = "8px";

  accounts.forEach((m) => {
    const btn = document.createElement("button");
    btn.className = "secondary-btn";
    btn.style.margin = "0";
    btn.style.fontSize = "0.85rem";
    btn.style.padding = "10px";
    btn.style.textAlign = "left";
    btn.textContent = `${m.account_holder} (${m.account_info})`;
    btn.addEventListener("click", () => {
      box.style.display = "flex";
      selectDepositMethod(m.key);
    });
    grid.appendChild(btn);
  });
  box.appendChild(grid);
}

function wireDepositScreenshotInput() {
  document.getElementById("deposit-screenshot")?.addEventListener("change", (e) => {
    const file = e.target.files?.[0];
    if (!file) return;
    if (file.size > 6 * 1024 * 1024) {
      toast(mbT("tt_shot_big"), "error");
      e.target.value = "";
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      state.depositScreenshotDataUrl = reader.result;
      const preview = document.getElementById("deposit-screenshot-preview");
      preview.src = reader.result;
      preview.classList.remove("hidden");
    };
    reader.readAsDataURL(file);
  });
}

/** Grows a textarea's height to fit its content instead of scrolling
 * internally - used for the deposit SMS box since pasted SMS text
 * varies a lot in length. */
function autoGrowTextarea(el) {
  el.style.height = "auto";
  el.style.height = `${el.scrollHeight}px`;
}

function wireAutoGrowTextareas() {
  const el = document.getElementById("deposit-sms-text");
  if (!el) return;
  const grow = () => autoGrowTextarea(el);
  el.addEventListener("input", grow);
  el.addEventListener("paste", () => setTimeout(grow, 0));
  grow();
}

async function submitDeposit() {
  const amount = Number(document.getElementById("deposit-amount").value);
  const smsText = document.getElementById("deposit-sms-text")?.value?.trim();

  if (!state.selectedDepositMethod) return toast(mbT("tt_pick_method"), "error");
  if (!amount) return toast(mbT("tt_need_amount"), "error");
  if (!smsText) return toast(mbT("tt_need_sms"), "error");
  if (!state.depositScreenshotDataUrl) {
    return toast(mbT("tt_need_shot"), "error");
  }
  const btn = document.getElementById("deposit-submit-btn");
  btn.disabled = true;
  try {
    const res = await api(`/api/wallet/${state.playerId}/deposit`, {
      method: "POST",
      body: JSON.stringify({
        amount,
        payment_method: state.selectedDepositMethod,
        sms_text: smsText,
        screenshot_base64: state.depositScreenshotDataUrl,
      }),
    });
    toast(mbT("tt_dep_sent"), "success");
    closeModal("deposit-modal");
    window.MB_A?.receipt({ kind: "deposit", id: res.transaction_id, amount: res.amount ?? amount, method: state.selectedDepositMethod });
    DEPOSIT_TIMELINE.lastFetch = 0;   // show the new deposit's timeline immediately
    refreshWallet();
  } catch (e) {
    toast(e.message, "error");
  } finally {
    btn.disabled = false;
  }
}

// ---------------------------------------------------------------------
// Withdraw modal
// ---------------------------------------------------------------------
async function openWithdrawModal() {
  try {
    await loadPaymentMethods();
  } catch (e) {
    toast(mbT("tt_methods_fail"), "error");
    return;
  }
  try {
    const res = await api(`/api/wallet/${state.playerId}/withdrawal-accounts`);
    state.withdrawAccounts = res.accounts || {};
  } catch (e) {
    state.withdrawAccounts = {};
  }
  const list = document.getElementById("withdraw-methods");
  list.innerHTML = "";
  const methods = state.paymentMethods.filter((m) => m.supports_withdrawal);

  if (methods.length === 0) {
    const p = document.createElement("p");
    p.className = "muted small";
    p.textContent = mbT("tt_no_wd_methods");
    list.appendChild(p);
  } else {
    methods.forEach((m) => {
      const btn = buildMethodCard(m, m.name, () => {
        document.querySelectorAll("#withdraw-methods .method-card").forEach((b) => b.classList.remove("selected"));
        btn.classList.add("selected");
        selectWithdrawMethod(m.key);
      });
      list.appendChild(btn);
    });
  }
  document.getElementById("withdraw-address-step").classList.remove("hidden");
  document.getElementById("withdraw-form-step").classList.add("hidden");
  try {
    const [limits, wallet] = await Promise.all([
      api(`/api/wallet/${state.playerId}/limits`),
      api(`/api/wallet/${state.playerId}`),
    ]);
    document.getElementById("withdraw-limits").textContent =
      mbT("wd_limits", { won: wallet.won_balance, min: limits.min_withdrawal, max: limits.max_withdrawal });
  } catch (e) { /* non-fatal */ }
  openModal("withdraw-modal");
}

function selectWithdrawMethod(key) {
  state.selectedWithdrawMethod = key;
  const method = state.paymentMethods.find((m) => m.key === key);
  document.getElementById("withdraw-method-name").textContent = mbT("wd_account", { name: method?.name || mbT("wd_account_default") });
  const existing = state.withdrawAccounts[key];
  document.getElementById("withdraw-address").value = existing || "";
  document.getElementById("withdraw-form-step").classList.toggle("hidden", !existing);
}

async function saveWithdrawAddress() {
  if (!state.selectedWithdrawMethod) return toast(mbT("tt_need_wd_method"), "error");
  const address = document.getElementById("withdraw-address").value.trim();
  if (!address) return toast(mbT("tt_need_address"), "error");
  try {
    await api(`/api/wallet/${state.playerId}/withdrawal-accounts`, {
      method: "POST",
      body: JSON.stringify({ method_key: state.selectedWithdrawMethod, address }),
    });
    state.withdrawAccounts[state.selectedWithdrawMethod] = address;
    document.getElementById("withdraw-form-step").classList.remove("hidden");
    toast(mbT("tt_addr_saved"), "success");
  } catch (e) {
    toast(e.message, "error");
  }
}

async function submitWithdrawal() {
  const amount = Number(document.getElementById("withdraw-amount").value);
  const legalName = document.getElementById("withdraw-legal-name").value.trim();
  if (!state.selectedWithdrawMethod) return toast(mbT("tt_need_wd_method"), "error");
  if (!amount) return toast(mbT("tt_need_wd_amount"), "error");
  if (!legalName) return toast(mbT("tt_need_legal"), "error");
  try {
    const res = await api(`/api/wallet/${state.playerId}/withdraw`, {
      method: "POST",
      headers: { "Idempotency-Key": idemKey("withdraw") },
      body: JSON.stringify({ amount, payment_method: state.selectedWithdrawMethod, legal_name: legalName }),
    });
    clearIdemKey("withdraw");
    toast(mbT("tt_wd_sent"), "success");
    closeModal("withdraw-modal");
    DEPOSIT_TIMELINE.lastFetch = 0;   // show the new request's tracker immediately
    window.MB_A?.receipt({ kind: "withdraw", id: res.transaction_id, amount: res.amount ?? amount, method: state.selectedWithdrawMethod });
    refreshWallet();
  } catch (e) {
    // A clean rejection (4xx) means nothing moved: next attempt may use a new key.
    if (e.status >= 400 && e.status < 500 && e.status !== 409 && e.status !== 429) clearIdemKey("withdraw");
    toast(e.message, "error");
  }
}

// ---------------------------------------------------------------------
// History modal
// ---------------------------------------------------------------------
async function openHistoryModal() {
  const res = await api(`/api/wallet/${state.playerId}/transactions`);
  const list = document.getElementById("history-list");
  list.innerHTML = "";
  if (res.transactions.length === 0) {
    const p = document.createElement("p");
    p.className = "muted";
    p.textContent = mbT("history_empty");
    list.appendChild(p);
  } else {
    const frag = document.createDocumentFragment();
    res.transactions.forEach((t) => {
      const row = document.createElement("div");
      row.className = "history-row";

      const type = document.createElement("span");
      type.textContent = t.type.replace(/_/g, " ");

      const amt = document.createElement("span");
      amt.className = t.amount >= 0 ? "positive" : "negative";
      amt.textContent = `${t.amount >= 0 ? "+" : ""}${t.amount} ETB`;

      const status = document.createElement("span");
      status.className = "muted small";
      status.textContent = mbT("s_" + t.status) === "s_" + t.status ? t.status : mbT("s_" + t.status);

      row.append(type, amt, status);
      frag.appendChild(row);
    });
    list.appendChild(frag);
  }
  openModal("history-modal");
}

// ---------------------------------------------------------------------
// Profile drawer
// ---------------------------------------------------------------------
function getSupportLink() {
  const contact = state.supportContact || "@melbete_support_1";
  if (contact.startsWith("@")) return `https://t.me/${contact.substring(1)}`;
  if (contact.startsWith("http://") || contact.startsWith("https://")) return contact;
  return `https://t.me/${contact}`;
}

function openProfileDrawer() {
  document.getElementById("profile-backdrop").classList.remove("hidden");
  document.getElementById("profile-drawer").classList.add("open");
  updateLinkTelegramVisibility();
  populateProfileDrawer();
}

function closeProfileDrawer() {
  document.getElementById("profile-backdrop").classList.add("hidden");
  document.getElementById("profile-drawer").classList.remove("open");
}

async function populateProfileDrawer() {
  const fields = document.getElementById("profile-fields");
  fields.innerHTML = "";
  const loadingMsg = document.createElement("p");
  loadingMsg.className = "muted small";
  loadingMsg.textContent = "Loading...";
  fields.appendChild(loadingMsg);

  const tgUser = tg?.initDataUnsafe?.user;
  let profile = null;
  try {
    profile = await api(`/api/profile/${state.playerId}`);
  } catch (e) {
    fields.innerHTML = "";
    const errorMsg = document.createElement("p");
    errorMsg.className = "muted small";
    errorMsg.textContent = isIdentityError(e)
      ? "Session needs refresh — close and reopen the app from Telegram."
      : "Profile info unavailable — it will update automatically.";
    fields.appendChild(errorMsg);
    setTimeout(() => populateProfileDrawer(), 8000);
    return;
  }

  fields.innerHTML = "";
  const frag = document.createDocumentFragment();

  const addSection = (title, items) => {
    const head = document.createElement("div");
    head.className = "profile-section-title";
    head.textContent = title;
    frag.appendChild(head);

    const group = document.createElement("div");
    group.className = "profile-group";
    items.forEach(([label, value, extraClass]) => {
      const field = document.createElement("div");
      field.className = "profile-field";
      const lbl = document.createElement("span");
      lbl.className = "label";
      lbl.textContent = label;
      const val = document.createElement("span");
      val.className = "value" + (extraClass ? ` ${extraClass}` : "");
      val.textContent = value;
      field.append(lbl, val);
      group.appendChild(field);
    });
    frag.appendChild(group);
  };

  addSection("Identity", [
    ["Name", tgUser ? [tgUser.first_name, tgUser.last_name].filter(Boolean).join(" ") : profile.display_name || "-"],
    ["Username", tgUser?.username ? `@${tgUser.username}` : "-"],
    ["User ID", String(tgUser?.id || profile.telegram_id || "-")],
  ]);

  addSection("Account", [
    ["Contact", profile.phone_number || "Not shared"],
    ["Status", profile.is_registered ? "Registered" : "Unregistered", profile.is_registered ? "success" : ""],
  ]);

  addSection("Wallet", [
    ["Total Balance", `${profile.balance} ETB`, "highlight"],
    ["Withdrawable", `${profile.won_balance} ETB`, "success"],
    ["Bonus Credit", `${profile.bonus_balance} ETB`],
    ["Lifetime In", `${profile.lifetime_deposited} ETB`],
  ]);

  fields.appendChild(frag);
}

// ---------------------------------------------------------------------
// Referral modal
// ---------------------------------------------------------------------
async function openReferralModal() {
  openModal("referral-modal");
  const content = document.getElementById("referral-modal-content");
  content.innerHTML = '<div class="loader" style="margin: 2rem auto;"></div>';

  try {
    const ref = await api(`/api/referral-link/${state.playerId}`);
    content.innerHTML = "";

    const statsRow = document.createElement("div");
    statsRow.className = "referral-stats-row";
    statsRow.innerHTML = `
      <div class="ref-stat">
        <span class="ref-stat-val">${ref.total_invited || 0}</span>
        <span class="ref-stat-lbl">Total Invited</span>
      </div>
      <div class="ref-stat">
        <span class="ref-stat-val">${ref.referral_count}/${ref.max_referrals}</span>
        <span class="ref-stat-lbl">Paid Invites</span>
      </div>
      <div class="ref-stat">
        <span class="ref-stat-val">${ref.bonus_per_referral} ETB</span>
        <span class="ref-stat-lbl">Per Invite</span>
      </div>
    `;
    content.appendChild(statsRow);

    const label = document.createElement("label");
    label.className = "field-label";
    label.textContent = "Your Referral Link";

    const linkWrap = document.createElement("div");
    linkWrap.className = "referral-link-wrap";

    const linkInput = document.createElement("input");
    linkInput.type = "text";
    linkInput.readOnly = true;
    linkInput.className = "referral-link-input";
    linkInput.value = ref.referral_link;

    const copyBtn = document.createElement("button");
    copyBtn.className = "referral-copy-btn";
    copyBtn.textContent = "Copy";

    copyBtn.addEventListener("click", async () => {
      try {
        if (navigator.clipboard) {
          await navigator.clipboard.writeText(ref.referral_link);
        } else {
          linkInput.select();
          document.execCommand("copy");
        }
        copyBtn.textContent = "Copied!";
        setTimeout(() => { copyBtn.textContent = "Copy"; }, 2000);
        toast("Copied!", "success");
      } catch (_) {
        toast("Could not copy", "error");
      }
    });

    linkWrap.append(linkInput, copyBtn);
    content.append(label, linkWrap);

    const shareBtn = document.createElement("button");
    shareBtn.className = "referral-share-btn";
    shareBtn.textContent = "📤 Share via Telegram";
    shareBtn.addEventListener("click", () => {
      const freeCredit = ref.free_credit_amount || 50;
      const shareText = `🎮 Join me on Melbete Games! 🎪\n\nI'm playing Melbete Bingo, the ultimate live 75-ball bingo game! Play with real people, win awesome payouts, and claim your ${freeCredit} ETB FREE welcome credit immediately! 💰\n\n👇 TAP HERE TO PLAY NOW AND CLAIM YOUR FREE CREDIT! 👇`;
      const shareUrl = `https://t.me/share/url?url=${encodeURIComponent(ref.referral_link)}&text=${encodeURIComponent(shareText)}`;
      if (tg?.openTelegramLink) {
        tg.openTelegramLink(shareUrl);
      } else {
        window.open(shareUrl, "_blank");
      }
    });
    content.appendChild(shareBtn);

    if (ref.slots_remaining <= 0) {
      const maxMsg = document.createElement("p");
      maxMsg.className = "muted small";
      maxMsg.style.textAlign = "center";
      maxMsg.style.marginTop = "1rem";
      maxMsg.textContent = `You've reached the maximum of ${ref.max_referrals} referrals. Well done! 🎉`;
      content.appendChild(maxMsg);
    }
  } catch (err) {
    content.innerHTML = '<p class="muted small" style="text-align: center; padding: 1rem 0;">Failed to load referral stats. Please make sure you are registered through the Telegram bot first.</p>';
  }
}

function openSupport() {
  const link = getSupportLink();
  if (tg?.openTelegramLink) {
    tg.openTelegramLink(link);
  } else {
    window.open(link, "_blank");
  }
}

// ---------------------------------------------------------------------
// Link Telegram Account (for players using the web version outside
// Telegram) - see /api/link/* in the backend.
// ---------------------------------------------------------------------
function updateLinkTelegramVisibility() {
  const row = document.getElementById("link-telegram-row");
  const linkedRow = document.getElementById("link-telegram-linked-row");
  if (!row || !linkedRow) return;

  if (tg?.initData) {
    row.classList.add("hidden");
    linkedRow.classList.add("hidden");
    return;
  }
  if (state.playerId?.startsWith("tg_")) {
    row.classList.add("hidden");
    linkedRow.classList.remove("hidden");
  } else {
    row.classList.remove("hidden");
    linkedRow.classList.add("hidden");
  }
}

let linkPollTimer = null;

function openLinkTelegramModal() {
  document.getElementById("link-telegram-start").classList.remove("hidden");
  document.getElementById("link-telegram-pending").classList.add("hidden");
  document.getElementById("link-telegram-success").classList.add("hidden");
  const statusEl = document.getElementById("link-telegram-status");
  statusEl.innerHTML = '<span class="link-telegram-spinner"></span> Waiting for confirmation...';
  clearInterval(linkPollTimer);
  openModal("link-telegram-modal");
}

async function generateLinkCode() {
  const btn = document.getElementById("link-telegram-generate-btn");
  btn.disabled = true;
  btn.textContent = "Generating...";
  try {
    const res = await api("/api/link/create", {
      method: "POST",
      body: JSON.stringify({ player_id: state.playerId }),
    });
    document.getElementById("link-telegram-code").textContent = res.code;

    const openBtn = document.getElementById("link-telegram-open-btn");
    const hint = document.getElementById("link-telegram-manual-hint");
    if (res.deep_link) {
      openBtn.href = res.deep_link;
      openBtn.classList.remove("hidden");
      hint.textContent = `Or send /link ${res.code} to the bot manually.`;
    } else {
      openBtn.classList.add("hidden");
      hint.textContent = `Open our Telegram bot and send: /link ${res.code}`;
    }

    document.getElementById("link-telegram-start").classList.add("hidden");
    document.getElementById("link-telegram-pending").classList.remove("hidden");
    startLinkPolling(res.code);
  } catch (e) {
    toast(e.message || "Couldn't generate a code - please try again.", "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "Generate Code";
  }
}

function startLinkPolling(code) {
  clearInterval(linkPollTimer);
  linkPollTimer = setInterval(async () => {
    if (document.getElementById("link-telegram-modal").classList.contains("hidden")) {
      clearInterval(linkPollTimer);
      return;
    }
    let res;
    try {
      res = await api(`/api/link/status/${code}`);
    } catch (e) {
      return;
    }
    if (res.status === "linked") {
      clearInterval(linkPollTimer);
      await completeAccountLink(res);
    } else if (res.status === "expired") {
      clearInterval(linkPollTimer);
      document.getElementById("link-telegram-status").innerHTML =
        "⚠️ Code expired - close this and generate a new one.";
    }
  }, 3000);
}

async function completeAccountLink(res) {
  localStorage.setItem("melbete_bingo_linked_player_id", res.player_id);
  localStorage.setItem("melbete_bingo_web_session_token", res.session_token);
  state.playerId = res.player_id;
  state.webSessionToken = res.session_token;
  state.telegramId = res.player_id.replace(/^tg_/, "");

  document.getElementById("link-telegram-pending").classList.add("hidden");
  document.getElementById("link-telegram-success").classList.remove("hidden");
  updateLinkTelegramVisibility();

  await refreshWallet();
  populateProfileDrawer();
  toast("Telegram account linked!", "success");

  setTimeout(() => closeModal("link-telegram-modal"), 1800);
}

// ---------------------------------------------------------------------
// Wire up
// ---------------------------------------------------------------------
function wireEvents() {
  wireGameCards();
  wireDepositScreenshotInput();
  wireAutoGrowTextareas();

  document.getElementById("hub-tab-profile")?.addEventListener("click", () => {
    setHubTab("profile");
    openProfileDrawer();
  });
  document.getElementById("profile-close-btn")?.addEventListener("click", () => {
    closeProfileDrawer();
    setHubTab("games");
  });
  document.getElementById("profile-backdrop")?.addEventListener("click", () => {
    closeProfileDrawer();
    setHubTab("games");
  });

  document.getElementById("hub-tab-games")?.addEventListener("click", () => setHubTab("games"));
  document.getElementById("hub-tab-promos")?.addEventListener("click", () => openPromoModal());
  document.getElementById("cashback-fab")?.addEventListener("click", () => openPromoModal("cashback"));
  document.getElementById("hub-balance-chip")?.addEventListener("click", openHistoryModal);
  document.getElementById("hub-tab-deposit")?.addEventListener("click", () => {
    setHubTab("deposit");
    openDepositModal();
  });
  document.getElementById("hub-tab-withdraw")?.addEventListener("click", () => {
    setHubTab("withdraw");
    openWithdrawModal();
  });
  document.getElementById("hub-tab-refer")?.addEventListener("click", () => {
    setHubTab("refer");
    openReferralModal();
  });
  document.getElementById("link-telegram-row")?.addEventListener("click", () => {
    closeProfileDrawer();
    openLinkTelegramModal();
  });
  document.getElementById("link-telegram-generate-btn")?.addEventListener("click", generateLinkCode);

  document.getElementById("support-btn")?.addEventListener("click", openSupport);
  document.getElementById("balance-btn")?.addEventListener("click", openHistoryModal);
  document.getElementById("header-profile-btn")?.addEventListener("click", () => {
    setHubTab("profile");
    openProfileDrawer();
  });
  document.getElementById("quick-deposit-btn")?.addEventListener("click", () => {
    setHubTab("deposit");
    openDepositModal();
  });
  document.getElementById("quick-withdraw-btn")?.addEventListener("click", () => {
    setHubTab("withdraw");
    openWithdrawModal();
  });
  document.getElementById("deposit-submit-btn")?.addEventListener("click", submitDeposit);
  document.getElementById("save-address-btn")?.addEventListener("click", saveWithdrawAddress);
  document.getElementById("withdraw-submit-btn")?.addEventListener("click", submitWithdrawal);
  document.querySelectorAll(".modal-close").forEach((btn) =>
    btn.addEventListener("click", () => closeModal(btn.dataset.close))
  );
  // Deposit/withdraw modals aren't their own screen - closing either one
  // (backdrop tap or the × button) drops the bottom nav back to Games.
  document.getElementById("deposit-modal")?.addEventListener("click", (e) => {
    if (e.target.id === "deposit-modal" || e.target.dataset.close === "deposit-modal") setHubTab("games");
  });
  document.getElementById("withdraw-modal")?.addEventListener("click", (e) => {
    if (e.target.id === "withdraw-modal" || e.target.dataset.close === "withdraw-modal") setHubTab("games");
  });
  document.getElementById("promo-modal")?.addEventListener("click", (e) => {
    if (e.target.id === "promo-modal" || e.target.dataset.close === "promo-modal") setHubTab("games");
  });
  document.getElementById("referral-modal")?.addEventListener("click", (e) => {
    if (e.target.id === "referral-modal" || e.target.dataset.close === "referral-modal") setHubTab("games");
  });
}

// Deep links from the bingo room footer: /app#deposit, #withdraw, #refer, #profile.
function openFromHash() {
  const action = (location.hash || "").replace("#", "");
  const map = {
    deposit: () => { setHubTab("deposit"); openDepositModal(); },
    withdraw: () => { setHubTab("withdraw"); openWithdrawModal(); },
    refer: () => { setHubTab("refer"); openReferralModal(); },
    profile: () => { setHubTab("profile"); openProfileDrawer(); },
    promos: () => openPromoModal(),
    cashback: () => openPromoModal("cashback"),
  };
  if (map[action]) {
    try { history.replaceState(null, "", location.pathname + location.search); } catch (e) { /* ignore */ }
    map[action]();
  }
}

// Highlights the active bottom-nav tab. Deposit/Withdraw/Refer/Profile open
// their existing modal/drawer on top of this same screen rather than
// navigating anywhere, so this only ever toggles the visual state.
function setHubTab(tab) {
  document.querySelectorAll(".hub-tab-btn").forEach((btn) => btn.classList.remove("active"));
  document.getElementById(`hub-tab-${tab}`)?.classList.add("active");
}

// ---------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------
async function main() {
  try {
    initIdentity();
    wireEvents();
    document.getElementById("top-bar").classList.remove("hidden");
    document.getElementById("hub-tabbar")?.classList.remove("hidden");
    document.getElementById("cashback-fab")?.classList.remove("hidden");
    document.getElementById("support-btn")?.classList.remove("hidden");
    await refreshWallet();
    try {
      const limits = await api(`/api/wallet/${state.playerId}/limits`);
      state.supportContact = limits.support_contact || "@melbete_support_1";
    } catch (e) {
      state.supportContact = "@melbete_support_1";
    }
    showView("game-select-view");
    openFromHash();
    renderPromoBanner();
    renderCartelaPicker();
    setInterval(renderCartelaPicker, 10000);
    setInterval(refreshWallet, 8000);
    refreshPromoBadge();
    setInterval(refreshPromoBadge, 60000);
  } catch (e) {
    console.error(e);
    showLoadError(e);
  }
}

function showLoadError(e) {
  const view = document.getElementById("loading-view");
  view.innerHTML = "";
  const icon = document.createElement("p");
  icon.style.fontSize = "2.5rem";
  icon.textContent = "⚠️";
  const title = document.createElement("h3");
  title.style.marginBottom = "0.5rem";
  title.textContent = "Failed to load";
  const msg = document.createElement("p");
  msg.className = "muted small";
  msg.style.maxWidth = "320px";
  msg.style.textAlign = "center";
  msg.textContent = e?.message || "Something went wrong. Please reopen the app.";
  view.append(icon, title, msg);
}

// Language switch: re-render the dynamic pieces that are built in JS.
document.addEventListener("melbete:lang", () => {
  DEPOSIT_TIMELINE.lastFetch = 0;
  refreshDepositTimeline();
  if (!state.selectedWithdrawMethod) {
    const n = document.getElementById("withdraw-method-name");
    if (n) n.textContent = mbT("wd_account", { name: mbT("wd_account_default") });
  }
});

main();
