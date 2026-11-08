// Melbete Bingo - Mini App frontend
// One live global session: pick a cartela (free), watch others join in
// real time, 30s countdown, then everyone reserved gets charged and the
// round starts together - even a single player is enough.
//
// Reliability note: WebSocket push is the primary channel for live
// updates, but Telegram's in-app browser (and some proxies) can drop or
// throttle long-lived sockets without a clean disconnect event. Rather
// than betting the whole game on the socket, a lightweight poll
// (`refreshCurrentRoom`) re-fetches session state every few seconds as a safety
// net, so called numbers / taken cartelas / round state always catch up
// even if a push message was missed - no manual reload required.

// `tg`, `API`, initIdentity(), toast(), api(), idemKey() come from
// melbete-core.js, which bingo.html loads first.
const state = {
  playerId: null,
  telegramId: null,
  displayName: "Player",
  track: null, // "real_10" | "real_50" | "bonus" - which of the 3 rooms the player is currently in
  room: null,
  myCards: [], // up to `room.cards_per_player` cartelas (optional 2nd cartela)
  cardsPerPlayer: null, // populated from room data
  ws: null,
  autoBingo: true, // always on - there is no manual mode
  lastCalledCount: -1, // tracks how many called numbers were last rendered — avoids
                       // redundant DOM rebuilds when poll and WS both fire close together
  countdownInterval: null,
  countdownRefreshTimer: null,
  countdownRemaining: null,
  pollTimer: null,
  wsHeartbeatTimer: null,
  claimedCardIds: new Set(),
  claimingCardIds: new Set(),
  isLeavingResults: false,
  // True for the last few seconds of the lobby countdown - see
  // startCountdownBanner() / LOBBY_LOCK_SECONDS. All cartela pick/release
  // buttons are disabled during this window; the server enforces the same
  // lock independently (see RoomService.LOBBY_LOCK_SECONDS), this is just
  // the UI reflecting it instantly instead of waiting for a rejected
  // request to bounce back.
  lobbyLocked: false,
};

// ---------------------------------------------------------------------
// BINGO letter formatting - standard 75-ball ranges, matching the column
// layout the backend already generates cards with (CardGenerator):
// B 1-15, I 16-30, N 31-45, G 46-60, O 61-75.
// ---------------------------------------------------------------------
const BINGO_LETTERS = ["B", "I", "N", "G", "O"];
const BINGO_COLUMN_SIZE = 15;
const BINGO_MIN_BALL = 1;
const BINGO_MAX_BALL = 75;

// Labels/descriptions for the 3 rooms shown on the room-select screen.
// Order here is display order. In Amharic - this screen's text is
// deliberately localized (the rest of the app stays English) since
// players are Ethiopia-based and the new 3-room change could otherwise
// be confusing.
const TRACK_META = {
  real_10: {
    icon: "💵",
    title: "10 ብር ክፍል",
    subtitle: "በጨዋታ 10 ብር",
    balanceKind: "real",
  },
};
const TRACK_ORDER = ["real_10"];

// A 75-ball bingo ball is always 1-75. Anything outside that range (e.g. a
// stray cartela number like 240) is NOT a valid ball and must never be shown
// as "O240" in the called-numbers display. Guard against it everywhere.
function isValidBall(n) {
  return Number.isInteger(n) && n >= BINGO_MIN_BALL && n <= BINGO_MAX_BALL;
}

function bingoLetterFor(number) {
  if (!isValidBall(number)) return "?";
  const idx = Math.min(Math.floor((number - 1) / BINGO_COLUMN_SIZE), BINGO_LETTERS.length - 1);
  return BINGO_LETTERS[Math.max(idx, 0)];
}

function formatCall(number) {
  if (!isValidBall(number)) return String(number);
  return `${bingoLetterFor(number)}${number}`;
}

// ---------------------------------------------------------------------
// Identity
// ---------------------------------------------------------------------
function showView(id) {
  document.querySelectorAll(".view").forEach((v) => v.classList.remove("active"));
  document.getElementById(id)?.classList.add("active");
  document.getElementById("app-shell")?.classList.toggle("track-select-active", id === "track-select-view");
  // Footer pill: visible on the room list, lobby and waiting screens; hidden while loading
  // and during the live draw so it never covers the cards.
  const showFooter = ["track-select-view", "lobby-view", "waiting-view"].includes(id);
  document.getElementById("room-footer")?.classList.toggle("hidden", !showFooter);
  document.getElementById("room-cashback-fab")?.classList.toggle("hidden", !showFooter);
  document.getElementById("rf-rooms")?.classList.toggle("active", id === "track-select-view");
}

function updateRoomHeader(trackId) {
  const shell = document.getElementById("app-shell");
  const meta = TRACK_META[trackId];
  shell?.classList.toggle("bingo-room-active", Boolean(meta));
  if (shell) shell.dataset.track = meta ? trackId : "";

  const name = document.getElementById("current-room-name");
  const subtitle = document.getElementById("current-room-subtitle");
  if (name) name.textContent = meta?.title || "Bingo";
  if (subtitle) subtitle.textContent = meta?.subtitle || "Game room";
}

// ---------------------------------------------------------------------
// Wallet / top bar
// ---------------------------------------------------------------------
// ---- Floating footer: live balance chip, unread badge, pending-deposit dot ----
function updateRoomChip(balance) {
  const val = document.getElementById("room-chip-value");
  if (!val) return;
  const next = Number(balance).toLocaleString();
  if (val.textContent !== next) {
    val.textContent = next;
    const chip = document.getElementById("room-chip");
    chip?.classList.remove("bump"); void chip?.offsetWidth; chip?.classList.add("bump");
  }
}

async function refreshFooterBadges() {
  if (!state.playerId || document.hidden) return;
  try {
    // pollNotice (melbete-core.js) also shows an in-app banner for a fresh message; tapping it opens the inbox.
    const unread_count = await pollNotice(state.playerId, () => { window.location.href = "/app#profile"; });
    const b = document.getElementById("rf-badge-profile");
    if (b) { b.textContent = unread_count > 99 ? "99+" : String(unread_count); b.classList.toggle("hidden", !(unread_count > 0)); }
  } catch (e) { /* badge just stays as it was */ }
  try {
    const bonus = await api(`/api/bonus/summary/${encodeURIComponent(state.playerId)}`);
    const r = bonus.rewards || {};
    const n = ["welcome", "daily"].filter((k) => r[k] && r[k].status === "available").length;
    const pb = document.getElementById("rf-badge-promos");
    if (pb) { pb.textContent = String(n); pb.classList.toggle("hidden", !(n > 0)); }
    document.getElementById("room-cashback-dot")?.classList.toggle("hidden", !(n > 0));
    if (r.cashback && window.paintCashback) window.paintCashback(r.cashback.amount_etb);
  } catch (e) { /* badges just stay as they were */ }
  try {
    const res = await api(`/api/wallet/${state.playerId}/transactions?limit=10`);
    const latest = (res.transactions || [])
      .filter((x) => String(x.type).startsWith("top_up"))
      .sort((a, b) => new Date(b.created_at) - new Date(a.created_at))[0];
    const pending = !!latest && latest.status === "pending";
    document.getElementById("rf-dot-deposit")?.classList.toggle("hidden", !pending);
    document.getElementById("rf-deposit")?.classList.toggle("has-dot", pending);
  } catch (e) { /* dot just stays as it was */ }
}

function wireRoomFooter() {
  const go = (hash) => { window.location.href = `/app#${hash}`; };
  document.getElementById("rf-rooms")?.addEventListener("click", () => document.getElementById("rooms-btn")?.click());
  document.getElementById("rf-games")?.addEventListener("click", () => { window.location.href = "/app"; });
  document.getElementById("rf-deposit")?.addEventListener("click", () => go("deposit"));
  document.getElementById("rf-promos")?.addEventListener("click", () => go("promos"));
  document.getElementById("room-cashback-fab")?.addEventListener("click", () => go("cashback"));
  document.getElementById("rf-profile")?.addEventListener("click", () => go("profile"));
  document.getElementById("room-chip")?.addEventListener("click", () => go("profile"));
  refreshFooterBadges();
  setInterval(refreshFooterBadges, 30000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshFooterBadges(); });
}

async function refreshWallet() {
  try {
    const wallet = await api(`/api/wallet/${state.playerId}`);
    document.getElementById("balance-real").textContent = wallet.balance;
    updateRoomChip(wallet.balance);
    const bonusPill = document.getElementById("balance-bonus-pill");
    if (wallet.bonus_balance > 0) {
      bonusPill.classList.remove("hidden");
      document.getElementById("balance-bonus").textContent = wallet.bonus_balance;
    } else {
      bonusPill.classList.add("hidden");
    }
  } catch (e) { /* non-fatal - top bar just won't update this tick */ }
}

async function loadPrefs() {
  try {
    const prefs = await api(`/api/player/${state.playerId}`);
    state.autoBingo = true; // auto BINGO is always on for every player
  } catch (e) { /* ignore */ }
}

