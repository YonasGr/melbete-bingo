/* Admin > Rewards: start / stop every promotional reward on its own, timers, budget auto-stop,
   pause-all, live numbers and change history with undo.
   Depends on admin.js globals: api, toast, escapeHtml, TAB_TITLES, TAB_CONTEXT, LOADERS. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const esc = (v) => escapeHtml(v == null ? "" : String(v));
  const STATE = { off_window: ["Off-hours", "info"], on: ["Running", "ok"], scheduled: ["Scheduled", "info"], ended: ["Ended", "warn"], stopped: ["Stopped", "bad"], paused_all: ["Paused (all)", "bad"] };
  let board = null, overrides = [], notify = false;
  const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

  TAB_TITLES.rewards = "Rewards";
  TAB_CONTEXT.rewards = "Start, stop and schedule every promotion";
  LOADERS.rewards = load;

  const etLocal = (iso) => { const t = Date.parse(iso || ""); return isNaN(t) ? "" : new Date(t + 3 * 3600e3).toISOString().slice(0, 16); };
  const toIso = (v) => (v ? `${v}:00+03:00` : "");
  const pct = (a, b) => (b > 0 && a != null ? Math.min(100, Math.round((a / b) * 100)) : 0);

  async function load() {
    const root = $("rewards-root");
    if (!board) root.innerHTML = '<p class="cc-note">Loading…</p>';
    try { board = await api("/api/admin/rewards/board"); overrides = (await api("/api/admin/rewards/overrides").catch(() => ({ items: [] }))).items; render(); }
    catch (e) { root.innerHTML = `<p class="cc-note">${esc(e.message)}</p>`; }
  }

  async function post(path, body, ok) {
    try { board = await api(path, { method: "POST", body: JSON.stringify(body) }); render(); toast(ok || "Saved", "success"); }
    catch (e) { toast(e.message, "error"); }
  }

  function bars(days) {
    const max = Math.max(1, ...days.map((d) => d.etb));
    return `<div class="rw-bars" aria-label="Last 7 days">${days.map((d) => `<i title="${esc(d.date)}: ${d.etb} ETB, ${d.claims} claims" style="height:${Math.max(4, Math.round((d.etb / max) * 100))}%"></i>`).join("")}</div>`;
  }

  function winRow(w) {
    return `<div class="rw-win"><div class="rw-days">${DAYS.map((d, i) => `<label><input type="checkbox" ${w.days[i] ? "checked" : ""}>${d}</label>`).join("")}</div>
      <input class="rw-from" maxlength="5" value="${esc(w.from)}" aria-label="From"> – <input class="rw-to" maxlength="5" value="${esc(w.to)}" aria-label="To"><button class="text-btn" data-rmwin type="button">✕</button></div>`;
  }

  function overridesHtml(edit) {
    const rows = overrides.length ? overrides.map((o) => `<li><div><b>${esc(o.name || o.player_id)}</b><small>${esc(o.player_id)}${o.note ? ` · ${esc(o.note)}` : ""}</small></div>${edit ? `<button class="text-btn" data-unblock="${esc(o.player_id)}">Unblock</button>` : ""}</li>`).join("") : '<li class="rw-muted">No players are blocked.</li>';
    return `<h3 class="rw-h">Player controls</h3><div class="rw-grid">
      <article class="rw-card"><h3>Blocked from all rewards</h3><ul class="rw-hist">${rows}</ul>${edit ? `<div class="rw-sched"><input id="rw-bid" placeholder="Player id (tg_123456789)"><input id="rw-bnote" maxlength="200" placeholder="Reason"><button class="danger-btn" data-block>Block player</button></div>` : ""}</article>
      <article class="rw-card"><h3>Give a one-off bonus</h3><p class="rw-muted">Credited to bonus balance (playable, not withdrawable). Saved in the audit log.</p>${edit ? `<div class="rw-sched"><input id="rw-gid" placeholder="Player id (tg_123456789)"><input id="rw-gamt" type="number" min="1" max="10000" placeholder="Amount (ETB)"><input id="rw-gnote" maxlength="200" placeholder="Reason (required)"><button class="primary-btn" data-grant>Give bonus</button></div>` : ""}</article></div>`;
  }

  function card(it) {
    const [txt, cls] = STATE[it.state] || [it.state, "info"];
    const c = it.config, u = it.usage, camp = it.campaign, edit = board.can_edit;
    const running = it.state === "on";
    let spend = '<p class="rw-muted">No spend figures for this reward.</p>';
    if (u) {
      spend = `<p class="rw-line"><b>${u.today_etb || 0} ETB</b> today · ${u.today_count || 0} claims${u.total_etb != null ? ` · ${u.total_etb} ETB total` : ""}</p>`;
      if (camp) {
        spend += `<div class="rw-meter"><span>Daily budget</span><div><i style="width:${pct(u.today_etb, camp.daily_budget_etb)}%"></i></div><em>${u.today_etb || 0}/${camp.daily_budget_etb}</em></div>`
          + `<div class="rw-meter"><span>Total budget</span><div><i style="width:${pct(u.total_etb, camp.total_budget_etb)}%"></i></div><em>${u.total_etb || 0}/${camp.total_budget_etb}</em></div>`;
        if (!camp.enabled) spend += '<p class="rw-muted">Campaign is switched off - Start will switch it on.</p>';
      }
    }
    const auto = camp ? `<label>Auto-stop at <select data-auto="${it.key}"><option value="0">Off</option>${[80, 90, 100].map((p) => `<option value="${p}"${c.auto_stop_pct === p ? " selected" : ""}>${p}% of budget</option>`).join("")}</select></label>` : "";
    return `<article class="rw-card rw-${cls}">
      <header><h3>${esc(it.label)}</h3><span class="rw-pill">${txt}</span></header>
      ${it.message ? `<p class="rw-msg">${esc(it.message)}</p>` : ""}
      ${spend}${bars(it.days)}
      <p class="rw-prev">👁 Players see: ${esc(it.preview)}</p>
      <div class="rw-actions">${edit ? `<span class="rw-flash">Flash: ${[[60, "1h"], [360, "6h"], [1440, "24h"]].map(([m, n]) => `<button class="secondary-btn" data-flash="${it.key}" data-min="${m}">${n}</button>`).join("")}</span>` : ""}${edit ? (running
        ? `<button class="danger-btn" data-stop="${it.key}">Stop…</button>`
        : `<button class="primary-btn" data-start="${it.key}">${it.state === "scheduled" || it.state === "ended" ? "Start now" : "Start"}</button>`) : ""}</div>
      <details><summary>Timer &amp; limits</summary>
        <div class="rw-sched">
          <label>Start at (Ethiopia time)<input type="datetime-local" data-start-at="${it.key}" value="${etLocal(c.start_at)}"></label>
          <label>Stop at (Ethiopia time)<input type="datetime-local" data-stop-at="${it.key}" value="${etLocal(c.stop_at)}"></label>
          <button class="secondary-btn" data-sched="${it.key}"${edit ? "" : " disabled"}>Save timer</button>
          ${auto}
          <div class="rw-box"><b>Weekly windows</b> <small>(Ethiopia time; none = always on)</small>
            <div data-wins="${it.key}">${(c.windows || []).map(winRow).join("")}</div>
            <button class="text-btn" data-addwin="${it.key}">+ Add window</button> <button class="secondary-btn" data-savewin="${it.key}"${edit ? "" : " disabled"}>Save windows</button></div>
          <div class="rw-box"><b>Who can claim</b> <small>(0 = anyone)</small>
            <label>New players only: account younger than (days)<input type="number" min="0" data-nd="${it.key}" value="${c.elig.new_players_days}"></label>
            <label>Minimum total deposits (ETB)<input type="number" min="0" data-md="${it.key}" value="${c.elig.min_deposits_etb}"></label>
            <button class="secondary-btn" data-saveelig="${it.key}"${edit ? "" : " disabled"}>Save filters</button></div>
          <div class="rw-box"><b>Abuse brake</b> <small>(auto-stop on a claim spike; 0 = off)</small>
            <label>Stop after this many claims<input type="number" min="0" data-sl="${it.key}" value="${c.spike_limit}"></label>
            <label>within (minutes)<input type="number" min="1" max="1440" data-sm="${it.key}" value="${c.spike_minutes}"></label>
            <button class="secondary-btn" data-savespike="${it.key}"${edit ? "" : " disabled"}>Save brake</button></div>
        </div>
      </details></article>`;
  }

  function render() {
    const m = board.master, edit = board.can_edit;
    const master = m.paused
      ? `<div class="rw-master rw-bad"><div><b>⏸ ALL rewards are paused</b> (${m.mode === "all" ? "earned rewards held too" : "new claims only"})${m.reason ? ` - ${esc(m.reason)}` : ""}</div>${edit ? '<button class="primary-btn" data-resume>Resume all</button>' : ""}</div>`
      : `<div class="rw-master"><div><b>Master switch</b><br><small>Pause every reward at once. Resuming brings each one back exactly as it was.</small></div>${edit ? '<button class="danger-btn" data-pause>Pause all…</button>' : ""}</div>`;
    const hist = board.history.length
      ? board.history.map((h) => `<li><div><b>${esc(h.summary || h.action)}</b><small>${esc(h.created_at.replace("T", " ").slice(0, 16))} · ${esc(h.actor_id)}${h.actor_role ? ` (${esc(h.actor_role)})` : ""}</small></div>${edit && h.undoable && h.action !== "rewards.undo" ? `<button class="text-btn" data-undo="${h.id}">Undo</button>` : ""}</li>`).join("")
      : '<li class="rw-muted">No changes yet.</li>';
    const nbar = edit ? `<label class="rw-notify"><input type="checkbox" id="rw-notify" ${notify ? "checked" : ""}> Tell all players on Telegram when I start, flash or stop a reward</label>` : "";
    $("rewards-root").innerHTML = `${master}${nbar}<div class="rw-grid">${board.items.map(card).join("")}</div>${overridesHtml(edit)}<h3 class="rw-h">Change history</h3><ul class="rw-hist">${hist}</ul>`;
  }

  function ask(title, withReason, cb) {
    const o = document.createElement("div");
    o.className = "rw-modal";
    o.innerHTML = `<div class="rw-sheet"><h3>${esc(title)}</h3>
      <label class="rw-opt"><input type="radio" name="rwm" value="new" checked><span><b>Pause new claims only</b><small>Players keep rewards they already earned.</small></span></label>
      <label class="rw-opt"><input type="radio" name="rwm" value="all"><span><b>Pause everything</b><small>Also holds payouts of rewards already earned (agent rewards, leaderboard prizes).</small></span></label>
      ${withReason ? '<input id="rw-reason" maxlength="200" placeholder="Reason (shown to players, saved in the audit log)">' : ""}
      <div class="rw-actions"><button class="secondary-btn" data-x>Cancel</button><button class="danger-btn" data-ok>Confirm</button></div></div>`;
    o.onclick = (e) => {
      if (e.target.dataset.x !== undefined || e.target === o) o.remove();
      else if (e.target.dataset.ok !== undefined) {
        cb(o.querySelector("input[name=rwm]:checked").value, (o.querySelector("#rw-reason")?.value || "").trim());
        o.remove();
      }
    };
    document.body.appendChild(o);
  }

  async function blockPlayer(id, note) {
    try { overrides = (await api("/api/admin/rewards/overrides", { method: "POST", body: JSON.stringify({ player_id: id, note }) })).items; render(); toast("Player blocked from rewards", "success"); }
    catch (e) { toast(e.message, "error"); }
  }
  async function unblock(id) {
    try { overrides = (await api(`/api/admin/rewards/overrides/${encodeURIComponent(id)}`, { method: "DELETE" })).items; render(); toast("Player unblocked", "success"); }
    catch (e) { toast(e.message, "error"); }
  }
  async function grant(id, amt, note) {
    try { await api("/api/admin/rewards/grant", { method: "POST", body: JSON.stringify({ player_id: id, amount_etb: amt, note }) }); toast(`Gave ${amt} ETB bonus`, "success"); load(); }
    catch (e) { toast(e.message, "error"); }
  }

  const root = $("rewards-root");
  if (!root) return;
  root.addEventListener("click", (e) => {
    const t = e.target.closest("button");
    if (!t) return;
    const d = t.dataset, A = "/api/admin/rewards/action";
    const q = (sel) => root.querySelector(sel);
    if (d.stop) ask("Stop this reward", true, (mode, reason) => post(A, { op: "stop", key: d.stop, mode, reason, notify }, "Reward stopped"));
    else if (d.start) post(A, { op: "start", key: d.start, notify }, "Reward started");
    else if (d.flash) post(A, { op: "flash", key: d.flash, minutes: Number(d.min), notify }, "Flash promo started");
    else if (d.addwin) q(`[data-wins="${d.addwin}"]`).insertAdjacentHTML("beforeend", winRow({ days: [true, true, true, true, true, true, true], from: "18:00", to: "22:00" }));
    else if (d.rmwin !== undefined) t.closest(".rw-win").remove();
    else if (d.savewin) post(A, { op: "windows", key: d.savewin, windows: [...q(`[data-wins="${d.savewin}"]`).querySelectorAll(".rw-win")].map((w) => ({ days: [...w.querySelectorAll(".rw-days input")].map((i) => i.checked), from: w.querySelector(".rw-from").value.trim(), to: w.querySelector(".rw-to").value.trim() })) }, "Windows saved");
    else if (d.saveelig) post(A, { op: "elig", key: d.saveelig, new_players_days: Number(q(`[data-nd="${d.saveelig}"]`).value), min_deposits_etb: Number(q(`[data-md="${d.saveelig}"]`).value) }, "Filters saved");
    else if (d.savespike) post(A, { op: "spike", key: d.savespike, spike_limit: Number(q(`[data-sl="${d.savespike}"]`).value), spike_minutes: Number(q(`[data-sm="${d.savespike}"]`).value) }, "Abuse brake saved");
    else if (d.block !== undefined) blockPlayer(q("#rw-bid").value.trim(), q("#rw-bnote").value.trim());
    else if (d.unblock) unblock(d.unblock);
    else if (d.grant !== undefined) grant(q("#rw-gid").value.trim(), Number(q("#rw-gamt").value), q("#rw-gnote").value.trim());
    else if (d.sched) post(A, { op: "schedule", key: d.sched, start_at: toIso(root.querySelector(`[data-start-at="${d.sched}"]`).value), stop_at: toIso(root.querySelector(`[data-stop-at="${d.sched}"]`).value) }, "Timer saved");
    else if (d.pause !== undefined) ask("Pause all rewards", true, (mode, reason) => post(A, { op: "pause_all", mode, reason }, "All rewards paused"));
    else if (d.resume !== undefined) post(A, { op: "resume_all" }, "Rewards resumed");
    else if (d.undo) post("/api/admin/rewards/undo", { audit_id: Number(d.undo) }, "Change undone");
  });
  root.addEventListener("change", (e) => {
    if (e.target.id === "rw-notify") { notify = e.target.checked; return; }
    if (e.target.dataset.auto) post("/api/admin/rewards/action", { op: "auto_stop", key: e.target.dataset.auto, auto_stop_pct: Number(e.target.value) }, "Auto-stop saved");
  });
})();
