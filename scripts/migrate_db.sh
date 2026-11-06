#!/usr/bin/env bash
# Dumps the OLD database (your Azure Database for PostgreSQL flexible
# server) and restores it into the local `db` container defined in
# docker-compose.yml.
#
# Usage:
#   1. export SOURCE_DB_URL="postgresql://<admin-user>:<password>@<server-name>.postgres.database.azure.com:5432/<dbname>?sslmode=require"
#   2. Make sure the local db container is up and has the schema applied
#     .
#   3. Run: ./scripts/migrate_db.sh
#
# Requires pg_dump/psql on the HOST (not in a container):
#   sudo pacman -S postgresql-libs postgresql   # (you already have this)

set -euo pipefail

SOURCE_DB_URL="${SOURCE_DB_URL:?Set SOURCE_DB_URL to your Azure Postgres connection string first}"
LOCAL_URL="${LOCAL_URL:-postgresql://bingo:bingo@localhost:5432/bingo}"

DUMP_FILE="azure_db_dump_$(date +%Y%m%d_%H%M%S).sql"

echo "==> Dumping source database to ${DUMP_FILE}..."
pg_dump "${SOURCE_DB_URL}" \
  --no-owner \
  --no-privileges \
  --format=plain \
  --file="${DUMP_FILE}"

echo "==> Dump complete ($(du -h "${DUMP_FILE}" | cut -f1))."
echo "==> Restoring into local database..."

psql "${LOCAL_URL}" -f "${DUMP_FILE}"

echo "==> Done. Sanity-check row counts (source vs local):"
for table in players wallets transactions game_rooms cards claims admins; do
  src=$(psql "${SOURCE_DB_URL}" -Atc "SELECT count(*) FROM ${table};" 2>/dev/null || echo "N/A")
  loc=$(psql "${LOCAL_URL}"     -Atc "SELECT count(*) FROM ${table};" 2>/dev/null || echo "N/A")
  printf "  %-15s source=%-8s local=%-8s %s\n" "${table}" "${src}" "${loc}" "$([ "$src" = "$loc" ] && echo OK || echo 'MISMATCH - CHECK')"
done

echo ""
echo "Keep ${DUMP_FILE} somewhere safe (off this machine too) until you're"
echo "fully confident in the local copy."