// ---------------------------------------------------------------------
// Lobby: cartela grid
//
// Built once per grid size, then updated in place on every subsequent
// render - never torn down and rebuilt. Rebuilding all ~250 buttons on
// every poll tick / websocket nudge was both slow (real, felt DOM churn
// several times a minute) and the direct cause of a visible flicker: a
// render using slightly-stale server data (because a pick/release the
// player just tapped hasn't landed yet) would wipe out that button's
// fresh optimistic styling and flash it back, only for it to flip again
// once the real response arrived a moment later. Updating in place fixes
// both: existing buttons are cheap to restyle, and a button flagged
// `pending` (an in-flight request the player is waiting on) is left
// alone by every render until that request's own handler resolves it.
// ---------------------------------------------------------------------
let cartelaGridButtons = null; // Map<number, HTMLButtonElement>

function buildCartelaGrid(maxNum) {
  const grid = document.getElementById("cartela-grid");
  grid.innerHTML = "";
  cartelaGridButtons = new Map();
  const frag = document.createDocumentFragment();
  for (let n = 1; n <= maxNum; n++) {
    const btn = document.createElement("button");
    btn.className = "cartela-cell";
    const num = document.createElement("span");
    num.className = "cn";
    num.textContent = n;
    const nm = document.createElement("span");
    nm.className = "cname";
    btn.append(num, nm);
    btn.addEventListener("click", () => onPickCartela(n, btn));
    frag.appendChild(btn);
    cartelaGridButtons.set(n, btn);
  }
  grid.appendChild(frag);
  buildCartelaTabs(maxNum);
}

// ---- Cartela list UI state: range tab, available-only, grid/list, panel theme ----
const CARTELA_BLOCK = 100;
const cartelaUi = (() => {
  const d = { range: -1, availOnly: false, view: "grid", dark: false };
  try {
    const saved = JSON.parse(localStorage.getItem("melbete_cartela_ui") || "{}");
    return { ...d, ...saved, range: -1 };
  } catch (e) { return d; }
})();
function saveCartelaUi() {
  try {
    const { availOnly, view, dark } = cartelaUi;
    localStorage.setItem("melbete_cartela_ui", JSON.stringify({ availOnly, view, dark }));
  } catch (e) { /* storage unavailable - preferences just won't persist */ }
}

function buildCartelaTabs(maxNum) {
  const el = document.getElementById("cartela-range-tabs");
  if (!el) return;
  el.innerHTML = "";
  if (maxNum <= CARTELA_BLOCK) { el.classList.add("hidden"); return; }
  el.classList.remove("hidden");
  const mk = (idx, label) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "cartela-tab";
    b.dataset.range = String(idx);
    b.innerHTML = `<span class="t-label">${label}</span><span class="t-free"></span>`;
    b.addEventListener("click", () => { cartelaUi.range = idx; filterCartelas(); });
    el.appendChild(b);
  };
  mk(-1, "All");
  for (let i = 0; i * CARTELA_BLOCK < maxNum; i++) {
    mk(i, `${i * CARTELA_BLOCK + 1}–${Math.min(maxNum, (i + 1) * CARTELA_BLOCK)}`);
  }
}

function applyCartelaUi() {
  const grid = document.getElementById("cartela-grid");
  grid?.classList.toggle("list-view", cartelaUi.view === "list");
  grid?.classList.toggle("panel-dark", cartelaUi.dark);
  const setOn = (id, on) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.classList.toggle("on", on);
    el.setAttribute("aria-pressed", on ? "true" : "false");
  };
  setOn("cartela-avail-toggle", cartelaUi.availOnly);
  setOn("cartela-view-grid", cartelaUi.view === "grid");
  setOn("cartela-view-list", cartelaUi.view === "list");
  const theme = document.getElementById("cartela-theme-toggle");
  if (theme) theme.textContent = cartelaUi.dark ? "☀️" : "🌙";
  // Keep the sticky range bar just under the (variable-height) top bar.
  const top = document.getElementById("top-bar");
  document.documentElement.style.setProperty("--topbar-h", `${top ? top.offsetHeight : 0}px`);
}

function filterCartelas() {
  const input = document.getElementById("cartela-search");
  const query = (input?.value || "").trim();
  const grid = document.getElementById("cartela-grid");
  const count = document.getElementById("cartela-picker-count");
  if (!grid) return;
  let visible = 0, available = 0, mine = 0, taken = 0;
  const takenSet = new Set(state.room?.taken_cartelas || []);
  const playerRows = state.room?.players || [];
  const takenBy = {};
  playerRows.forEach((p) => { takenBy[p.cartela_number] = p; });
  const blockFree = {};
  (cartelaGridButtons || new Map()).forEach((btn, n) => {
    const isMine = takenBy[n]?.id === state.playerId;
    const isTakenOther = !isMine && takenSet.has(n);
    const block = Math.floor((n - 1) / CARTELA_BLOCK);
    if (!isMine && !isTakenOther) blockFree[block] = (blockFree[block] || 0) + 1;
    const matches = (!query || String(n).includes(query))
      && (cartelaUi.range < 0 || block === cartelaUi.range)
      && (!cartelaUi.availOnly || !isTakenOther);
    btn.hidden = !matches;
    if (!matches) return;
    visible++;
    if (isMine) mine++;
    else if (isTakenOther) taken++;
    else available++;
  });
  document.querySelectorAll("#cartela-range-tabs .cartela-tab").forEach((tab) => {
    const idx = Number(tab.dataset.range);
    tab.classList.toggle("active", idx === cartelaUi.range);
    const free = idx < 0
      ? Object.values(blockFree).reduce((a, b) => a + b, 0)
      : (blockFree[idx] || 0);
    const badge = tab.querySelector(".t-free");
    if (badge) badge.textContent = String(free);
  });
  const filtered = query || cartelaUi.range >= 0 || cartelaUi.availOnly;
  if (count) {
    count.textContent = !visible
      ? "No cartelas match - try another range or clear the filters."
      : filtered
        ? `${visible} shown · ${available} available`
        : `${available} available · ${mine} selected by you · ${taken} taken`;
  }
  applyCartelaUi();
}

function onRandomCartela() {
  if (state.lobbyLocked) { toast("Round starting - selection locked", "error"); return; }
  const pool = [];
  (cartelaGridButtons || new Map()).forEach((btn, n) => {
    if (btn.hidden || btn.disabled) return;
    if (btn.classList.contains("taken") || btn.classList.contains("mine")) return;
    pool.push([n, btn]);
  });
  if (!pool.length) { toast("No free cartela to pick here", "error"); return; }
  const want = window.MelbeteFlow ? window.MelbeteFlow.choices() : 3;   // admin: Experience > Dice offers (0 = instant pick)
  const copy = pool.slice(), picks = [];
  while (picks.length < Math.max(1, want) && copy.length) picks.push(copy.splice(Math.floor(Math.random() * copy.length), 1)[0]);
  if (window.MelbeteFlow && want > 0) {
    window.MelbeteFlow.chooseRandom(picks.map((p) => p[0]), (num) => {
      const b = cartelaGridButtons && cartelaGridButtons.get(num);
      if (!b) return;
      b.scrollIntoView({ block: "center", behavior: "smooth" });
      onPickCartela(num, b);
    }, onRandomCartela);
    return;
  }
  const [n, btn] = picks[0];
  btn.scrollIntoView({ block: "center", behavior: "smooth" });
  onPickCartela(n, btn);
}

function renderCartelaGrid() {
  const maxNum = state.room?.cartela_max || state.room?.number_range_max || 500;
  if (!cartelaGridButtons || cartelaGridButtons.size !== maxNum) {
    buildCartelaGrid(maxNum);
  }

  const taken = new Set(state.room?.taken_cartelas || []);
  const players = state.room?.players || [];
  const takenBy = {};
  players.forEach((p) => (takenBy[p.cartela_number] = p));
  const atLimit = state.myCards.length >= state.cardsPerPlayer;

  cartelaGridButtons.forEach((btn, n) => {
    // An in-flight pick/release for this exact button owns it until its
    // own success/failure handler is done - never let a render in
    // between (using data from before that request landed) touch it.
    if (btn.dataset.pending === "1") return;

    const mine = takenBy[n]?.id === state.playerId;
    const isTaken = taken.has(n);
    btn.classList.toggle("mine", mine);
    btn.classList.toggle("taken", !mine && isTaken);
    btn.classList.toggle("free", !mine && !isTaken);
    const nameEl = btn.querySelector(".cname");
    if (nameEl) {
      nameEl.textContent = mine ? "You" : (isTaken ? (takenBy[n]?.name || "Taken") : "Free");
      nameEl.classList.toggle("is-free", !mine && !isTaken);
    }
    btn.disabled = state.lobbyLocked || (isTaken && !mine) || (!mine && atLimit);
    btn.title = state.lobbyLocked
      ? "Round starting - selection locked"
      : mine
      ? "Your cartela"
      : isTaken
        ? `Taken by ${takenBy[n]?.name || "another player"}`
        : atLimit
          ? `You already have ${state.cardsPerPlayer} cartelas this round`
          : "Tap to pick";
  });  filterCartelas();
}

