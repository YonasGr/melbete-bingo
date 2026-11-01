# Windows 24/7 local deployment

This is the recommended low-cost setup for leaving Melbete Bingo running on a
Windows PC. Docker Desktop runs the API, Telegram bot, and a private Postgres
database. Docker restart policies bring the containers back after a crash or
Docker Desktop restart. An optional Cloudflare Tunnel provides HTTPS without
opening router ports.

> **Important:** this is self-hosting, not a backup service. Keep the PC
> powered, prevent sleep, use a UPS if possible, and test database backups.
> Anyone who can access the machine can potentially access the local data.

## 1. Prepare the Windows PC

1. Use Windows 10/11 64-bit with virtualization enabled in firmware.
2. Install [Docker Desktop for Windows](https://docs.docker.com/desktop/setup/install/windows-install/).
   During installation, choose the WSL 2 backend.
3. Start Docker Desktop and enable **Settings → General → Start Docker
   Desktop when you sign in**.
4. Install [Git for Windows](https://git-scm.com/download/win).
5. Disable sleep while plugged in: **Settings → System → Power → Screen and
   sleep**, set sleep to **Never**. Locking the screen is fine; closing the
   terminal is fine.

## 2. Clone and configure the project

Open PowerShell:

```powershell
git clone https://github.com/YonasGr/melbete-bingo.git
cd melbete-bingo
Copy-Item env.example .env
notepad .env
```

Set at least:

```dotenv
TELEGRAM_BOT_TOKEN=...
TELEGRAM_BOT_USERNAME=...
SECRET_KEY=<long-random-value>
APP_ENV=production
DEBUG=False
PUBLIC_BASE_URL=https://your-tunnel-hostname.example.com
POSTGRES_PASSWORD=<long-random-local-password>
```

Do **not** put `.env` in Git or share it. The Compose file automatically
overrides `DATABASE_URL` inside the API and bot with the local `db` container,
so no Supabase account is required. The database is not published to a
Windows port.

Generate a secret without storing it in shell history:

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

## 3. Start it as a background service

The terminal is not required after this command:

```powershell
.\scripts\windows\start-melbete.ps1
.\scripts\windows\status-melbete.ps1
```

The API is available locally at `http://localhost:8001`. The bot is a separate
container, so it continues polling Telegram after PowerShell closes.

To start the stack automatically after every Windows sign-in:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
.\scripts\windows\register-melbete-startup.ps1
```

The task starts Compose after login. Docker Desktop itself must start first;
the containers have `restart: unless-stopped`, so Docker will also restore
them after a Docker daemon restart. If the task runs before Docker is ready,
run `start-melbete.ps1` once manually after signing in.

## 4. Give Telegram a public HTTPS URL (recommended)

Telegram Mini Apps require HTTPS. Do **not** expose Postgres and do not
forward port 5432 from the router. Cloudflare Tunnel is the safest simple
option here:

1. Create a Cloudflare account and add a domain you control.
2. In **Zero Trust → Networks → Tunnels**, create a tunnel.
3. Install `cloudflared` locally only to create the tunnel, or use the
   Cloudflare dashboard's Docker connector instructions.
4. Add a public hostname such as `bingo.example.com` with service
   `http://api:8000` when the connector is attached to this Compose network.
   If the dashboard-generated connector uses a different network, follow its
   generated `docker run` instructions instead.
5. Copy the tunnel token into `.env`:

```dotenv
CLOUDFLARE_TUNNEL_TOKEN=eyJ...
PUBLIC_BASE_URL=https://bingo.example.com
```

6. Start the optional tunnel container:

```powershell
.\scripts\windows\start-melbete-public.ps1
```

The tunnel is outbound-only: no inbound router rule or Windows firewall port
opening is needed. The public hostname must be stable; do not use a temporary
`trycloudflare.com` URL for a permanent Telegram Mini App configuration.

If you do not own a domain yet, use a temporary tunnel only for testing. A
stable domain is strongly recommended because changing `PUBLIC_BASE_URL`
requires restarting the API and updating the Telegram Mini App button.

## 5. Operate and update

```powershell
.\scripts\windows\status-melbete.ps1
docker compose logs -f api
docker compose logs -f bot
.\scripts\windows\stop-melbete.ps1
.\scripts\windows\update-melbete.ps1
```

Run `update-melbete.ps1` only when the working tree has no local changes.
There must be exactly one bot container for a Telegram token; duplicate bot
instances cause Telegram polling conflicts.

## 6. Backups and recovery

Create a database dump regularly, and copy the resulting `backups` folder to
another disk:

```powershell
.\scripts\windows\backup-melbete.ps1
```

The Postgres volume survives container recreation, but it is not a backup.
To restore a dump into a stopped/empty database:

```powershell
Get-Content .\backups\melbete_YYYYMMDD_HHMMSS.sql | `
  docker compose exec -T db psql -U bingo -d melbete_bingo
```

## Troubleshooting

- **API is unhealthy:** run `docker compose logs api`; verify Docker Desktop is
  running and `.env` contains the Telegram token, secret, and production
  settings.
- **Database connection errors:** never use `localhost` in `DATABASE_URL`
  inside Compose; the hostname is `db`. The Compose override already sets it.
- **Telegram Mini App does not open:** `PUBLIC_BASE_URL` must be the exact
  stable `https://` Cloudflare hostname, with no trailing slash.
- **Tunnel does not start:** confirm `CLOUDFLARE_TUNNEL_TOKEN` is present and
  run `docker compose --profile public logs cloudflared`.
- **After a power cut:** start Docker Desktop, wait until it says running, and
  check `status-melbete.ps1`. The persistent Postgres volume should be reused.
