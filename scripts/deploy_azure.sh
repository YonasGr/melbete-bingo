#!/usr/bin/env bash
# One-command Azure deploy for Melbete Bingo (Container Apps + ACR + Blob + Postgres).
#
# Usage:
#   cp deploy.env.example deploy.env     # fill in the required values
#   az login
#   ./scripts/deploy_azure.sh
#
# Safe to re-run: it creates what is missing and rolls out a new image.
# Works in Azure Cloud Shell, WSL, macOS and Linux (needs: az, python3, and
# psql if you want the script to apply the database schema for you).
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE="${ENV_FILE:-deploy.env}"
if [ -f "$ENV_FILE" ]; then set -a; . "./$ENV_FILE"; set +a; fi

: "${AZ_RESOURCE_GROUP:=bingo-rg}"
: "${AZ_LOCATION:=uaenorth}"
: "${NAME_PREFIX:=melbetebingo}"
: "${IMAGE_TAG:=$(git rev-parse --short=12 HEAD 2>/dev/null || date +%Y%m%d%H%M%S)}"

die() { echo "ERROR: $*" >&2; exit 1; }
command -v az >/dev/null || die "Azure CLI (az) not found."
command -v python3 >/dev/null || die "python3 not found."
[ -n "${TELEGRAM_BOT_TOKEN:-}" ] || die "TELEGRAM_BOT_TOKEN is required (set it in $ENV_FILE)."
[ -n "${SUPER_ADMIN_TELEGRAM_ID:-}" ] || die "SUPER_ADMIN_TELEGRAM_ID is required."
[ -n "${ADMIN_TOTP_SECRETS:-}" ] || echo "WARNING: ADMIN_TOTP_SECRETS is empty - admin login will be disabled until you set it and re-run."

persist() { # persist KEY VALUE -> append to env file so re-runs reuse it
  touch "$ENV_FILE"; chmod 600 "$ENV_FILE"; echo "$1=$2" >> "$ENV_FILE"
}
gen() { python3 -c "import secrets;print(secrets.token_hex(24))"; }

if [ -z "${SECRET_KEY:-}" ]; then SECRET_KEY="$(gen)"; persist SECRET_KEY "$SECRET_KEY"; echo "Generated SECRET_KEY (saved to $ENV_FILE)."; fi

if [ -n "${EXTERNAL_DATABASE_URL:-}" ]; then CREATE_PG=false; else
  CREATE_PG=true
  if [ -z "${POSTGRES_ADMIN_PASSWORD:-}" ]; then POSTGRES_ADMIN_PASSWORD="$(gen)"; persist POSTGRES_ADMIN_PASSWORD "$POSTGRES_ADMIN_PASSWORD"; echo "Generated POSTGRES_ADMIN_PASSWORD (saved to $ENV_FILE)."; fi
fi

