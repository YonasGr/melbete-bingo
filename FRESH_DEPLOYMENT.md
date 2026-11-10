# Melbete Bingo — Fresh Bingo-only deployment

This package is for a **new deployment with a clean PostgreSQL/Supabase database**. Only Bingo game code is included; the historical migrations for Aviator, Keno, and slots have been removed.

## 1. Create a clean database

1. Create a new Supabase project (or a new empty PostgreSQL database).
2. Open the Supabase SQL Editor.
3. Copy all of `supabase/schema.sql` into a new query and run it.
4. Confirm the query completes successfully. The schema is idempotent and does not create sample players, money balances, or transaction records.

Do not point this fresh install at an old database if you want a clean start. Creating a new project is safer than deleting tables from an existing project.

## 2. Configure backend secrets

Copy `.env.example` to `.env` for local development, or set the values as encrypted environment variables in your host. At minimum, configure:

- `DATABASE_URL`: connection string for the new database.
- `TELEGRAM_BOT_TOKEN`: token from BotFather.
- `SECRET_KEY`: a newly generated, long random secret.
- `PUBLIC_BASE_URL`: the deployed HTTPS URL.
- `SUPER_ADMIN_TELEGRAM_ID`: your Telegram numeric user ID. Several owners are allowed, separated by commas (`111111111,222222222`).
- `ADMIN_TOTP_SECRETS`: set up the required admin second-factor secret before using the admin panel.
- `APP_ENV=production` and `DEBUG=False` in production.

For a Supabase transaction-pooler connection, keep `DB_USE_PREPARED_STATEMENTS=false`. Never commit `.env` or real credentials to Git.

## 3. Deploy the backend

This application uses a persistent Python/FastAPI process, WebSockets/game loops, and a Telegram bot. Deploy the backend to a host that supports long-running Python services (for example, a suitable container host). Ordinary Vercel serverless functions are not a good fit for the long-running game and bot process without architectural changes.

Build and run using the included `Dockerfile`/deployment configuration, then set the environment variables in the hosting dashboard. Configure the Telegram webhook/polling approach consistently with the app's current bot setup.

## 4. First-start behavior

On first startup the app verifies database connectivity, creates the broadcast-progress status row if missing, and bootstraps the super-admin record when `SUPER_ADMIN_TELEGRAM_ID` is configured. The system settings row is created from application defaults when first requested. Payment methods must be configured in the admin panel; no payment account details are seeded.

## 5. Smoke test

- Confirm the backend health endpoint responds.
- Open the Bingo lobby from the public URL/Telegram Mini App.
- Verify a new player is created and receives a wallet row when needed.
- Test a Bingo round using non-production/test balances before accepting real deposits.
- Verify admin authentication, deposit approval, withdrawal handling, winner verification, and payout ledger entries.

## Database contents

The clean schema includes only the Bingo product's data model: players, game rooms, cards, wallets, transactions, payment methods, admins, settings, bot players, referrals, claims, draw logs, account-link codes, and broadcast progress. It contains no historical user data or real payment details.

## Bonus rewards

The fresh schema includes the promotional bonus tables and API. Welcome (20 ETB), referral (5 ETB), and daily (1 ETB) campaigns are deliberately disabled by default. Apply the fresh `supabase/schema.sql` to a new database, or apply `supabase/migrations/202610140001_bonus_rewards.sql` to an existing database. An authorized super-admin can manage campaigns at `/api/admin/bonus/campaigns` and pause/resume globally at `/api/admin/bonus/global`. Do not enable rewards until legal review, budget approval, and staging verification are complete. Bonus credit is non-withdrawable and cartelas-only.

## First-deposit promotional reward

A first-deposit reward campaign is available with a default percentage of 50% and is disabled by default. After an administrator approves a player's first deposit, the backend automatically credits 50% of that deposit to `wallets.bonus_balance` (rounded down to whole ETB), provided the global bonus switch and campaign are enabled and the campaign budget has room. The bonus is non-withdrawable and can only be used for Bingo cartelas. It is keyed to the approved deposit transaction to prevent duplicate grants.

For an existing Supabase database, apply `supabase/migrations/202610140002_first_deposit_reward.sql` after the earlier bonus-rewards migration. Then enable the `first_deposit` campaign and the global bonus switch in the admin bonus campaign controls. Test with a staging account first.
