#!/usr/bin/env bash
# Rebuilds api/bot with a fresh cache-buster query string stamped onto
# every /static/css/*.css and /static/js/*.js reference in the HTML
# templates - the exact same trick as the "Inject cache buster into
# HTML templates" step in .github/workflows/deploy.yml, just running
# locally on every rebuild instead of once per CI deploy.
#
# Why: Telegram's in-app WebView aggressively caches Mini App static
# assets by URL. Without a changing query string, players can keep
# seeing stale JS/CSS after you've rebuilt and restarted the container.
#
# Usage:
#   ./scripts/rebuild.sh            # rebuilds + restarts both api and bot
#   ./scripts/rebuild.sh api        # just the api
#   ./scripts/rebuild.sh bot        # just the bot

set -euo pipefail

SERVICES="${1:-api bot}"
CACHE_BUST=$(date +%Y%m%d%H%M%S)

echo "==> Stamping cache buster v=${CACHE_BUST} into src/templates/*.html"
# Same regex as deploy.yml's "Inject cache buster" step: matches
# /static/css/... or /static/js/... references (with or without an
# existing ?v=... from a previous run) and rewrites the query string.
sed -i -E "s@(/static/(css|js)/[^\"]+\.(css|js))(\?v=[a-zA-Z0-9_-]*)?@\1?v=${CACHE_BUST}@g" src/templates/*.html

echo "==> Building ${SERVICES}..."
docker compose build ${SERVICES}

echo "==> Reverting local template changes (already baked into the built image - keeps 'git status' clean)"
git checkout -- src/templates/*.html

echo "==> Restarting ${SERVICES}..."
docker compose up -d ${SERVICES}

echo "==> Done. This build's cache buster: v=${CACHE_BUST}"
echo "    If Telegram still shows stale assets after this, fully close and"
echo "    reopen the chat (not just the Mini App) - Telegram's WebView"
echo "    process sometimes needs a hard restart, not just a URL change."
