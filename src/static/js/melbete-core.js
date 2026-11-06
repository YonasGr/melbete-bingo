// Melbete - shared client core.
//
// The one place for Telegram bootstrap, identity, the authenticated fetch
// wrapper, toasts and idempotency keys. Loaded before app.js (game room,
// bingo.html) and gameselect.js (hub + wallet, index.html) so the two pages
// can no longer drift apart. These are plain-script globals: do NOT redeclare
// tg / API / api / toast / idemKey / initIdentity in page scripts.

const tg = window.Telegram?.WebApp;
tg?.ready();
tg?.expand();

const API = "";

// Fills the page's `state` (playerId, telegramId, displayName) from Telegram's
// signed identity. Throws outside Telegram - there is no browser-only login.
function initIdentity(target) {
  const user = tg?.initDataUnsafe?.user;
  if (user) {
    target.telegramId = String(user.id);
    target.playerId = `tg_${user.id}`;
    target.displayName = [user.first_name, user.last_name].filter(Boolean).join(" ") || user.username || "Player";
    return;
  }
  throw new Error("For your security, this game must be opened from the official Telegram bot.");
}

function toast(msg, kind = "info") {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.className = `toast ${kind}`;
  el.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), 3500);
}

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  // Telegram's own signed initData string is the only accepted player
  // credential; long-lived browser bearer tokens are disabled.
  if (tg?.initData) headers["X-Telegram-Init-Data"] = tg.initData;
  const res = await fetch(`${API}${path}`, { ...options, headers });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.detail || `Request failed (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return data;
}

// One Idempotency-Key per user action, reused if the same action is retried (double tap,
// flaky network) so the server never moves money twice. Cleared once the action succeeds
// or is cleanly rejected, so the next attempt gets a fresh key.
const _idemKeys = {};
function idemKey(scope) {
  if (!_idemKeys[scope]) _idemKeys[scope] = `${scope}-${crypto.randomUUID()}`;
  return _idemKeys[scope];
}
function clearIdemKey(scope) { delete _idemKeys[scope]; }

// A 403 from an identity-checked endpoint means the server rejected this
// session's Telegram initData (missing/stale/invalid) - the one case
// where "please reopen the app" is the actually-correct, actionable
// message, rather than a generic network-error toast.
function isIdentityError(e) {
  return e && e.status === 403;
}

// ---- In-app notification banner ------------------------------------------------------------
// The server stores every player notification (inbox) and sends it to Telegram. This adds the third
// leg: a banner while the Mini App is open. pollNotice() is called by each page's existing unread-count
// poll; it shows a banner only when the unread count went UP since the previous poll (so opening the app
// never replays old messages) and the newest message is still fresh. Time-sensitive "round starting"
// messages expire after a minute, everything else after 15.
const _notice = { last: null, shownId: null, timer: null };
const NOTICE_MAX_AGE_S = { room_filling: 60 };

function showNotice(n, onTap) {
  let el = document.getElementById("notice-banner");
  if (!el) {
    el = document.createElement("button");
    el.id = "notice-banner";
    el.type = "button";
    el.className = "notice-banner hidden";
    el.setAttribute("role", "status");
    document.body.appendChild(el);
  }
  el.innerHTML = "";
  const title = document.createElement("strong");
  title.textContent = n.title || "Notification";
  const body = document.createElement("span");
  body.textContent = n.body || "";
  el.append(title, body);
  el.onclick = () => { el.classList.add("hidden"); if (onTap) onTap(n); };
  el.classList.remove("hidden");
  clearTimeout(_notice.timer);
  _notice.timer = setTimeout(() => el.classList.add("hidden"), 8000);
}

async function pollNotice(playerId, onTap) {
  const q = encodeURIComponent(playerId);
  const { unread_count } = await api(`/api/notifications/unread-count?player_id=${q}`);
  const prev = _notice.last;
  _notice.last = unread_count;
  if (prev !== null && unread_count > prev && !document.hidden) {
    try {
      const { notifications } = await api(`/api/notifications?player_id=${q}&limit=1`);
      const n = notifications && notifications[0];
      const age = n ? (Date.now() - new Date(n.created_at).getTime()) / 1000 : Infinity;
      if (n && !n.read && n.id !== _notice.shownId && age <= (NOTICE_MAX_AGE_S[n.type] || 900)) {
        _notice.shownId = n.id;
        showNotice(n, onTap);
      }
    } catch (e) { /* the badge still updates; a missing banner is harmless */ }
  }
  return unread_count;
}
