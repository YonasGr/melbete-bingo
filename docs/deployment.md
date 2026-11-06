# Deployment

One guide for installing and running Melbete Bingo. It replaces the old `DEPLOY_AZURE.md`, `FRESH_DEPLOYMENT.md`, `DEPLOYMENT.md` and `AZURE_STORAGE_SETUP.md`.

The app is one Docker image that runs as two processes: the **API** (FastAPI, WebSocket, the Mini App frontend and the draw loops) and the **bot** (Telegram long polling). Both talk to one Postgres database. Plain serverless hosts (e.g. Vercel functions) do not fit, because the game loops and the bot are long-running.

**Contents:** [1 Database](#1-database) · [2 Telegram bot and secrets](#2-telegram-bot-and-secrets) · [3 Azure (recommended)](#3-azure-container-apps-recommended) · [4 Other hosts](#4-other-hosts) · [5 First start and smoke test](#5-first-start-and-smoke-test) · [6 Scaling](#6-scaling) · [7 Updating, rollback, operations](#7-updating-rollback-and-operations) · [8 Promotional bonuses](#8-promotional-bonuses)

## 1. Database

Use a **new, empty** Postgres database (Supabase or any standard provider). Do not point a fresh install at an old database, and never run `schema.sql` as a "reset".

1. Create the project/database.
2. Run all of [`supabase/schema.sql`](../supabase/schema.sql) once (Supabase: SQL Editor, New query). It is idempotent and creates no players, balances, transactions or payment accounts.
3. Copy the connection string into `DATABASE_URL`. On Supabase use the **Transaction pooler** URI (host contains `pooler.supabase.com`, port `6543`) and set `DB_USE_PREPARED_STATEMENTS=false`.

Existing databases: apply the files in [`supabase/migrations/`](../supabase/migrations) that are newer than your last one, in filename order. `schema.sql` already contains everything in them, so fresh installs skip migrations. Note that `FRESH_DEPLOYMENT.md` used to mention `202610140001_bonus_rewards.sql`; that file is not in this repository (the bonus tables are in `schema.sql`), so an older database that predates bonuses must be brought up to date from `schema.sql` by a developer rather than from a migration.

Optional check before going live (needs `DATABASE_URL`):

```bash
python scripts/verify_supabase_repositories.py   # every repository's real SQL
python scripts/verify_supabase_e2e.py            # one full round end to end
```

## 2. Telegram bot and secrets

Create a bot with [@BotFather](https://t.me/BotFather) (`/newbot`) to get `TELEGRAM_BOT_TOKEN`. Copy `.env.example` to `.env` for local use, or set the same names as encrypted secrets in your host. Never commit `.env` or real credentials.

| Variable | Notes |
|---|---|
| `DATABASE_URL` | From step 1. |
| `TELEGRAM_BOT_TOKEN` | From BotFather. |
| `SECRET_KEY` | New, long, random. Signs admin sessions. |
| `PUBLIC_BASE_URL` | The public HTTPS URL (Mini Apps refuse plain `http://`). |
| `SUPER_ADMIN_TELEGRAM_ID` | Your numeric Telegram ID. Bootstrapped as super admin on startup. |
| `ADMIN_TOTP_SECRETS` | `ID:BASE32SEED,ID:BASE32SEED`. Without an entry nobody can log in to `/admin`. See [security.md](security.md#admin-access-and-two-factor-login). |
| `ADMIN_CHAT_ID` | Optional. Group that receives deposit alerts and the daily reconciliation report. |
| `BOT_CONFIG_SECRET_KEY` | Separate random value; must not equal `SECRET_KEY`. |
| `AZURE_STORAGE_CONNECTION_STRING` | Where deposit screenshots are stored (container `screenshots`). Provisioned automatically on Azure, see below. |
| `APP_ENV=production`, `DEBUG=False` | Always in production. |

Payment methods are configured in the admin panel; no payment account details are seeded.

The bot uses **long polling** by default, which needs no public endpoint. Run exactly **one** bot instance (Telegram rejects concurrent polling for one token). Webhooks are not implemented.

## 3. Azure Container Apps (recommended)

```
Telegram ──► Container App "api"  (HTTPS + WebSocket, 1 replica)  ─┐
             Container App "bot"  (long polling, 1 replica)        ├─► PostgreSQL
             Container Registry (image)                            ─┤   Blob Storage (screenshots)
             Log Analytics (logs)                                   ┘
```

Needs the Azure CLI and `python3` (and `psql`, recommended). Azure Cloud Shell has all three.

```bash
cp deploy.env.example deploy.env      # set TELEGRAM_BOT_TOKEN, SUPER_ADMIN_TELEGRAM_ID, ADMIN_TOTP_SECRETS
az login
./scripts/deploy_azure.sh
```

The script creates the resource group and infrastructure ([`infra/main.bicep`](../infra/main.bicep): registry, **private blob storage for screenshots**, optional Azure PostgreSQL, Log Analytics), builds the image in ACR, applies `schema.sql` if the database is empty, creates both apps and waits for `/health`. It prints the URL. Re-running it is safe and deploys the current code. `SECRET_KEY` and the Postgres password are generated on first run and saved to `deploy.env` (git-ignored; keep it).

To keep Supabase instead of Azure Postgres, put its transaction-pooler URL in `EXTERNAL_DATABASE_URL`; the script then skips creating a database and sets `DB_USE_PREPARED_STATEMENTS=false`.

After the first deploy: send `/start` to the bot; the Mini App is `<url>/app`, the admin panel `<url>/admin`. For a custom domain, add it on the `api` app, set `PUBLIC_BASE_URL` in `deploy.env`, and re-run the script so CORS and bot links use it.

**Own storage account instead of the Bicep one.** Create a StorageV2 account with public blob access disabled and a private container named `screenshots` (`az storage account create ... --allow-blob-public-access false`, then `az storage container create --name screenshots`), and give the app its connection string as `AZURE_STORAGE_CONNECTION_STRING`. Without it, screenshots would be lost on every redeploy.

### CI/CD (GitHub Actions)

`.github/workflows/deploy.yml` runs the tests, builds in ACR and updates both apps on every push to `main`, using passwordless OIDC login. Infrastructure changes still go through `deploy_azure.sh`; CI only ships new images.

```bash
SUB=$(az account show --query id -o tsv)
APP=$(az ad app create --display-name melbete-bingo-deploy --query appId -o tsv)
az ad sp create --id $APP
az role assignment create --assignee $APP --role Contributor \
  --scope /subscriptions/$SUB/resourceGroups/bingo-rg
az ad app federated-credential create --id $APP --parameters '{
  "name":"github-production","issuer":"https://token.actions.githubusercontent.com",
  "subject":"repo:<OWNER>/<REPO>:environment:production","audiences":["api://AzureADTokenExchange"]}'
```

On the GitHub `production` environment (require a trusted reviewer) add secrets `AZURE_CLIENT_ID` (= `$APP`), `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` and variables `AZURE_RESOURCE_GROUP` (`bingo-rg`), `NAME_PREFIX` (`melbetebingo`). Runtime secrets live as Container Apps secrets: change them in `deploy.env` and re-run the script.

> The old `DEPLOYMENT.md` section "Azure Container Instances via GitHub Actions" (service principal JSON in `AZURE_CREDENTIALS`, `ACR_USERNAME`/`ACR_PASSWORD`, delete-and-recreate containers) described an earlier pipeline. The current workflow is the OIDC / Container Apps one above, so that section was dropped.

## 4. Other hosts

Any host that runs a Docker image works. The same image is the API (`python main.py api`) and the bot (`python main.py bot`); give both the variables from section 2 and keep both at **one** instance.

- **Render:** New Web Service from the repo (Docker); add a second *Background Worker* with start command `python main.py bot`. Set `PUBLIC_BASE_URL` to the `.onrender.com` URL.
- **Railway:** Deploy from GitHub (it detects the `Dockerfile`), generate a public domain, add a second service with start command `python main.py bot`.
- **Fly.io:** `fly launch --no-deploy`, `fly secrets set TELEGRAM_BOT_TOKEN=... DATABASE_URL=... SECRET_KEY=... PUBLIC_BASE_URL=...`, `fly deploy`; run the bot as a second process group or machine.
- **Google Cloud Run:** `gcloud run deploy melbete-bingo-api --source . --allow-unauthenticated --set-env-vars ...`; deploy the bot the same way with command `python main.py bot` and no public ingress. Cloud Run scales to zero by default; the bot and game loops need an always-on instance (min instances 1).
- **AWS:** there is no AWS guide or infrastructure in this repository. The same image runs on ECS/Fargate or App Runner with the same rules (one API task, one bot task, secrets from Secrets Manager); nothing here has been tested on AWS.

## 5. First start and smoke test

On startup the app checks the database, creates the broadcast-progress row if missing, and bootstraps the super admin from `SUPER_ADMIN_TELEGRAM_ID`. `/health` is liveness only; `/ready` (database reachable) gates traffic.

1. `curl https://<domain>/health` returns `{"status": "healthy", ...}`.
2. Send `/start` to the bot and open the Mini App from Telegram.
3. Confirm a new player gets a wallet.
4. Log in to `/admin` (Telegram ID plus TOTP) and add payment methods.
5. Play a round with test balances before accepting real deposits. Verify deposit approval, withdrawal handling, winner verification and the ledger entries.

If startup fails with `DATABASE_URL is not set` or a connection error, the variable is missing, mistyped or points at the wrong project (the app fails fast on purpose).

## 6. Scaling

**Run exactly one API instance and one bot instance by default.** The live round, WebSocket connections, draw loops and engagement job run inside the API process. Two copies would double-draw; two bot copies would double-poll.

Rollouts are still safe: Container Apps starts the new revision before stopping the old one, and every background loop holds a short Postgres lease (`loop_locks`) so only one instance runs it. The old instance hands leases over on shutdown, the new one takes the round within about 5 seconds, and a crashed instance's lease expires after at most 30 seconds.

More than one API container is possible but **not yet load-tested**. Already safe: draw loops, lobby timers, bot filling, reconciliation, engagement and agent-rewards jobs (database leases), money endpoints (database idempotency keys and atomic SQL), admin sessions (signed cookies). Needed first:

1. **Redis for live messages.** Create Azure Cache for Redis (Basic C0 is enough) and deploy with `redisUrl` set to `rediss://:<key>@<host>:6380/0`. Without it only players on the container running the draw loop get live updates.
2. **Rate limits are per container** (`src/core/rate_limit.py` is in memory), so N containers allow up to N times the limit. Money endpoints stay protected by idempotency and atomic updates; this weakens abuse protection, not balances. Move it to Redis beyond 2 containers.
3. **Test it:** two phones on different containers both see every number; kill the container running the draw loop mid-round and confirm takeover within seconds; deploy during a lobby countdown and confirm the round starts once.
4. Then set `apiMaxReplicas` to 2 and redeploy. The bot stays at one replica.

## 7. Updating, rollback and operations

- **Update:** push to `main` (CI) or re-run `deploy_azure.sh`. If a release adds a migration, apply it before deploying the new code; migrations are additive and idempotent.
- **Rollback:** revert the commit and push, or re-run an older successful workflow run.
- **Logs:** `az containerapp logs show -g bingo-rg -n melbetebingo-api --follow` (or `-bot`).
- **Backups:** Azure PostgreSQL has 7-day point-in-time restore; its firewall allows Azure services only (plus your IP during schema setup). Supabase has its own backups.
- **Daily reconciliation:** 03:00 Ethiopia time a read-only job posts a wallet check to `ADMIN_CHAT_ID`; `RECONCILIATION_HOUR_EAT=-1` disables it. See [operations.md](operations.md).
- **Cost:** two small always-on replicas, Postgres B1ms, Basic ACR and storage; check the Azure pricing calculator. Supabase's free tier is enough for light use.

## 8. Promotional bonuses

The schema includes the bonus tables and API. The welcome (20 ETB), referral (5 ETB) and daily (1 ETB) campaigns, and the first-deposit reward (50% of the first approved deposit, rounded down), are **disabled by default**. A super admin manages campaigns at `/api/admin/bonus/campaigns` and the global switch at `/api/admin/bonus/global`. Bonus credit is non-withdrawable and usable only for cartelas. Do not enable rewards until legal review, budget approval and a staging test are done. For an existing database, `202610140002_first_deposit_reward.sql` adds the first-deposit reward.