/** Freezes/unfreezes all lobby activity (grid taps + "Remove" buttons) for
 * the final seconds of the countdown - see RoomService.LOBBY_LOCK_SECONDS.
 * The server rejects these calls independently either way; this just makes
 * the UI stop offering an action that's about to be refused. */
function setLobbyLocked(locked) {
  if (state.lobbyLocked === locked) return;
  state.lobbyLocked = locked;
  document.getElementById("cartela-grid")?.classList.toggle("locked", locked);
  document.getElementById("my-cartelas-preview")?.classList.toggle("locked", locked);
  document.getElementById("countdown-banner")?.classList.toggle("locked", locked);
  renderCartelaGrid();
  renderCartelaPreview();
}

function lobbyStatusText(room) {
  const min = room?.min_cartelas_to_start || 4;
  const taken = room?.taken_cartelas?.length || 0;
  if (room?.countdown_started_at) return `${taken} cartela(s) reserved - the round starts when the countdown ends.`;
  const need = Math.max(min - taken, 0);
  return `${taken} of ${min} cartelas reserved. Reserve ${need} more to start the ${room?.countdown_seconds || 30}s countdown.`;
}

function renderCartelaPreview() {
  const el = document.getElementById("my-cartelas-preview");
  const countEl = document.getElementById("my-cartelas-count");
  if (countEl) {
    countEl.textContent = state.myCards.length
      ? `Your cartelas: ${state.myCards.length}/${state.cardsPerPlayer}`
      : `Tap a cartela number to preview it - you can play 1 to ${state.cardsPerPlayer} cartelas.`;
  }
  if (!state.myCards.length) {
    el.classList.add("hidden");
    el.innerHTML = "";
    return;
  }
  el.classList.remove("hidden");
  el.innerHTML = "";
  state.myCards.forEach((card) => {
    el.appendChild(buildCardElement(card, { compact: true, showRelease: true }));
  });
  if (state.myCards.length < state.cardsPerPlayer) {
    const hint = document.createElement("div");
    hint.className = "add-cartela-hint";
    hint.textContent = `Optional: pick a ${ordinal(state.myCards.length + 1)} cartela below (up to ${state.cardsPerPlayer})`;
    el.appendChild(hint);
  }
}

function ordinal(n) {
  return n === 1 ? "1st" : n === 2 ? "2nd" : n === 3 ? "3rd" : `${n}th`;
}

/** Builds a rendered card: BINGO header + 5x5 grid, optionally compact
 * (lobby preview) or with a claim button (game view). Shared between the
 * lobby preview and the live game so a player always sees exactly what
 * their cartela looks like. */
function buildCardElement(card, { compact = false, showRelease = false, interactive = false } = {}) {
  const wrapper = document.createElement("div");
  wrapper.className = compact ? "preview-card" : "bingo-card-block";

  if (compact) {
    const title = document.createElement("div");
    title.className = "preview-card-title";
    const span = document.createElement("span");
    span.textContent = `Cartela #${card.cartela_number}`;
    title.appendChild(span);
    if (showRelease) {
      const btn = document.createElement("button");
      btn.className = "text-btn";
      btn.textContent = "Remove";
      btn.disabled = state.lobbyLocked;
      btn.title = state.lobbyLocked ? "Round starting - locked" : "";
      btn.addEventListener("click", () => onReleaseCartela(card.id));
      title.appendChild(btn);
    }
    wrapper.appendChild(title);
  } else {
    const title = document.createElement("div");
    title.className = "bingo-card-title";
    title.textContent = `Cartela #${card.cartela_number}`;
    wrapper.appendChild(title);
  }

  const header = document.createElement("div");
  header.className = "card-header-row";
  BINGO_LETTERS.forEach((letter, i) => {
    const cell = document.createElement("div");
    cell.className = `card-header-cell col-${i}`;
    cell.textContent = letter;
    header.appendChild(cell);
  });
  wrapper.appendChild(header);

  const table = document.createElement("div");
  table.className = "card-grid";
  card.grid.forEach((row) => {
    row.forEach((cell) => {
      const cellEl = document.createElement("div");
      cellEl.className = "card-cell";
      if (cell.free) cellEl.classList.add("free");
      if (cell.marked) cellEl.classList.add("marked");
      cellEl.textContent = cell.free ? "★" : cell.value;
      if (interactive) cellEl.addEventListener("click", () => toggleMark(card, cell, cellEl));
      table.appendChild(cellEl);
    });
  });
  wrapper.appendChild(table);

  if (interactive) {
    const btn = document.createElement("button");
    btn.className = "bingo-claim-btn";
    btn.textContent = "🎉 BINGO!";
    btn.addEventListener("click", () => onClaimBingo(card, false));
    wrapper.appendChild(btn);
  }

  return wrapper;
}

// Tap a free number -> reserved immediately (no confirmation step), then
// the reserved card is shown as a preview. Tapping one of your own
// cartelas shows the same preview with a Remove button.
async function onPickCartela(n, btn) {
  if (!btn || btn.disabled || state.lobbyLocked) return;
  let card = state.myCards.find((c) => c.cartela_number === n);
  if (!card) {
    card = await reserveCartela(n, btn);
    if (!card) return;
  }
  openCartelaSheet(card);
}

function openCartelaSheet(card) {
  const sheet = document.getElementById("cartela-preview-sheet");
  const holder = document.getElementById("cartela-sheet-card");
  const note = document.getElementById("cartela-sheet-note");
  const confirmBtn = document.getElementById("cartela-sheet-confirm");
  const cancelBtn = document.getElementById("cartela-sheet-cancel");
  const close = () => {
    sheet.classList.add("hidden");
    holder.innerHTML = "";
    document.removeEventListener("keydown", onKey);
  };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  const stake = state.room?.stake_amount ?? 0;
  holder.innerHTML = "";
  holder.appendChild(buildCardElement(card, { compact: true, showRelease: false }));
  note.textContent = `Cartela ${card.cartela_number} is reserved for you (${state.myCards.length}/${state.cardsPerPlayer}). ${stake} ETB is charged only when the round starts.`;
  confirmBtn.textContent = "\u2713 Keep this cartela";
  confirmBtn.disabled = false;
  confirmBtn.onclick = close;
  cancelBtn.textContent = "Remove";
  cancelBtn.disabled = state.lobbyLocked;
  cancelBtn.onclick = async () => {
    close();
    await onReleaseCartela(card.id);
  };
  document.getElementById("cartela-sheet-backdrop").onclick = close;
  document.addEventListener("keydown", onKey);
  sheet.classList.remove("hidden");
}

async function reserveCartela(n, btn) {
  // ── Optimistic UI & debounce ──────────────────────────────────────
  // Prevent double-taps: disable button immediately, mark as mine
  // optimistically so the user sees instant feedback. `pending` also
  // tells renderCartelaGrid() to leave this exact button alone until we
  // resolve it below - otherwise a poll tick landing mid-request could
  // flip it back using data from before this pick was confirmed.
  if (!btn) return null;
  if (btn.disabled) return null;
  btn.disabled = true;
  btn.dataset.pending = "1";
  btn.classList.add("mine", "pending");

  try {
    const result = await api(`/api/session/${state.room.id}/select-cartela`, {
      method: "POST",
      body: JSON.stringify({ player_id: state.playerId, cartela_number: n }),
    });
    state.myCards.push(result.card);
    state.room = { ...state.room, ...result.room };
    delete btn.dataset.pending;
    btn.classList.remove("pending");
    renderCartelaPreview();
    renderCartelaGrid();
    connectWebSocket();
    startPolling();
    return result.card;
  } catch (e) {
    // Revert optimistic state on failure
    delete btn.dataset.pending;
    btn.classList.remove("mine", "pending");
    btn.disabled = false;
    toast(e.message, "error");
    // Re-render grid to show accurate state
    renderCartelaGrid();
    return null;
  }
}

async function onReleaseCartela(cardId) {
  try {
    await api(`/api/session/${state.room.id}/release-cartela`, {
      method: "POST",
      body: JSON.stringify({ player_id: state.playerId, card_id: cardId }),
    });
    state.myCards = state.myCards.filter((c) => c.id !== cardId);
    renderCartelaPreview();
    await refreshCurrentRoom();
  } catch (e) {
    toast(e.message, "error");
  }
}

// ---------------------------------------------------------------------
// Session / game loop
//
// Single source of truth: `renderRoom(room)` is the ONLY place that
// decides what's on screen, driven purely by `room.state`
// ("waiting" | "running" | "ended"). Every entry point - first page load,
// the poll safety-net, a WebSocket "something changed" nudge, or the
// player clicking "Go to Lobby" - ends up calling this same function with
// freshly-fetched room data. There is no second, parallel implementation
// of "what does a winner look like" that could ever drift out of sync
// with what a reload would show.
// ---------------------------------------------------------------------

