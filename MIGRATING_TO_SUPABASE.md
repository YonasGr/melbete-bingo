# Migrating to / Setting up Supabase

This app's database is Postgres, hosted on [Supabase](https://supabase.com/).
This document covers two audiences:

- **Setting up fresh** - you've never run this app before, and just want
  a working database. Read [Fresh setup](#fresh-setup).
- **Migrating from the old Firestore version** - you were running an
  earlier version of this app on Cloud Firestore and want to move to
  Supabase. Read [Migrating from Firestore](#migrating-from-firestore).

Either way, start with [Why Supabase](#why-this-migration-happened) if
you're curious, or skip straight to [Fresh setup](#fresh-setup).

---

## Why this migration happened

The app originally ran on Cloud Firestore. Two concrete problems came up
in production:

1. **Firestore's free tier has a hard daily quota** (roughly 50,000 reads
   and 20,000 writes a day). Once real players started using the app
   concurrently, ordinary traffic - session polling, wallet checks, the
   bot's own lookups - hit that ceiling, and Firestore started rejecting
   *every* request with `429 Quota exceeded` until the quota reset at
   midnight Pacific time. From the outside this looked exactly like the
   whole app crashing, for both the website and the bot simultaneously,
   because both depend on the same database.
2. **Firestore's data model doesn't support "nested arrays"** - an array
   whose elements are themselves bare arrays. This app's bingo cards are
   naturally shaped as "rows of cells" (an array of arrays), which
   required an awkward flattening workaround just to store a winning
   card's grid. That workaround is gone entirely now - Postgres JSONB has
   no such restriction.

Supabase's free tier has no equivalent hard daily cutoff (it has storage
and bandwidth limits instead, which scale far more gracefully), it's
real schema-based Postgres (so a `psql` client, standard SQL tooling, and
anyone who already knows SQL can all work with the data directly), and
this app's query patterns (a handful of tables, simple lookups, no
complex joins) are a very natural fit for a relational database.

---

## Fresh setup

### 1. Create a Supabase project

