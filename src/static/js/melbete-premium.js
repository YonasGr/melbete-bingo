/* Melbete Bingo premium feel: balance count-up (home) and the one-tap copy
   button used for payment account numbers (home + bingo room).
   Presentational only - it never changes a balance, it only animates how a
   number that app.js / gameselect.js already wrote is displayed. */
(() => {
  "use strict";
  const reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  const haptic = () => { try { window.Telegram.WebApp.HapticFeedback.notificationOccurred("success"); } catch (e) { /* not in Telegram */ } };

  // ---- One-tap copy button for account numbers ------------------------------
  async function copyText(value) {
    try {
      await navigator.clipboard.writeText(value);
      return true;
    } catch (e) {
      const tmp = document.createElement("textarea");
      tmp.value = value;
      tmp.style.cssText = "position:fixed;opacity:0;top:0;left:0";
      document.body.appendChild(tmp);
      tmp.select();
      let ok = false;
      try { ok = document.execCommand("copy"); } catch (e2) { /* give up */ }
      document.body.removeChild(tmp);
      return ok;
    }
  }

  /** Returns <span class="account-number">…</span><button class="copy-btn">…</button> as a fragment. */
  window.melbeteAccountCopy = function (value, label, doneLabel) {
    const frag = document.createDocumentFragment();
    const num = document.createElement("span");
    num.className = "account-number";
    num.textContent = value;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "copy-btn";
    btn.setAttribute("aria-label", `${label || "Copy"} ${value}`);
    btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2.5" stroke="currentColor" stroke-width="2"/><path d="M5 15V6.5A2.5 2.5 0 017.5 4H15" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg><span class="copy-label"></span>';
    const lab = btn.querySelector(".copy-label");
    lab.textContent = label || "Copy";
    btn.addEventListener("click", async () => {
      const ok = await copyText(String(value));
      if (!ok) { if (typeof toast === "function") toast("Could not copy", "error"); return; }
      btn.classList.add("copied");
      lab.textContent = doneLabel || "Copied";
      haptic();
      clearTimeout(btn._t);
      btn._t = setTimeout(() => { btn.classList.remove("copied"); lab.textContent = label || "Copy"; }, 2000);
    });
    frag.append(num, btn);
    return frag;
  };

  // ---- Balance count-up (home) ------------------------------------------------
  const bal = document.getElementById("balance-real");
  if (bal) {
    let shown = parseFloat(String(bal.textContent).replace(/,/g, "")) || 0;
    let busy = false, raf = 0;
    const fmt = (v, dec) => v.toLocaleString("en-US", { minimumFractionDigits: dec, maximumFractionDigits: dec });
    const mo = new MutationObserver(() => {
      if (busy) return;
      const raw = String(bal.textContent).trim();
      const target = parseFloat(raw.replace(/,/g, ""));
      if (!isFinite(target) || target === shown) return;
      const from = shown, dec = raw.includes(".") ? Math.min(2, raw.split(".")[1].length) : 0;
      shown = target;
      const card = bal.closest(".wallet-summary");
      if (card) {
        card.classList.remove("melbete-bal-up", "melbete-bal-down");
        void card.offsetWidth;                       // restart the flash
        card.classList.add(target > from ? "melbete-bal-up" : "melbete-bal-down");
        if (target > from && !reduce) {                // a few coins pop out of the balance card
          for (let i = 0; i < 7; i++) {
            const c = document.createElement("span");
            c.className = "melbete-coin-pop";
            c.style.cssText = `left:${15 + Math.random() * 40}%;--dx:${(Math.random() - 0.3) * 120}px;--dy:${-(40 + Math.random() * 50)}px;animation-delay:${i * 60}ms`;
            card.appendChild(c);
            setTimeout(() => c.remove(), 1400 + i * 60);
          }
        }
      }
      if (reduce) return;
      cancelAnimationFrame(raf);
      const t0 = performance.now(), dur = Math.min(1400, 600 + Math.abs(target - from) * 2);
      const step = (now) => {
        const p = Math.min(1, (now - t0) / dur), v = from + (target - from) * (1 - Math.pow(1 - p, 3));
        busy = true;
        bal.textContent = p < 1 ? fmt(v, dec) : raw;   // land on the exact text the app wrote
        mo.takeRecords();
        busy = false;
        if (p < 1) raf = requestAnimationFrame(step);
      };
      raf = requestAnimationFrame(step);
    });
    mo.observe(bal, { childList: true, characterData: true, subtree: true });
  }
})();