// First load (or after Telegram's WebView suspends/resumes the page):
// find whatever the current session is FOR THE CHOSEN TRACK, no matter
// its state. No-ops if the player hasn't picked a room yet (they're on
// the room-select screen - see loadTrackSelect()/selectTrack()).
async function bootstrapSession() {
  if (!state.track) return;
  const room = await api(`/api/session?track=${state.track}`);
  await renderRoom(room);
}

// ---------------------------------------------------------------------
// Room select - the first screen a player sees. Shows the single
// real-money room with live status pulled from /api/lobby, and
// lets the player switch rooms at any time via the header 🏠 button.
// ---------------------------------------------------------------------
function trackStateLabel(track) {
  if (track.state === "running") return "በቀጥታ · ጨዋታ በመካሄድ ላይ";
  if (track.state === "waiting") {
    return "በመጠባበቅ ላይ";
  }
  return "ጨዋታው አሁን አብቅቷል";
}

function trackActionLabel(track) {
  if (track.state === "running") return "ይመልከቱ";
  if (track.state === "ended") return "ውጤቱን ይመልከቱ";
  return "ይግቡ";
}

async function loadTrackSelect() {
  const container = document.getElementById("track-cards");
  let lobby;
  try {
    lobby = await api("/api/lobby");
  } catch (e) {
    container.innerHTML = "";
    const summary = document.getElementById("track-ready-summary");
    if (summary) summary.textContent = "የሎቢ ሁኔታን ማየት አልተቻለም";
    const err = document.createElement("p");
    err.className = "muted small";
    err.textContent = "ክፍሎችን መጫን አልተቻለም - እንደገና ለመሞከር ወደ ታች ይጎትቱ።";
    container.appendChild(err);
    return;
  }

  container.innerHTML = "";
  const readyCount = TRACK_ORDER.filter((trackId) => lobby[trackId]?.state === "waiting").length;
  const summary = document.getElementById("track-ready-summary");
  if (summary) {
    summary.textContent = readyCount > 0
      ? `${readyCount} ክፍሎች ለመግባት ዝግጁ`
      : "ቀጣዩን ዙር በመጠባበቅ ላይ";
  }
  TRACK_ORDER.forEach((trackId) => {
    const meta = TRACK_META[trackId];
    const track = lobby[trackId];
    if (!meta || !track) return;

    const chipLabel = "10 ብር · መደበኛ";

    const card = document.createElement("button");
    card.type = "button";
    card.className = `track-card track-card-${trackId} track-card-${meta.balanceKind} track-card-${track.state}`;
    card.setAttribute("aria-label", `${meta.title}፣ ${trackStateLabel(track)}፣ ${track.prize_pool ?? 0} ብር ሽልማት`);
    card.innerHTML = `
      <div class="track-card-top">
        <div class="track-card-header-left">
          <div class="track-card-icon" aria-hidden="true">${meta.icon || "🎲"}</div>
          <div class="track-card-titles">
            <div class="track-card-title-row">
              <h3>${meta.title}</h3>
              <span class="track-card-chip track-card-chip-${trackId}">${chipLabel}</span>
            </div>
            <span class="track-card-subtitle">${meta.subtitle}</span>
          </div>
        </div>
        <span class="track-card-state"><i aria-hidden="true"></i>${trackStateLabel(track)}</span>
      </div>
      <div class="track-card-stats">
        <div class="track-card-metrics">
          <span class="track-card-stat"><small>ሽልማት</small><strong class="prize-val">${track.prize_pool ?? 0} ብር</strong></span>
          <span class="track-card-stat"><small>${track.state === "running" ? "ተጫዋቾች" : "ተቀላቅለዋል"}</small><strong>${track.player_count ?? 0}</strong></span>
        </div>
        <span class="track-card-action">${trackActionLabel(track)} <span aria-hidden="true">›</span></span>
      </div>
    `;
    card.addEventListener("click", () => selectTrack(trackId));
    container.appendChild(card);
  });
}

async function selectTrack(trackId) {
  if (!TRACK_META[trackId]) return;
  state.track = trackId;
  state.room = null;
  updateRoomHeader(trackId);
  document.getElementById("rooms-btn")?.classList.remove("hidden");
  showView("loading-view");
  try {
    await bootstrapSession();
  } catch (e) {
    toast(e.message || "ክፍሉን መቀላቀል አልተቻለም - እባክዎ እንደገና ይሞክሩ።", "error");
    await switchToTrackSelect();
  }
}

async function switchToTrackSelect() {
  state.track = null;
  state.room = null;
  updateRoomHeader(null);
  state.ws?.close();
  state.ws = null;
  showView("track-select-view");
  await loadTrackSelect();
}

// Re-sync with the room we're already in - used by polling, and by
// WebSocket messages that signal "something changed, go look".
async function refreshCurrentRoom() {
  if (!state.track) return; // on the room-select screen, nothing to refresh
  if (state.room?.state === "ended") {
    // Personal results screen: once a round has ended, every player gets
    // their own copy of this screen and reads it at their own pace. It
    // must stay exactly as-is - regardless of another player clicking
    // their own "Go to Lobby" (which advances the *shared* session for
    // whoever looks next), and regardless of a new round already
    // starting in the background - until THIS player makes their own
    // explicit choice to leave (see goToLobby()). So: nothing to refresh
    // here while parked on results; just do nothing and wait for that click.
    return;
  }
  // Deliberately re-asks "what's the current global session" (the same
  // call bootstrapSession makes) rather than re-fetching a specific
  // captured room id - this app has exactly one shared session at a time
  // for anyone still actively in the lobby or a live round, so there's
  // never a good reason to ask about a specific id here.
  return bootstrapSession();
}

async function renderRoom(room) {

  const switchedRoom = state.room?.id !== room.id;
  state.room = room;
  state.cardsPerPlayer = room.cards_per_player || 4;

  if (switchedRoom) {
    // A genuinely different room than what was on screen (first load, or
    // just advanced past a finished one) - reset per-round UI state and
    // point the live channel at it.
    state.myCards = [];
    state.claimedCardIds = new Set();
    state.claimingCardIds = new Set();
    state.isLeavingResults = false;
    stopNextRoundCountdown();
    state.lastCalledCount = -1; // force a full called-numbers repaint on the new round
    setLobbyLocked(false);
    document.getElementById("winner-overlay").classList.add("hidden");
    connectWebSocket();
  }

  if (room.state === "waiting") {
    await renderLobby(room, switchedRoom);
  } else if (room.state === "running") {
    await renderRunning(room, switchedRoom);
  } else if (room.state === "ended") {
    await renderEnded(room, switchedRoom);
  }
}

async function renderLobby(room, switchedRoom) {
  showView("lobby-view");
  if (switchedRoom) {
    try {
      const cardsRes = await api(`/api/session/${room.id}/my-cards?player_id=${state.playerId}`);
      state.myCards = cardsRes.cards;
    } catch (e) {
      // Not fatal - the picker still works from taken_cartelas alone, and
      // the next successful poll/WS refresh will pick this back up. What
      // matters is that a transient failure here doesn't throw out of
      // renderRoom and leave the screen half-rendered.
    }
  }
  renderCartelaPreview();
  renderCartelaGrid();
  document.getElementById("lobby-status").textContent = lobbyStatusText(room);
  // Works whether or not the WebSocket `countdown_started` message ever
  // arrives - derived straight from the server timestamp.
  showCountdownFromServer();
}

async function renderRunning(room, switchedRoom) {
  if (switchedRoom || state.myCards.length === 0) {
    try {
      const cardsRes = await api(`/api/session/${room.id}/my-cards?player_id=${state.playerId}`);
      state.myCards = cardsRes.cards;
    } catch (e) {
      // Not fatal - worst case this falls through to the spectator view
      // below and recovers on the next successful refresh, instead of
      // throwing out of renderRoom and leaving the previous screen frozen
      // mid-update (the exact shape of "the game glitches mid-play").
    }
  }

  if (!state.myCards.length) {
    // Joined while a round was already running with no cartela this
    // round - show a rich spectator screen instead of the picker.
    renderDrawScreen(room, { watching: true });
    return;
  }

  renderDrawScreen(room, { watching: false });
  // Only re-render called numbers if new ones arrived — prevents the WS path
  // and the 2s poll path from fighting each other and causing double renders
  // or the "stuck / two numbers at once" glitch.
  const calledNow = (room.called_numbers || []).length;
  if (calledNow !== state.lastCalledCount) {
    renderCalledNumbers(room.called_numbers || []);
  }
  renderMyCards();
  // maybeAutoMark only ever marks a still-unmarked cell, so replaying the
  // full called-numbers list on every render is safe and idempotent -
  // no need to track "which numbers are new" separately.
  (room.called_numbers || []).forEach((n) => maybeAutoMark(n));
}

