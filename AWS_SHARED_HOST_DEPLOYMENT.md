# AWS deployment alongside Safari-Games

This deployment runs Melbete Bingo on the same AWS EC2 host as Safari-Games,
but keeps the applications isolated:

- Safari remains in `~/bingo` with its existing `bingo-api`, `bingo-bot`, and
  `bingo-caddy` containers.
- Melbete is deployed independently in `~/melbete-bingo` as
  `melbete-api` and `melbete-bot`.
- Melbete uses its own database name/credentials in the existing RDS
  PostgreSQL instance. Do not point it at Safari's production database.
- Safari's existing Caddy remains the only process binding host ports 80 and
  443.
- GitHub Actions deploys Melbete on every push to `main` after tests pass.

The recommended hostname is `https://melbete.yonasgr.me`. It is separate from
`https://safari.yonasgr.me`, but both can point to the same EC2 public IP.

## One-time AWS and DNS setup

### 1. Confirm the shared host

The GitHub repository must use the same EC2 host as Safari. The existing
deployment uses these GitHub secrets:

- `EC2_HOST`
- `EC2_SSH_PRIVATE_KEY`

Reuse those exact values in this repository's `production` environment. Do not
copy the private key into the repository.

On the EC2 host, verify the existing stack and network:

```bash
cd ~/bingo
docker compose -f docker-compose.production.yml ps
docker network ls
docker network inspect bingo_default
```

If the network is not named `bingo_default`, update the `name` under
`safari_ingress` in [docker-compose.production.yml](docker-compose.production.yml)
and the matching Caddy instructions before deploying.

### 2. Create a separate RDS database

Melbete must not share Safari's tables. Ask the RDS administrator for a
separate database, for example `melbete_bingo`, and a separate application
user. A typical administrator command is:

```sql
CREATE USER melbete_app WITH PASSWORD '<strong-password>';
CREATE DATABASE melbete_bingo OWNER melbete_app;
```

If the database was created by another RDS role, the application role must
also be granted ownership or schema creation privileges before the first
startup. Connect to `melbete_bingo` as the RDS master/admin role and run:

```sql
ALTER DATABASE melbete_bingo OWNER TO melbete_app;
ALTER SCHEMA public OWNER TO melbete_app;
GRANT CONNECT ON DATABASE melbete_bingo TO melbete_app;
GRANT USAGE, CREATE ON SCHEMA public TO melbete_app;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO melbete_app;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO melbete_app;
```

The `DATABASE_URL` user must be `melbete_app`, not the Safari `bingo` user.
Verify the grants before starting the API:

```sql
SELECT
  has_schema_privilege('melbete_app', 'public', 'USAGE') AS can_use,
  has_schema_privilege('melbete_app', 'public', 'CREATE') AS can_create;
```

Both values must be `t`.

Run the complete schema and required migrations against this new database
before the first public deployment. The exact RDS connection string becomes
the `DATABASE_URL` secret in this repository. It should use SSL, for example:

```text
postgresql://melbete_app:<password>@<rds-endpoint>:5432/melbete_bingo?sslmode=require
```

Do not paste this value into chat, a commit, or an issue.

### 3. Add the DNS record

In the DNS provider for `yonasgr.me`, add:

```text
Type: A
Name: melbete
Value: <same EC2 public IPv4 address used by safari.yonasgr.me>
TTL: 300 (or automatic)
```

If the existing domain uses an alias or Cloudflare proxy, mirror the
`safari.yonasgr.me` record's routing model instead of creating a conflicting
record. Do not add a router port-forward; AWS security groups already expose
the existing Caddy ports.

### 4. Add the Caddy route once

On the EC2 host, edit the Safari deployment's `~/bingo/Caddyfile` and add:

```caddy
melbete.yonasgr.me {
    reverse_proxy melbete-api:8000
}
```

The same snippet is stored in
[infra/caddy-melbete.snippet](infra/caddy-melbete.snippet). Then validate and
reload Caddy:

```bash
cd ~/bingo
docker compose -f docker-compose.production.yml cp Caddyfile caddy:/tmp/Caddyfile
docker compose -f docker-compose.production.yml exec caddy \
  caddy validate --config /tmp/Caddyfile
docker compose -f docker-compose.production.yml restart caddy
```

If `docker compose cp` is unavailable, validate the mounted file directly:

```bash
docker exec bingo-caddy caddy validate --config /etc/caddy/Caddyfile
docker restart bingo-caddy
```

The Safari repository's deployment workflow currently syncs its own
`Caddyfile`. Keep this new block in that repository too, otherwise a future
Safari deployment could remove the route.

## GitHub Actions setup

The workflow is [deploy-aws.yml](.github/workflows/deploy-aws.yml). In this
repository, create a GitHub **Environment** named `production`, then add
these secrets:

| Secret | Purpose |
|---|---|
| `EC2_HOST` | EC2 public DNS name or static IP |
| `EC2_SSH_PRIVATE_KEY` | Private key for the `ubuntu` deployment user |
| `DATABASE_URL` | Melbete's separate RDS database URL |
| `TELEGRAM_BOT_TOKEN` | Melbete's Telegram bot token |
| `TELEGRAM_BOT_USERNAME` | Melbete bot username without `@` |
| `SECRET_KEY` | Long random production secret |
| `SUPER_ADMIN_TELEGRAM_ID` | Initial Melbete admin Telegram ID |
| `ADMIN_TOTP_SECRETS` | Melbete admin TOTP mapping |
| `ADMIN_TELEGRAM_IDS` | Optional legacy admin IDs |
| `ADMIN_CHAT_ID` | Optional payment-review chat ID |

Generate a secret locally:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

The workflow does not use the same concurrency group as Safari, so a push to
one repository does not cancel or block a deployment from the other. It also
uses distinct directories, image names, container names, and database
configuration.

After adding the secrets, push to `main` or use **Actions → Deploy Melbete
Bingo to shared AWS EC2 → Run workflow**.

## First deployment checks

On the EC2 host:

```bash
cd ~/melbete-bingo
docker compose -f docker-compose.production.yml ps
docker compose -f docker-compose.production.yml logs --tail=100 api bot
```

From any machine:

```bash
curl -fsS https://melbete.yonasgr.me/health
curl -fsS https://melbete.yonasgr.me/ready
```

The bot must be a new Telegram bot token. Never run the same token in both
Safari and Melbete; Telegram allows only one long-polling consumer per token.

## Rollback and temporary shutdown

Stop only Melbete:

```bash
cd ~/melbete-bingo
docker compose -f docker-compose.production.yml stop
```

Safari and its Caddy remain online. To remove the Melbete containers while
preserving uploads and the database, use:

```bash
docker compose -f docker-compose.production.yml down
```

Do not use `docker compose down -v` unless you intentionally want to remove
Melbete's named volumes. The RDS database is independent and is not removed by
Compose.
