# Deployment Guide

Melbete Bingo has no local database to run - all game state lives in
Postgres, via Supabase, a fully-managed service. Deploying the app is
therefore just "run the Python process somewhere with the right
environment variables." This guide covers the Supabase setup (once) and
several hosting options.

## 1. One-time Supabase setup

1. Create a free project at [supabase.com](https://supabase.com/). Save
   the database password you set - you'll need it below.
2. Open **SQL Editor -> New query**, paste the entire contents of
   [`supabase/schema.sql`](supabase/schema.sql), and click **Run**. This
   creates every table the app needs.
3. Open **Project Settings -> Database -> Connection string -> URI**,
   and copy the **Transaction pooler** variant (host contains
   `pooler.supabase.com`, port `6543` - recommended for this app, which
   opens many short-lived connections per request). Replace
   `[YOUR-PASSWORD]` with your actual database password. This full URI
   is `DATABASE_URL`.

That's the entire database setup. No ongoing connection pool tuning, no
server to patch - Supabase manages the Postgres instance itself.

For the full walkthrough (including how to verify your setup actually
works before deploying, and how to migrate existing data from an older
Firestore-based deployment), see
[MIGRATING_TO_SUPABASE.md](MIGRATING_TO_SUPABASE.md).

### Verifying your database before deploying

```bash
DATABASE_URL="postgresql://...your connection string..." \
    python scripts/verify_supabase_repositories.py
DATABASE_URL="postgresql://...your connection string..." \
    python scripts/verify_supabase_e2e.py
```

Both should print all `[OK]` - see
[MIGRATING_TO_SUPABASE.md](MIGRATING_TO_SUPABASE.md#4-verify-it-works-before-deploying)
for details.

## 2. Get a Telegram bot token

Message [@BotFather](https://t.me/BotFather) on Telegram, run `/newbot`,
and follow the prompts. You'll get a token like
`123456789:AAExampleTokenValue`. That's `TELEGRAM_BOT_TOKEN`.

## 3. Choose a hosting platform

The app is a standard Python/FastAPI process (plus an optional
long-polling bot process); any platform that runs a Docker container or a
Python buildpack works. Three good, generous-free-tier options:

### Option A: Render (simplest)

1. Push this repo to GitHub.
2. In Render, **New -> Web Service**, connect the repo.
3. Environment: **Docker** (Render will use the included `Dockerfile`).
4. Add the environment variables from `.env.example` under
   **Environment**, using your real values (`DATABASE_URL` is your
   Supabase connection string - see [Section 1](#1-one-time-supabase-setup)).
5. Set `PUBLIC_BASE_URL` to the `.onrender.com` URL Render assigns you
   (or a custom domain once attached).
6. Deploy. Render gives you HTTPS automatically.
7. For the Telegram bot process, add a second service: **New -> Background
   Worker**, same repo/image, start command `python main.py bot`, same
   environment variables.

### Option B: Railway

1. **New Project -> Deploy from GitHub repo**.
2. Railway auto-detects the `Dockerfile`. Add the same environment
   variables under **Variables**.
3. Under **Settings -> Networking**, generate a public domain; use it as
   `PUBLIC_BASE_URL`.
4. Add a second service from the same repo with start command
   `python main.py bot` for the Telegram bot.

### Option C: Fly.io

```bash
fly launch --no-deploy        # generates fly.toml from the Dockerfile
fly secrets set \
  TELEGRAM_BOT_TOKEN=... \
  DATABASE_URL=... \
  SECRET_KEY=... \
  PUBLIC_BASE_URL=https://your-app.fly.dev
fly deploy
```
Run the bot as a second Fly "process group" (add a `[processes]` block in
`fly.toml` mapping `bot = "python main.py bot"` and `api = "python main.py
api"`), or run a second small VM with `fly machine run` using the bot
command.

### Option D: Google Cloud Run

```bash
gcloud run deploy melbete-bingo-api \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --set-env-vars TELEGRAM_BOT_TOKEN=...,DATABASE_URL=...,SECRET_KEY=...,PUBLIC_BASE_URL=https://melbete-bingo-api-xyz.a.run.app
```
Deploy the bot the same way with `--command python,--args main.py,bot`
and no public ingress needed (`--no-allow-unauthenticated`). Since
Supabase is reached over the public internet either way, there's no
Google-Cloud-specific credential wiring needed here anymore - just the
same `DATABASE_URL` as everywhere else.

## 4. Telegram webhook vs. polling

By default (`python main.py bot`) the bot uses long-polling, which needs
no public endpoint and works everywhere out of the box - this is the
simplest option and what the commands above assume. If you'd rather use
webhooks (lower latency, no idle polling connection), see the
[python-telegram-bot webhook docs](https://docs.python-telegram-bot.org/)
and point Telegram at `https://<PUBLIC_BASE_URL>/telegram/webhook`
(you'll need to add that route - it isn't included by default since
polling is sufficient for most deployments of this size).

## 5. Verifying the deployment

```bash
curl https://<your-domain>/health
# {"status": "healthy", "timestamp": "..."}
```

Then open your bot in Telegram and send `/start`. If the database
connection is wrong, the API will fail fast on startup - check your
platform's logs for a connection error or `RuntimeError: DATABASE_URL is
not set`, which almost always means `DATABASE_URL` is missing, mistyped,
or pointing at the wrong Supabase project. See
[MIGRATING_TO_SUPABASE.md#troubleshooting](MIGRATING_TO_SUPABASE.md#troubleshooting).

## 6. Updating a deployment

All platforms above redeploy automatically on `git push` to your
connected branch (Render/Railway) or on `fly deploy` / `gcloud run
deploy`. If a future update to this repo changes `supabase/schema.sql`
(e.g. a new column), re-run the updated file in your Supabase SQL Editor
before deploying the new code - it's additive and idempotent
(`CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS` throughout),
so this is safe to do without downtime.

## 7. Scaling notes

- **API/WebSocket service**: safe to run multiple instances behind a load
  balancer for HTTP traffic. WebSocket broadcasts are currently
  in-process (matching the original design), so players in the same room
  should be routed to the same instance for real-time updates - use
  sticky sessions, or scale to a single instance per region for small/
  medium deployments. A future enhancement would move broadcast to a
  pub/sub layer (e.g. Postgres `LISTEN`/`NOTIFY` or a dedicated queue) to
  support fully stateless horizontal scaling.
- **Bot process**: run exactly one long-polling instance (Telegram
  rejects concurrent polling connections for the same token). If you need
  multiple instances, switch to webhooks instead.
- **Database**: Supabase's connection pooler (see
  [Section 1](#1-one-time-supabase-setup)) is specifically designed to
  let many short-lived backend connections share a small number of real
  Postgres connections - this is what makes it safe to run several API
  instances without exhausting Postgres's own connection limit.

## 8. Cost

- Supabase's free tier (500MB database, no daily request quota - unlike
  Firestore's old hard daily read/write cutoff) is generous for
  casual/small-scale play; a single game room with a dozen players and a
  fast draw interval writes only a small JSON update per number called.
- Render/Railway/Fly.io all have free or low-cost tiers suitable for a
  hobby deployment; check current pricing on each platform before
  committing to production traffic.

---

**Security note:** never commit `.env` or any real credentials
(including your Supabase database password / `DATABASE_URL`) to version
control. `.gitignore` already excludes `.env`.

## 9. Automated deploys to Azure Container Instances via GitHub Actions

If you're running on Azure Container Instances (as opposed to
Render/Railway/Fly.io above), `.github/workflows/deploy.yml` automates
what would otherwise be manual `az container create` commands: every push
to `main` runs the test suite, builds the Docker image, pushes it to your
Azure Container Registry, and redeploys both the API and bot container
instances from it.

### One-time setup

**Create an Azure service principal for GitHub Actions** (don't reuse
your personal `az login` credentials):

```bash
az ad sp create-for-rbac \
  --name "bingo-github-actions" \
  --role contributor \
  --scopes /subscriptions/<YOUR_SUBSCRIPTION_ID>/resourceGroups/bingo-rg \
  --sdk-auth
```

(Get your subscription ID with `az account show --query id -o tsv`.)

This prints a JSON block - copy the whole thing, you'll paste it into a
GitHub secret below. Treat it like a password; it grants contributor
access to your `bingo-rg` resource group only.

**Add these repository secrets** (Settings -> Secrets and variables ->
Actions -> New repository secret):

| Secret name | Value |
|---|---|
| `AZURE_CREDENTIALS` | The entire JSON block from the command above; store it as a protected **production environment** secret. |
| `ACR_USERNAME` | `az acr credential show --name <your-acr> --query username -o tsv` |
| `ACR_PASSWORD` | `az acr credential show --name <your-acr> --query "passwords[0].value" -o tsv` |
| `TELEGRAM_BOT_TOKEN` | Your bot token from @BotFather |
| `TELEGRAM_BOT_USERNAME` | *(optional)* Your bot's `@username`, no `@`. Only used to build the one-tap deep link in the web app's account-linking flow; leave it empty and that flow still works, just with a manual `/link CODE` step instead of a button. |
| `DATABASE_URL` | Your Supabase Postgres connection string (Transaction pooler variant - see [Section 1](#1-one-time-supabase-setup)) |
| `SECRET_KEY` | Your app's secret key |
| `BOT_CONFIG_SECRET_KEY` | A separate random value used only to satisfy the bot's shared configuration; it must not equal `SECRET_KEY`. |
| `SUPER_ADMIN_TELEGRAM_ID` | Your Telegram numeric ID |
| `ADMIN_TELEGRAM_IDS` | Comma-separated admin Telegram IDs |
| `ADMIN_TOTP_SECRETS` | Per-admin Base32 TOTP seed mapping. Supplied only to the API container; desktop admin access is disabled. |

Create a GitHub Environment named **production**, place these values in its
Environment secrets, and require one or more trusted reviewers before a
deployment. The workflow references values only as `${{ secrets.NAME }}` and
does not echo them. All runtime secrets are passed with
`--secure-environment-variables`, which prevents them appearing in
`az container show`; failed-deploy diagnostics also intentionally do not copy
container logs into GitHub Actions.

Non-secret config (resource group name, ACR login server, container
names, region, your domain, game/deposit/withdrawal tuning values) is
hardcoded directly in `.github/workflows/deploy.yml` near the top and in
each `--environment-variables` block - edit those and push if you need to
change them, no need to touch Azure directly.

### Deploying

```bash
git push origin main
```

Watch it run under the repo's **Actions** tab: test -> build & push ->
deploy. You can also trigger it manually without a new commit from
**Actions -> Test, Build, and Deploy to Azure -> Run workflow**.

### How it handles updates

ACI doesn't reliably support swapping just the image on a running
container group, so (matching what manual deploys required) each run
deletes and recreates both container instances from the freshly-pushed
image. There's a brief gap - usually under a minute - while the new
container pulls and starts; the Mini App and bot both reconnect on their
own.

### Rollback

Revert the bad commit and push (redeploys the previous code), or re-run
an older successful workflow from the Actions tab ("Re-run all jobs") to
redeploy that exact commit again.

### Still manual

- First-time infrastructure (ACR, resource group, custom domain/DNS) -
  the pipeline only manages the two container instances, not the
  registry, resource group, or DNS records.
- Rotating secrets - update the GitHub secret; the workflow always reads
  the current value on the next run.