/** Renders a rich, informative screen for users who joined while a game was already running. */
function showWaitingScreen(room) {
  showView("waiting-view");

  const calledCount = (room.called_numbers || []).length;
  const playerCount = room.player_count ?? new Set((room.players || []).map((p) => p.id)).size;
  const prizePool   = room.prize_pool || 0;

  const numEl    = document.getElementById("wait-numbers-called");
  const prizeEl  = document.getElementById("wait-prize-pool");
  const playerEl = document.getElementById("wait-players");

  if (numEl)    numEl.textContent    = calledCount;
  if (prizeEl)  prizeEl.textContent  = `${prizePool} ETB`;
  if (playerEl) playerEl.textContent = playerCount;
}

async function renderEnded(room, switchedRoom) {
  if (switchedRoom || state.myCards.length === 0) {
    try {
      const cardsRes = await api(`/api/session/${room.id}/my-cards?player_id=${state.playerId}`);
      state.myCards = cardsRes.cards;
    } catch (e) { /* not fatal - the results screen can still fetch the winning card by id */ }
  }
  // Keep a real screen behind the results overlay (rather than a blank
  // view) - the game view still meaningfully shows the final prize pool
  // and called numbers for context.
  renderDrawScreen(room, { watching: !state.myCards.length });
  renderCalledNumbers(room.called_numbers || []);
  if (state.myCards.length) renderMyCards();

  await showResultsScreen(room);
  startNextRoundCountdown(room);
}

/** Shows the draw screen (stats strip, called board, ball) for a running/ended round.
 *  `watching` = true for someone who joined without a cartela: the panel under the ball
 *  shows "Watching Only"; otherwise it shows the player's own cartela(s). */
function renderDrawScreen(room, { watching }) {
  showView("game-view");
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
  const players = room.player_count ?? new Set((room.players || []).map((p) => p.id)).size;
  set("gv-game-id", String(room.id || "").slice(0, 10).toUpperCase() || "-");
  set("gv-players", players);
  set("gv-bet", room.stake_amount ?? 0);
  set("prize-pool", room.prize_pool ?? 0);
  set("gv-called", (room.called_numbers || []).filter(isValidBall).length);
  document.getElementById("gv-watch").classList.toggle("hidden", !watching);
  document.getElementById("my-cards-container").classList.toggle("hidden", watching);
  if (watching) document.getElementById("my-cards-container").innerHTML = "";
  buildMasterBoard();
  updateMasterBoard(room.called_numbers || []);
}

/** The 1-75 board: 15 rows x 5 columns (B I N G O), row r holds r, r+15, r+30, r+45, r+60. */
function buildMasterBoard() {
  const grid = document.getElementById("master-board");
  if (!grid || grid.childElementCount) return;
  const frag = document.createDocumentFragment();
  for (let row = 1; row <= BINGO_COLUMN_SIZE; row++) {
    for (let col = 0; col < BINGO_LETTERS.length; col++) {
      const n = row + col * BINGO_COLUMN_SIZE;
      const cell = document.createElement("div");
      cell.className = "gv-cell";
      cell.dataset.n = n;
      cell.textContent = n;
      frag.appendChild(cell);
    }
  }
  grid.appendChild(frag);
}

function updateMasterBoard(numbers) {
  const grid = document.getElementById("master-board");
  if (!grid) return;
  buildMasterBoard();
  const valid = (numbers || []).filter(isValidBall);
  const called = new Set(valid);
  const latest = valid.length ? valid[valid.length - 1] : null;
  grid.querySelectorAll(".gv-cell").forEach((cell) => {
    const n = Number(cell.dataset.n);
    cell.classList.toggle("called", called.has(n));
    cell.classList.toggle("latest", n === latest);
  });
}

function renderCalledNumbers(numbers) {
  const current = document.getElementById("current-call");
  const letterEl = document.getElementById("current-call-letter");
  const numberEl = document.getElementById("current-call-number");
  // Only ever show valid 1-75 balls in the "current call" display. A bogus
  // value (e.g. a cartela number) must never render as "O240".
  const valid = (numbers || []).filter(isValidBall);
  const latestNumber = valid.length ? valid[valid.length - 1] : null;
  const latestText = latestNumber != null ? String(latestNumber) : "-";
  if (numberEl.textContent !== latestText) {
    letterEl.textContent = latestNumber != null ? bingoLetterFor(latestNumber) : "";
    // The ball roll must always finish well before the next call: 40% of the draw interval, between 0.35s and 0.8s.
    const every = Number(state.room?.draw_interval) || 4;
    document.documentElement.style.setProperty("--melbete-roll", `${Math.round(Math.min(800, Math.max(350, every * 400)))}ms`);
    numberEl.textContent = latestText;
  }
  // Only rebuild the history chips if the count actually changed — avoids
  // unnecessary DOM churn on concurrent poll + WS renders of the same numbers.
  if (valid.length === state.lastCalledCount) return;
  state.lastCalledCount = valid.length;
  updateMasterBoard(valid);
  const history = document.getElementById("called-history");
  history.innerHTML = "";
  const frag = document.createDocumentFragment();
  valid.slice(-4).forEach((n) => {
    const span = document.createElement("span");
    span.className = `gv-chip col-${BINGO_LETTERS.indexOf(bingoLetterFor(n))}`;
    span.textContent = `${bingoLetterFor(n)}-${n}`;
    frag.appendChild(span);
  });
  history.appendChild(frag);
}

function renderMyCards() {
  const container = document.getElementById("my-cards-container");
  container.innerHTML = "";
  const n = state.myCards.length;
  // Drive the grid layout from a class instead of CSS :has() - :has() is
  // unsupported on older Telegram WebViews (pre-Chrome 105), which made the
  // 1-card case fail to center there. A class works everywhere.
  container.classList.remove("cards-1", "cards-2", "cards-3", "cards-4");
  if (n === 1) container.classList.add("cards-1");
  else if (n === 2) container.classList.add("cards-2");
  else if (n === 3) container.classList.add("cards-3");
  else if (n >= 4) container.classList.add("cards-4");
  state.myCards.forEach((card) => {
    container.appendChild(buildCardElement(card, { compact: false, interactive: false }));
  });
}

function toggleMark(card, cell, cellEl) {
  if (cell.free) return;
  const called = new Set(state.room.called_numbers || []);
  if (!called.has(cell.value)) {
    toast("That number hasn't been called yet.", "error");
    return;
  }
  cell.marked = !cell.marked;
  cellEl.classList.toggle("marked", cell.marked);
  maybeAutoClaim(card);
}

function maybeAutoClaim(card) {
  if (state.autoBingo && !state.claimedCardIds.has(card.id)) {
    onClaimBingo(card, true);
  }
}

async function onClaimBingo(card, silent = false) {
  // A card can be inspected from both the websocket and the polling render
  // before the first claim response returns.  Track the in-flight request as
  // well as accepted claims so automatic mode cannot submit duplicates.
  if (!card || state.claimedCardIds.has(card.id) || state.claimingCardIds.has(card.id)) return;
  state.claimingCardIds.add(card.id);
  try {
    const result = await api(`/api/session/${state.room.id}/claim`, {
      method: "POST",
      headers: { "Idempotency-Key": `claim-${card.id}` },
      body: JSON.stringify({ player_id: state.playerId, card_id: card.id }),
    });
    if (result.valid) {
      state.claimedCardIds.add(card.id);
      document.getElementById("game-message").textContent =
        `\u{1F389} BINGO on Cartela #${card.cartela_number}! You won ${result.payout} ETB!`;
      // Don't wait on the WebSocket broadcast or the next poll tick - the
      // round has already ended and the payout already landed by the time
      // this response came back, so go get the authoritative room and
      // show the results screen right now.
      await refreshCurrentRoom();
    } else if (!silent) {
      document.getElementById("game-message").textContent = `Not a valid bingo yet: ${result.message}`;
    }
  } catch (e) {
    if (!silent) toast(e.message, "error");
  } finally {
    state.claimingCardIds.delete(card.id);
  }
}

// ---------------------------------------------------------------------
// Polling fallback - the safety net that guarantees "must reload to see
// updates" can never happen again, independent of WebSocket health. Every
// tick just re-syncs with the room and re-renders through the exact same
// path a fresh page load would take, so there's no separate "polling
// logic" that could ever disagree with what a reload shows.
// ---------------------------------------------------------------------
function startPolling() {
  clearTimeout(state.pollTimer);
  // WebSocket delivery is the normal real-time path; this is the safety
  // net for a dropped/zombied Telegram socket (see connectWebSocket's
  // heartbeat). Now that the API and DB are co-located on Azure UAE North,
  // each poll is fast and cheap - we can run a tighter cadence without
  // straining the DB. During "running" every 2s is the upper bound on
  // auto-bingo latency for players with a dead socket; lobby/idle uses 5s.
  const scheduleNext = () => {
    const intervalMs = state.room?.state === "running" ? 2000 : 5000;
    state.pollTimer = setTimeout(async () => {
      if (!document.hidden) await refreshCurrentRoom().catch(() => {});
      scheduleNext();
    }, intervalMs);
  };
  scheduleNext();
}

