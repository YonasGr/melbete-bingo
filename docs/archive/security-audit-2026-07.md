> **Archived - July 2026 report, written for the Firestore-era code.** Kept for history; do not follow it for a current install. See [docs/README.md](../README.md).

# Security Audit Report - Melbete Bingo

**Date:** 2026-07-16
**Scope:** Injection, auth bypass, secrets in code, unsafe dependencies
**Status:** Historical report — superseded by `INCIDENT_RESPONSE.md` on 2026-07-21.

> Warning: this report describes intended fixes, not a guarantee of the
> currently deployed source. Do not use it as a production-security sign-off.

---

## Critical Findings & Fixes

### 1. Exposed Firebase Service Account Credentials (CRITICAL)
**File:** `ethio-bingo-ba74c-firebase-adminsdk-fbsvc-59c69aaa10.json`

A Firebase Admin SDK service-account JSON with a **private key** was present in the
repository root. Anyone with repo access (or who found it via `.gitignore` leakage)
could impersonate the service account, read/write all Firestore data, and access
payment records, wallets, and admin lists.

**Fix:** File deleted from disk; `.gitignore` updated to block all
`*firebase-adminsdk*.json` and `firebase-service-account.json` files.

---

### 2. Vulnerable Dependencies (HIGH)
`requirements.txt` pinned packages with known CVEs:

| Package | Old | CVEs | Fixed |
|---------|-----|------|-------|
| fastapi | 0.109.0 | PYSEC-2024-38 (ReDoS via python-multipart) | 0.115.0 |
| starlette | 0.35.1 | PYSEC-2026-161/248/249/1943/1941/2281/2280 (Host-header injection → auth bypass, DoS, SSRF on Windows) | (via fastapi 0.115) |
| jinja2 | 3.1.3 | PYSEC-2026-1471/1472/1474/1475 (sandbox escape, XSS via `xmlattr`) | 3.1.6 |
| python-dotenv | 1.0.0 | PYSEC-2026-2270 (symlink following) | 1.0.1 |
| pytest / black / flake8 / mypy | old | various dev-only ReDoS / cache-poisoning | current |

**Fix:** All runtime + dev deps bumped to patched versions.

---

### 3. Overly Permissive CORS (HIGH)
**File:** `src/api/main.py`

```python
allow_origins=["*"]   # ANY website could call the API with the user's credentials
```

A malicious site could make authenticated requests on behalf of a logged-in user.

**Fix:** Restricted to `settings.public_base_url` (your deployed domain). Returns no
CORS headers when `PUBLIC_BASE_URL` is unset.

---

### 4. Bonus-Card Win Manipulation / Draw Rigging (HIGH - Fairness/Integrity)
**File:** `src/services/room_service.py` → `draw_number()`

The original code inspected the draw pool and **deliberately swapped the next ball**
if drawing it would make a *bonus-funded* card win BINGO while no *real-funded* card
would. This is server-side outcome manipulation — functionally equivalent to rigging
the game against bonus players.

**Fix:** Removed the swap logic. The draw pool is now consumed in its
cryptographically-shuffled order (see `DrawEngine.initialize_draw_pool`), identical
for every player regardless of funding source. Winnings still correctly credit to the
correct balance pool (`WalletService.credit_payout` respects `funded_by`).

---

### 5. Telegram initData Replay Window Too Long (MEDIUM)
**File:** `src/core/telegram_auth.py`

`MAX_INIT_DATA_AGE_SECONDS = 24 * 60 * 60` (24h). A captured `initData` string could be
replayed for up to a day to impersonate a user.

**Fix:** Reduced to `5 * 60` (5 minutes) per Telegram's own recommendation.

---

### 6. Unauthenticated WebSocket (MEDIUM)
**File:** `src/api/main.py` → `websocket_endpoint()`

Anyone could open `/ws/{room_id}` without verification, wasting connections.

**Fix:** Connection now rejected (code 4004) if the room does not exist. The channel
remains read-only (clients can only send `ping`); no server state is mutated via WS.

---

### 7. Screenshot Upload Validation (MEDIUM)
**File:** `src/api/main.py` → `_save_screenshot()`

Original code accepted any `image/*` MIME and did not strictly validate base64 or
decoded size.

**Fix:**
- Allowlist: `image/jpeg`, `image/png`, `image/webp` only.
- `base64.b64decode(..., validate=True)` rejects malformed input.
- Decoded-size guard (6 MB ceiling).
- Path-traversal guard on destination.

---

### 8. Missing Rate Limiting (MEDIUM)
**File:** `src/api/main.py`

No throttling on money-moving or admin endpoints → brute-force / abuse risk.

**Fix:** Added in-memory sliding-window middleware (100 req / 60s per IP) on `/api/*`
and `/admin*`. For multi-instance prod, swap for Redis-backed limiter.

---

### 9. Unbounded `limit` Query Params (LOW)
Endpoints like `/api/admin/users?limit=`, `/api/session/recent?limit=` had no upper
bound.

**Fix:** All `limit` params capped (users ≤500, games ≤200, transactions ≤100,
recent ≤50).

---

## Items Reviewed & Found SAFE

| Area | Result |
|------|--------|
| SQL/NoSQL injection | Firestore uses parameterized `.where()` / `.document()` — no string-built queries |
| Auth bypass | Admin + player endpoints require cryptographically-verified Telegram `initData` HMAC |
| Hardcoded secrets in code | None found — all secrets from env / `.env` (not committed) |
| Session fixation | No cookie/session token; identity = signed Telegram `initData` (unguessable) |
| Telegram bot token | Loaded from `settings.telegram_bot_token` (env), never hardcoded |
| Firebase Admin SDK | Creds from env/base64 only; `Application Default Credentials` fallback on GCP |
| `SECRET_KEY` config | Required field, no default — but currently unused (placeholder for future JWT/session) |

---

## Recommendations (Not Yet Implemented)

1. **Add a SECURITY.md** with a responsible-disclosure contact.
2. **Rotate the exposed Firebase key** at console.firebase.google.com if the JSON was
   ever committed/pushed to a remote.
3. **Move rate limiter to Redis** if running >1 API instance.
4. **Enable Firestore Security Rules** so the DB is not solely protected by app-level
   auth (defense in depth).
5. **Add CI dependency scanning** (pip-audit / Dependabot) to block future CVE regressions.
6. **Consider dropping `secret_key`** from config if it remains unused, or implement
   signed session tokens for the admin panel's plain-browser path.