PARAMS_FILE="$(mktemp)"; chmod 600 "$PARAMS_FILE"
CLIENT_RULE=""; PG_SERVER=""
cleanup() {
  rm -f "$PARAMS_FILE"
  if [ -n "$CLIENT_RULE" ] && [ -n "$PG_SERVER" ]; then
    az postgres flexible-server firewall-rule delete -g "$AZ_RESOURCE_GROUP" -n "$PG_SERVER" --rule-name deploy-client --yes >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

write_params() { # write_params <deployApps true|false>
  DEPLOY_APPS="$1" CREATE_PG="$CREATE_PG" NAME_PREFIX="$NAME_PREFIX" IMAGE_TAG="$IMAGE_TAG" python3 - "$PARAMS_FILE" <<'PY'
import json, os, sys
e = os.environ
def v(x): return {"value": x}
p = {
  "namePrefix": v(e["NAME_PREFIX"]),
  "deployApps": v(e["DEPLOY_APPS"] == "true"),
  "imageTag": v(e["IMAGE_TAG"]),
  "createPostgres": v(e["CREATE_PG"] == "true"),
  "telegramBotToken": v(e["TELEGRAM_BOT_TOKEN"]),
  "secretKey": v(e["SECRET_KEY"]),
  "telegramBotUsername": v(e.get("TELEGRAM_BOT_USERNAME", "")),
  "superAdminTelegramId": v(e["SUPER_ADMIN_TELEGRAM_ID"]),
  "adminTotpSecrets": v(e.get("ADMIN_TOTP_SECRETS", "")),
  "adminTelegramIds": v(e.get("ADMIN_TELEGRAM_IDS", "")),
  "adminChatId": v(e.get("ADMIN_CHAT_ID", "")),
  "publicBaseUrl": v(e.get("PUBLIC_BASE_URL", "")),
  "postgresAdminPassword": v(e.get("POSTGRES_ADMIN_PASSWORD", "")),
  "externalDatabaseUrl": v(e.get("EXTERNAL_DATABASE_URL", "")),
}
json.dump({"$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
           "contentVersion": "1.0.0.0", "parameters": p}, open(sys.argv[1], "w"))
PY
}

deploy() { # deploy <name> <deployApps>
  write_params "$2"
  az deployment group create -g "$AZ_RESOURCE_GROUP" -n "$1" \
    --template-file infra/main.bicep --parameters "@$PARAMS_FILE" \
    --query properties.outputs -o json
}

az config set extension.use_dynamic_install=yes_without_prompt >/dev/null 2>&1 || true
az provider register --namespace Microsoft.App --wait >/dev/null 2>&1 || true
az provider register --namespace Microsoft.OperationalInsights --wait >/dev/null 2>&1 || true

echo "==> 1/5 Resource group + shared infrastructure"
az group create -n "$AZ_RESOURCE_GROUP" -l "$AZ_LOCATION" -o none
OUT="$(deploy melbete-infra false)"
out() { python3 -c "import json,sys;print(json.load(sys.stdin)['$1']['value'])" <<<"$OUT"; }
ACR="$(out acrName)"; PG_SERVER="$(out postgresServerName)"; PG_HOST="$(out postgresHost)"

echo "==> 2/5 Building image in Azure Container Registry ($ACR, tag $IMAGE_TAG)"
bash scripts/fetch_fonts.sh || echo "WARNING: fonts not fetched - the app will use system fonts."
az acr build -r "$ACR" -t "bingo-app:$IMAGE_TAG" -t "bingo-app:latest" .

echo "==> 3/5 Database schema"
if [ "$CREATE_PG" = "true" ]; then
  DB_URL="postgresql://bingoadmin:${POSTGRES_ADMIN_PASSWORD}@${PG_HOST}:5432/bingo?sslmode=require"
  MYIP="$(curl -fsS https://api.ipify.org || true)"
  if [ -n "$MYIP" ]; then
    az postgres flexible-server firewall-rule create -g "$AZ_RESOURCE_GROUP" -n "$PG_SERVER" \
      --rule-name deploy-client --start-ip-address "$MYIP" --end-ip-address "$MYIP" -o none
    CLIENT_RULE=1
  fi
else
  DB_URL="$EXTERNAL_DATABASE_URL"
fi
if command -v psql >/dev/null; then
  HAS_PLAYERS="$(psql "$DB_URL" -Atc "SELECT to_regclass('public.players') IS NOT NULL" 2>/dev/null || echo err)"
  if [ "$HAS_PLAYERS" = "f" ]; then
    psql "$DB_URL" -v ON_ERROR_STOP=1 -q -f supabase/schema.sql && echo "Schema applied."
  elif [ "$HAS_PLAYERS" = "t" ]; then
    echo "Schema already present - skipping."
  else
    echo "WARNING: could not reach the database from here. Apply supabase/schema.sql manually before first use."
  fi
else
  echo "psql not found - apply supabase/schema.sql to the database manually (see DEPLOY_AZURE.md)."
fi

echo "==> 4/5 Creating/updating api + bot apps"
OUT="$(deploy melbete-apps true)"
URL="$(out publicBaseUrl)"

echo "==> 5/5 Waiting for /ready"
for i in $(seq 1 30); do
  if curl -fsS "$URL/ready" >/dev/null 2>&1; then echo "Healthy: $URL"; break; fi
  [ "$i" = 30 ] && echo "WARNING: /health not responding yet. Check: az containerapp logs show -g $AZ_RESOURCE_GROUP -n ${NAME_PREFIX}-api --follow"
  sleep 10
done

cat <<DONE

Deployed.
  Mini App / web:  $URL/app
  Admin panel:     $URL/admin
  Logs (api):      az containerapp logs show -g $AZ_RESOURCE_GROUP -n ${NAME_PREFIX}-api --follow
  Logs (bot):      az containerapp logs show -g $AZ_RESOURCE_GROUP -n ${NAME_PREFIX}-bot --follow
Next: open your bot in Telegram and send /start.
DONE
