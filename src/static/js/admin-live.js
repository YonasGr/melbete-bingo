/* Melbete Bingo - live operations dashboard (Dashboard tab).
   One request to /api/admin/live every few seconds while the tab is open:
   alerts, who is playing now, money waiting, today vs yesterday, and a 7-day trend.
   Depends on admin.js globals: api, escapeHtml, goToTab, LOADERS. */
(() => {
  "use strict";
  const esc = (v) => escapeHtml(v);
  const etb = (n) => `${Math.round(Number(n) || 0).toLocaleString()} ETB`;
  const num = (n) => (Number(n) || 0).toLocaleString();

  function delta(pct) {
    if (pct == null) return `<small class="lv-delta">no data yesterday</small>`;
    const up = pct >= 0;
    return `<small class="lv-delta ${up ? "up" : "down"}">${up ? "▲" : "▼"} ${Math.abs(pct)}% vs yesterday</small>`;
  }
  function card(label, value, sub = "", tab = "") {
    const inner = `<div class="stat-label">${esc(label)}</div><div class="stat-value">${value}</div>${sub}`;
    return tab ? `<button class="stat-card lv-card lv-click" data-lv-tab="${tab}">${inner}</button>` : `<div class="stat-card lv-card">${inner}</div>`;
  }
  const age = (m) => (m == null ? "" : m < 60 ? `oldest ${m} min` : `oldest ${Math.floor(m / 60)} h ${m % 60} min`);

  function chart(series) {
    const max = Math.max(1, ...series.map((d) => Math.max(Math.abs(d.revenue), d.deposits)));
    const bar = (v, cls) => `<i class="lv-bar ${cls} ${v < 0 ? "neg" : ""}" style="height:${Math.max(2, Math.round((Math.abs(v) / max) * 100))}%" title="${etb(v)}"></i>`;
    return `<div class="lv-chart" role="img" aria-label="Revenue and deposits for the last 7 days">${series.map((d) => `
      <div class="lv-col"><div class="lv-bars">${bar(d.deposits, "dep")}${bar(d.revenue, "rev")}</div>
      <small>${new Date(d.day + "T12:00:00").toLocaleDateString([], { weekday: "short" })}</small></div>`).join("")}</div>
      <div class="lv-legend"><span><i class="lv-dot dep"></i>Deposits</span><span><i class="lv-dot rev"></i>Revenue</span></div>`;
  }

  function render(d) {
    const box = document.getElementById("live-today");
    if (!box) return;
    const t = d.today, p = d.pending;
    const canPublish = typeof state !== "undefined" && !!(state.isSuperAdmin || (state.perms && state.perms.has("control.publish")));
    const alertHtml = (a) => (a.action === "publish" && canPublish
      ? `<div class="lv-alert ${a.level} lv-alert-row"><button type="button" class="lv-alert-text" data-lv-tab="${a.tab}">${esc(a.text)} →</button><button type="button" class="primary-btn lv-publish" data-lv-publish>Publish</button></div>`
      : `<button class="lv-alert ${a.level}" ${a.tab ? `data-lv-tab="${a.tab}"` : ""}>${esc(a.text)}${a.tab ? " →" : ""}</button>`);
    const alerts = d.alerts.length
      ? `<div class="lv-alerts">${d.alerts.map(alertHtml).join("")}</div>`
      : `<div class="lv-alerts"><span class="lv-alert ok">All clear: nothing needs attention right now.</span></div>`;
    const rooms = d.rooms.length ? d.rooms.map((r) => `${esc(r.track.replace("_", " "))}: ${esc(r.state)}, ${r.players} player${r.players === 1 ? "" : "s"}`).join(" · ") : "No active round";
    box.innerHTML = `${alerts}
      <h3 class="lv-h">Right now</h3>
      <div class="stat-grid lv-grid">
        ${card("Playing now", num(d.playing_now), `<small>${rooms}</small>`)}
        ${card("Live connections", num(d.connections), `<small>open on this server</small>`)}
        ${card("Deposits waiting", num(p.deposit.count), `<small>${etb(p.deposit.amount)} ${p.deposit.count ? "· " + age(p.deposit.oldest_minutes) : ""}</small>`, "deposits")}
        ${card("Withdrawals waiting", num(p.withdrawal.count), `<small>${etb(p.withdrawal.amount)} ${p.withdrawal.count ? "· " + age(p.withdrawal.oldest_minutes) : ""}</small>`, "withdrawals")}
      </div>
      <h3 class="lv-h">Today</h3>
      <div class="stat-grid lv-grid">
        ${card("Revenue", `<span class="${t.revenue < 0 ? "neg" : ""}">${etb(t.revenue)}</span>`, delta(d.change.revenue), "economy")}
        ${card("Deposits approved", etb(t.deposits), delta(d.change.deposits) + `<small>${num(t.deposit_count)} deposit${t.deposit_count === 1 ? "" : "s"}</small>`)}
        ${card("Withdrawals paid", etb(t.withdrawals), `<small>${num(t.withdrawal_count)} payout${t.withdrawal_count === 1 ? "" : "s"}</small>`)}
        ${card("Players who played", num(t.players_played), delta(d.change.players_played))}
        ${card("New players", num(t.new_players), `<small>${num(d.yesterday.new_players)} yesterday</small>`)}
        ${card("Rounds", num(t.rounds_ended), `<small>${num(t.rounds_started)} started · ${num(t.stake_count)} cartela${t.stake_count === 1 ? "" : "s"} sold</small>`)}
      </div>
      <article class="work-panel lv-trend"><div class="panel-heading"><div><h3>Last 7 days</h3><p>Deposits and platform revenue per day</p></div><small class="lv-stamp">Updated ${new Date(d.generated_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</small></div>${chart(d.series)}</article>`;
  }

  async function loadLive() {
    try { render(await api("/api/admin/live")); }
    catch (e) { const box = document.getElementById("live-today"); if (box && !box.innerHTML) box.innerHTML = `<p class="cc-note">Live numbers are unavailable right now.</p>`; }
  }

  document.addEventListener("click", (e) => {
    const pb = e.target.closest && e.target.closest("[data-lv-publish]");
    if (pb && document.getElementById("live-today")?.contains(pb)) {
      pb.disabled = true;
      Promise.resolve(window.publishControlDraft ? window.publishControlDraft() : false).finally(() => { pb.disabled = false; loadLive(); });
      return;
    }
    const b = e.target.closest && e.target.closest("[data-lv-tab]");
    if (b && document.getElementById("live-today")?.contains(b)) goToTab(b.dataset.lvTab);
  });

  // The dashboard tab is polled by admin.js through LOADERS.dashboard; piggy-back on it.
  const original = LOADERS.dashboard;
  LOADERS.dashboard = async () => { await Promise.all([original ? original() : null, loadLive()]); };
})();
