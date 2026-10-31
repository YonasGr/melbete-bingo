/* Melbete Bingo effects: glossy ball draw, winner celebration, number announcer.
   Purely presentational - it watches the DOM that app.js already renders and
   never touches game state, wallet or API calls. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const tg = window.Telegram && window.Telegram.WebApp;
  const reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  const store = {
    get(k, d) { try { return localStorage.getItem(k) || d; } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* storage may be blocked */ } },
  };
  const haptic = (kind) => {
    try {
      if (kind === "success") tg.HapticFeedback.notificationOccurred("success");
      else tg.HapticFeedback.impactOccurred(kind || "light");
    } catch (e) { /* not inside Telegram */ }
  };
  const LETTERS = "BINGO";
  const letterFor = (n) => LETTERS[Math.min(4, Math.floor((n - 1) / 15))];

  // ---- Amharic number words (1-75) ---------------------------------------
  const ONES = ["", "አንድ", "ሁለት", "ሦስት", "አራት", "አምስት", "ስድስት", "ሰባት", "ስምንት", "ዘጠኝ"];
  const TENS = { 2: "ሃያ", 3: "ሠላሳ", 4: "አርባ", 5: "አምሳ", 6: "ስድሳ", 7: "ሰባ" };
  const AM_LETTER = { B: "ቢ", I: "አይ", N: "ኤን", G: "ጂ", O: "ኦ" };
  function amNumber(n) {
    if (n < 10) return ONES[n];
    if (n === 10) return "አሥር";
    if (n < 20) return "አሥራ " + ONES[n - 10];
    const t = Math.floor(n / 10), o = n % 10;
    return TENS[t] + (o ? " " + ONES[o] : "");
  }

  // ---- Announcer (Off -> English -> Amharic) --------------------------------
  const MODES = ["en", "am", "off"];
  const LABEL = { en: "EN", am: "አማ", off: "Off" };
  let mode = MODES.includes(store.get("melbete_voice", "en")) ? store.get("melbete_voice", "en") : "en";
  const hasSpeech = "speechSynthesis" in window;
  const voices = () => (hasSpeech ? speechSynthesis.getVoices() : []);
  if (hasSpeech) speechSynthesis.addEventListener && speechSynthesis.addEventListener("voiceschanged", voices);

  function speak(n) {
    if (mode === "off" || !hasSpeech) return;
    const L = letterFor(n);
    const amVoice = voices().find((v) => v.lang.toLowerCase().startsWith("am"));
    const useAm = mode === "am" && amVoice;     // no Amharic voice installed -> fall back to English
    const u = new SpeechSynthesisUtterance(useAm ? `${AM_LETTER[L]}፣ ${amNumber(n)}` : `${L}, ${n}`);
    u.lang = useAm ? "am-ET" : "en-US";
    u.rate = 0.92;
    const v = useAm ? amVoice : voices().find((x) => x.lang.toLowerCase().startsWith("en"));
    if (v) u.voice = v;
    try { speechSynthesis.cancel(); speechSynthesis.speak(u); } catch (e) { /* ignore */ }
  }

  const btn = document.createElement("button");
  btn.type = "button";
  btn.id = "melbete-voice-btn";
  const paint = () => {
    btn.textContent = (mode === "off" ? "\u{1F507} " : "\u{1F50A} ") + LABEL[mode];
    btn.setAttribute("aria-label", `Number announcer: ${LABEL[mode]}`);
  };
  btn.addEventListener("click", () => {
    mode = MODES[(MODES.indexOf(mode) + 1) % MODES.length];
    store.set("melbete_voice", mode);
    paint();
    haptic("light");
    if (mode !== "off") speak(7);   // preview; also unlocks audio on iOS (needs a tap)
    else if (hasSpeech) speechSynthesis.cancel();
  });
  paint();
  document.body.appendChild(btn);
  // Only show the control while the draw screen is on screen.
  setInterval(() => { const c = $("current-call"); btn.hidden = !(c && c.offsetParent); }, 800);

  // ---- Glossy ball draw ---------------------------------------------------------
  const numEl = $("current-call-number"), ball = $("current-call");
  if (numEl && ball) {
    const colorize = () => {
      const n = parseInt(numEl.textContent, 10);
      if (n >= 1 && n <= 75) ball.dataset.col = String(Math.floor((n - 1) / 15));
    };
    colorize();
    let last = numEl.textContent.trim();
    new MutationObserver(() => {
      const t = numEl.textContent.trim();
      if (t === last) return;
      last = t;
      const n = parseInt(t, 10);
      if (!(n >= 1 && n <= 75)) return;
      colorize();
      if (!reduce) { ball.classList.remove("melbete-roll"); void ball.offsetWidth; ball.classList.add("melbete-roll"); }
      haptic("light");
      speak(n);
    }).observe(numEl, { childList: true, characterData: true, subtree: true });
  }

  // ---- Winner celebration ----------------------------------------------------------
  const CROWN = '<svg class="melbete-crown" viewBox="0 0 96 72" aria-hidden="true"><defs><linearGradient id="melbeteG" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#FFE08A"/><stop offset="1" stop-color="#E0A21B"/></linearGradient></defs><path d="M8 58 14 20 33 38 48 10 63 38 82 20 88 58Z" fill="url(#melbeteG)" stroke="#B87A0E" stroke-width="2" stroke-linejoin="round"/><rect x="8" y="58" width="80" height="10" rx="3" fill="url(#melbeteG)" stroke="#B87A0E" stroke-width="2"/><circle cx="14" cy="18" r="5" fill="#FFE08A"/><circle cx="48" cy="8" r="5.5" fill="#FFE08A"/><circle cx="82" cy="18" r="5" fill="#FFE08A"/><path d="M48 30 55 42 48 54 41 42Z" fill="#477BFF" stroke="#1d3fa8"/></svg>';

  function fanfare() {
    if (mode === "off") return;
    try {
      const C = new (window.AudioContext || window.webkitAudioContext)();
      [523, 659, 784, 1047].forEach((f, i) => {
        const o = C.createOscillator(), g = C.createGain(), t = C.currentTime + i * 0.12;
        o.type = "triangle"; o.frequency.value = f;
        g.gain.setValueAtTime(0.0001, t);
        g.gain.exponentialRampToValueAtTime(0.18, t + 0.02);
        g.gain.exponentialRampToValueAtTime(0.0001, t + 0.35);
        o.connect(g); g.connect(C.destination); o.start(t); o.stop(t + 0.4);
      });
      setTimeout(() => C.close(), 1600);
    } catch (e) { /* audio unavailable */ }
  }

  function countUp(el, raw) {
    const target = parseFloat(raw.replace(/,/g, ""));
    if (!isFinite(target) || reduce) return;
    const text = el.textContent, dec = raw.includes(".") ? 2 : 0, t0 = performance.now(), dur = 1100;
    const step = (now) => {
      const p = Math.min(1, (now - t0) / dur), v = target * (1 - Math.pow(1 - p, 3));
      el.textContent = text.replace(raw, v.toLocaleString("en-US", { minimumFractionDigits: dec, maximumFractionDigits: dec }));
      if (p < 1) requestAnimationFrame(step); else el.textContent = text;
    };
    requestAnimationFrame(step);
  }

  function celebrate(ov, payoutEl, raw) {
    ov.classList.add("melbete-win");
    const box = ov.querySelector(".winner-overlay-content");
    if (box) box.insertAdjacentHTML("afterbegin", CROWN);
    if (!reduce) {
      const colors = ["#FFC933", "#FFE08A", "#FFB800", "#E08A00", "#F8FAFC"];
      const pieces = (navigator.hardwareConcurrency || 8) <= 4 ? 16 : 40;   // lighter on low-end phones
      for (let i = 0; i < pieces; i++) {
        const s = document.createElement("span");
        s.className = "melbete-confetti";
        s.style.cssText = `left:${Math.random() * 100}%;background:${colors[i % colors.length]};` +
          `animation-duration:${2.2 + Math.random() * 2}s;animation-delay:${Math.random() * 0.8}s`;
        ov.appendChild(s);
      }
    }
    if (!reduce) {                                   // gold coins tumble in after the confetti
      const coins = (navigator.hardwareConcurrency || 8) <= 4 ? 8 : 16;
      for (let i = 0; i < coins; i++) {
        const c = document.createElement("span");
        c.className = "melbete-coin";
        c.style.cssText = `left:${6 + Math.random() * 88}%;width:${16 + Math.random() * 10}px;height:${16 + Math.random() * 10}px;` +
          `animation-duration:${2.4 + Math.random() * 1.8}s;animation-delay:${0.25 + Math.random() * 1.1}s`;
        ov.appendChild(c);
      }
    }
    countUp(payoutEl, raw);
    haptic("success");
    fanfare();
  }

  const ov = $("winner-overlay");
  if (ov) {
    new MutationObserver(() => {
      if (ov.classList.contains("hidden")) {
        ov.classList.remove("melbete-win");
        ov.querySelectorAll(".melbete-crown,.melbete-confetti,.melbete-coin").forEach((e) => e.remove());
        return;
      }
      if (ov.classList.contains("melbete-win")) return;
      // The payout line is filled just after the overlay opens; retry briefly.
      [120, 500, 1100].forEach((ms) => setTimeout(() => {
        if (ov.classList.contains("hidden") || ov.classList.contains("melbete-win")) return;
        const p = $("winner-payout");
        const m = p && /You won\s+([\d.,]+)/i.exec(p.textContent || "");
        if (m) celebrate(ov, p, m[1]);   // only the winner sees "You won ..."
      }, ms));
    }).observe(ov, { attributes: true, attributeFilter: ["class"] });
  }

  // ---- Cartela polish: pulse the latest number, glow the completed line ----------
  // Presentational only: reads the cards' DOM and toggles classes. It never
  // marks a cell, claims BINGO or touches state - app.js still owns all of that.
  const LINES = (() => {
    const L = [];
    for (let r = 0; r < 5; r++) L.push([0, 1, 2, 3, 4].map((c) => r * 5 + c));   // rows
    for (let c = 0; c < 5; c++) L.push([0, 1, 2, 3, 4].map((r) => r * 5 + c));   // columns
    L.push([0, 6, 12, 18, 24], [4, 8, 12, 16, 20], [0, 4, 20, 24]);              // diagonals, 4 corners
    return L;
  })();
  const cardRoots = () => document.querySelectorAll("#my-cards-container, #winner-card");
  let cardsMo = null;

  function paintCards() {
    const latest = numEl ? parseInt(numEl.textContent, 10) : NaN;
    document.querySelectorAll("#my-cards-container .card-grid, #winner-card .card-grid").forEach((grid) => {
      const cells = grid.children;
      if (cells.length !== 25) return;
      const on = (i) => cells[i].classList.contains("marked") || cells[i].classList.contains("free");
      const win = new Set();
      LINES.forEach((line) => { if (line.every(on)) line.forEach((i) => win.add(i)); });
      const order = [...win].sort((a, b) => a - b);
      const live = !!grid.closest("#my-cards-container");
      for (let i = 0; i < 25; i++) {
        const el = cells[i];
        el.classList.toggle("melbete-line", win.has(i));
        if (win.has(i)) el.style.setProperty("--i", String(order.indexOf(i)));
        el.classList.toggle("melbete-latest", live && !el.classList.contains("free") && latest >= 1 && el.textContent.trim() === String(latest));
      }
    });
    if (cardsMo) cardsMo.takeRecords();   // drop the class changes we just made ourselves
  }
  let paintQueued = false;
  const queuePaint = () => {
    if (paintQueued) return;
    paintQueued = true;
    requestAnimationFrame(() => { paintQueued = false; paintCards(); });
  };
  cardsMo = new MutationObserver(queuePaint);
  cardRoots().forEach((root) => cardsMo.observe(root, { childList: true, subtree: true, attributes: true, attributeFilter: ["class"] }));
  if (numEl) new MutationObserver(queuePaint).observe(numEl, { childList: true, characterData: true, subtree: true });
  queuePaint();
})();
