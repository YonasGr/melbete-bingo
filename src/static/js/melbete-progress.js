/* Daily-streak chip in the hub footer (next to Promos) and level frames (room).
   Display only: reads /api/progress, never changes game or wallet state. */
(() => {
  "use strict";
  const KEY = "melbete_level", root = document.documentElement;
  try { const l = localStorage.getItem(KEY); if (l) root.dataset.melbeteLevel = l; } catch (e) { /* storage blocked */ }
  const picker = document.getElementById("cartela-quick-picker");
  if (!picker || typeof api !== "function") return;           // bingo room: frame attribute above is all it needs

  // Skeleton first, so the picker never jumps when the lobby arrives.
  picker.classList.remove("hidden");
  picker.innerHTML = '<div class="sk sk-line"></div><div class="sk-grid">' + '<div class="sk"></div>'.repeat(12) + "</div>";
  const chip = document.getElementById("hub-streak");
  const promosBtn = document.getElementById("hub-tab-promos");

  // Daily streak lives in the footer, next to the Promos tab.
  function render(d) {
    const days = (d.streak && d.streak.days) || 0;
    if (chip) {
      chip.textContent = `\u{1F525}${days}`;
      chip.classList.toggle("off", days === 0);
      chip.classList.remove("hidden");
      chip.title = `${days}-day streak`;
    }
    promosBtn?.setAttribute("aria-label", `Promotions, ${days}-day streak`);
    const name = d.level.name.toLowerCase();                   // cosmetic card frames in the room
    root.dataset.melbeteLevel = name;
    try { localStorage.setItem(KEY, name); } catch (e) { /* ignore */ }
  }

  async function load() {
    try { render(await api(`/api/progress?player_id=${encodeURIComponent(state.playerId)}`)); }
    catch (e) { /* optional - never block the hub */ }
  }
  const wait = setInterval(() => {
    if (typeof state !== "undefined" && state.playerId) { clearInterval(wait); load(); setInterval(load, 60000); }
  }, 300);
})();
