/* Melbete Bingo - player-side Control Centre client.
   Polls /api/control/public (no login needed) and applies what the admin published:
   maintenance screen, announcement bar, promo banner, text overrides and switched-off features.
   Everything is inserted with textContent - admin-written text is never treated as HTML. */
(() => {
  "use strict";
  const POLL_MS = 30000;
  let lastKey = "", busy = false;
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };

  async function refresh() {
    if (busy) return; busy = true;
    try {
      const res = await fetchRaw("/api/control/public", { cache: "no-store" });
      if (!res.ok) return;
      const cfg = await res.json();
      const key = JSON.stringify(cfg);
      if (key !== lastKey) { lastKey = key; apply(cfg); }
    } catch (_) { /* offline or restarting: keep what is on screen */ } finally { busy = false; }
  }

  /* Admin-written messages (per language) and running promotions, shared with the other player scripts */
  let cxTexts = {}, cxPromos = [];
  window.cxText = (key) => cxTexts[`${key}_${(window.MB_I18N && window.MB_I18N.lang) || "en"}`] || "";
  window.cxBoost = () => cxPromos.reduce((m, p) => (p.applies === "cashback" ? Math.max(m, Number(p.multiplier) || 1) : m), 1);

  function apply(cfg) {
    cxTexts = cfg.texts || {}; cxPromos = cfg.promotions || [];
    if (window.paintCashback) window.paintCashback();
    document.dispatchEvent(new Event("melbete:texts"));
    maintenance(cfg.maintenance);
    announcement(cfg.announcement);
    banner(cfg.banner, cfg.texts && cfg.texts.home_note);
    texts(cfg.texts || {});
    flags(cfg);
  }

  /* ---- maintenance: full-screen, lifts itself when the admin turns it off ---- */
  function maintenance(m) {
    let ov = $("cx-maint");
    if (!m || !m.enabled) { if (ov) ov.remove(); document.documentElement.classList.remove("cx-locked"); return; }
    if (!ov) {
      ov = el("div", "cx-maint"); ov.id = "cx-maint"; ov.setAttribute("role", "alertdialog"); ov.setAttribute("aria-live", "assertive");
      const card = el("div", "cx-maint-card");
      const logo = el("img", "cx-maint-logo"); logo.src = "/static/img/melbete-bingo-logo.png"; logo.alt = "Melbete Bingo";
      card.append(logo, el("h2", "", "Maintenance"), el("p", "cx-maint-msg"), el("p", "cx-maint-eta"));
      ov.append(card); document.body.append(ov);
    }
    ov.querySelector(".cx-maint-msg").textContent = m.message || "";
    const eta = ov.querySelector(".cx-maint-eta"); eta.textContent = m.eta ? `Expected back: ${m.eta}` : ""; eta.hidden = !m.eta;
    document.documentElement.classList.add("cx-locked");
  }

  /* ---- announcement bar ---- */
  function announcement(a) {
    let bar = $("cx-announce");
    if (!a) { if (bar) bar.remove(); return; }
    if (!bar) { bar = el("div", "cx-announce"); bar.id = "cx-announce"; bar.setAttribute("role", "status"); document.body.prepend(bar); }
    bar.className = `cx-announce cx-${a.level || "info"}`; bar.replaceChildren();
    const text = el("div", "cx-announce-text");
    if (a.title) text.append(el("strong", "", a.title));
    text.append(el("span", "", a.body));
    bar.append(text);
    if (a.level !== "critical") {
      const x = el("button", "cx-x", "×"); x.type = "button"; x.setAttribute("aria-label", "Dismiss");
      x.onclick = () => bar.remove(); bar.append(x);
    }
  }

  /* ---- promo banner + home note, above the game list ---- */
  function banner(b, note) {
    let box = $("cx-banner");
    const host = $("game-select-view");
    if ((!b && !note) || !host) { if (box) box.remove(); return; }
    if (!box) { box = el("div", "cx-banner-wrap"); box.id = "cx-banner"; host.prepend(box); }
    box.replaceChildren();
    if (b) {
      const card = el("div", "cx-banner");
      const body = el("div", "cx-banner-body");
      if (b.tag) body.append(el("span", "cx-banner-tag", b.tag));
      body.append(el("strong", "", b.title));
      if (b.detail) body.append(el("small", "", b.detail));
      card.append(body);
      if (b.cta_label && b.cta_target !== "none") {
        const btn = el("button", "cx-banner-cta", b.cta_label); btn.type = "button";
        btn.onclick = () => $({ deposit: "hub-tab-deposit", withdraw: "hub-tab-withdraw", promos: "hub-tab-promos", profile: "header-profile-btn" }[b.cta_target] || "")?.click();
        card.append(btn);
      }
      box.append(card);
    }
    if (note) box.append(el("p", "cx-home-note", note));
  }

  /* ---- text overrides (blank = built-in wording stays) ---- */
  const TEXT_TARGETS = { deposit_intro: "#deposit-modal .modal-content > p.muted", withdraw_intro: "#withdraw-modal .modal-content > p.muted" };
  const originals = new Map();
  function texts(t) {
    for (const [key, sel] of Object.entries(TEXT_TARGETS)) {
      const node = document.querySelector(sel); if (!node) continue;
      if (!originals.has(key)) originals.set(key, node.textContent);
      node.textContent = t[key] || originals.get(key);
    }
  }

  /* ---- switched-off features: dim + explain, the server enforces it regardless ---- */
  const FLAG_BUTTONS = { deposits: ["hub-tab-deposit"], withdrawals: ["hub-tab-withdraw"] };
  function flags(cfg) {
    for (const [flag, ids] of Object.entries(FLAG_BUTTONS)) {
      for (const id of ids) {
        const b = $(id); if (!b) continue;
        const off = cfg.flags && cfg.flags[flag] === false;
        b.classList.toggle("cx-off", off);
        b.dataset.cxMsg = off ? (cfg.flag_messages && cfg.flag_messages[flag]) || "Temporarily unavailable." : "";
      }
    }
  }
  document.addEventListener("click", (e) => {
    const b = e.target.closest && e.target.closest(".cx-off");
    if (!b) return;
    e.stopImmediatePropagation(); e.preventDefault();
    const t = $("toast"); if (t) { t.textContent = b.dataset.cxMsg; t.classList.remove("hidden"); setTimeout(() => t.classList.add("hidden"), 3500); }
  }, true);

  /* ---- react at once when the server answers 503 + Retry-After (maintenance began) ---- */
  const fetchRaw = window.fetch.bind(window);
  window.fetch = async (...args) => {
    const res = await fetchRaw(...args);
    if (res.status === 503 && res.headers.get("Retry-After")) refresh();
    return res;
  };

  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  setInterval(refresh, POLL_MS);
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", refresh); else refresh();
})();