Go to [supabase.com](https://supabase.com/), sign in, and click **New
project**. Pick any name/region; the free tier is enough to run this app
comfortably for a small-to-medium player base. Save the database password
you're asked to set - you'll need it for the connection string below.

### 2. Run the schema

1. In your project, open **SQL Editor** (left sidebar) -> **New query**.
2. Open [`supabase/schema.sql`](supabase/schema.sql) from this repo,
   copy its entire contents, and paste it into the query editor.
3. Click **Run**.

This creates all 12 tables the app needs (players, game_rooms, cards,
wallets, transactions, payment_methods, admins, system_settings,
referrals, claims, draw_logs, link_codes) with the indexes each
repository's actual query patterns need. It's idempotent - safe to run
again later (e.g. after pulling an update to this repo that changed the
schema) without duplicating anything or losing data, since every
statement is `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`.

### 3. Get your connection string

Open **Project Settings -> Database -> Connection string -> URI**.
You'll see two variants:

- **Transaction pooler** (recommended) - host contains
  `pooler.supabase.com`, port `6543`. Use this one. This app opens many
  short-lived connections (every request that touches the database), and
  the transaction pooler (PgBouncer under the hood) is exactly what
  Supabase provides for that pattern - it's also what makes this app
  work well on serverless/autoscaling hosts, where a new process
  spinning up shouldn't have to fight for a limited number of direct
  Postgres connections.
- **Direct connection** - port `5432`. Works fine too (this app has no
  features that specifically require the pooler), but doesn't scale as
  well under many concurrent instances. Use this only if you have a
  specific reason to (e.g. a migration tool that needs a direct
  connection).

Copy the URI, replace `[YOUR-PASSWORD]` with your actual database
password, and set it as `DATABASE_URL`:

```env
DATABASE_URL=postgresql://postgres.abcdefghijk:your-password@aws-0-us-east-1.pooler.supabase.com:6543/postgres
```

### 4. Verify it works before deploying

Two scripts in `scripts/` exercise the real database - not fakes, not
mocks - so you can confirm your setup is correct before pointing a real
deployment at it:

```bash
pip install -r requirements.txt

# Exercises every repository's actual SQL (51 checks)
DATABASE_URL="postgresql://...your connection string..." \
    python scripts/verify_supabase_repositories.py

# Plays one full bingo round through the real service layer:
# reserve a cartela, charge a stake, draw numbers, claim a win,
# verify the payout actually landed, advance to the next round.
DATABASE_URL="postgresql://...your connection string..." \
    python scripts/verify_supabase_e2e.py
```

Both print `[OK]`/`[FAIL]` per check and exit non-zero on any failure.
If everything prints `[OK]`, your database is ready.

> These scripts create real (throwaway) rows in your database. Safe to
> run against a fresh project; if you run them against a project that
> already has real player data, expect a handful of test rows (a player
> named "Test Player" / "E2E Player" and similar) to show up - harmless,
> but you may want to run them against a separate scratch project instead.

### 5. Set the rest of your environment variables

Continue with the normal setup in [README.md](README.md#3-configure-environment-variables)
- `TELEGRAM_BOT_TOKEN`, `SECRET_KEY`, etc. `DATABASE_URL` is a drop-in
  alongside those, nothing else about the setup flow changes.

### 6. Run the app

```bash
python -m src.api.main      # API server
python -m src.bot.telegram_bot   # bot (separate process)
```

On startup, the app automatically seeds `system_settings` (game
defaults) and bootstraps your super admin (from `SUPER_ADMIN_TELEGRAM_ID`)
and default payment methods - the same first-boot behavior as before,
just writing to Postgres instead of Firestore.

---

## Migrating from Firestore

If you were running an earlier version of this app on Firestore and have
real player data (wallets, transaction history, player records) you want
to keep, you have two options:

### Option A: Fresh start (recommended for most cases)

If your Firestore deployment was in early testing (few real users, or
you're fine with players re-registering / support handling a handful of
manual wallet adjustments), the simplest path is: follow
[Fresh setup](#fresh-setup) above with a brand new, empty Supabase
database, deploy the new code, and let the app rebuild its data
naturally as players use it again (`/start` re-registers a returning
player under the exact same `tg_<telegram_id>` id it always used, since
that id is deterministic - it's not tied to Firestore at all).

**This is what the maintainers of this repo actually did** - Firestore's
free-tier quota problems meant there was no ongoing production data worth
preserving at migration time. If that's also true for you, this is by
far the least error-prone path: there is no export/import step to get
subtly wrong.

### Option B: Export and import real data

If you do have production data worth preserving, the shape of the work
is:

1. Export each Firestore collection to JSON (Firebase console -> Firestore
   Database -> each collection -> "Export collection", or the `gcloud
   firestore export` CLI for a full-database export).
2. For each collection, write its rows into the matching Postgres table
   (see [`supabase/schema.sql`](supabase/schema.sql) - table names and
   column names match the old Firestore collection/field names
   one-to-one; see the table below).
3. Pay special attention to the `winners` field on `game_rooms` records
   from *before* this migration: those may have a `grid_flat` key (a
   flattened 25-entry array) instead of a normal nested `grid`. The app's
   `_reshape_winners` function (`src/api/main.py`) already handles both
   shapes transparently when reading, so you don't need to transform
   this during import - just copy it over as-is.
4. Firestore document IDs become the Postgres primary key (`id` column)
   for every table.

| Firestore collection | Postgres table    |
|-----------------------|--------------------|
| `players`             | `players`          |
| `game_rooms`          | `game_rooms`       |
| `cards`                | `cards`             |
| `wallets`              | `wallets`           |
| `transactions`         | `transactions`      |
| `payment_methods`      | `payment_methods`   |
| `admins`               | `admins`            |
| `system_settings`      | `system_settings`   |
| `referrals`            | `referrals`         |
| `claims`               | `claims`            |
| `draw_logs`            | `draw_logs`         |
| `link_codes`           | `link_codes`        |

There is no automated migration script for this in the repo (the
maintainers didn't need one - see Option A above) - if you need to do a
real data migration, a short one-off Python script using `asyncpg` to
insert each exported JSON row is the most direct approach, using the
column lists in each `src/repositories/*.py` file's `INSERT` statement
as your reference for exact column order.

### What changed in the code (for context)

If you're comparing against an old checkout: every file under
`src/repositories/` was rewritten against Postgres, `src/core/firebase.py`
was replaced by `src/core/db.py`, and `FIREBASE_PROJECT_ID` /
`FIREBASE_CREDENTIALS_FILE` / `FIREBASE_CREDENTIALS_BASE64` were replaced
by the single `DATABASE_URL` variable. **Nothing else changed** - the
service layer (`RoomService`, `WalletService`, `AdminService`), the API
routes, the bot, and the frontend are completely unmodified, because they
never talked to Firestore directly in the first place; only the
repository layer did.

---

## Troubleshooting

- **`RuntimeError: DATABASE_URL is not set`** - you haven't set the
  `DATABASE_URL` environment variable. See [step 3](#3-get-your-connection-string).
- **`asyncpg.exceptions.InvalidPasswordError`** - the password in your
  connection string doesn't match your project's database password.
  Reset it from **Project Settings -> Database -> Reset database
  password** if you're not sure what it is, and update `DATABASE_URL`.
- **Connection hangs or times out** - double check you copied the
  **pooler** connection string (contains `pooler.supabase.com`), not an
  internal-only hostname. Also confirm your network/firewall allows
  outbound connections on port `6543` (or `5432` for a direct
  connection).
- **`relation "players" does not exist`** (or any other table) - the
  schema hasn't been applied yet. Re-run [step 2](#2-run-the-schema).
- **Everything worked locally but fails when deployed** - make sure
  `DATABASE_URL` is set as an actual secret/env var on your hosting
  platform, not just in your local `.env` file (which never gets
  deployed). See [DEPLOYMENT.md](DEPLOYMENT.md) and
  `.github/workflows/deploy.yml` for where this repo's own CI/CD expects
  it.
