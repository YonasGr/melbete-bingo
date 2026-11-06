# Architecture

Melbete Bingo is a real-time Bingo game delivered as a Telegram Mini App. Players hold a wallet in ETB, buy cartelas for a round, and winners are paid from the pot. Deposits and withdrawals are manual: admins approve them against Telebirr/bank records.

## Processes

```
Telegram chat ──► Bot (python main.py bot, long polling) ─┐
Telegram Mini App (browser) ──► FastAPI (python main.py api) ──┤──► PostgreSQL (Supabase / Azure)
Admin panel (/admin) ──────────► FastAPI                    ─┘     + Blob storage (deposit screenshots)
```

`python main.py` with no argument runs both. Only the API serves the Mini App (`/app`), the admin panel (`/admin`), REST, and the WebSocket. Running instances: one API and one bot (see [deployment.md](deployment.md#6-scaling)).

## Code layout

```
main.py                 entry point: bot | api | both
src/
  api/main.py           FastAPI app, lifespan (background loops), /health, /ready
  api/routers/          bingo, wallet, auth, bonus, missions, leaderboard, progress,
                        notifications, agent, admin, control (admin Control Center)
  bot/telegram_bot.py   bot commands, deposit/withdrawal approval buttons
  core/                 config (env settings), db (asyncpg pool), loop_lock (Postgres leases),
                        rate_limit, admin_auth (TOTP + session cookie)
  models/schemas.py     Pydantic models
  repositories/         the only code that talks to the database, one class per area
  services/             game rules (game_service), rounds (room_service), money (wallet_service),
                        reconciliation, finance_export, bonuses, agents, notifications, control...
  static/, templates/   Mini App and admin frontend (plain JS/CSS)
  tests/                unit tests (in-memory fakes) and integration tests (real Postgres)
supabase/schema.sql     full schema for a fresh database; migrations/ for upgrades
infra/main.bicep        Azure infrastructure; scripts/deploy_azure.sh deploys it
```

Layers only call downwards: routers and bot handlers call services, services call repositories. This is why most tests run against in-memory fakes with no database.

## Frontend scripts

Shared browser plumbing (Telegram bootstrap, `api()`, `toast()`, idempotency keys, identity) lives in `static/js/melbete-core.js`, loaded first by both pages. `app.js` is the game room (`bingo.html`); `gameselect.js` is the hub with the whole wallet UI (`index.html`) and is the only wallet implementation. `admin.js` and `agent.js` are separate pages with their own small helpers.

## Money model

A wallet has `balance` (real money, playable), `won_balance` (the withdrawable part; invariant `0 <= won_balance <= balance`) and `bonus_balance` (cartelas only, never withdrawable). Deposited money is playable but not withdrawable until it has been won. Every change is a row in `transactions`; deposits and withdrawals change `type` in place from `*_pending` to `*_approved` or `*_rejected` with `admin_id` and `resolved_at`. Approvals are atomic and idempotent (row locks and idempotency keys), so a double click or retry cannot credit twice. Withdrawal funds are held when requested; rejecting refunds them exactly once.

## Fairness

Card values and draw order use cryptographic randomness; the draw shuffle is seeded and the seed is stored. Each round's `draw_logs` row records the sequence and is finalised with a SHA-256 hash, so a finished round can be re-verified. Claims are verified on the server against its own called numbers, never the client's. When several players complete on the same last number, the pot is split.

## Background jobs and leases

Draw loops, lobby countdowns, bot filling, the engagement job, the agent-rewards job and the 03:00 reconciliation each take a short Postgres lease (`loop_locks`), so a rolling deploy never runs one twice.

## Live updates

WebSocket broadcasts are in-process; set `REDIS_URL` before running more than one API instance.

## Tests

`pytest -v` runs everything; the integration tests in `src/tests/integration` create a throwaway schema in a real Postgres (see `conftest.py`). `scripts/verify_supabase_*.py` check a deployed database.

## Documentation

See [README.md](README.md) in this folder for the index.
