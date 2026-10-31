# Deploying Melbete Bingo on Azure

One Docker image serves everything (FastAPI API + WebSocket + the Mini App frontend); a second app runs the same image as the Telegram bot.

```
Telegram ──► Container App "api"  (HTTPS + WebSocket, 1 replica)  ─┐
             Container App "bot"  (long polling, 1 replica)        ├─► Azure Database for PostgreSQL
             Azure Container Registry (image)                      ─┤   Blob Storage (deposit screenshots)
             Log Analytics (logs)                                   ┘
```

## Deploy

Needs the Azure CLI, `python3`, and (optional but recommended) `psql`. Azure Cloud Shell has all three.

```bash
cp deploy.env.example deploy.env      # set TELEGRAM_BOT_TOKEN, SUPER_ADMIN_TELEGRAM_ID, ADMIN_TOTP_SECRETS
az login
./scripts/deploy_azure.sh
```

The script creates the resource group and infrastructure (`infra/main.bicep`), builds the image in ACR, applies `supabase/schema.sql` if the database is empty, creates the two apps, and waits for `/health`. It prints the URL at the end. Re-running it is safe and deploys the current code. `SECRET_KEY` and the Postgres password are generated on first run and saved to `deploy.env` (git-ignored); keep that file.

Keep Supabase instead of Azure Postgres? Put its transaction-pooler URL in `EXTERNAL_DATABASE_URL`. The script then skips creating a database and sets `DB_USE_PREPARED_STATEMENTS=false`.

## Why exactly one replica

The live round, WebSocket connections, draw loops and the engagement job run inside the API process, and the bot uses long polling. Two copies would double-draw and double-poll. `minReplicas = maxReplicas = 1` is deliberate. To scale out later, that state has to move out of process first (Redis or Postgres pub/sub plus a leader lock).

**Rollouts are safe at any time.** Container Apps starts the new revision before stopping the old one, so two API processes briefly overlap. Each background loop (a round's draws, the lobby countdown, the engagement job) now holds a short Postgres lease (`loop_locks` table), renewed on every draw. Only the holder runs the loop; the old instance hands its leases over on shutdown and the new one takes the round within about 5 seconds. A crashed instance's lease expires after at most 30 seconds. `/ready` (database reachable) gates traffic; `/health` is liveness only.

## After the first deploy

1. Open the bot in Telegram and send `/start`; the Mini App URL is `<url>/app`.
2. Admin panel: `<url>/admin` (Telegram ID plus TOTP code).
3. Custom domain: add it on the `api` app (Container Apps > Custom domains), then set `PUBLIC_BASE_URL` in `deploy.env` and re-run the script so CORS and bot links use it.

## CI/CD (GitHub Actions)

`.github/workflows/deploy.yml` runs the tests, builds in ACR and updates both apps on every push to `main`, using passwordless OIDC login.

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

In GitHub, on the `production` environment, add secrets `AZURE_CLIENT_ID` (= `$APP`), `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, and variables `AZURE_RESOURCE_GROUP` (`bingo-rg`) and `NAME_PREFIX` (`melbetebingo`). Infrastructure changes still go through `deploy_azure.sh`; CI only ships new images.

## Operations

- Daily reconciliation: at 03:00 Ethiopia time a read-only job checks wallets (negative balances, winnings above balance, lifetime deposits vs approved top-ups) and posts a summary to `ADMIN_CHAT_ID` (set `RECONCILIATION_HOUR_EAT=-1` to disable). It never changes a balance.
- Logs: `az containerapp logs show -g bingo-rg -n melbetebingo-api --follow` (or `-bot`).
- Secrets live as Container Apps secrets; change them in `deploy.env` and re-run the script.
- Postgres has 7-day point-in-time backup. Its firewall allows Azure services only (plus your IP during schema setup). For stricter isolation, move it behind a VNet and private endpoint.
- Rough cost: Container Apps (two small always-on replicas), Postgres B1ms, Basic ACR, and storage come to a modest monthly bill; check the Azure pricing calculator for your region.

## More than one API container

The API runs as a single container by default (`apiMaxReplicas = 1`). Running two or more removes the
single point of failure (a crash or deploy no longer interrupts live games) but is only safe once the
steps below are done. **Not yet load-tested.**

Already safe with several containers: draw loops, lobby timers, bot filling, the daily reconciliation,
the engagement job and the agent-rewards job all use database leases (`loop_locks`), money endpoints use
database idempotency keys and atomic SQL, and admin sessions are signed cookies (no server-side session).

Needs work before you scale out:

1. **Redis for live messages.** Create Azure Cache for Redis (Basic C0 is enough to start) and deploy with
   `redisUrl` set to its `rediss://:<key>@<host>:6380/0` connection string. Every container then delivers
   each drawn number / claim / countdown to its own players. Without it, only players on the container
   that runs the round's draw loop receive live updates.
2. **Rate limits are per container.** `src/core/rate_limit.py` keeps counters in memory, so with N
   containers a player can make up to N times the limit. Money endpoints are still protected by
   idempotency and atomic balance updates, so this weakens abuse protection, not balances. Move it to
   Redis before scaling beyond 2 containers.
3. **Test it.** With 2 containers and Redis: join a round from two phones that land on different
   containers, confirm both see every number, kill the container running the draw loop mid-round and
   confirm the other takes over within a few seconds, and confirm a deploy during a lobby countdown
   starts the round once.
4. Then set `apiMaxReplicas` to 2 and redeploy. The Telegram bot stays at one replica (long polling).
