// Melbete Bingo - payment flow: turns the deposit and withdraw modals into short one-task steps
// (Method -> Amount -> Proof, and Amount -> Account -> Review), with quick-amount chips, a saved
// account card, inline validation, a fixed bottom button and a status timeline on the receipt.
// Display layer only: it reuses the existing elements, ids and submit handlers, and never changes
// wallet logic or API payloads. Loaded after melbete-roundA.js (needs `state`, `mbT`, MB_A).
(() => {
  "use strict";
  if (typeof mbT !== "function") return;
  const $ = (id) => document.getElementById(id);
  const t = (k, v) => mbT(k, v);
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  const fmt = (n) => Number(n).toLocaleString("en-US");
  const tag = (node, steps) => { if (node) node.dataset.pf = steps; return node; };
  const labelFor = (id) => document.querySelector(`label[for="${id}"]`);
  const REF_RE = /https?:\/\/\S+|\b(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{8,14}\b/;
  const normKey = (k) => String(k || "").trim().toLowerCase();
  const findSavedAcct = (accounts, key) => {
    if (!accounts || !key) return "";
    if (accounts[key]) return accounts[key];
    const nk = normKey(key);
    for (const k of Object.keys(accounts)) {
      if (normKey(k) === nk && accounts[k]) return accounts[k];
    }
    return "";
  };

  // ------------------------------------------------------------------ generic step engine
  function buildShell(flow) {
    const modal = $(flow.modalId);
    const content = modal && modal.querySelector(".modal-content");
    if (!content || content.classList.contains("mb-pf")) return null;
    content.classList.add("mb-pf");
    flow.content = content;

    const bar = el("div", "mb-pf-bar");
    const track = el("div", "mb-pf-track");
    flow.fill = el("i"); track.appendChild(flow.fill);
    flow.barLabel = el("p", "mb-pf-barlabel");
    flow.barLabel.setAttribute("aria-live", "polite");
    bar.append(track, flow.barLabel);
    content.querySelector("h3")?.after(bar);

    flow.foot = el("div", "mb-pf-foot");
    flow.err = el("p", "mb-pf-err"); flow.err.setAttribute("role", "alert");
    const row = el("div", "mb-pf-btns");
    flow.back = el("button", "secondary-btn mb-pf-back", ""); flow.back.type = "button";
    flow.next = el("button", "primary-btn mb-pf-next", ""); flow.next.type = "button";
    const submit = $(flow.submitId);
    flow.submit = submit;
    row.append(flow.back, flow.next);
    if (submit) row.appendChild(submit);
    flow.foot.append(flow.err, row);
    content.appendChild(flow.foot);

    flow.back.addEventListener("click", () => go(flow, flow.step - 1));
    flow.next.addEventListener("click", async () => {
      flow.next.disabled = true;
      try {
        const r = await flow.validate(flow.step);
        if (r && r.msg) return setErr(flow, r.msg);
        if (r && r.stay) return;
        go(flow, flow.step + 1);
      } finally { flow.next.disabled = false; }
    });
    // Last step: inline checks first; the original handler (bound on the button) then submits.
    flow.foot.addEventListener("click", (e) => {
      if (e.target !== flow.submit || !flow.submitGuard) return;
      const msg = flow.submitGuard();
      if (msg) { e.stopImmediatePropagation(); e.preventDefault(); setErr(flow, msg); }
    }, true);
    return content;
  }

  function setErr(flow, msg) {
    flow.err.textContent = msg || "";
    flow.err.classList.toggle("on", !!msg);
  }

  function paintChrome(flow) {
    const last = flow.steps.length - 1;
    flow.content.dataset.step = String(flow.step);
    flow.fill.style.width = `${((flow.step + 1) / flow.steps.length) * 100}%`;
    flow.barLabel.textContent = `${t("pf_step_of", { n: flow.step + 1, total: flow.steps.length })} · ${t(flow.steps[flow.step])}`;
    flow.back.textContent = t("pf_back");
    flow.next.textContent = t("pf_continue");
    flow.back.classList.toggle("hidden", flow.step === 0);
    flow.next.classList.toggle("hidden", flow.step === last);
    flow.submit?.classList.toggle("hidden", flow.step !== last);
    flow.content.querySelectorAll("[data-pf-t]").forEach((n) => { n.textContent = t(n.dataset.pfT); });
  }

  function go(flow, n) {
    flow.step = Math.max(0, Math.min(flow.steps.length - 1, n));
    setErr(flow, "");
    paintChrome(flow);
    flow.enter?.(flow.step);
    flow.content.classList.remove("mb-pf-anim"); void flow.content.offsetWidth; flow.content.classList.add("mb-pf-anim");
    flow.content.scrollTop = 0;
  }

  function heading(key, steps) { const h = el("h4", "mb-pf-h"); h.dataset.pfT = key; h.textContent = t(key); return tag(h, steps); }

  // ------------------------------------------------------------------ deposit
  const dep = {
    modalId: "deposit-modal", submitId: "deposit-submit-btn", steps: ["st_method", "st_amount", "st_proof"], step: 0,
    limits: null, bonusPct: 0, built: false,
  };

  function depositAmount() { return Number($("deposit-amount")?.value) || 0; }

  function depositAmountError() {
    const a = depositAmount();
    if (!a) return t("pf_err_amount");
    if (dep.limits && a < dep.limits.min_deposit) return t("pf_err_min", { n: fmt(dep.limits.min_deposit) });
    if (dep.limits && a > dep.limits.max_deposit) return t("pf_err_max", { n: fmt(dep.limits.max_deposit) });
    return "";
  }

  function paintReceiveLine() {
    const a = depositAmount();
    dep.get.textContent = a > 0 ? t("pf_get", { n: fmt(a) }) : "";
    dep.get.classList.toggle("hidden", !(a > 0));
    dep.bonus.textContent = dep.bonusPct > 0 ? t("pf_bonus", { pct: dep.bonusPct }) : "";
    dep.bonus.classList.toggle("hidden", !(dep.bonusPct > 0));
    dep.chips.querySelectorAll("button[data-amt]").forEach((b) => b.classList.toggle("on", Number(b.dataset.amt) === a));
    const other = dep.chips.querySelector("button[data-other]");
    if (other) other.classList.toggle("on", a > 0 && ![100, 200, 500, 1000].includes(a));
  }

  function paintProofChecks() {
    const sms = ($("deposit-sms-text")?.value || "").trim();
    const shot = !!state.depositScreenshotDataUrl;
    const set = (node, ok, text, soft) => {
      node.classList.toggle("ok", ok); node.classList.toggle("soft", !!soft && !ok);
      node.querySelector("span").textContent = text;
      node.querySelector("b").textContent = ok ? "✓" : (soft ? "!" : "○");
    };
    set(dep.chkSms, !!sms, t("pf_chk_sms"));
    set(dep.chkShot, shot, t("pf_chk_shot"));
    const hasRef = REF_RE.test(sms);
    set(dep.chkRef, hasRef, hasRef ? t("pf_ref_ok") : t("pf_ref_no"), !hasRef && !!sms);
    dep.chkRef.classList.toggle("hidden", !sms);
  }

  function buildDeposit() {
    if (dep.built || !buildShell(dep)) return;
    dep.built = true;
    const c = dep.content;
    // Step 0: method + account details + a one-line nudge
    tag(c.querySelector(":scope > p.muted"), "0");
    const methods = $("deposit-methods"), info = $("deposit-account-info");
    methods.before(heading("pf_dep_h1", "0"));
    tag(methods, "0"); tag(info, "0");
    const hint = tag(el("p", "mb-pf-hint"), "0"); hint.dataset.pfT = "pf_send_hint"; hint.textContent = t("pf_send_hint");
    info.after(hint);
    // Step 1: amount with quick chips and a "you'll get" line
    const amtLabel = labelFor("deposit-amount"), amt = $("deposit-amount"), lim = $("deposit-limits");
    amtLabel.before(heading("pf_dep_h2", "1"));
    tag(amtLabel, "1"); tag(amt, "1"); tag(lim, "1");
    dep.chips = tag(el("div", "mb-pf-chips"), "1");
    [100, 200, 500, 1000].forEach((v) => {
      const b = el("button", "mb-pf-chip", fmt(v)); b.type = "button"; b.dataset.amt = String(v);
      b.addEventListener("click", () => { amt.value = String(v); amt.dispatchEvent(new Event("input", { bubbles: true })); setErr(dep, ""); });
      dep.chips.appendChild(b);
    });
    const other = el("button", "mb-pf-chip", ""); other.type = "button"; other.dataset.other = "1"; other.dataset.pfT = "pf_chip_other"; other.textContent = t("pf_chip_other");
    other.addEventListener("click", () => { amt.value = ""; amt.dispatchEvent(new Event("input", { bubbles: true })); amt.focus(); });
    dep.chips.appendChild(other);
    dep.get = tag(el("p", "mb-pf-get hidden"), "1");
    dep.bonus = tag(el("p", "mb-pf-bonus hidden"), "1");
    amt.after(dep.chips); dep.chips.after(dep.get); dep.get.after(dep.bonus);
    amt.addEventListener("input", () => {
      paintReceiveLine();
      setErr(dep, dep.step === 1 && amt.value ? depositAmountError() : "");
    });
    // Step 2: proof with a live tick list instead of the long warning
    const smsLabel = labelFor("deposit-sms-text"), sms = $("deposit-sms-text");
    const shotLabel = labelFor("deposit-screenshot"), drop = c.querySelector(".file-drop"), prev = $("deposit-screenshot-preview");
    const warn = c.querySelector(":scope > p.warning-text");
    smsLabel.before(heading("pf_dep_h3", "2"));
    [smsLabel, sms, shotLabel, drop, prev].forEach((n) => tag(n, "2"));
    tag(warn, "none");
    const tip = tag(el("p", "mb-pf-hint"), "2"); tip.dataset.pfT = "pf_shot_tip"; tip.textContent = t("pf_shot_tip");
    drop.after(tip);
    const list = tag(el("ul", "mb-pf-checks"), "2");
    const mk = () => { const li = el("li"); li.append(el("b", "", "○"), el("span", "", "")); return li; };
    dep.chkSms = mk(); dep.chkShot = mk(); dep.chkRef = mk();
    list.append(dep.chkSms, dep.chkShot, dep.chkRef);
    prev.after(list);
    const trust = tag(el("p", "mb-pf-trust"), "2"); trust.textContent = "🔒 " + t("pf_trust");
    list.after(trust);
    tag(c.querySelector(":scope > p.center-text"), "none");   // the trust line replaces this footnote
    sms.addEventListener("input", () => { paintProofChecks(); setErr(dep, ""); });
    sms.addEventListener("paste", () => setTimeout(paintProofChecks, 0));
    prev.addEventListener("load", () => { paintProofChecks(); setErr(dep, ""); });
    $("deposit-screenshot")?.addEventListener("change", () => setTimeout(paintProofChecks, 400));
    dep.trust = trust;

    dep.validate = async (step) => {
      if (step === 0 && !state.selectedDepositMethod) return { msg: t("pf_err_method") };
      if (step === 1) { const m = depositAmountError(); if (m) return { msg: m }; }
      return {};
    };
    dep.submitGuard = () => {
      if (!($("deposit-sms-text")?.value || "").trim()) return t("pf_err_sms");
      if (!state.depositScreenshotDataUrl) return t("pf_err_shot");
      return "";
    };
    dep.enter = (step) => {
      if (step === 1) paintReceiveLine();
      if (step === 2) paintProofChecks();
      dep.trust.textContent = "🔒 " + t("pf_trust");
    };
  }

  async function startDeposit() {
    buildDeposit();
    if (!dep.built) return;
    dep.limits = null; dep.bonusPct = 0;
    go(dep, 0);
    paintReceiveLine(); paintProofChecks();
    if (typeof api !== "function" || !state.playerId) return;
    try { dep.limits = await api(`/api/wallet/${state.playerId}/limits`); } catch (e) { /* non-fatal: range check is skipped */ }
    try {
      const data = await api(`/api/bonus/summary/${encodeURIComponent(state.playerId)}`);
      const fd = data && data.rewards && data.rewards.first_deposit;
      dep.bonusPct = fd && fd.status === "automatic" ? Number(fd.amount_etb) || 0 : 0;
      paintReceiveLine();
    } catch (e) { /* no bonus line */ }
  }

  // ------------------------------------------------------------------ withdraw
  const wd = { modalId: "withdraw-modal", submitId: "withdraw-submit-btn", steps: ["st_amount", "st_account", "pf_st_review"], step: 0, changing: false, built: false };

  function maskAccount(a) {
    const s = String(a || "").replace(/\s+/g, "");
    return s.length >= 8 ? `${s.slice(0, 2)}•• ••• ${s.slice(-3)}` : s;
  }
  function wdMethod() {
    const k = state.selectedWithdrawMethod;
    return (state.paymentMethods || []).find((m) => m.key === k)
      || (state.paymentMethods || []).find((m) => normKey(m.key) === normKey(k));
  }
  function wdAmount() { return Number($("withdraw-amount")?.value) || 0; }
  function wdAmountError() {
    const a = wdAmount(), max = withdrawableOf(state.wallet), lim = state.withdrawLimits;
    if (!a) return t("pf_err_amount");
    if (lim && a < lim.min_withdrawal) return t("pf_err_min", { n: fmt(lim.min_withdrawal) });
    if (a > max) return t("pf_err_wd_over", { n: fmt(max) });
    if (lim && a > lim.max_withdrawal) return t("pf_err_max", { n: fmt(lim.max_withdrawal) });
    return "";
  }

  function renderAccount() {
    const key = state.selectedWithdrawMethod;
    const saved = findSavedAcct(state.withdrawAccounts, key);
    const showCard = !!saved && !wd.changing;
    wd.card.classList.toggle("hidden", !showCard);
    $("withdraw-address-step").classList.toggle("hidden", !key || showCard);
    wd.nameLabel.classList.toggle("hidden", !key);
    wd.name.classList.toggle("hidden", !key);
    if (showCard) {
      const m = wdMethod();
      wd.cardText.textContent = `${m ? (m.name || "").trim() : ""} · ${maskAccount(saved)}`;
    }
  }

  function renderReview() {
    const key = state.selectedWithdrawMethod, m = wdMethod();
    const saved = findSavedAcct(state.withdrawAccounts, key) || $("withdraw-address").value.trim();
    wd.rv.innerHTML = "";
    [["pf_rv_amount", `${fmt(wdAmount())} ${t("chip_etb")}`], ["pf_rv_method", m ? (m.name || "").trim() : "-"], ["pf_rv_account", maskAccount(saved) || "-"], ["pf_rv_name", wd.name.value.trim() || "-"]]
      .forEach(([k, v]) => { const d = el("div"); d.append(el("dt", "", t(k)), el("dd", "", v)); wd.rv.appendChild(d); });
    wd.rvNote.textContent = t("pf_rv_note");
  }

  function buildWithdraw() {
    if (wd.built || !buildShell(wd)) return;
    wd.built = true;
    const c = wd.content;
    // Step 0: summary card first, then the amount
    const box = c.querySelector(".withdraw-balance-box"), dbtn = $("withdraw-deposit-btn"), limits = $("withdraw-limits");
    [box, dbtn, limits].forEach((n) => tag(n, "0"));
    const form = $("withdraw-form-step");
    const amtLabel = labelFor("withdraw-amount"), amtRow = c.querySelector(".amount-row");
    const nameLabel = labelFor("withdraw-legal-name"), name = $("withdraw-legal-name");
    limits.after(heading("pf_wd_h1", "0"));
    const h1 = c.querySelector('h4[data-pf="0"]');
    h1.after(amtLabel, amtRow);
    tag(amtLabel, "0"); tag(amtRow, "0");
    // Step 1: method, saved account card, account number (only when none saved), legal name
    const methods = $("withdraw-methods"), addr = $("withdraw-address-step");
    methods.before(heading("pf_wd_h2", "1"));
    tag(methods, "1"); tag(addr, "1");
    wd.card = tag(el("div", "mb-pf-saved hidden"), "1");
    const badge = el("span", "mb-pf-saved-k", ""); badge.dataset.pfT = "pf_saved_acct"; badge.textContent = t("pf_saved_acct");
    wd.cardText = el("strong", "mb-pf-saved-v", "");
    const chg = el("button", "mb-pf-link", ""); chg.type = "button"; chg.dataset.pfT = "pf_change"; chg.textContent = t("pf_change");
    const left = el("div", "mb-pf-saved-l"); left.append(badge, wd.cardText);
    wd.card.append(left, chg);
    methods.after(wd.card);
    chg.addEventListener("click", () => { wd.changing = true; renderAccount(); $("withdraw-address").focus(); });
    addr.after(nameLabel, name);
    tag(nameLabel, "1"); tag(name, "1");
    wd.nameLabel = nameLabel; wd.name = name;
    $("save-address-btn")?.classList.add("hidden");   // saved automatically on Continue
    // Step 2: review
    wd.rvHead = heading("pf_wd_h3", "2");
    wd.rv = tag(el("dl", "mb-pf-review"), "2");
    wd.rvNote = tag(el("p", "mb-pf-hint"), "2");
    const trust = tag(el("p", "mb-pf-trust"), "2");
    wd.trust = trust;
    const foot = c.querySelector(":scope > p.center-text");
    c.insertBefore(wd.rvHead, wd.foot);
    wd.rvHead.after(wd.rv); wd.rv.after(wd.rvNote); wd.rvNote.after(trust);
    tag(foot, "none");   // the trust line replaces this footnote
    // The original form step is emptied above (its children now live in steps); keep it out of the way.
    form.classList.add("pf-gone");

    $("withdraw-methods").addEventListener("click", () => { wd.changing = false; setTimeout(renderAccount, 0); });
    const live = () => setErr(wd, wd.step === 0 && $("withdraw-amount").value ? wdAmountError() : "");
    $("withdraw-amount").addEventListener("input", live);
    $("withdraw-max-btn")?.addEventListener("click", () => setTimeout(live, 0));
    $("withdraw-address").addEventListener("input", () => setErr(wd, ""));
    name.addEventListener("input", () => setErr(wd, ""));

    wd.validate = async (step) => {
      if (step === 0) { const m = wdAmountError(); return m ? { msg: m } : {}; }
      if (step === 1) {
        const key = state.selectedWithdrawMethod;
        if (!key) return { msg: t("pf_err_method") };
        const saved = findSavedAcct(state.withdrawAccounts, key);
        const typed = $("withdraw-address").value.trim();
        const addrNow = wd.changing || !saved ? typed : saved;
        if (!addrNow) return { msg: t("pf_err_addr") };
        if (!wd.name.value.trim()) return { msg: t("pf_err_name") };
        if (addrNow !== saved) {
          await saveWithdrawAddress();
          if (findSavedAcct(state.withdrawAccounts, key) !== addrNow) return { stay: true };
          wd.changing = false; renderAccount();
        }
      }
      return {};
    };
    wd.enter = (step) => {
      if (step === 1) renderAccount();
      if (step === 2) { renderReview(); wd.trust.textContent = "🔒 " + t("pf_trust"); }
    };
  }

  function startWithdraw() {
    buildWithdraw();
    if (!wd.built) return;
    wd.changing = false;
    // Pre-pick the method the player already has a saved account for.
    const list = (state.paymentMethods || []).filter((m) => m.supports_withdrawal);
    const idx = list.findIndex((m) => !!findSavedAcct(state.withdrawAccounts, m.key));
    if (idx >= 0) $("withdraw-methods").querySelectorAll(".method-card")[idx]?.click();
    $("withdraw-amount").value = "";
    go(wd, 0);
  }

  // ------------------------------------------------------------------ receipt timeline
  function decorateReceipt(info) {
    const card = document.querySelector("#receipt-modal .receipt-card");
    const status = $("rc-status");
    if (!card || !status) return;
    card.querySelector(".rc-tl")?.remove();
    const isDep = info && info.kind === "deposit";
    const keys = isDep ? ["pf_tl_sent", "pf_tl_check", "pf_tl_credit"] : ["pf_tl_req", "pf_tl_appr", "pf_tl_paid"];
    const ol = el("ol", "rc-tl");
    keys.forEach((k, i) => {
      const li = el("li", i === 0 ? "done" : (i === 1 ? "now" : ""));
      li.append(el("i", "", i === 0 ? "✓" : String(i + 1)), el("span", "", t(k)));
      ol.appendChild(li);
    });
    status.after(ol);
  }
  function hookReceipt() {
    if (!window.MB_A || !window.MB_A.receipt || window.MB_A.__pf) return;
    const orig = window.MB_A.receipt;
    window.MB_A.receipt = (info) => { orig(info); try { decorateReceipt(info); } catch (e) { /* cosmetic only */ } };
    window.MB_A.__pf = true;
  }

  // ------------------------------------------------------------------ boot
  function watch(modalId, flow, start) {
    const modal = $(modalId);
    if (!modal) return;
    let wasOpen = !modal.classList.contains("hidden");
    new MutationObserver(() => {
      const open = !modal.classList.contains("hidden");
      if (open && !wasOpen) start();
      wasOpen = open;
    }).observe(modal, { attributes: true, attributeFilter: ["class"] });
  }
  function boot() {
    hookReceipt();
    watch("deposit-modal", dep, startDeposit);
    watch("withdraw-modal", wd, startWithdraw);
    document.addEventListener("melbete:lang", () => {
      [dep, wd].forEach((f) => { if (f.built) { paintChrome(f); f.enter?.(f.step); } });
      if (dep.built) { paintReceiveLine(); paintProofChecks(); }
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else boot();
})();