// ---------------------------------------------------------------------
// WebSocket - fast-path real-time updates (polling above is the fallback)
// ---------------------------------------------------------------------
function connectWebSocket() {
  if (!state.room) return;
  if (state.ws && state.ws._roomId === state.room.id && state.ws.readyState === WebSocket.OPEN) return;
  state.ws?.close();
  clearInterval(state.wsHeartbeatTimer);

  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  // The browser WebSocket API cannot attach arbitrary headers. Send the
  // short-lived Telegram assertion as a base64url WebSocket subprotocol,
  // not in a query string where reverse proxies commonly log it.
  const initData = tg?.initData || "";
  const encodedInitData = btoa(unescape(encodeURIComponent(initData))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const ws = new WebSocket(`${proto}//${location.host}/ws/${state.room.id}`, [`tg.${encodedInitData}`]);
  ws._roomId = state.room.id;
  ws._lastMessageAt = Date.now();
  state.ws = ws;

  ws.onmessage = (evt) => {
    ws._lastMessageAt = Date.now();
    try {
      handleWsMessage(JSON.parse(evt.data));
    } catch (e) { /* ignore malformed frame */ }
  };
  ws.onopen = () => { window.MelbeteNet?.socket(true); };
  ws.onclose = () => {
    clearInterval(state.wsHeartbeatTimer);
    if (state.ws === ws) { window.MelbeteNet?.socket(false); setTimeout(() => connectWebSocket(), 2000); }
  };
  ws.onerror = () => ws.close();

  // A dropped connection reliably fires onclose and reconnects within 2s.
  // What it doesn't catch is a socket the OS/WebView silently stopped
  // delivering data on without ever closing it - this is common when a
  // Telegram Mini App gets backgrounded, and it's exactly the failure mode
  // that made auto-bingo look "slow": the socket still reports OPEN, so
  // nothing ever reconnects, and every draw only reaches that player via
  // the much slower polling fallback. Ping every 15s and, if nothing at
  // all (message or pong) has arrived in the 12s after that, treat the
  // connection as dead and force a reconnect - catching the problem in
  // well under half the old worst-case delay.
  state.wsHeartbeatTimer = setInterval(() => {
    if (ws.readyState !== WebSocket.OPEN) return;
    if (Date.now() - ws._lastMessageAt > 27000) {
      ws.close(); // triggers onclose -> reconnect above
      return;
    }
    try { ws.send(JSON.stringify({ type: "ping" })); } catch (e) { /* socket already dying, onclose will handle it */ }
  }, 15000);
}

// Telegram Mini Apps commonly suspend timers/sockets while backgrounded and
// resume without any clean disconnect event. Rather than wait out the
// heartbeat watchdog above, force an immediate resync + connection check
// the instant the app is visible again - this is the single biggest lever
// on "my auto-bingo claim was late" for anyone who had briefly switched
// away and back during a round.
document.addEventListener("visibilitychange", () => {
  if (document.hidden || !state.room) return;
  refreshCurrentRoom().catch(() => {});
  connectWebSocket();
});

// A transient failure here (a dropped connection mid-request, a slow
// response under load, momentarily-stale identity) must never surface as
// a silent unhandled rejection that can leave a render half-applied -
// that's indistinguishable from "the game glitched" to a player. Swallow
// it and let the next poll tick (now as fast as 4s during "running", see
// startPolling) or the next WebSocket push pick the state back up.
function safeRefreshRoom() {
  refreshCurrentRoom().catch(() => {});
}

function handleWsMessage(msg) {
  switch (msg.type) {
    case "player_joined":
    case "player_left":
      if (state.room) state.room.taken_cartelas = msg.taken_cartelas;
      if (document.getElementById("lobby-view").classList.contains("active")) {
        renderCartelaGrid();
        document.getElementById("lobby-status").textContent = lobbyStatusText(state.room);
      }
      break;
    case "countdown_started":
      // The stored server timestamp is authoritative. A delayed socket frame
      // must not make a 60s lobby visibly begin at 40s on the client.
      safeRefreshRoom();
      break;
    case "player_dropped":
      if (msg.player_id === state.playerId) {
        toast(msg.message, "error");
        state.myCards = [];
      }
      break;
    case "number_drawn":
      if (state.room && !state.room.called_numbers.includes(msg.number)) {
        if (state.room.called_numbers.length === 0) window.MelbeteCue?.firstNumber();
        state.room.called_numbers = [...state.room.called_numbers, msg.number];
        if (document.getElementById("game-view").classList.contains("active")) {
          renderCalledNumbers(state.room.called_numbers);
        }
        maybeAutoMark(msg.number);
      }
      break;
    // Anything that changes which SCREEN should be on display - a round
    // starting, being cancelled, someone winning, or ending with no
    // winner - goes through the exact same authoritative path a poll
    // tick or a page reload would take: re-fetch the room and render it
    // from scratch. There is no separate "fast path" rendering here that
    // could ever show something different from what a reload would.
    case "round_cancelled":
      toast(msg.message, "error");
      safeRefreshRoom();
      break;
    case "round_terminated":
      toast(msg.message || "This round was ended by an admin.", "error");
      safeRefreshRoom();
      break;
    case "game_started":
      window.MelbeteCue?.start();
      document.getElementById("countdown-banner").classList.add("hidden");
      safeRefreshRoom();
      break;
    case "winner_announced":
    case "round_ended":
      safeRefreshRoom();
      break;
    // Non-stop rounds: the server has started the next round by itself. A player still parked on the
    // results screen of the round that just finished is moved on to it (everyone else already follows
    // the normal poll / "game_started" path).
    case "new_round":
      if (state.room?.state === "ended" && state.room.id === msg.previous_room_id) autoLeaveResults();
      break;
  }
}

// Server-driven version of goToLobby(): the next round already exists, so just hide the results and render it.
async function autoLeaveResults() {
  if (state.isLeavingResults || state.room?.state !== "ended") return;
  state.isLeavingResults = true;
  stopNextRoundCountdown();
  try {
    const room = await api(`/api/session?track=${state.track}`);
    document.getElementById("winner-overlay").classList.add("hidden");
    await renderRoom(room);
  } catch (e) {
    state.isLeavingResults = false; // the player can still tap "Go to Lobby"
  }
}

// "Next round starts in Ns" under the results buttons.
let _nextRoundTimer = null;
function stopNextRoundCountdown() {
  if (_nextRoundTimer) { clearInterval(_nextRoundTimer); _nextRoundTimer = null; }
  document.getElementById("winner-next-round")?.classList.add("hidden");
}
function startNextRoundCountdown(room) {
  stopNextRoundCountdown();
  const total = Number(room.auto_next_round_seconds || 0);
  const el = document.getElementById("winner-next-round");
  if (!el || total <= 0) return;
  const serverNow = room.server_time ? new Date(room.server_time).getTime() : Date.now();
  const skew = Date.now() - serverNow;
  const endedAt = room.ended_at ? new Date(room.ended_at).getTime() + skew : Date.now();
  const tick = () => {
    const left = Math.max(0, Math.ceil((endedAt + total * 1000 - Date.now()) / 1000));
    el.textContent = left > 0 ? `Next round starts automatically in ${left}s` : "Starting the next round…";
    el.classList.remove("hidden");
  };
  tick();
  _nextRoundTimer = setInterval(tick, 1000);
}

// The results screen (winner, or a no-winner ending) - the single place
// that renders `winner-overlay`, always driven by the authoritative room
// object, never by a WebSocket payload directly. This is what guarantees
// the overlay looks the same whether it appeared via a push message, a
// poll tick, or the page being reloaded mid-round.
/** Results for a round with several winners: everyone shares the prize equally. */
async function showSharedResults(room) {
  const winners = room.winners;
  const mine = winners.find((w) => w.player_id === state.playerId);
  const nameOf = (w) => {
    if (w.player_id === state.playerId) return "You";
    const p = (room.players || []).find((pl) => pl.id === w.player_id);
    return p ? p.name : (w.player_name || "Player");
  };
  const each = winners[0].payout;
  document.getElementById("winner-name").textContent = `${winners.length} Winners!`;
  document.getElementById("winner-subtitle").textContent = winners.map((w) => `${nameOf(w)} (#${w.cartela_number})`).join(", ");

  const cardEl = document.getElementById("winner-card");
  cardEl.innerHTML = "";
  const shown = mine || winners[0];
  if (shown.grid) cardEl.appendChild(buildCardElement({ grid: shown.grid, cartela_number: shown.cartela_number }, { compact: true }));

  const payoutEl = document.getElementById("winner-payout");
  payoutEl.textContent = mine
    ? `You won ${each} ETB \u2013 the prize was shared equally by ${winners.length} winners.`
    : `The prize was shared equally: ${each} ETB each.`;
  payoutEl.classList.remove("hidden");
  if (mine) refreshWallet();
}

async function showResultsScreen(room) {
  document.getElementById("winner-overlay").classList.remove("hidden");
  prepareRematchButton();
  const winner = room.winners && room.winners.length ? room.winners[room.winners.length - 1] : null;

  if (!winner) {
    // The round ended with nobody winning (pool exhausted / everyone
    // disconnected too long). Every charged stake is refunded the
    // instant the round ends (see the backend's end_round_without_winner)
    // - say so plainly rather than leaving the player to wonder where
    // their stake went.
    document.getElementById("winner-name").textContent = "Round Over";
    document.getElementById("winner-subtitle").textContent =
      room.end_reason === "no_winner_pool_exhausted"
        ? "All 75 numbers were called and nobody got BINGO."
        : "This round ended with no winner.";
    document.getElementById("winner-card").innerHTML = "";
    const payoutEl = document.getElementById("winner-payout");
    payoutEl.textContent = "No winner this round - your stake has been fully refunded.";
    payoutEl.classList.remove("hidden");
    refreshWallet();
    return;
  }

  if (room.winners.length > 1) {
    await showSharedResults(room);
    return;
  }

  const isMe = winner.player_id === state.playerId;
  let playerName = "Player";
  if (isMe) {
    playerName = "You";
  } else {
    const p = (room.players || []).find((pl) => pl.id === winner.player_id);
    playerName = p ? p.name : (winner.player_name || "Player");
  }

  const patternMap = {
    "any_line": "a Line, Diagonal, or 4 Corners",
    "any_line_or_corners": "a Line, Diagonal, or 4 Corners",
    "horizontal_line": "a Horizontal Line",
    "vertical_line": "a Vertical Line",
    "diagonal": "a Diagonal Line",
    "four_corners": "4 Corners",
    "full_house": "a Full House",
  };
  const patternName = patternMap[winner.pattern] || (winner.pattern || "").replace(/_/g, " ");
  const patternText = patternName ? ` with ${patternName}` : "";

  document.getElementById("winner-name").textContent = playerName;
  document.getElementById("winner-subtitle").textContent = `got BINGO on Cartela #${winner.cartela_number}${patternText}!`;

  const cardEl = document.getElementById("winner-card");
  cardEl.innerHTML = "";
  // The winner record carries its own grid snapshot (with the winning
  // hits marked) straight from the backend - no separate fetch needed in
  // the normal case. Fall back to the dedicated card endpoint only if an
  // older room somehow lacks it.
  const grid = winner.grid || state.myCards.find((c) => c.cartela_number === winner.cartela_number)?.grid;
  if (grid) {
    cardEl.appendChild(buildCardElement({ grid, cartela_number: winner.cartela_number }, { compact: true }));
  } else if (winner.card_id) {
    try {
      const res = await api(`/api/session/${room.id}/card/${winner.card_id}`);
      if (res && res.grid) {
        cardEl.innerHTML = "";
        cardEl.appendChild(buildCardElement({ grid: res.grid, cartela_number: winner.cartela_number }, { compact: true }));
      }
    } catch (err) {
      console.error("Failed to fetch winner card:", err);
    }
  }

  // The payout was already credited to the wallet the instant the claim
  // was verified (see the backend's claim_bingo handler) - it's always
  // final and known by the time this renders, never estimated.
  const payoutEl = document.getElementById("winner-payout");
  payoutEl.textContent = isMe ? `You won ${winner.payout} ETB!` : `${playerName} won ${winner.payout} ETB`;
  payoutEl.classList.remove("hidden");

  if (isMe) refreshWallet();
}

// Manual close - the ONLY way the results screen ever goes away. There is
// no auto-dismiss timer anywhere: the player decides when they're done
// looking at who won, the card, and the payout breakdown. This is also
// the ONLY thing that ever moves the game on to a new round (see the
// backend's /advance endpoint) - never a timer, never automatic.
async function goToLobby() {
  if (state.isLeavingResults || state.room?.state !== "ended") return;
  state.isLeavingResults = true;
  const closeBtn = document.getElementById("winner-close-btn");
  const closeX = document.getElementById("winner-close-x");
  if (closeBtn) closeBtn.disabled = true;
  if (closeX) closeX.disabled = true;
  try {
    const res = await api(`/api/session/${state.room.id}/advance`, { method: "POST", body: JSON.stringify({ player_id: state.playerId }) });
    document.getElementById("winner-overlay").classList.add("hidden");
    await renderRoom(res.room);
  } catch (e) {
    toast(e.message || "Couldn't return to the lobby - please try again.", "error");
    state.isLeavingResults = false;
  } finally {
    if (closeBtn) closeBtn.disabled = false;
    if (closeX) closeX.disabled = false;
  }
}

// One-tap "Play again": the server moves on from this round and reserves the SAME cartela numbers the
// player just had, as far as they are still free and affordable (see POST /rematch).
function prepareRematchButton() {
  const btn = document.getElementById("winner-rematch-btn");
  if (!btn) return;
  const numbers = state.myCards.map((c) => c.cartela_number).sort((a, b) => a - b);
  state.rematchNumbers = numbers;
  const keep = !window.MelbeteFlow || window.MelbeteFlow.keepRematch();   // admin: Experience > Play again keeps cartelas
  btn.classList.toggle("hidden", numbers.length === 0 || !keep);
  btn.textContent = numbers.length ? `Play again (cartela ${numbers.map((n) => `#${n}`).join(", ")})` : "Play again";
}

async function playAgain() {
  if (state.isLeavingResults || state.room?.state !== "ended") return;
  state.isLeavingResults = true;
  const btn = document.getElementById("winner-rematch-btn");
  const closeBtn = document.getElementById("winner-close-btn");
  [btn, closeBtn].forEach((b) => { if (b) b.disabled = true; });
  try {
    const res = await api(`/api/session/${state.room.id}/rematch`, { method: "POST", body: JSON.stringify({ player_id: state.playerId }) });
    document.getElementById("winner-overlay").classList.add("hidden");
    await renderRoom(res.room);
    const reasons = { taken: "already taken", limit: "over your cartela limit", balance: "not enough balance", round_started: "the round already started" };
    if (res.reserved.length) toast(`Reserved ${res.reserved.map((n) => `#${n}`).join(", ")} for the next round`, "success");
    if (res.unavailable.length) {
      toast(res.unavailable.map((u) => `#${u.cartela_number} ${reasons[u.reason] || "unavailable"}`).join("; "), "error");
    }
  } catch (e) {
    toast(e.message || "Couldn't start the next round - please try again.", "error");
    state.isLeavingResults = false;
  } finally {
    [btn, closeBtn].forEach((b) => { if (b) b.disabled = false; });
  }
}

function maybeAutoMark(number) {
  // Auto BINGO is always on: every called number is marked on the player's
  // cartelas automatically and a completed card is claimed immediately.
  if (!state.myCards.length) return;
  let anyChanged = false;
  state.myCards.forEach((card) => {
    let changed = false;
    card.grid.forEach((row) =>
      row.forEach((cell) => {
        if (!cell.free && cell.value === number && !cell.marked) {
          cell.marked = true;
          changed = true;
        }
      })
    );
    if (changed) {
      anyChanged = true;
      maybeAutoClaim(card);
    }
  });
  if (anyChanged && document.getElementById("game-view").classList.contains("active")) {
    renderMyCards();
  }
}

// Count of DISTINCT human players (not cartelas) who have joined the round.
// A single user can hold several cartelas, so we must dedupe by player id -
// otherwise the banner would say "4 players joined" when it's really 1 person
// who picked 4 cartelas.
function uniquePlayerCount() {
  const players = state.room?.players || [];
  if (players.length) return new Set(players.map((p) => p.id)).size;
  // Fallback: count distinct owners implied by reserved cartelas isn't possible
  // from numbers alone, so if we have no player list just report cartela count.
  return state.room?.taken_cartelas?.length || 0;
}

function startCountdownBanner(seconds) {
  clearInterval(state.countdownInterval);
  clearTimeout(state.countdownRefreshTimer);
  setLobbyLocked(false);
  const lockThresholdMs = (state.room?.lobby_lock_seconds ?? 5) * 1000;
  const deadline = Date.now() + Math.max(Number(seconds) || 0, 0) * 1000;
  const banner = document.getElementById("countdown-banner");
  banner.classList.remove("hidden");
  document.getElementById("players-joined-count").textContent = state.room?.taken_cartelas?.length || 0;
  let lastCueSecond = null;
  const tick = () => {
    const remainingMs = Math.max(deadline - Date.now(), 0);
    // ceil avoids showing 0 before the actual server deadline.
    const secondsLeft = Math.ceil(remainingMs / 1000);
    document.getElementById("countdown-seconds").textContent = secondsLeft;
    // Soft tick (sound + haptic) for the last 5 seconds, once per second, so nobody misses the start.
    if (remainingMs > 0 && secondsLeft <= 5 && secondsLeft !== lastCueSecond) {
      lastCueSecond = secondsLeft;
      window.MelbeteCue?.tick(secondsLeft);
    }
    setLobbyLocked(remainingMs <= lockThresholdMs);
    if (remainingMs > 0) return;
    clearInterval(state.countdownInterval);
    // The server may still be charging several reserved cards. Re-sync now
    // and again shortly after, instead of leaving a stale 0-second lobby
    // visible until the normal multi-second polling interval fires.
    refreshCurrentRoom().catch(() => {});
    state.countdownRefreshTimer = setTimeout(() => refreshCurrentRoom().catch(() => {}), 500);
  };
  tick();
  state.countdownInterval = setInterval(tick, 250);
}

// Fallback so the countdown shows even if the WebSocket `countdown_started`
// message never reaches us (e.g. a web client where the socket dropped, or a
// slow reconnect). Derived purely from the server's `countdown_started_at`
// timestamp, which the API already exposes.
function showCountdownFromServer() {
  const room = state.room;
  if (room && room.state === "waiting" && !room.countdown_started_at) {
    // Not enough cartelas (or the countdown was reset) - no clock running.
    clearInterval(state.countdownInterval);
    clearTimeout(state.countdownRefreshTimer);
    document.getElementById("countdown-banner")?.classList.add("hidden");
    setLobbyLocked(false);
    return;
  }
  if (!room || room.state !== "waiting" || !room.countdown_started_at) return;
  const started = new Date(room.countdown_started_at).getTime();
  if (Number.isNaN(started)) return;
  const nowMs = room.server_time ? new Date(room.server_time).getTime() : Date.now();
  const elapsed = (nowMs - started) / 1000;
  const remaining = Math.max(room.countdown_seconds - elapsed, 0);
  startCountdownBanner(remaining);
}

// ---------------------------------------------------------------------
// Wire up
// ---------------------------------------------------------------------
function wireEvents() {
  wireRoomFooter();
  document.getElementById("cartela-search")?.addEventListener("input", filterCartelas);
  document.getElementById("cartela-avail-toggle")?.addEventListener("click", () => {
    cartelaUi.availOnly = !cartelaUi.availOnly; saveCartelaUi(); filterCartelas();
  });
  document.getElementById("cartela-random")?.addEventListener("click", onRandomCartela);
  document.getElementById("cartela-view-grid")?.addEventListener("click", () => {
    cartelaUi.view = "grid"; saveCartelaUi(); filterCartelas();
  });
  document.getElementById("cartela-view-list")?.addEventListener("click", () => {
    cartelaUi.view = "list"; saveCartelaUi(); filterCartelas();
  });
  document.getElementById("cartela-theme-toggle")?.addEventListener("click", () => {
    cartelaUi.dark = !cartelaUi.dark; saveCartelaUi(); filterCartelas();
  });
  window.addEventListener("resize", applyCartelaUi);
  applyCartelaUi();
  document.getElementById("cartela-search-clear")?.addEventListener("click", () => {
    const input = document.getElementById("cartela-search");
    if (input) { input.value = ""; input.focus(); }
    filterCartelas();
  });
  // Wallet/profile (deposit, withdraw, history, referral, Telegram linking)
  // lives only on the /app hub - see gameselect.js.
  document.getElementById("game-select-btn")?.addEventListener("click", () => {
    window.location.href = "/app";
  });

  document.getElementById("rooms-btn")?.addEventListener("click", () => {
    // Only block navigation if the user has active cards in a running game.
    // If they have no cards (e.g. they joined a running room as a spectator,
    // or just browsed in), let them go back freely.
    if (state.room?.state === "running" && state.myCards?.length > 0) {
      toast("ይህ ዙር ካለቀ በኋላ ክፍል መቀየር ይችላሉ።", "info");
      return;
    }
    switchToTrackSelect();
  });
  document.getElementById("winner-close-btn")?.addEventListener("click", goToLobby);
  document.getElementById("winner-rematch-btn")?.addEventListener("click", playAgain);
  document.getElementById("winner-close-x")?.addEventListener("click", goToLobby);
}

async function main() {
  try {
    initIdentity(state);
    wireEvents();
    document.getElementById("top-bar").classList.remove("hidden");
    await Promise.all([refreshWallet(), loadPrefs()]);
    await switchToTrackSelect();
    startPolling();
    setInterval(refreshWallet, 8000);
  } catch (e) {
    console.error(e);
    showLoadError(e);
  }
}

function showLoadError(e) {
  const view = document.getElementById("loading-view");
  view.innerHTML = "";

  const icon = document.createElement("p");
  icon.style.fontSize = "2.5rem";
  icon.textContent = "⚠️";

  const title = document.createElement("h3");
  title.style.marginBottom = "0.5rem";
  title.textContent = "Failed to load the game";

  const msg = document.createElement("p");
  msg.className = "muted small";
  msg.style.maxWidth = "320px";
  msg.style.textAlign = "center";
  msg.style.wordBreak = "break-word";
  msg.textContent = (e && e.message) || e;

  const btn = document.createElement("button");
  btn.id = "retry-btn";
  btn.className = "primary-btn";
  btn.style.marginTop = "1rem";
  btn.style.maxWidth = "200px";
  btn.textContent = "Retry";
  btn.addEventListener("click", () => location.reload());

  view.append(icon, title, msg, btn);
}

main();


// Promotional Bonus Center. Every claim is authenticated by Telegram initData;
// the server is authoritative for eligibility, dates, and budget caps.
async function openBonusCenter() {
  const modal = document.getElementById('bonus-modal');
  const rewards = document.getElementById('bonus-center-rewards');
  if (!modal || !state.playerId) return;
  modal.classList.remove('hidden');
  rewards.innerHTML = '<p class="muted">Loading rewards…</p>';
  try {
    const data = await api(`/api/bonus/summary/${encodeURIComponent(state.playerId)}`);
    document.getElementById('bonus-center-balance').textContent = data.bonus_balance ?? 0;
    const labels = {welcome:'Welcome bonus', referral:'Referral reward', daily:'Daily bonus', first_deposit:'First deposit reward', cashback:'Daily cashback'};
    rewards.innerHTML = Object.entries(data.rewards || {}).map(([kind, reward]) => {
      const automatic = kind === 'first_deposit' || kind === 'cashback';
      const claimable = !automatic && kind !== 'referral' && reward.enabled && reward.status !== 'claimed';
      const status = reward.status === 'claimed' ? 'Already claimed' : (reward.status === 'automatic' ? (kind === 'cashback' ? 'Paid automatically the next day if you deposit and do not withdraw that day' : 'Applied automatically to your first approved deposit') : (reward.enabled ? 'Available' : 'Paused'));
      const value = automatic ? `${Number(reward.amount_etb)}%` : `${Number(reward.amount_etb)} ETB`;
      return `<section class="bonus-reward-row" style="padding:12px 0;border-bottom:1px solid var(--border-color,#ddd)">
        <div style="display:flex;justify-content:space-between;gap:10px;align-items:center"><strong>${labels[kind] || kind}</strong><strong>${value}</strong></div>
        <p class="muted small">${status} · Non-withdrawable; Bingo cartelas only.</p>
        ${claimable ? `<button class="primary-btn bonus-claim-btn" data-reward="${kind}">Claim ${Number(reward.amount_etb)} ETB</button>` : ''}
      </section>`;
    }).join('') || '<p>No rewards configured.</p>';
    const ref = document.getElementById('bonus-center-referral');
    ref.textContent = data.referral_link ? `Referral link: ${data.referral_link} · Successful referrals: ${data.referrals?.successful || 0} · Pending: ${data.referrals?.pending || 0}` : `Successful referrals: ${data.referrals?.successful || 0} · Pending: ${data.referrals?.pending || 0}`;
    rewards.querySelectorAll('.bonus-claim-btn').forEach(btn => btn.addEventListener('click', async () => {
      btn.disabled = true;
      try {
        const key = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : `claim-${state.playerId}-${btn.dataset.reward}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
        const result = await api(`/api/bonus/${btn.dataset.reward}/claim/${encodeURIComponent(state.playerId)}`, {method:'POST', body:JSON.stringify({idempotency_key:key})});
        toast(result.status === 'granted' ? `Bonus credited: ${result.amount_etb} ETB` : 'Reward already processed', 'success');
        await openBonusCenter();
        await refreshWallet();
      } catch (e) { toast(typeof e.message === 'string' ? e.message : (e.message?.message || 'Unable to claim this reward'), 'error'); btn.disabled = false; }
    }));
    try {
      const hist = await api(`/api/bonus/history/${encodeURIComponent(state.playerId)}?limit=10`);
      document.getElementById('bonus-center-history').innerHTML = '<h4>Recent bonus activity</h4>' + ((hist.items || []).map(x => `<p class="muted small">${new Date(x.created_at).toLocaleString()} · ${x.entry_type} ${x.amount_etb} ETB · ${x.reward_type || 'bonus'}</p>`).join('') || '<p class="muted small">No bonus activity yet.</p>');
    } catch (_) { /* history is optional if unavailable */ }
  } catch (e) {
    rewards.textContent = typeof e.message === 'string' ? e.message : 'Bonus Center unavailable. Please try again later.';
  }
}
document.getElementById('bonus-center-btn')?.addEventListener('click', openBonusCenter);
