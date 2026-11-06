# 🎉 Melbete Bingo

A real-time multiplayer Bingo game built as a **Telegram Mini App**, with
a wallet in ETB, manually approved deposits and withdrawals, server-verified
claims, an auditable random number generator and an admin panel. All game state lives in **Postgres, via
Supabase** - setup is: create a free Supabase project, run one schema
file, get a Telegram bot token, set a few environment variables, run...

![Python](https://img.shields.io/badge/python-3.11+-blue.svg)
![FastAPI](https://img.shields.io/badge/FastAPI-0.109+-green.svg)
![Postgres](https://img.shields.io/badge/database-Postgres%20(Supabase)-336791.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)

**New to this repo?** Start here, then:
- [docs/deployment.md](docs/deployment.md) - database, secrets, Azure and other
  hosts, scaling, CI/CD (one guide)
- [docs/operations.md](docs/operations.md) - admin roles, audit log, daily
  reconciliation, CSV exports
- [docs/security.md](docs/security.md) - admin 2FA, rotating secrets
- [docs/architecture.md](docs/architecture.md) - code layout and the money model
- [docs/README.md](docs/README.md) - full documentation index
- [CONTRIBUTING.md](CONTRIBUTING.md) - contribution guidelines

## Contents

- [How it works](#how-it-works)
- [Project layout](#project-layout)
- [Prerequisites](#prerequisites)
- [Setup](#setup)
  - [1. Get a Telegram bot token](#1-get-a-telegram-bot-token)
  - [2. Create a Supabase project + run the schema](#2-create-a-supabase-project--run-the-schema)
  - [3. Configure environment variables](#3-configure-environment-variables)
  - [4. Install and run](#4-install-and-run)
- [Running with Docker](#running-with-docker)
- [Testing](#testing)
- [Deploying](#deploying)
- [Troubleshooting](#troubleshooting)

## How it works

- **Telegram bot** (`src/bot`) handles chat commands (`/start`, `/play`, `/balance`, `/topup`, `/help` and the
  admin approval buttons) and hands off to a **Mini App** - a small web app
  Telegram renders inside the chat.
- **FastAPI backend** (`src/api`) serves that Mini App and the admin panel,
  a REST API for wallet/card/claim management, and a WebSocket endpoint that
  broadcasts live game events (numbers drawn, players joining, claim results).
- **Game logic** (`src/services/game_service.py`) is pure, dependency-free
  Python: card generation, the seeded RNG draw engine, and pattern
  verification. It's unit-tested with no external services required.
- **Room lifecycle** (`src/services/room_service.py`) ties the game logic
  to persistence: create a room, join it, start it, draw numbers, verify
  claims.
- **Persistence** (`src/repositories`) is Postgres, via Supabase,
  accessed only through this layer - nothing else in the codebase talks
  to the database directly, so the game logic and room lifecycle can be
  tested without any live database (see [Testing](#testing)).

```
Telegram chat  ──►  Bot commands  ──┐
                                     ├──►  FastAPI (REST + WebSocket)
Telegram Mini App (browser) ────────┘            │
                                                   ▼
                                          RoomService (game rules)
                                                   │
                                                   ▼
                                   Repositories  ──►  Postgres (Supabase)
```

## Project layout

```
src/
├── api/main.py            FastAPI app: REST endpoints, WebSocket broadcast
├── bot/telegram_bot.py    Telegram bot commands and Mini App launch links
├── core/
│   ├── config.py          Environment-variable settings
│   └── db.py              Postgres connection pool bootstrap (asyncpg)
├── models/schemas.py      Pydantic data models (Player, GameRoom, Card, ...)
├── repositories/          Postgres read/write layer, one class per table
├── services/
│   ├── game_service.py    Card generation, RNG draw engine, pattern rules
│   └── room_service.py    Room lifecycle: create/join/start/draw/claim
├── static/                Mini App frontend (CSS/JS)
├── templates/index.html   Mini App HTML shell
└── tests/                 Unit tests
supabase/
└── schema.sql             Full Postgres schema - run once against a new
                            Supabase project (see docs/deployment.md)
```

## Prerequisites

- Python 3.11+
- A [Telegram bot token](https://t.me/botfather)
- A [Supabase](https://supabase.com/) project (free tier is enough to
  develop and test)
- (Optional) Docker, if you'd rather not manage a local virtualenv

## Setup

### 1. Get a Telegram bot token

Message [@BotFather](https://t.me/botfather), send `/newbot`, and follow
the prompts. Save the token it gives you, and note the bot's `@username`
too (shown right under its name in BotFather, and in the URL of its
chat, `t.me/<username>`) - you'll need both.

### 2. Create a Supabase project + run the schema

1. Create a free project at [supabase.com](https://supabase.com/).
2. Open **SQL Editor -> New query**, paste the entire contents of
   [`supabase/schema.sql`](supabase/schema.sql), and click **Run**. This
   creates every table this app needs - safe to re-run, it won't
   duplicate anything.
3. Open **Project Settings -> Database -> Connection string -> URI**,
   and copy the **Transaction pooler** version (host contains
   `pooler.supabase.com`, port `6543`) - that's your `DATABASE_URL`.

Full details, including why the pooler variant specifically and how to
verify your setup actually works before deploying, are in
[docs/deployment.md](docs/deployment.md#1-database).

### 3. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env`:

```env
TELEGRAM_BOT_TOKEN=123456789:AAExampleTokenValue
TELEGRAM_BOT_USERNAME=your_bot_username_without_the_at_sign
PUBLIC_BASE_URL=http://localhost:8000        # or your ngrok/deployed URL

DATABASE_URL=postgresql://postgres.xxxx:PASSWORD@aws-0-region.pooler.supabase.com:6543/postgres

SECRET_KEY=any-random-string-for-local-dev
```

### 4. Install and run

The included script automates the steps below (venv, install, run/test):
```bash
./quickstart.sh
```

Or manually:
```bash
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

# Run both the bot and API together:
python main.py

# ...or run them separately (useful for logs / restarting independently):
python main.py api     # FastAPI on http://localhost:8000
python main.py bot     # Telegram bot (long-polling)
```

Once the API is running, visit `http://localhost:8000/docs` for
interactive Swagger docs, or `http://localhost:8000/health` for a quick
liveness check.

**Testing the Mini App locally:** Telegram requires an HTTPS URL for Mini
Apps, so for local testing tunnel your API with something like
[ngrok](https://ngrok.com/) (`ngrok http 8000`) and set `PUBLIC_BASE_URL`
to the `https://*.ngrok.io` URL it gives you before starting the bot.

## Running with Docker

```bash
cp .env.example .env   # fill in real values first
docker compose up --build
```

This starts two containers - the API and the bot - both talking to your
real Supabase Postgres project (there are no local database containers to
manage, though `docker-compose.yml` also has a commented-out local
Postgres service if you'd rather develop fully offline). See
`docker-compose.yml` for details.

## Testing

```bash
pytest -v            # all tests
pytest --cov=src     # with coverage
```

- `src/tests/test_game_service.py` tests the pure game logic (card
  generation, RNG, pattern verification) - no I/O, no credentials needed.
- `src/tests/test_room_service.py` tests the full room lifecycle (create
  → join → start → draw → claim) against **in-memory fake repositories**,
  so it also runs with no database or credentials required.

If you want integration tests against real Postgres behavior, spin up a
local Postgres (or point at a scratch Supabase project), run
`supabase/schema.sql` against it, then use the scripts in `scripts/`:

```bash
DATABASE_URL="postgresql://postgres:postgres@localhost:5432/bingo_test" \
    python scripts/verify_supabase_repositories.py   # every repository, real SQL
DATABASE_URL="postgresql://postgres:postgres@localhost:5432/bingo_test" \
    python scripts/verify_supabase_e2e.py             # a full game round end-to-end
```

## Deploying

See [docs/deployment.md](docs/deployment.md) for the full guide: database setup,
Azure (one command, plus CI/CD), Render/Railway/Fly.io/Cloud Run, scaling and
backups. A fresh install runs `supabase/schema.sql` against a new empty database;
never apply historical migrations to it.

## Troubleshooting

- **`RuntimeError: DATABASE_URL is not set`** or a connection error on
  startup - `DATABASE_URL` is missing, wrong, or your Supabase project's
  connection pooler isn't reachable. The app fails fast on startup so
  this is easy to spot in the logs. See
  [docs/deployment.md](docs/deployment.md#5-first-start-and-smoke-test).
- **Mini App button does nothing** - `PUBLIC_BASE_URL` must be a real,
  reachable HTTPS URL (Telegram Mini Apps refuse plain `http://`, except
  when testing via `t.me` deep links in some clients - use ngrok locally).
- **Bot and Mini App disagree about a player or balance** - the API and bot
  must point at the *same* `DATABASE_URL`.

## License

MIT License - see [LICENSE](LICENSE).

