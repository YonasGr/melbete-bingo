/* ===========================================================================
   Melbete Bingo - Admin Control Center
   Draft -> review -> publish for games, money rules, content and switches;
   instant (audited) maintenance + kill switch; player controls; staff roles; audit log.
   Depends on admin.js globals: api, toast, escapeHtml, TAB_TITLES, TAB_CONTEXT, LOADERS.
   =========================================================================== */
(() => {
  "use strict";

  const cc = { snap: null, draft: null, dirty: false, section: "games", perms: new Set(), player: null, auditBefore: null };
  const $ = (id) => document.getElementById(id);
  const esc = (v) => escapeHtml(v);
  const clone = (o) => JSON.parse(JSON.stringify(o));
  const can = (p) => cc.perms.has(p);

  TAB_TITLES.control = "Control Center";
  TAB_CONTEXT.control = "Draft, review and publish platform controls";
  LOADERS.control = loadControl;

  const SECTIONS = [
    ["games", "Games"], ["money", "Money"], ["content", "Content"], ["promotions", "Promotions"], ["experience", "Experience"], ["switches", "Switches"],
    ["alerts", "Alerts"], ["players", "Players"], ["staff", "Staff"], ["audit", "Audit"],
  ];
  const TRACK_NAMES = { real_10: "Real money · 10 ETB", real_50: "Real money · 50 ETB", bonus: "Bonus balance" };
  const LABELS = {
    "flags.deposits": "Deposits switch", "flags.withdrawals": "Withdrawals switch", "flags.play": "Playing switch",
    "flags.bonus_center": "Bonus Center switch", "flags.agent_portal": "Agent portal switch",
    "money.daily_deposit_limit": "Daily deposit limit", "money.daily_withdrawal_limit": "Daily withdrawal limit",
    "money.withdrawal_review_above": "Senior review above", "money.auto_approve.enabled": "Auto-approve deposits",
    "money.auto_approve.deposit_max": "Auto-approve up to", "money.auto_approve.min_prior_approved": "Auto-approve needs approved deposits",
    "money.auto_approve.require_verification_url": "Auto-approve needs payment link",
    "settings.payout_ratio": "Payout ratio (winner share)", "settings.payout_custom_real_10": "10 ETB room: own commission", "settings.payout_custom_real_50": "50 ETB room: own commission", "settings.payout_custom_bonus": "Bonus room: own commission",
    "settings.payout_ratio_real_10": "10 ETB room winner share", "settings.payout_ratio_real_50": "50 ETB room winner share", "settings.payout_ratio_bonus": "Bonus room winner share", "settings.draw_interval": "Seconds between balls",
    "settings.lobby_countdown_seconds": "Lobby countdown", "settings.cards_per_player": "Cartelas per player",
    "settings.min_cartelas_to_start": "Cartelas needed to start", "settings.stake_amount_real_10": "Stake (10 ETB room)",
    "settings.min_deposit": "Minimum deposit", "settings.max_deposit": "Maximum deposit",
    "settings.min_withdrawal": "Minimum withdrawal", "settings.max_withdrawal": "Maximum withdrawal",
    "content.announcement.enabled": "Announcement shown", "content.announcement.body": "Announcement text",
    "content.banner.enabled": "Promo banner shown", "content.banner.title": "Banner title",
    "alerts.enabled": "Telegram alerts", "alerts.big_withdrawal_amount": "Large withdrawal alert from",
    "alerts.big_wallet_adjust_amount": "Large wallet adjustment alert from", "alerts.failed_logins_before_alert": "Failed sign-ins before alert",
  };
  const label = (path) => LABELS[path]
    || (path.match(/^games\.tracks\.(\w+)\.open$/) ? `${TRACK_NAMES[path.split(".")[2]] || path} open` : null)
    || path.replace(/^settings\./, "").replace(/[._]/g, " ");
  const show = (v) => (v === true ? "on" : v === false ? "off" : v === "" || v == null ? "—" : String(v));

  /* ---------------- small helpers ---------------- */
  function getPath(obj, path) { return path.split(".").reduce((o, k) => (o == null ? o : o[k]), obj); }
  function setPath(obj, path, value) {
    const keys = path.split("."); const last = keys.pop();
    const target = keys.reduce((o, k) => (o[k] = o[k] ?? {}), obj);
    target[last] = value;
  }
  const settingVal = (k) => (cc.draft.settings && k in cc.draft.settings ? cc.draft.settings[k] : cc.snap.live_settings[k]);
  const fmtTime = (iso) => (iso ? new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "—");

  function confirmDialog(title, bodyHtml, okLabel = "Confirm", danger = false) {
    return new Promise((resolve) => {
      const ov = document.createElement("div");
      ov.className = "cc-modal";
      ov.innerHTML = `<div class="cc-modal-card" role="dialog" aria-modal="true"><h3>${esc(title)}</h3><div class="cc-modal-body">${bodyHtml}</div>
        <div class="cc-modal-actions"><button class="secondary-btn" data-r="0">Cancel</button><button class="${danger ? "btn btn-delete" : "primary-btn"}" data-r="1">${esc(okLabel)}</button></div></div>`;
      const done = (v) => { ov.remove(); resolve(v); };
      ov.addEventListener("click", (e) => { const b = e.target.closest("[data-r]"); if (b) done(b.dataset.r === "1"); else if (e.target === ov) done(false); });
      document.body.appendChild(ov);
    });
  }

  function toggle(path, text, hint = "") {
    const on = !!getPath(cc.draft, path);
    return `<div class="toggle-row"><div><strong>${esc(text)}</strong>${hint ? `<span>${esc(hint)}</span>` : ""}</div>
      <label class="toggle-switch"><input type="checkbox" data-path="${path}" data-type="bool" ${on ? "checked" : ""}><span class="toggle-track"></span></label></div>`;
  }
  function field(text, path, { type = "int", unit = "", hint = "", min = "", max = "", textarea = false, maxlength = "" } = {}) {
    const isSetting = path.startsWith("settings.");
    let value = isSetting ? settingVal(path.slice(9)) : getPath(cc.draft, path);
    if (type === "pct") value = value == null ? "" : Math.round((1 - Number(value)) * 1000) / 10;
    if (type === "datetime" && value) value = String(value).slice(0, 16);
    const ctl = textarea
      ? `<textarea data-path="${path}" data-type="text" rows="3" maxlength="${maxlength}">${esc(value ?? "")}</textarea>`
      : `<input data-path="${path}" data-type="${type}" type="${type === "text" ? "text" : type === "datetime" ? "datetime-local" : "number"}" ${type === "float" || type === "pct" ? 'step="0.1"' : ""} ${min !== "" ? `min="${min}"` : ""} ${max !== "" ? `max="${max}"` : ""} ${maxlength ? `maxlength="${maxlength}"` : ""} value="${esc(value ?? "")}">`;
    return `<div class="field"><label>${esc(text)}${unit ? ` <small>(${esc(unit)})</small>` : ""}</label>${ctl}${hint ? `<small class="cc-hint">${esc(hint)}</small>` : ""}</div>`;
  }
  const lock = () => (can("control.edit") ? "" : "disabled");

  /* ---------------- load + frame ---------------- */
  async function loadControl() {
    cc.snap = await api("/api/admin/control");
    adopt(cc.snap);
    render();
  }
  function adopt(snap) {
    cc.snap = snap; cc.draft = clone(snap.draft); cc.dirty = false; cc.perms = new Set(snap.permissions);
  }

  function render() {
    const root = $("cc-root");
    if (!root || !cc.snap) return;
    root.innerHTML = `${draftBar()}<nav class="cc-seg" role="tablist">${SECTIONS.map(([k, n]) => `<button class="${cc.section === k ? "active" : ""}" data-sec="${k}">${n}</button>`).join("")}</nav><div id="cc-body"></div>`;
    renderSection();
  }
  function draftBar() {
    const s = cc.snap, n = s.changes.length;
    const status = cc.dirty ? `<b class="cc-warn">Unsaved edits</b>` : s.has_draft ? `<b>${n} unpublished change${n === 1 ? "" : "s"}</b><small>Draft by ${esc(s.draft_by || "?")} · ${fmtTime(s.draft_updated_at)}</small>` : `<b>All changes are live</b><small>Version ${s.version}${s.published_at ? ` · published ${fmtTime(s.published_at)}` : ""}</small>`;
    return `<div class="cc-bar"><div class="cc-bar-status">${status}</div><div class="cc-bar-actions">
      ${can("control.edit") || can("experience.edit") ? `<button class="secondary-btn" data-act="save" ${cc.dirty ? "" : "disabled"}>Save draft</button>` : ""}
      ${can("control.edit") && s.has_draft && !cc.dirty ? `<button class="secondary-btn" data-act="discard">Discard</button>` : ""}
      ${can("control.publish") ? `<button class="primary-btn" data-act="publish" ${s.has_draft && !cc.dirty ? "" : "disabled"}>Publish</button>` : ""}
      ${can("control.publish") && s.can_rollback ? `<button class="secondary-btn" data-act="rollback">Undo last publish</button>` : ""}
    </div></div>${can("control.edit") ? "" : `<p class="cc-note">Your role can view these controls but not change them.</p>`}${s.has_draft && !can("control.publish") ? `<p class="cc-note">Your role can't publish. Ask a Super Admin or Manager to publish these changes.</p>` : ""}${cc.dirty && can("control.publish") ? `<p class="cc-note">Save your draft first, then tap Publish.</p>` : ""}`;
  }
  function renderSection() {
    const body = $("cc-body");
    const fn = { games: gamesHtml, money: moneyHtml, content: contentHtml, promotions: promotionsHtml, experience: experienceHtml, switches: switchesHtml, alerts: alertsHtml, players: playersHtml, staff: staffHtml, audit: auditHtml }[cc.section];
    body.innerHTML = `<fieldset class="cc-fieldset" ${["players", "staff", "audit", "switches", "alerts"].includes(cc.section) ? "" : cc.section === "experience" ? (can("control.edit") || can("experience.edit") ? "" : "disabled") : lock()}>${fn()}</fieldset>`;
    if (cc.section === "staff") loadStaff();
    if (cc.section === "alerts") loadAlertsMine();
    if (cc.section === "audit") loadAudit(true);
  }

  /* ---------------- sections ---------------- */
  function selectField(text, path, options, hint = "") {
    const cur = getPath(cc.draft, path);
    return `<div class="field"><label>${esc(text)}</label><select data-path="${path}" data-type="text">${options.map(([v, n]) => `<option value="${v}" ${cur === v ? "selected" : ""}>${esc(n)}</option>`).join("")}</select>${hint ? `<small class="cc-hint">${esc(hint)}</small>` : ""}</div>`;
  }
  function openFlowDemo() {
    const cfg = btoa(unescape(encodeURIComponent(JSON.stringify(cc.draft.experience || {}))));
    window.open(`/static/flow-demo.html?cfg=${encodeURIComponent(cfg)}`, "_blank", "noopener");
  }
  function experienceHtml() {
    const card = (h, p, body) => `<article class="work-panel cc-card"><div class="panel-heading"><div><h3>${h}</h3><p>${p}</p></div></div>${body}</article>`;
    return `<div class="cc-cards">
      <article class="work-panel cc-card"><div class="panel-heading"><div><h3>Test as player</h3><p>Plays the get-ready splash, countdown ring and win screens with the settings below (unsaved edits included). No real round, no money.</p></div><button class="primary-btn" type="button" data-act="flow-demo">Test as player</button></div></article>
      ${card("Win celebrations", "Bigger wins get a bigger show", `<div class="form-grid">${field("Big win from", "experience.tiers.big_win_etb", { unit: "ETB", min: 1, hint: "Gold glow + extra confetti" })}${field("Jackpot from", "experience.tiers.jackpot_etb", { unit: "ETB", min: 1, hint: "Full-screen gold burst + JACKPOT banner" })}</div>
        ${toggle("experience.tiers.jackpot_burst", "Jackpot burst and banner")}${toggle("experience.tiers.coin_fly", "Coin flies into the balance after a win")}`)}
      ${card("Lobby countdown", "What players see while the round fills", `<div class="form-grid">${field("Show 'picking closes soon' at", "experience.countdown.warn_seconds", { unit: "seconds left", min: 0, max: 120 })}${field("Ring turns red at", "experience.countdown.hot_seconds", { unit: "seconds left", min: 0, max: 60 })}</div>
        ${toggle("experience.countdown.lobby_feed", "Live feed: 'Cartela #47 was taken'")}${toggle("experience.countdown.show_pool", "Show cartelas taken and ETB staked")}`)}
      ${card("Round start and play", "Splash, alerts and the verify moment", `${toggle("experience.splash.enabled", "Get-ready splash (3-2-1)")}${field("Splash length", "experience.splash.seconds", { unit: "seconds", min: 1, max: 5 })}
        ${toggle("experience.alerts.near_win", "'1 number away!' alert on cards")}${toggle("experience.alerts.haptics", "Phone vibration cues")}
        ${field("'Checking your card…' shows for at least", "experience.verify.min_ms", { unit: "milliseconds", min: 0, max: 3000, hint: "0 turns the verify screen off" })}`)}
      ${card("Called-number voice", "Reads each ball aloud with the phone's own speech", selectField("Voice", "experience.voice.mode", [["player", "Each player chooses (off by default)"], ["on_by_default", "On by default (player can turn off)"], ["off", "Off for everyone"]]))}
      ${card("Random pick and rematch", "The dice button and play-again", `${field("Dice offers", "experience.random_picker.choices", { unit: "cartelas", min: 0, max: 5, hint: "0 = pick one instantly, no choice" })}${toggle("experience.rematch.keep_cartelas", "Play again keeps the same cartelas", "Off: players choose fresh cartelas each round")}`)}
      ${card("Room rules in one place", "Same values as Games > Rules; changing them here changes them there", `<div class="form-grid">${field("Cartelas needed to start", "settings.min_cartelas_to_start", { min: 1, max: 500 })}${field("Cartelas per player", "settings.cards_per_player", { min: 1, max: 4 })}${field("Lobby countdown", "settings.lobby_countdown_seconds", { unit: "seconds", min: 5, max: 300 })}${field("Seconds between balls", "settings.draw_interval", { unit: "seconds", min: 1, max: 30 })}
        ${field("Picking locks for the last", "experience.timing.lock_seconds", { unit: "seconds", min: 0, max: 30, hint: "0 = default (5)" })}${field("Next round starts after", "experience.timing.next_round_seconds", { unit: "seconds", min: 0, max: 300, hint: "0 = server default (15)" })}</div>`)}
    </div>`;
  }

  function gamesHtml() {
    const active = cc.snap.active_tracks || ["real_10"];
    const trackCards = cc.snap.tracks.map((t) => {
      const isActive = active.includes(t);
      const stakeKey = `stake_amount_${t}`;
      return `<article class="work-panel cc-card ${isActive ? "" : "cc-dim"}"><div class="panel-heading"><div><h3>${esc(TRACK_NAMES[t] || t)}</h3><p>${isActive ? "Offered to players" : "Not currently offered by the platform"}</p></div></div>
        ${toggle(`games.tracks.${t}.open`, "Open for new players", "Turn off to pause joining; rounds in progress finish normally")}
        ${field("Message when paused", `games.tracks.${t}.pause_message`, { type: "text", maxlength: 200, hint: "Shown to players who try to join" })}
        <div class="form-grid">${field("Stake", `settings.${stakeKey}`, { unit: "ETB", min: 1 })}
        ${t === "real_10" || t === "real_50" || t === "bonus" ? field("Max bots", `settings.max_bots_${t}`, { min: 0 }) : ""}</div>
        ${toggle(`settings.bot_enabled_${t}`, "Bots fill this room")}
        ${toggle(`settings.payout_custom_${t}`, "Own commission for this room", "Off: the commission under Rules for every game applies")}
        ${field("This room's commission", `settings.payout_ratio_${t}`, { type: "pct", unit: "% of pool", min: 0, max: 50, hint: "Used only while the switch above is on. Winner receives the rest." })}</article>`;
    }).join("");
    return `<div class="cc-grid">${trackCards}
      <article class="work-panel cc-card"><div class="panel-heading"><div><h3>Rules for every game</h3><p>Timing, commission and cartelas</p></div></div><div class="form-grid">
        ${field("Platform commission", "settings.payout_ratio", { type: "pct", unit: "% of pool", min: 0, max: 50, hint: "Winner receives the rest. Maximum 50%." })}
        ${field("Lobby countdown", "settings.lobby_countdown_seconds", { unit: "seconds", min: 5, max: 300 })}
        ${field("Seconds between balls", "settings.draw_interval", { unit: "seconds", min: 1, max: 30 })}
        ${field("Cartelas per player", "settings.cards_per_player", { min: 1, max: 4 })}
        ${field("Cartelas needed to start", "settings.min_cartelas_to_start", { min: 1 })}
        ${field("Cartela numbers from", "settings.cartela_min", { min: 1 })}${field("Cartela numbers to", "settings.cartela_max", { min: 1 })}</div></article></div>`;
  }
  function moneyHtml() {
    return `<div class="cc-grid">
      <article class="work-panel cc-card"><div class="panel-heading"><div><h3>Transaction limits</h3><p>Per request, for every player</p></div></div><div class="form-grid">
        ${field("Minimum deposit", "settings.min_deposit", { unit: "ETB", min: 1 })}${field("Maximum deposit", "settings.max_deposit", { unit: "ETB", min: 1 })}
        ${field("Minimum withdrawal", "settings.min_withdrawal", { unit: "ETB", min: 1 })}${field("Maximum withdrawal", "settings.max_withdrawal", { unit: "ETB", min: 1 })}</div></article>
      <article class="work-panel cc-card"><div class="panel-heading"><div><h3>Daily limits</h3><p>Rolling 24 hours, per player. 0 = no limit. A personal limit set on a player overrides this.</p></div></div><div class="form-grid">
        ${field("Deposits per 24h", "money.daily_deposit_limit", { unit: "ETB", min: 0 })}${field("Withdrawals per 24h", "money.daily_withdrawal_limit", { unit: "ETB", min: 0 })}
        ${field("Senior approval above", "money.withdrawal_review_above", { unit: "ETB", min: 0, hint: "Withdrawals above this need the 'approve large' permission. 0 = off." })}</div></article>
      <article class="work-panel cc-card"><div class="panel-heading"><div><h3>Auto-approve deposits</h3><p>Off by default. Withdrawals are never auto-approved.</p></div></div>
        <p class="cc-note cc-note-warn">Auto-approval credits money without a person checking the payment. Keep the limit small and require a payment link.</p>
        ${toggle("money.auto_approve.enabled", "Auto-approve trusted deposits")}
        <div class="form-grid">${field("Up to", "money.auto_approve.deposit_max", { unit: "ETB", min: 0 })}${field("Player needs approved deposits", "money.auto_approve.min_prior_approved", { min: 1 })}</div>
        ${toggle("money.auto_approve.require_verification_url", "Require a payment verification link")}
        <small class="cc-hint">Never applies to banned, frozen or flagged players.</small></article>
      <article class="work-panel cc-card"><div class="panel-heading"><div><h3>Rewards</h3><p>Referral and starter credit</p></div></div><div class="form-grid">
        ${field("Referral bonus", "settings.referral_bonus", { unit: "ETB", min: 0 })}${field("Max paid referrals per player", "settings.max_referrals_per_user", { min: 0 })}${field("Starter credit", "settings.free_credit_amount", { unit: "ETB", min: 0 })}</div></article></div>`;
  }
  function contentHtml() {
    const a = cc.draft.content.announcement, b = cc.draft.content.banner;
    return `<div class="cc-grid">
      <article class="work-panel cc-card"><div class="panel-heading"><div><h3>Announcement bar</h3><p>A notice across the top of the player app</p></div></div>
        ${toggle("content.announcement.enabled", "Show announcement")}
        <div class="field"><label>Style</label><select data-path="content.announcement.level" data-type="text">${["info", "success", "warning", "critical"].map((l) => `<option ${a.level === l ? "selected" : ""}>${l}</option>`).join("")}</select></div>
        ${field("Title", "content.announcement.title", { type: "text", maxlength: 80 })}
        ${field("Message", "content.announcement.body", { type: "text", textarea: true, maxlength: 500 })}
        ${field("Hide automatically at", "content.announcement.ends_at", { type: "datetime", hint: "Optional. Your local time." })}</article>
      <article class="work-panel cc-card"><div class="panel-heading"><div><h3>Promo banner</h3><p>The highlighted card on the home screen</p></div></div>
        ${toggle("content.banner.enabled", "Show promo banner")}
        <div class="form-grid">${field("Tag", "content.banner.tag", { type: "text", maxlength: 24 })}${field("Button text", "content.banner.cta_label", { type: "text", maxlength: 24 })}</div>
        ${field("Title", "content.banner.title", { type: "text", maxlength: 80 })}${field("Details", "content.banner.detail", { type: "text", maxlength: 160 })}
        <div class="field"><label>Button opens</label><select data-path="content.banner.cta_target" data-type="text">${["deposit", "withdraw", "promos", "profile", "none"].map((l) => `<option ${b.cta_target === l ? "selected" : ""}>${l}</option>`).join("")}</select></div></article>
      <article class="work-panel cc-card"><div class="panel-heading"><div><h3>Texts</h3><p>Leave blank to keep the built-in wording</p></div></div>
        ${field("Deposit instructions", "content.texts.deposit_intro", { type: "text", textarea: true, maxlength: 600 })}
        ${field("Withdrawal notes", "content.texts.withdraw_intro", { type: "text", textarea: true, maxlength: 600 })}
        ${field("Home screen note", "content.texts.home_note", { type: "text", textarea: true, maxlength: 600, hint: "A short line shown above the game list" })}
        <hr class="cc-hr"><div class="panel-heading"><div><h3>Cashback and agent messages</h3><p>English and Amharic. Leave blank to keep the built-in wording. Use {p} for the cashback percentage.</p></div></div>
        ${field("Cashback note (English)", "content.texts.cashback_note_en", { type: "text", textarea: true, maxlength: 300, hint: "Shown in the deposit screen" })}
        ${field("Cashback note (Amharic)", "content.texts.cashback_note_am", { type: "text", textarea: true, maxlength: 300 })}
        ${field("Agent application intro (English)", "content.texts.agent_intro_en", { type: "text", textarea: true, maxlength: 400, hint: "Shown above the Become an agent form" })}
        ${field("Agent application intro (Amharic)", "content.texts.agent_intro_am", { type: "text", textarea: true, maxlength: 400 })}
        ${field("Telegram link hint (English)", "content.texts.agent_link_hint_en", { type: "text", maxlength: 120, hint: "Placeholder in the link box" })}
        ${field("Telegram link hint (Amharic)", "content.texts.agent_link_hint_am", { type: "text", maxlength: 120 })}
        ${field("Description hint (English)", "content.texts.agent_note_hint_en", { type: "text", maxlength: 160, hint: "Placeholder in the description box" })}
        ${field("Description hint (Amharic)", "content.texts.agent_note_hint_am", { type: "text", maxlength: 160 })}
        <hr class="cc-hr">
        ${field("Support contact", "settings.support_contact", { type: "text", maxlength: 64, hint: "e.g. @melbete_support_1" })}</article></div>`;
  }
  const DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  function promotionsHtml() {
    const list = cc.draft.promotions || [];
    const rows = list.map((p, i) => `<article class="work-panel cc-card" data-promo="${i}">
        <div class="panel-heading"><div><h3>${esc(p.name || "New promotion")}</h3><p>Cashback × ${esc(p.multiplier)} for deposits inside this window</p></div>
          <button class="btn btn-delete" type="button" data-act="promo-del" data-i="${i}">Remove</button></div>
        ${toggle(`promotions.${i}.enabled`, "Running", "Turn off to pause without deleting")}
        <div class="form-grid">${field("Name", `promotions.${i}.name`, { type: "text", maxlength: 40 })}
          ${field("Cashback multiplier", `promotions.${i}.multiplier`, { type: "float", unit: "×", min: 1.1, max: 5, hint: "2 = double cashback (5% becomes 10%)" })}</div>
        <div class="field"><label>Days (Ethiopia time)</label><div class="cc-days">${DAY_NAMES.map((d, di) => `<label class="cc-day"><input type="checkbox" data-path="promotions.${i}.days.${di}" data-type="bool" ${p.days && p.days[di] ? "checked" : ""}><span>${d}</span></label>`).join("")}</div></div>
        <div class="form-grid">${field("From time", `promotions.${i}.start_time`, { type: "text", maxlength: 5, hint: "24-hour, like 18:00. Same start and end = all day" })}
          ${field("Until time", `promotions.${i}.end_time`, { type: "text", maxlength: 5, hint: "A window like 22:00 to 02:00 runs past midnight" })}
          ${field("Start date (optional)", `promotions.${i}.start_date`, { type: "text", maxlength: 10, hint: "like 2026-12-01" })}
          ${field("End date (optional)", `promotions.${i}.end_date`, { type: "text", maxlength: 10, hint: "like 2026-12-31" })}</div></article>`).join("");
    return `<div class="cc-grid"><article class="work-panel cc-card"><div class="panel-heading"><div><h3>Timed cashback boosts</h3><p>Deposits made inside a promotion earn more cashback. Players still need to leave the money in that day. The best matching promotion applies (they do not stack). Part of the draft: applies when published.</p></div></div>
      ${list.length < 10 ? `<button class="primary-btn" type="button" data-act="promo-add">Add a promotion</button>` : `<p class="cc-hint">You can have up to 10 promotions.</p>`}</article>${rows}</div>`;
  }
  function addPromotion() {
    cc.draft.promotions = cc.draft.promotions || [];
    cc.draft.promotions.push({ name: "Friday double cashback", enabled: true, applies: ["cashback"], multiplier: 2, days: [false, false, false, false, true, false, false], start_date: "", end_date: "", start_time: "00:00", end_time: "00:00" });
    cc.dirty = true; render();
  }
  function removePromotion(i) {
    cc.draft.promotions.splice(i, 1);
    cc.dirty = true; render();
  }
  function switchesHtml() {
    const pub = cc.snap.published, m = pub.maintenance, k = pub.kill_switch, canEm = can("control.emergency");
    const flagRows = Object.entries(cc.snap.flags).map(([f, name]) => toggle(`flags.${f}`, name, "Part of the draft - applies when published")).join("");
    return `<div class="cc-grid">
      <article class="work-panel cc-card"><div class="panel-heading"><div><h3>Feature switches</h3><p>Turn individual features on or off for all players</p></div></div><fieldset class="cc-fieldset" ${lock()}>${flagRows}</fieldset></article>
      <article class="work-panel cc-card cc-danger"><div class="panel-heading"><div><h3>Emergency controls</h3><p>These apply <b>immediately</b> and are logged. They bypass the draft.</p></div><span class="cc-chip ${m.enabled ? "bad" : "ok"}">${m.enabled ? "Maintenance ON" : "Maintenance off"}</span></div>
        <div class="field"><label>Maintenance message</label><textarea id="cc-m-msg" rows="2" maxlength="400" ${canEm ? "" : "disabled"}>${esc(m.message)}</textarea></div>
        <div class="form-grid"><div class="field"><label>Expected back</label><input id="cc-m-eta" type="text" maxlength="80" placeholder="e.g. 21:00" value="${esc(m.eta)}" ${canEm ? "" : "disabled"}></div>
        <div class="field"><label>Admins can still use the app</label><select id="cc-m-admins" ${canEm ? "" : "disabled"}><option value="1" ${m.allow_admins ? "selected" : ""}>Yes</option><option value="0" ${m.allow_admins ? "" : "selected"}>No</option></select></div></div>
        <button class="${m.enabled ? "primary-btn" : "btn btn-delete"}" data-act="maint" ${canEm ? "" : "disabled"}>${m.enabled ? "Turn maintenance off" : "Turn maintenance on now"}</button>
        <hr class="cc-hr">
        <div class="panel-heading"><div><h3>Kill switch</h3><p>Stops deposits, withdrawals and joining rounds at once</p></div><span class="cc-chip ${k.enabled ? "bad" : "ok"}">${k.enabled ? "Kill switch ON" : "Off"}</span></div>
        <div class="field"><label>Message shown to players</label><input id="cc-k-reason" type="text" maxlength="300" placeholder="Optional" value="${esc(k.reason)}" ${canEm ? "" : "disabled"}></div>
        <button class="${k.enabled ? "primary-btn" : "btn btn-delete"}" data-act="kill" ${canEm ? "" : "disabled"}>${k.enabled ? "Turn kill switch off" : "Activate kill switch now"}</button>
        ${canEm ? "" : `<p class="cc-note">Your role cannot use emergency controls.</p>`}</article></div>`;
  }

  /* ---------------- alerts ---------------- */
  function alertsHtml() {
    return `<div class="cc-grid">
      <article class="work-panel cc-card"><div class="panel-heading"><div><h3>When to alert</h3><p>Part of the draft: applies when published</p></div></div>
        <fieldset class="cc-fieldset" ${lock()}>${toggle("alerts.enabled", "Send Telegram alerts to admins")}
        <div class="form-grid">${field("Large withdrawal from", "alerts.big_withdrawal_amount", { unit: "ETB", min: 0, hint: "0 = never" })}
        ${field("Large wallet adjustment from", "alerts.big_wallet_adjust_amount", { unit: "ETB", min: 0, hint: "0 = never" })}
        ${field("Failed sign-ins before alerting", "alerts.failed_logins_before_alert", { min: 1, max: 20, hint: "Within 10 minutes from one place" })}</div></fieldset></article>
      <article class="work-panel cc-card" id="cc-alerts-mine"><p class="cc-note">Loading your alerts…</p></article></div>`;
  }
  async function loadAlertsMine() {
    const a = await api("/api/admin/alerts"), box = $("cc-alerts-mine"); if (!box) return;
    cc.alerts = a;
    const rows = a.types.map((t) => `<label class="toggle-row"><div><strong>${esc(t.label)}</strong><span>${esc(t.description)}${t.group ? " Also sent to the admin group." : ""}</span></div>
      <span class="toggle-switch"><input type="checkbox" data-alert="${esc(t.key)}" ${a.mine.includes(t.key) ? "checked" : ""} ${a.can_customise ? "" : "disabled"}><span class="toggle-track"></span></span></label>`).join("");
    box.innerHTML = `<div class="panel-heading"><div><h3>My alerts</h3><p>Saved immediately. ${a.custom ? "You chose your own list." : "Using your role's defaults."}</p></div></div>
      <div class="cc-chips"><span class="cc-chip ${a.delivery.bot_configured ? "ok" : "bad"}">${a.delivery.bot_configured ? "Telegram bot connected" : "Telegram bot not configured"}</span>
      <span class="cc-chip ${a.delivery.group_chat ? "ok" : ""}">${a.delivery.group_chat ? "Admin group connected" : "No admin group set"}</span></div>
      ${a.can_customise ? "" : `<p class="cc-note">You are an environment-configured admin. Ask a super admin to add you to the staff list to customise alerts.</p>`}
      ${rows}<div class="cc-actions"><button class="secondary-btn" data-aact="test">Send me a test alert</button>${a.custom ? `<button class="secondary-btn" data-aact="reset">Back to role defaults</button>` : ""}</div>
      <p class="cc-hint">Alerts arrive as private Telegram messages from the bot. If the test fails, open the bot in Telegram and press Start.</p>`;
  }
  async function saveMyAlerts() {
    const types = [...document.querySelectorAll("[data-alert]:checked")].map((c) => c.dataset.alert);
    await api("/api/admin/alerts/mine", { method: "PUT", body: JSON.stringify({ types }) });
    toast("Alert choices saved", "success"); await loadAlertsMine();
  }

  /* ---------------- players ---------------- */
  function playersHtml() {
    if (!can("players.view")) return `<p class="cc-note">Your role cannot view players.</p>`;
    return `<article class="work-panel cc-card"><div class="panel-heading"><div><h3>Find a player</h3><p>Name, Telegram ID or phone</p></div></div>
      <div class="cc-search"><input id="cc-psearch" type="search" placeholder="Search players" autocomplete="off"><button class="primary-btn" data-act="psearch">Search</button></div>
      <div id="cc-presults" class="record-list"><p class="cc-note">Search to manage a player's access, limits, wallet and notes.</p></div></article>`;
  }
  async function searchPlayers() {
    const q = $("cc-psearch").value.trim();
    const { users } = await api(`/api/admin/users?limit=30&search=${encodeURIComponent(q)}`);
    $("cc-presults").innerHTML = users.length ? users.map((u) => `<button class="record-card cc-prow" data-player="${esc(u.id)}"><div><strong>${esc(u.display_name)}</strong><small>${esc(u.telegram_id)}${u.phone_number ? " · " + esc(u.phone_number) : ""}</small></div>
      <div class="cc-prow-r">${u.is_banned ? `<span class="cc-chip bad">Banned</span>` : ""}<span>${Number(u.balance || 0).toLocaleString()} ETB</span></div></button>`).join("") : `<p class="cc-note">No players found.</p>`;
  }
  async function openPlayer(id) {
    const d = await api(`/api/admin/users/${encodeURIComponent(id)}/control`);
    cc.player = d;
    const p = d.player, w = d.wallet || { balance: 0, won_balance: 0, bonus_balance: 0 };
    const dl = p.daily_deposit_limit ?? "", wl = p.daily_withdrawal_limit ?? "";
    const html = `<div class="cc-pl-head"><div><h3>${esc(p.display_name)}</h3><small>${esc(p.telegram_id)}${p.phone_number ? " · " + esc(p.phone_number) : ""}</small></div><button class="secondary-btn" data-act="pclose">Close</button></div>
      <div class="cc-chips">${p.is_banned ? `<span class="cc-chip bad">Banned${p.ban_reason ? ": " + esc(p.ban_reason) : ""}</span>` : `<span class="cc-chip ok">Active</span>`}${p.is_frozen ? `<span class="cc-chip warn">Frozen${p.frozen_reason ? ": " + esc(p.frozen_reason) : ""}</span>` : ""}</div>
      <div class="cc-stats"><div><small>Real</small><b>${w.balance.toLocaleString()}</b></div><div><small>Winnings</small><b>${w.won_balance.toLocaleString()}</b></div><div><small>Bonus</small><b>${w.bonus_balance.toLocaleString()}</b></div>
        <div><small>Deposited 24h</small><b>${d.used_24h.deposit.toLocaleString()}</b></div><div><small>Withdrawn 24h</small><b>${d.used_24h.withdrawal.toLocaleString()}</b></div></div>
      ${can("players.ban") ? `<div class="cc-actions">${p.is_banned ? `<button class="primary-btn" data-pact="unban">Unban</button>` : `<button class="btn btn-delete" data-pact="ban">Ban</button>`}
        ${p.is_frozen ? `<button class="primary-btn" data-pact="unfreeze">Unfreeze</button>` : `<button class="secondary-btn" data-pact="freeze">Freeze money</button>`}</div>` : ""}
      ${can("players.notes") ? `<h4>Personal daily limits</h4><div class="form-grid"><div class="field"><label>Deposits <small>(ETB, blank = platform ${d.platform_limits.deposit || "none"})</small></label><input id="cc-l-dep" type="number" min="0" value="${esc(dl)}"></div>
        <div class="field"><label>Withdrawals <small>(ETB, blank = platform ${d.platform_limits.withdrawal || "none"})</small></label><input id="cc-l-wd" type="number" min="0" value="${esc(wl)}"></div></div><button class="secondary-btn" data-pact="limits">Save limits</button>` : ""}
      ${can("players.wallet_adjust") ? `<h4>Adjust wallet</h4><div class="form-grid"><div class="field"><label>Pool</label><select id="cc-a-kind"><option value="real">Real (playable)</option><option value="winnings">Winnings (withdrawable)</option><option value="bonus">Bonus</option></select></div>
        <div class="field"><label>Amount <small>(+ add, − remove)</small></label><input id="cc-a-delta" type="number" placeholder="e.g. 100 or -50"></div></div>
        <div class="field"><label>Reason (required)</label><input id="cc-a-reason" type="text" maxlength="200"></div><button class="secondary-btn" data-pact="adjust">Apply adjustment</button>` : ""}
      ${can("players.notes") ? `<h4>Staff notes</h4><div class="cc-search"><input id="cc-n-text" type="text" maxlength="1000" placeholder="Add a note"><button class="secondary-btn" data-pact="note">Add</button></div>` : ""}
      <div class="record-list">${d.notes.map((n) => `<div class="record-card"><div><strong>${esc(n.admin_id)}</strong> <small>${fmtTime(n.created_at)}</small><p>${esc(n.note)}</p></div>${can("players.notes") ? `<button class="btn btn-ghost" data-pact="delnote" data-id="${esc(n.id)}">Delete</button>` : ""}</div>`).join("") || `<p class="cc-note">No notes yet.</p>`}</div>
      <h4>Recent activity</h4><div class="record-list">${d.transactions.map((t) => `<div class="record-card"><div><strong>${esc(t.type.replace(/_/g, " "))}</strong> <small>${fmtTime(t.created_at)}</small>${t.note ? `<p>${esc(t.note)}</p>` : ""}</div><b class="${t.amount < 0 ? "neg" : "pos"}">${t.amount > 0 ? "+" : ""}${t.amount.toLocaleString()}</b></div>`).join("") || `<p class="cc-note">No transactions.</p>`}</div>
      <h4>Staff actions on this player</h4><div class="record-list">${d.audit.map((a) => `<div class="record-card"><div><strong>${esc(a.action)}</strong> <small>${esc(a.actor_id)} · ${fmtTime(a.created_at)}</small>${a.summary ? `<p>${esc(a.summary)}</p>` : ""}</div></div>`).join("") || `<p class="cc-note">None yet.</p>`}</div>`;
    let drawer = $("cc-player-drawer");
    if (!drawer) { drawer = document.createElement("div"); drawer.id = "cc-player-drawer"; drawer.className = "cc-modal"; document.body.appendChild(drawer); }
    drawer.innerHTML = `<div class="cc-modal-card cc-drawer" role="dialog" aria-modal="true">${html}</div>`;
    drawer.style.display = "flex";
  }
  const closePlayer = () => { const d = $("cc-player-drawer"); if (d) d.style.display = "none"; };

  async function playerAction(action, target) {
    const p = cc.player.player, base = `/api/admin/users/${encodeURIComponent(p.id)}`;
    const post = (path, body, method = "POST") => api(base + path, { method, body: JSON.stringify(body || {}) });
    if (action === "ban" || action === "freeze") {
      const reason = window.prompt(action === "ban" ? "Reason for the ban?" : "Reason for the freeze?", action === "ban" ? "Suspended by admin" : "Account under review");
      if (reason === null) return;
      await post(`/${action}`, { reason }); toast(action === "ban" ? "Player banned" : "Money frozen", "success");
    } else if (action === "unban" || action === "unfreeze") {
      await post(`/${action}`); toast("Done", "success");
    } else if (action === "limits") {
      await post("/limits", { daily_deposit_limit: $("cc-l-dep").value, daily_withdrawal_limit: $("cc-l-wd").value }, "PUT"); toast("Limits saved", "success");
    } else if (action === "note") {
      const note = $("cc-n-text").value.trim(); if (!note) return;
      await post("/notes", { note }); toast("Note added", "success");
    } else if (action === "delnote") {
      await api(`${base}/notes/${encodeURIComponent(target.dataset.id)}`, { method: "DELETE" });
    } else if (action === "adjust") {
      const delta = Number($("cc-a-delta").value), kind = $("cc-a-kind").value, reason = $("cc-a-reason").value.trim();
      if (!delta) return toast("Enter an amount", "error");
      const ok = await confirmDialog("Adjust wallet", `<p>${delta > 0 ? "Add" : "Remove"} <b>${Math.abs(delta).toLocaleString()} ETB</b> ${delta > 0 ? "to" : "from"} <b>${esc(p.display_name)}</b>'s ${esc(kind)} balance?</p><p class="cc-hint">Recorded in the player's ledger and the audit log.</p>`, "Apply");
      if (!ok) return;
      await post("/wallet-adjust", { kind, delta, reason }); toast("Wallet adjusted", "success");
    }
    await openPlayer(p.id);
  }

  /* ---------------- staff ---------------- */
  // Placeholder shown while loadStaff() fetches; renderSection() looks this function up for every section.
  function staffHtml() { return `<p class="cc-note">Loading staff…</p>`; }
  async function loadStaff() {
    const body = $("cc-body");
    const [{ admins }, roles] = await Promise.all([api("/api/admin/admins"), api("/api/admin/roles")]);
    const manage = can("staff.manage");
    const permCheckbox = (a, p, name) => `<label class="cc-check"><input type="checkbox" data-xp="${p}" ${a.extra_permissions.includes(p) ? "checked" : ""} ${manage && !a.is_super_admin ? "" : "disabled"}> ${esc(name)}</label>`;
    const rows = admins.map((a) => `<article class="work-panel cc-card" data-staff="${esc(a.telegram_id)}"><div class="panel-heading"><div><h3>${esc(a.telegram_id)} ${a.is_super_admin ? "👑" : ""}</h3><p>${a.added_by ? "Added by " + esc(a.added_by) : "Original administrator"}</p></div>
      <span class="cc-chip">${esc(roles.roles[a.role]?.label || a.role)}</span></div>
      ${a.is_super_admin ? `<p class="cc-hint">Super admins have every permission.</p>` : `
        <div class="field"><label>Role</label><select data-role ${manage ? "" : "disabled"}>${roles.assignable.map((r) => `<option value="${r}" ${a.role === r ? "selected" : ""}>${esc(roles.roles[r].label)}</option>`).join("")}</select>
        <small class="cc-hint" data-role-hint>${esc(roleSummary(roles, a.role))}</small></div>
        <details><summary>Extra permissions</summary><div class="cc-checks">${Object.entries(roles.permissions).map(([p, n]) => permCheckbox(a, p, n)).join("")}</div></details>
        ${manage ? `<div class="cc-actions"><button class="primary-btn" data-sact="save">Save</button><button class="btn btn-delete" data-sact="remove">Remove</button></div>` : ""}`}</article>`).join("");
    const addForm = manage ? `<article class="work-panel cc-card"><div class="panel-heading"><div><h3>Add staff</h3><p>Give someone a role</p></div></div><div class="form-grid"><div class="field"><label>Telegram ID</label><input id="cc-s-id" inputmode="numeric" placeholder="e.g. 123456789"></div>
      <div class="field"><label>Role</label><select id="cc-s-role">${roles.assignable.map((r) => `<option value="${r}">${esc(roles.roles[r].label)}</option>`).join("")}</select></div></div><button class="primary-btn" data-sact="add">Add</button></article>` : "";
    const legend = `<article class="work-panel cc-card"><div class="panel-heading"><div><h3>What each role can do</h3></div></div>${roles.assignable.concat("super").map((r) => `<p><b>${esc(roles.roles[r].label)}</b><br><small>${esc(roleSummary(roles, r))}</small></p>`).join("")}</article>`;
    body.innerHTML = `<div class="cc-grid">${addForm}${rows}${legend}</div>`;
    body._roles = roles;
  }
  const roleSummary = (roles, r) => (roles.roles[r]?.permissions || []).map((p) => roles.permissions[p]).join(" · ") || "—";

  async function staffAction(act, card) {
    if (act === "add") {
      await api("/api/admin/admins", { method: "POST", body: JSON.stringify({ telegram_id: $("cc-s-id").value.trim(), role: $("cc-s-role").value }) });
      toast("Staff added", "success");
    } else {
      const id = card.dataset.staff;
      if (act === "remove") {
        if (!(await confirmDialog("Remove staff", `<p>Remove access for <b>${esc(id)}</b>?</p>`, "Remove", true))) return;
        await api(`/api/admin/admins/${encodeURIComponent(id)}`, { method: "DELETE" }); toast("Removed", "success");
      } else {
        const extra = [...card.querySelectorAll("[data-xp]:checked")].map((c) => c.dataset.xp);
        await api(`/api/admin/admins/${encodeURIComponent(id)}/role`, { method: "PUT", body: JSON.stringify({ role: card.querySelector("[data-role]").value, extra_permissions: extra }) });
        toast("Role saved", "success");
      }
    }
    await loadStaff();
  }

  /* ---------------- audit ---------------- */
  function auditHtml() {
    if (!can("audit.view")) return `<p class="cc-note">Your role cannot view the audit log.</p>`;
    return `<article class="work-panel cc-card"><div class="panel-heading"><div><h3>Audit log</h3><p>Every admin action: who, what, when</p></div></div>
      <div class="cc-search"><select id="cc-au-cat"><option value="">All actions</option><option value="control.">Control Center</option><option value="player.">Players</option><option value="wallet.">Wallet adjustments</option><option value="deposit.">Deposits</option><option value="withdrawal.">Withdrawals</option><option value="staff.">Staff</option><option value="settings.">Settings</option></select>
      <input id="cc-au-actor" type="text" placeholder="Admin ID"><button class="secondary-btn" data-act="aufilter">Filter</button></div>
      <div id="cc-audit-list" class="record-list"></div><button id="cc-audit-more" class="secondary-btn" data-act="aumore" style="display:none">Load more</button></article>`;
  }
  async function loadAudit(reset) {
    if (reset) cc.auditBefore = null;
    const params = new URLSearchParams({ limit: 40 });
    const cat = $("cc-au-cat")?.value, actor = $("cc-au-actor")?.value.trim();
    if (cat) params.set("action", cat); if (actor) params.set("actor", actor); if (cc.auditBefore) params.set("before_id", cc.auditBefore);
    const { entries, next_before_id } = await api(`/api/admin/audit?${params}`);
    const list = $("cc-audit-list"); if (!list) return;
    const rows = entries.map((e) => `<div class="record-card cc-audit"><div><strong>${esc(e.action)}</strong> <small>${fmtTime(e.created_at)}</small>
      <p>${esc(e.actor_id)}${e.actor_role ? ` <span class="cc-chip">${esc(e.actor_role)}</span>` : ""}${e.target_type ? ` → ${esc(e.target_type)} ${esc(e.target_id || "")}` : ""}</p>
      ${e.summary ? `<p>${esc(e.summary)}</p>` : ""}${e.before_json || e.after_json ? `<details><summary>Details</summary><pre>${esc(JSON.stringify({ before: e.before_json, after: e.after_json }, null, 2))}</pre></details>` : ""}</div></div>`).join("");
    list.innerHTML = (reset ? "" : list.innerHTML) + (rows || (reset ? `<p class="cc-note">Nothing recorded yet.</p>` : ""));
    cc.auditBefore = next_before_id; $("cc-audit-more").style.display = next_before_id ? "" : "none";
  }

  /* ---------------- actions ---------------- */
  async function saveDraft() {
    adopt(await api("/api/admin/control/draft", { method: "PUT", body: JSON.stringify({ config: cc.draft }) }));
    toast("Draft saved", "success"); render();
  }
  async function publish() {
    const rows = cc.snap.changes.map((c) => `<li><b>${esc(label(c.path))}</b>: ${esc(show(c.from))} → ${esc(show(c.to))}</li>`).join("");
    const ok = await confirmDialog("Publish changes", `<p>These go live for all players now:</p><ul class="cc-changes">${rows}</ul>`, "Publish now");
    if (!ok) return;
    const snap = await api("/api/admin/control/publish", { method: "POST" });
    adopt(snap);
    const sync = snap && snap.lobby_sync, lobbyNames = { draw_interval: "draw interval", countdown_seconds: "countdown", min_cartelas_to_start: "minimum to start", stake_amount: "stake", cards_per_player: "cartelas per player", cartela_min: "cartela range", cartela_max: "cartela range" };
    const list = (a) => [...new Set((a || []).map((k) => lobbyNames[k] || k))].join(", ");
    toast(`Published.${sync && sync.applied && sync.applied.length ? ` Applied to the open lobby: ${list(sync.applied)}.` : ""}${sync && sync.deferred && sync.deferred.length ? ` From the next round: ${list(sync.deferred)}.` : ""}`, "success"); render();
  }

  // Used by the dashboard's "unpublished changes" banner: refresh the draft, then run the normal
  // review-and-publish dialog. Returns true when the publish went through.
  window.publishControlDraft = async () => {
    try {
      if (cc.dirty) { toast("You have unsaved edits in Control Center. Save the draft first.", "error"); goToTab("control"); return false; }
      adopt(await api("/api/admin/control"));
      if (!can("control.publish")) { toast("Your role can't publish. Ask a Super Admin or Manager.", "error"); return false; }
      if (!cc.snap.has_draft || !cc.snap.changes.length) { toast("Nothing to publish.", "success"); return false; }
      await publish();
      return true;
    } catch (err) {
      toast((err && err.message) || "Could not publish", "error");
      return false;
    }
  };

  document.addEventListener("click", async (e) => {
    const root = $("cc-root"), drawer = $("cc-player-drawer");
    const inRoot = root && root.contains(e.target), inDrawer = drawer && drawer.contains(e.target);
    if (!inRoot && !inDrawer) return;
    try {
      const sec = e.target.closest("[data-sec]");
      if (sec && inRoot) { cc.section = sec.dataset.sec; return render(); }
      const pr = e.target.closest("[data-player]"); if (pr) return await openPlayer(pr.dataset.player);
      const pa = e.target.closest("[data-pact]"); if (pa) return await playerAction(pa.dataset.pact, pa);
      const sa = e.target.closest("[data-sact]"); if (sa) return await staffAction(sa.dataset.sact, sa.closest("[data-staff]"));
      const aa = e.target.closest("[data-aact]");
      if (aa && inRoot) {
        if (aa.dataset.aact === "test") { await api("/api/admin/alerts/test", { method: "POST" }); return toast("Test alert sent. Check Telegram.", "success"); }
        if (aa.dataset.aact === "reset") { await api("/api/admin/alerts/mine", { method: "PUT", body: JSON.stringify({ types: null }) }); toast("Back to role defaults", "success"); return await loadAlertsMine(); }
      }
      const act = e.target.closest("[data-act]")?.dataset.act; if (!act) { if (e.target === drawer) closePlayer(); return; }
      if (act === "pclose") return closePlayer();
      if (act === "save") return await saveDraft();
      if (act === "publish") return await publish();
      if (act === "discard") { if (await confirmDialog("Discard draft", "<p>Throw away all unpublished changes?</p>", "Discard", true)) { adopt(await api("/api/admin/control/discard", { method: "POST" })); render(); } return; }
      if (act === "rollback") { if (await confirmDialog("Undo last publish", "<p>Restore the configuration from before the last publish? Settings it changed go back too.</p>", "Undo publish", true)) { adopt(await api("/api/admin/control/rollback", { method: "POST" })); toast("Rolled back", "success"); render(); } return; }
      if (act === "maint" || act === "kill") return await emergency(act);
      if (act === "promo-add") return addPromotion();
      if (act === "flow-demo") return openFlowDemo();
      if (act === "promo-del") return removePromotion(Number(e.target.closest("[data-i]").dataset.i));
      if (act === "psearch") return await searchPlayers();
      if (act === "aufilter") return await loadAudit(true);
      if (act === "aumore") return await loadAudit(false);
    } catch (err) { toast(err.message || "Something went wrong", "error"); }
  });

  async function emergency(kind) {
    const m = cc.snap.published.maintenance, k = cc.snap.published.kill_switch;
    const turningOn = kind === "maint" ? !m.enabled : !k.enabled;
    const what = kind === "maint" ? "Maintenance mode" : "The kill switch";
    const body = turningOn
      ? `<p><b>${what}</b> will take effect <b>immediately</b> for every player.</p><p class="cc-hint">${kind === "maint" ? "Players see the maintenance screen until you turn it off." : "Deposits, withdrawals and joining rounds stop. Rounds in progress finish."}</p>`
      : `<p>Turn off ${what.toLowerCase()} and let players back in?</p>`;
    if (!(await confirmDialog(turningOn ? `Turn on ${what.toLowerCase()}?` : `Turn off ${what.toLowerCase()}?`, body, turningOn ? "Yes, do it now" : "Turn off", turningOn))) return;
    const payload = kind === "maint"
      ? { maintenance: { enabled: turningOn, message: $("cc-m-msg").value, eta: $("cc-m-eta").value, allow_admins: $("cc-m-admins").value === "1" } }
      : { kill_switch: { enabled: turningOn, reason: $("cc-k-reason").value } };
    const snap = await api("/api/admin/control/emergency", { method: "POST", body: JSON.stringify(payload) });
    // keep unsaved local edits; only refresh what the emergency changed
    cc.snap = snap; cc.perms = new Set(snap.permissions);
    for (const key of ["maintenance", "kill_switch"]) cc.draft[key] = clone(snap.published[key]);
    toast(turningOn ? "Applied immediately" : "Turned off", "success"); render();
  }

  function onEdit(e) {
    if (e.type === "change" && e.target.matches && e.target.matches("[data-alert]")) { saveMyAlerts().catch((err) => toast(err.message || "Could not save", "error")); return; }
    const el = e.target.closest("[data-path]");
    if (el && $("cc-body")?.contains(el)) {
      const type = el.dataset.type; let v;
      if (type === "bool") v = el.checked;
      else if (type === "int") v = el.value === "" ? null : parseInt(el.value, 10);
      else if (type === "float") v = el.value === "" ? null : parseFloat(el.value);
      else if (type === "pct") v = el.value === "" ? null : Math.round((1 - parseFloat(el.value) / 100) * 10000) / 10000;
      else if (type === "datetime") v = el.value ? new Date(el.value).toISOString() : "";
      else v = el.value;
      const path = el.dataset.path;
      if (path.startsWith("settings.")) { cc.draft.settings = cc.draft.settings || {}; if (v === null) delete cc.draft.settings[path.slice(9)]; else cc.draft.settings[path.slice(9)] = v; }
      else setPath(cc.draft, path, v ?? 0);
      if (!cc.dirty) { cc.dirty = true; const bar = document.querySelector(".cc-bar"); if (bar) bar.outerHTML = draftBar(); }
      return;
    }
    const sel = e.target.closest("[data-role]");
    if (sel) { const hint = sel.closest(".field").querySelector("[data-role-hint]"); const roles = $("cc-body")._roles; if (hint && roles) hint.textContent = roleSummary(roles, sel.value); }
  }
  document.addEventListener("input", onEdit);
  document.addEventListener("change", onEdit);
  document.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.id === "cc-psearch") searchPlayers().catch((err) => toast(err.message, "error")); });
})();
