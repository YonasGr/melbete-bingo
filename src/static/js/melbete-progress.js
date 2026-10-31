/* Cosmetic level card + 7-day streak calendar (home) and level frames (room).
   Display only: reads /api/progress, never changes game or wallet state. */
(() => {
  "use strict";
  const KEY = "melbete_level", root = document.documentElement;
  try { const l = localStorage.getItem(KEY); if (l) root.dataset.melbeteLevel = l; } catch (e) { /* storage blocked */ }
  const picker = document.getElementById("cartela-quick-picker");
  if (!picker || typeof api !== "function") return;           // bingo room: frame attribute above is all it needs

  const TIER = { Bronze: ["#e0975a", "#8a4b12"], Silver: ["#f1f5f9", "#8b93a3"], Gold: ["#ffe08a", "#d99a1c"],
                 Platinum: ["#bae6fd", "#3b82c4"], Diamond: ["#c4b5fd", "#4f46e5"], Crown: ["#ffe08a", "#e0a21b"] };
  const CROWN = '<svg viewBox="0 0 96 72" aria-hidden="true"><path d="M8 58 14 20 33 38 48 10 63 38 82 20 88 58Z" fill="#7a4a05"/><rect x="8" y="58" width="80" height="10" rx="3" fill="#7a4a05"/></svg>';

  // Skeletons first, so the layout never jumps when data arrives.
  picker.classList.remove("hidden");
  picker.innerHTML = '<div class="sk sk-line"></div><div class="sk-grid">' + '<div class="sk"></div>'.repeat(12) + "</div>";
  const card = document.createElement("section");
  card.id = "melbete-level-card"; card.className = "melbete-level"; card.setAttribute("aria-label", "Your level");
  card.innerHTML = '<div class="sk sk-line"></div><div class="sk" style="height:56px"></div><div class="sk" style="height:36px;margin-top:14px"></div>';
  picker.before(card);

  function render(d) {
    const lv = d.level, t = TIER[lv.name] || TIER.Bronze;
    const pct = lv.next_at ? Math.min(100, Math.round(((lv.rounds - lv.from) / (lv.next_at - lv.from)) * 100)) : 100;
    const sub = lv.next_at ? `${lv.rounds} rounds - ${lv.next_at - lv.rounds} to ${lv.next_name}` : `${lv.rounds} rounds - top level`;
    const days = d.streak.calendar.map((c, i) => {
      const dow = "SMTWTFS"[new Date(c.date + "T00:00:00").getDay()];
      return `<span class="nl-day${c.played ? " on" : ""}${i === d.streak.calendar.length - 1 ? " today" : ""}" aria-label="${c.date}${c.played ? " played" : ""}">${dow}</span>`;
    }).join("");
    card.innerHTML =
      `<div class="nl-top"><div class="nl-badge" style="--t1:${t[0]};--t2:${t[1]}">${lv.name === "Crown" ? CROWN : lv.index + 1}</div>` +
      `<div class="nl-main"><strong>${lv.name}</strong><span class="nl-sub">${sub}</span><div class="nl-bar"><i style="width:${pct}%"></i></div></div></div>` +
      `<div class="nl-streak"><span class="nl-flame">\u{1F525} ${d.streak.days}-day streak</span><div class="nl-days">${days}</div></div>` +
      `<p class="nl-note">Levels are cosmetic: they unlock card frames and carry no cash value.</p>`;
    const name = lv.name.toLowerCase();
    root.dataset.melbeteLevel = name;
    try { localStorage.setItem(KEY, name); } catch (e) { /* ignore */ }
  }

  async function load() {
    try { render(await api(`/api/progress?player_id=${encodeURIComponent(state.playerId)}`)); }
    catch (e) { card.remove(); }                                 // optional - never block the hub
  }
  const wait = setInterval(() => {
    if (typeof state !== "undefined" && state.playerId) { clearInterval(wait); load(); setInterval(load, 60000); }
  }, 300);
})();
