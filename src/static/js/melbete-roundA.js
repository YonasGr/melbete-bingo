// Melbete Bingo - Round A: language toggle, first-run onboarding, deposit/withdraw
// step progress bars and request receipts. Display only: it reads the same form state the
// existing deposit/withdraw code already keeps and never touches wallet logic or API calls.
// Loaded after gameselect.js (needs its global `state`) and melbete-i18n.js (needs mbT).
(() => {
  "use strict";
  if (typeof mbT !== "function") return;
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* storage blocked */ } },
  };

  // ------------------------------------------------------------------ language toggle
  function paintLangButton() {
    const seg = $("lang-seg");
    if (!seg) return;
    seg.setAttribute("aria-label", mbT("lang_switch"));
    seg.querySelectorAll(".lang-seg-opt").forEach((o) => {
      const on = o.dataset.lang === MB_I18N.lang;
      o.classList.toggle("active", on);
      o.setAttribute("aria-checked", on ? "true" : "false");
      o.tabIndex = on ? 0 : -1;
    });
  }
  function wireLangButton() {
    const seg = $("lang-seg");
    if (!seg) return;
    seg.addEventListener("click", (e) => {
      const o = e.target.closest(".lang-seg-opt");
      if (!o || o.dataset.lang === MB_I18N.lang) return;
      MB_I18N.setLang(o.dataset.lang);
    });
    // Arrow keys move between the two options, as a radio group should.
    seg.addEventListener("keydown", (e) => {
      if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(e.key)) return;
      e.preventDefault();
      const next = MB_I18N.lang === "am" ? "en" : "am";
      MB_I18N.setLang(next);
      seg.querySelector(`.lang-seg-opt[data-lang="${next}"]`)?.focus();
    });
    paintLangButton();
  }

  // ------------------------------------------------------------------ step progress bars
  // steps: [labelKey, isDone()] - the first not-done step is "now".
  const BARS = {
    "deposit-modal": [
      ["st_method", () => !!state.selectedDepositMethod],
      ["st_amount", () => Number($("deposit-amount")?.value) > 0],
      ["st_proof", () => !!($("deposit-sms-text")?.value || "").trim() && !!state.depositScreenshotDataUrl],
    ],
    "withdraw-modal": [
      ["st_method", () => !!state.selectedWithdrawMethod],
      ["st_account", () => !!(state.selectedWithdrawMethod && state.withdrawAccounts && state.withdrawAccounts[state.selectedWithdrawMethod])],
      ["st_details", () => Number($("withdraw-amount")?.value) > 0 && !!($("withdraw-legal-name")?.value || "").trim()],
    ],
  };

  function buildBar(modalId) {
    const modal = $(modalId);
    if (!modal || modal.querySelector(".mb-steps")) return;
    const bar = el("ol", "mb-steps");
    bar.setAttribute("aria-label", "Progress");
    BARS[modalId].forEach(([key], i) => {
      const li = el("li", "mb-step");
      li.append(el("span", "mb-step-dot", String(i + 1)), el("span", "mb-step-label", mbT(key)));
      li.dataset.key = key;
      bar.appendChild(li);
    });
    const fill = el("div", "mb-steps-fill");
    const wrap = el("div", "mb-steps-wrap");
    wrap.append(bar, fill);
    modal.querySelector(".modal-content")?.insertBefore(wrap, modal.querySelector(".modal-content > h3")?.nextSibling || null);
  }

  function paintBar(modalId) {
    const modal = $(modalId);
    if (!modal || modal.classList.contains("hidden")) return;
    const steps = BARS[modalId];
    const items = modal.querySelectorAll(".mb-step");
    if (items.length !== steps.length) return;
    let now = -1, done = 0;
    steps.forEach(([key, isDone], i) => {
      let d = false;
      try { d = !!isDone(); } catch (e) { d = false; }
      if (d) done++;
      if (!d && now < 0) now = i;
      const li = items[i];
      li.classList.toggle("done", d);
      li.classList.toggle("now", !d && i === now);
      li.querySelector(".mb-step-dot").textContent = d ? "\u2713" : String(i + 1);
      li.querySelector(".mb-step-label").textContent = mbT(key);
      if (!d && i === now) li.setAttribute("aria-current", "step"); else li.removeAttribute("aria-current");
    });
    modal.querySelector(".mb-steps-wrap").style.setProperty("--mb-pct-n", String(Math.min(done, steps.length - 1) / (steps.length - 1)));
  }

  function startBars() {
    Object.keys(BARS).forEach(buildBar);
    // Form state changes from several places (taps, paste, async file read), so a light poll
    // while a modal is open is simpler and more reliable than hooking each one.
    setInterval(() => Object.keys(BARS).forEach(paintBar), 300);
  }

  // ------------------------------------------------------------------ receipt
  function methodName(key) {
    const m = (typeof state !== "undefined" && state.paymentMethods || []).find((x) => x.key === key);
    return (m && m.name) || key || "-";
  }
  function shortRef(id) { return String(id || "").replace(/-/g, "").slice(-8).toUpperCase() || "-"; }

  function buildReceiptModal() {
    if ($("receipt-modal")) return;
    const m = el("div", "modal hidden"); m.id = "receipt-modal"; m.setAttribute("role", "dialog"); m.setAttribute("aria-labelledby", "receipt-title");
    m.innerHTML =
      '<div class="modal-content receipt-card">' +
      '<div class="rc-check" aria-hidden="true"><svg viewBox="0 0 52 52"><circle cx="26" cy="26" r="23"/><path d="M15 27l8 8 14-16"/></svg></div>' +
      '<h3 id="receipt-title"></h3>' +
      '<div class="rc-amount"><strong id="rc-amount-val">0</strong> <small data-i18n="chip_etb">ETB</small></div>' +
      '<span class="rc-status" id="rc-status"></span>' +
      '<dl class="rc-rows">' +
      '<div><dt data-i18n="rc_ref"></dt><dd><button type="button" id="rc-ref" class="rc-ref" aria-label=""></button></dd></div>' +
      '<div><dt data-i18n="rc_method"></dt><dd id="rc-method"></dd></div>' +
      '<div><dt data-i18n="rc_time"></dt><dd id="rc-time"></dd></div>' +
      '</dl>' +
      '<p class="muted small rc-note" id="rc-note"></p>' +
      '<button type="button" class="primary-btn" id="rc-done" data-i18n="rc_done"></button>' +
      '</div>';
    $("app-shell").appendChild(m);
    MB_I18N.apply(m);
    const close = () => m.classList.add("hidden");
    $("rc-done").addEventListener("click", close);
    m.addEventListener("click", (e) => { if (e.target === m) close(); });
    $("rc-ref").addEventListener("click", async () => {
      const v = $("rc-ref").dataset.ref || "";
      try { await navigator.clipboard.writeText(v); } catch (e) {
        const t = document.createElement("textarea"); t.value = v; t.style.cssText = "position:fixed;opacity:0";
        document.body.appendChild(t); t.select(); try { document.execCommand("copy"); } catch (e2) { /* give up */ } t.remove();
      }
      if (typeof toast === "function") toast(mbT("rc_copied"), "success");
    });
  }

  // info: {kind: "deposit"|"withdraw", id, amount, method}
  function showReceipt(info) {
    buildReceiptModal();
    const dep = info.kind === "deposit";
    $("receipt-title").textContent = mbT(dep ? "rc_title_dep" : "rc_title_wd");
    $("rc-amount-val").textContent = Math.abs(Number(info.amount) || 0).toLocaleString("en-US");
    $("rc-status").textContent = mbT("rc_pending");
    const ref = shortRef(info.id);
    const refBtn = $("rc-ref");
    refBtn.textContent = `#${ref}  \u29c9`; refBtn.dataset.ref = ref; refBtn.setAttribute("aria-label", mbT("rc_copy"));
    $("rc-method").textContent = methodName(info.method);
    $("rc-time").textContent = new Date().toLocaleString(MB_I18N.lang === "am" ? "am-ET" : "en-GB", { dateStyle: "medium", timeStyle: "short" });
    $("rc-note").textContent = mbT(dep ? "rc_note_dep" : "rc_note_wd");
    const m = $("receipt-modal");
    m.classList.remove("hidden");
    const card = m.querySelector(".receipt-card");
    card.classList.remove("rc-in"); void card.offsetWidth; card.classList.add("rc-in");
    if (navigator.vibrate) try { navigator.vibrate(18); } catch (e) { /* not supported */ }
  }

  // ------------------------------------------------------------------ onboarding
  const OB_KEY = "melbete_onboarded";
  function buildOnboarding() {
    const wrap = el("div", "mb-onboard"); wrap.id = "mb-onboard"; wrap.setAttribute("role", "dialog"); wrap.setAttribute("aria-modal", "true");
    wrap.innerHTML =
      '<div class="ob-stars" aria-hidden="true"></div>' +
      '<button type="button" class="ob-skip" id="ob-skip" data-i18n="ob_skip"></button>' +
      '<div class="ob-slides">' +
        '<section class="ob-slide" data-i="0">' +
          '<img class="ob-logo" src="/static/img/melbete-bingo-logo.png" alt="">' +
          '<h2 data-i18n="ob1_title"></h2><p data-i18n="ob1_text"></p>' +
          '<div class="ob-langs"><button type="button" data-lang="am" class="ob-lang">አማርኛ</button><button type="button" data-lang="en" class="ob-lang">English</button></div>' +
        '</section>' +
        '<section class="ob-slide" data-i="1">' +
          '<h2 data-i18n="ob2_title"></h2>' +
          '<ol class="ob-list">' +
            '<li><b>1</b><span data-i18n="ob2_a"></span></li><li><b>2</b><span data-i18n="ob2_b"></span></li><li><b>3</b><span data-i18n="ob2_c"></span></li>' +
          '</ol>' +
        '</section>' +
        '<section class="ob-slide" data-i="2">' +
          '<h2 data-i18n="ob3_title"></h2>' +
          '<ul class="ob-list ob-money">' +
            '<li><b>＋</b><span><strong data-i18n="ob3_a"></strong><em data-i18n="ob3_a2"></em></span></li>' +
            '<li><b>★</b><span><strong data-i18n="ob3_b"></strong><em data-i18n="ob3_b2"></em></span></li>' +
            '<li><b>◔</b><span><strong data-i18n="ob3_c"></strong><em data-i18n="ob3_c2"></em></span></li>' +
          '</ul>' +
        '</section>' +
      '</div>' +
      '<div class="ob-foot"><div class="ob-dots" aria-hidden="true"><i></i><i></i><i></i></div><button type="button" class="primary-btn" id="ob-next"></button></div>';
    return wrap;
  }

  function showOnboarding() {
    if (store.get(OB_KEY) === "1" || $("mb-onboard")) return;
    const wrap = buildOnboarding();
    $("app-shell").appendChild(wrap);
    MB_I18N.apply(wrap);
    let i = 0;
    const slides = wrap.querySelectorAll(".ob-slide"), dots = wrap.querySelectorAll(".ob-dots i"), next = $("ob-next");
    const paint = () => {
      slides.forEach((s, n) => { s.classList.toggle("on", n === i); s.classList.toggle("past", n < i); });
      dots.forEach((d, n) => d.classList.toggle("on", n === i));
      next.textContent = mbT(i === slides.length - 1 ? "ob_start" : "ob_next");
      $("ob-skip").classList.toggle("hidden", i === slides.length - 1);
      wrap.querySelectorAll(".ob-lang").forEach((b) => b.classList.toggle("sel", b.dataset.lang === MB_I18N.lang));
    };
    const finish = () => { store.set(OB_KEY, "1"); wrap.classList.add("out"); setTimeout(() => wrap.remove(), 320); };
    wrap.querySelectorAll(".ob-lang").forEach((b) => b.addEventListener("click", () => { MB_I18N.setLang(b.dataset.lang); paint(); }));
    next.addEventListener("click", () => { if (i < slides.length - 1) { i++; paint(); } else finish(); });
    $("ob-skip").addEventListener("click", finish);
    document.addEventListener("melbete:lang", paint);
    paint();
    next.focus({ preventScroll: true });
  }

  // First-run: wait for the hub to be on screen, then show the intro once.
  function whenHubReady(fn) {
    let n = 0;
    const t = setInterval(() => {
      const hub = $("game-select-view");
      if (hub && hub.classList.contains("active")) { clearInterval(t); setTimeout(fn, 350); }
      else if (++n > 200) clearInterval(t);
    }, 200);
  }

  // ------------------------------------------------------------------ boot
  function boot() {
    wireLangButton();
    startBars();
    buildReceiptModal();
    document.addEventListener("melbete:lang", () => { paintLangButton(); Object.keys(BARS).forEach(paintBar); });
    whenHubReady(showOnboarding);
  }
  window.MB_A = { receipt: showReceipt };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else boot();
})();
