/* Melbete Bingo - one back-button behaviour for every player page.
   Back first closes whatever is open on top (preview sheet, notifications, modal, profile drawer).
   If nothing is open: sub-pages go back (room -> hub, agent/download -> previous page or hub); the hub is
   the root, so there is nothing to go back to there.
   Telegram's native BackButton and the in-page arrow both use this, so they always agree. */
(function () {
  "use strict";
  const tg = window.Telegram && window.Telegram.WebApp;
  const path = (location.pathname.replace(/\/+$/, "") || "/");
  const isRoot = path === "/" || path === "/app";
  const $ = (id) => document.getElementById(id);
  const open = (el) => el && !el.classList.contains("hidden");

  /** The topmost thing that Back should close, or null. */
  function topOverlay() {
    const sheet = $("cartela-preview-sheet");
    if (open(sheet)) return { close: () => $("cartela-sheet-cancel")?.click() };
    const inbox = $("inbox-sheet");
    if (open(inbox)) return { close: () => inbox.querySelector("[data-close]")?.click() };
    const modals = [...document.querySelectorAll(".modal:not(.hidden), .modal-overlay:not(.hidden)")];
    if (modals.length) {
      const m = modals[modals.length - 1];
      return { close: () => { const x = m.querySelector(".modal-close, [data-close]"); if (x) x.click(); else m.classList.add("hidden"); } };
    }
    const drawer = $("profile-drawer");
    if (drawer && drawer.classList.contains("open")) return { close: () => $("profile-close-btn")?.click() };
    return null;
  }

  function goBack() {
    const o = topOverlay();
    if (o) { o.close(); return; }
    if (isRoot) return;
    if (path === "/bingo" && $("game-select-btn")) { $("game-select-btn").click(); return; }
    if (document.referrer && history.length > 1) history.back();
    else window.location.href = "/app";
  }

  // ---- Telegram native back button -------------------------------------------------------
  let raf = 0;
  function sync() {
    raf = 0;
    if (!tg || !tg.BackButton) return;
    const show = !isRoot || !!topOverlay();
    try { show ? tg.BackButton.show() : tg.BackButton.hide(); } catch (e) { /* old client */ }
  }
  function schedule() { if (!raf) raf = requestAnimationFrame(sync); }
  if (tg && tg.BackButton) {
    try { tg.BackButton.onClick(goBack); } catch (e) { /* old client */ }
    new MutationObserver(schedule).observe(document.body, { subtree: true, attributes: true, attributeFilter: ["class"], childList: true });
    sync();
  }

  // ---- in-page arrow for pages that have no header of their own (agent, download) -------
  if (path === "/agent" || path === "/download") {
    const b = document.createElement("button");
    b.type = "button";
    b.setAttribute("aria-label", "Back");
    b.innerHTML = '<svg viewBox="0 0 24 24" width="22" height="22" fill="none" aria-hidden="true"><path d="M19 12H5M5 12l7 7M5 12l7-7" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>';
    b.style.cssText = "display:grid;place-items:center;width:42px;height:42px;margin:0 0 10px;padding:0;border:1px solid rgba(255,201,51,.35);border-radius:50%;background:rgba(255,201,51,.12);color:#FFC933;cursor:pointer;box-shadow:none;";
    b.addEventListener("click", goBack);
    const host = document.querySelector("main") || document.body;
    host.insertBefore(b, host.firstChild);
  }

  window.MB_BACK = { goBack };
})();
