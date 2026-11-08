/* Melbete flow layer: reserve -> countdown -> start -> play -> claim -> celebrate -> next round.
   Presentational only: reads `state` and the DOM, never marks cells, claims BINGO or changes game rules. */
(() => {
  "use strict";
  if (typeof state === "undefined") return;
  const $ = (id) => document.getElementById(id);
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const ls = { g(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }, s(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* ignore */ } } };
  /* Admin-tunable experience (Control Center > Experience), published through /api/control/public.
     Defaults match the shipped behaviour, so nothing changes until an admin edits a value. */
  const D = {
    tiers: { big_win_etb: 500, jackpot_etb: 2000, jackpot_burst: true, coin_fly: true },
    countdown: { warn_seconds: 10, hot_seconds: 5, lobby_feed: true, show_pool: true },
    splash: { enabled: true, seconds: 3 }, voice: { mode: "player" }, alerts: { near_win: true, haptics: true },
    verify: { min_ms: 1100 }, random_picker: { choices: 3 }, rematch: { keep_cartelas: true },
  };
  const X = (g, k) => { const v = (window.cxExperience || {})[g]; return v && k in v ? v[k] : D[g][k]; };
  const buzz = (t) => { if (!X("alerts", "haptics")) return; try { Telegram.WebApp.HapticFeedback.notificationOccurred(t); } catch (e) { /* ignore */ } };
  const mk = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; };
  const watch = (n, fn, o) => { if (n) new MutationObserver(fn).observe(n, o || { childList: true, characterData: true, subtree: true }); };
  if ((navigator.hardwareConcurrency || 8) <= 4 || (navigator.deviceMemory || 4) <= 2) document.documentElement.classList.add("flow-lite");
  const room = () => state.room || {};
  const LOGO = "/static/img/melbete-bingo-logo.png";
  $("cartela-grid")?.addEventListener("click", (e) => {      // premium tap burst on a cartela
    const c = e.target.closest(".cartela-cell");
    if (!c || c.disabled || c.classList.contains("taken")) return;
    c.classList.remove("flow-tap"); void c.offsetWidth; c.classList.add("flow-tap");
    setTimeout(() => c.classList.remove("flow-tap"), 650);
    try { Telegram.WebApp.HapticFeedback.impactOccurred("light"); } catch (err) { /* ignore */ }
  });
  const mine = () => state.myCards || [];

  /* 1B quick-pick strip: your cartelas pinned above the grid, tap x to remove */
  const tools = document.querySelector("#lobby-view .cartela-picker-tools");
  const strip = mk("div", "flow-strip hidden");
  if (tools) tools.before(strip);
  function renderStrip() {
    strip.classList.toggle("hidden", !mine().length);
    strip.innerHTML = "<span>Your cartelas</span>";
    mine().forEach((c) => {
      const b = mk("button", "flow-chip", `#${c.cartela_number} <i>✕</i>`);
      b.type = "button"; b.disabled = !!state.lobbyLocked;
      b.onclick = () => onReleaseCartela(c.id);
      strip.appendChild(b);
    });
  }
  watch($("my-cartelas-count"), renderStrip);
  watch($("my-cartelas-preview"), renderStrip, { childList: true });

  /* 1C smart random: three free cartelas to choose from */
  function chooseRandom(nums, pick, again) {
    $("flow-pick")?.remove();
    const box = mk("div", "flow-pick", '<div class="flow-pick-bg"></div><div class="flow-pick-panel"><h3>🎲 Pick one of these</h3><div class="flow-pick-row"></div><div class="flow-pick-acts"><button type="button" class="secondary-btn" data-a="again">Shuffle again</button><button type="button" class="text-btn" data-a="x">Close</button></div></div>');
    box.id = "flow-pick";
    const row = box.querySelector(".flow-pick-row");
    nums.forEach((n) => { const b = mk("button", "flow-pick-n", `#${n}`); b.type = "button"; b.onclick = () => { box.remove(); pick(n); }; row.appendChild(b); });
    box.onclick = (e) => {
      const a = e.target.dataset && e.target.dataset.a;
      if (a === "again") { box.remove(); again(); } else if (a === "x" || e.target.classList.contains("flow-pick-bg")) box.remove();
    };
    document.body.appendChild(box);
  }
  window.MelbeteFlow = { chooseRandom, choices: () => X("random_picker", "choices"), keepRematch: () => X("rematch", "keep_cartelas") };

  /* 2A ring countdown + 2B lock warning + 2C lobby feed */
  const banner = $("countdown-banner"), secEl = $("countdown-seconds");
  let ring, pool, warn;
  if (banner && secEl) {
    ring = mk("div", "flow-ring", '<svg viewBox="0 0 44 44"><circle class="bg" cx="22" cy="22" r="19"/><circle class="fg" cx="22" cy="22" r="19" pathLength="1"/></svg><b></b>');
    pool = mk("div", "flow-pool");
    warn = mk("div", "flow-warn hidden");
    banner.prepend(ring); banner.append(pool, warn);
    watch(secEl, tickRing);
  }
  function tickRing() {
    const s = parseInt(secEl.textContent, 10);
    if (isNaN(s)) return;
    const total = Math.max(Number(room().countdown_seconds) || 30, s);
    ring.querySelector(".fg").style.strokeDashoffset = String(1 - s / total);
    ring.querySelector("b").textContent = s;
    const hot = X("countdown", "hot_seconds"), wn = X("countdown", "warn_seconds");
    banner.classList.toggle("flow-hot", hot > 0 && s <= hot);
    warn.classList.toggle("hidden", !(wn > 0 && s <= wn && s > 0));
    warn.textContent = state.lobbyLocked ? "🔒 Picking closed" : "⏳ Picking closes soon – confirm your cartela!";
    strip.classList.toggle("flow-glow", wn > 0 && s <= wn && mine().length > 0);
  }
  const feedBox = mk("div", "flow-feed"); document.body.appendChild(feedBox);
  function feed(msg) {
    while (feedBox.children.length >= 3) feedBox.firstChild.remove();
    const i = mk("div", "flow-feed-i", msg); feedBox.appendChild(i);
    setTimeout(() => i.remove(), 3200);
  }

  /* 3A get-ready splash, 3B auto-focus + collapsible called-numbers board */
  const gm = document.querySelector(".gv-main");
  if (gm && gm.querySelector(".gv-board")) {
    const t = mk("button", "flow-board-toggle", "📋 Called numbers ▾"); t.type = "button";
    gm.before(t);
    if (matchMedia("(max-width:700px)").matches) gm.classList.add("flow-board-collapsed");
    t.onclick = () => { const c = gm.classList.toggle("flow-board-collapsed"); t.textContent = c ? "📋 Called numbers ▾" : "📋 Called numbers ▴"; };
  }
  function splash() {
    const cards = mine();
    if (!cards.length || !X("splash", "enabled")) return;
    const o = mk("div", "flow-splash", `<div class="flow-splash-in"><img class="flow-splash-logo" src="${LOGO}" alt=""><div class="flow-splash-t">Get ready!</div><div class="flow-splash-n pop">${X("splash", "seconds")}</div><div class="flow-splash-c">${cards.map((c) => `<span>#${c.cartela_number}</span>`).join("")}</div><div class="flow-splash-s">Stake ${(Number(room().stake_amount) || 0) * cards.length} ETB</div></div>`);
    document.body.appendChild(o);
    buzz("success");
    let n = X("splash", "seconds"); const ne = o.querySelector(".flow-splash-n");
    const iv = setInterval(() => {
      n--;
      if (n <= 0) {
        clearInterval(iv); o.classList.add("out");
        setTimeout(() => { o.remove(); $("my-cards-container")?.scrollIntoView({ block: "start", behavior: reduce ? "auto" : "smooth" }); }, 250);
      } else { ne.textContent = n; ne.classList.remove("pop"); void ne.offsetWidth; ne.classList.add("pop"); try { window.MelbeteCue?.tick(n); } catch (err) { /* ignore */ } }
    }, 700);
  }

  /* 4B ball roll + optional voice call */
  const callNum = $("current-call-number"), callLetter = $("current-call-letter"), ball = $("current-call");
  let lastBall = "";
  const voiceOn = () => { const m = X("voice", "mode"); if (m === "off") return false; const v = ls.g("flow-voice"); return v == null ? m === "on_by_default" : v === "1"; };
  const vbtn = mk("button", "flow-voice", voiceOn() ? "🔊" : "🔇"); vbtn.type = "button"; vbtn.setAttribute("aria-label", "Toggle voice call");
  document.querySelector(".gv-ball-wrap")?.appendChild(vbtn);
  vbtn.onclick = () => { const on = !voiceOn(); ls.s("flow-voice", on ? "1" : "0"); vbtn.textContent = on ? "🔊" : "🔇"; };
  document.addEventListener("melbete:experience", () => { vbtn.style.display = X("voice", "mode") === "off" ? "none" : ""; vbtn.textContent = voiceOn() ? "🔊" : "🔇"; });
  watch(callNum, () => {
    const n = callNum.textContent.trim();
    if (!n || n === "-" || n === lastBall) return;
    lastBall = n;
    if (ball && !reduce) { ball.classList.remove("flow-roll"); void ball.offsetWidth; ball.classList.add("flow-roll"); }
    if (voiceOn() && "speechSynthesis" in window) {
      try {
        speechSynthesis.cancel();
        const u = new SpeechSynthesisUtterance(`${(callLetter?.textContent || "").trim()} ${n}`);
        u.lang = /^am/i.test(document.documentElement.lang || "") ? "am-ET" : "en-US";
        speechSynthesis.speak(u);
      } catch (e) { /* ignore */ }
    }
  });

  /* 4A near-win alert, 4C progress bar, 5A BINGO badge */
  const LINES = (() => {
    const L = [];
    for (let r = 0; r < 5; r++) L.push([0, 1, 2, 3, 4].map((c) => r * 5 + c));
    for (let c = 0; c < 5; c++) L.push([0, 1, 2, 3, 4].map((r) => r * 5 + c));
    L.push([0, 6, 12, 18, 24], [4, 8, 12, 16, 20], [0, 4, 20, 24]);
    return L;
  })();
  const cont = $("my-cards-container");
  let cmo = null, queued = false, lastBuzz = 0;
  function paint() {
    cont?.querySelectorAll(".card-grid").forEach((g) => {
      const cells = g.children;
      if (cells.length !== 25) return;
      const on = (i) => cells[i].classList.contains("marked") || cells[i].classList.contains("free");
      let best = 0;
      LINES.forEach((l) => { const c = l.filter(on).length; if (c > best) best = c; });
      const host = g.parentElement;
      let bar = host.querySelector(":scope > .flow-prog");
      if (!bar) { bar = mk("div", "flow-prog", "<i></i><span></span>"); g.before(bar); }
      bar.firstChild.style.width = Math.round(Math.min(best, 5) / 5 * 100) + "%";
      const near = best === 4 && X("alerts", "near_win");
      bar.lastChild.textContent = best >= 5 ? "🎉 BINGO! Claiming…" : near ? "🔥 1 number away!" : `${best}/5 in best line`;
      host.classList.toggle("flow-near", near);
      host.classList.toggle("flow-bingo", best >= 5);
      if (near && Date.now() - lastBuzz > 20000) { lastBuzz = Date.now(); buzz("warning"); }
    });
    cmo?.takeRecords();
  }
  if (cont) {
    cmo = new MutationObserver(() => { if (queued) return; queued = true; requestAnimationFrame(() => { queued = false; paint(); }); });
    cmo.observe(cont, { childList: true, subtree: true, attributes: true, attributeFilter: ["class"] });
  }

  /* 5B verify moment: "Checking your card..." before the results screen */
  const vf = mk("div", "flow-verify hidden", '<div class="flow-spin"></div><b>Checking your card…</b><small>Verifying with the server</small>');
  document.body.appendChild(vf);
  let vfAt = 0;
  function verifyTick() {
    const busy = state.claimingCardIds && state.claimingCardIds.size > 0 && !!document.querySelector("#my-cards-container .flow-bingo");
    const hidden = vf.classList.contains("hidden"), minMs = X("verify", "min_ms");
    if (busy && hidden && minMs > 0) { vfAt = Date.now(); vf.classList.remove("hidden"); }
    else if (!busy && !hidden && Date.now() - vfAt > minMs) vf.classList.add("hidden");
  }

  /* 6A tiered celebration, 6C other-players view, 6D wallet landing */
  const ov = $("winner-overlay"), payout = $("winner-payout");
  let won = false;
  const COLORS = ["#FFC933", "#FFE08A", "#FFB800", "#E08A00", "#F8FAFC"];
  function confetti(n) {
    if (reduce) return;
    for (let i = 0; i < n; i++) {
      const s = mk("span", "melbete-confetti");
      s.style.cssText = `left:${Math.random() * 100}%;background:${COLORS[i % 5]};animation-duration:${2 + Math.random() * 2.2}s;animation-delay:${Math.random()}s`;
      ov.appendChild(s);
    }
  }
  function decorate() {
    if (!ov || ov.classList.contains("hidden") || !payout) return;
    const txt = payout.textContent || "";
    const m = /You won\s+([\d.,]+)/i.exec(txt);
    if (m) {
      if (ov.dataset.flowTier) return;
      const amt = parseFloat(m[1].replace(/,/g, "")) || 0;
      const t = { big: X("tiers", "big_win_etb"), jackpot: X("tiers", "jackpot_etb") };
      const tier = amt >= t.jackpot ? "jackpot" : amt >= t.big ? "big" : "win";
      ov.dataset.flowTier = tier; ov.classList.add("flow-" + tier); won = true;
      if (tier !== "win") confetti(tier === "jackpot" ? 60 : 30);
      if (tier === "jackpot" && X("tiers", "jackpot_burst")) {
        ov.appendChild(mk("div", "flow-burst"));
        ov.querySelector(".winner-overlay-content")?.insertAdjacentHTML("afterbegin", '<div class="flow-jp">🏆 JACKPOT!</div>');
      }
    } else if (/\d\s*ETB/i.test(txt)) {
      if (ov.dataset.flowOther) return;
      ov.dataset.flowOther = "1"; ov.classList.add("flow-other");
      if (mine().length) payout.after(mk("div", "flow-mine", `Your cartelas ${mine().map((c) => `<span>#${c.cartela_number}</span>`).join("")} – not this time. Next round could be yours! 🍀`));
    }
  }
  function flyCoin() {
    const t = $("balance-real");
    if (!t || reduce || !X("tiers", "coin_fly")) return;
    const r = t.getBoundingClientRect(), c = mk("div", "flow-coin", "🪙");
    document.body.appendChild(c);
    const a = c.animate([
      { transform: `translate(${innerWidth / 2}px,${innerHeight / 2}px) scale(2.2)`, opacity: 1 },
      { transform: `translate(${r.left + r.width / 2}px,${r.top + r.height / 2}px) scale(.6)`, opacity: 0.9 },
    ], { duration: 900, easing: "cubic-bezier(.5,0,.3,1)" });
    a.onfinish = () => {
      c.remove(); buzz("success");
      const p = $("balance-display");
      if (p) { p.classList.add("flow-bump"); setTimeout(() => p.classList.remove("flow-bump"), 700); }
    };
  }
  if (ov) {
    watch(payout, decorate);
    watch(ov, () => {
      if (ov.classList.contains("hidden")) {
        if (ov.dataset.flowTier || ov.dataset.flowOther) {
          ["flow-win", "flow-big", "flow-jackpot", "flow-other"].forEach((c) => ov.classList.remove(c));
          delete ov.dataset.flowTier; delete ov.dataset.flowOther;
          ov.querySelectorAll(".flow-burst,.flow-jp,.flow-mine").forEach((e) => e.remove());
          if (won) { won = false; flyCoin(); }
        }
      } else decorate();
    }, { attributes: true, attributeFilter: ["class"] });
  }

  /* 7B next-round progress bar on the results screen */
  const nr = $("winner-next-round");
  if (nr) {
    const bar = mk("div", "flow-nr hidden", "<i></i>");
    nr.after(bar);
    watch(nr, () => {
      const m = /in\s+(\d+)s/.exec(nr.textContent), tot = Number(room().auto_next_round_seconds) || 0;
      bar.classList.toggle("hidden", nr.classList.contains("hidden") || !m || !tot);
      if (m && tot) bar.firstChild.style.width = Math.min(100, m[1] / tot * 100) + "%";
    });
  }

  /* main loop: state transitions, lobby feed, pool line */
  let prevState = null, prevRoomId = null, seen = null;
  setInterval(() => {
    const r = room(), s = r.state;
    if (r.id !== prevRoomId) { prevRoomId = r.id; seen = null; }
    if (prevState === "waiting" && s === "running") splash();
    prevState = s;
    const taken = new Set(r.taken_cartelas || []), my = new Set(mine().map((c) => c.cartela_number));
    if (seen && s === "waiting" && X("countdown", "lobby_feed")) { let k = 0; taken.forEach((n) => { if (!seen.has(n) && !my.has(n) && k++ < 2) feed(`🎟 Cartela #${n} was taken`); }); }
    seen = taken;
    if (pool) pool.classList.toggle("hidden", !X("countdown", "show_pool"));
    if (pool) pool.textContent = `${taken.size} cartela(s) · ${taken.size * (Number(r.stake_amount) || 0)} ETB staked`;
    strip.querySelectorAll(".flow-chip").forEach((b) => { b.disabled = !!state.lobbyLocked; });
    verifyTick();
  }, 250);
  renderStrip(); paint();
})();
