#!/usr/bin/env bash
# Nightly backup of the local `db` container's database. Meant to run via
# cron (see LOCAL_MIGRATION_GUIDE.md "Backups" section for the crontab
# line). Keeps the last 14 daily dumps and deletes older ones.

set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-$HOME/bingo-db-backups}"
LOCAL_URL="${LOCAL_URL:-postgresql://bingo:bingo@localhost:5432/bingo}"
KEEP_DAYS=14

mkdir -p "${BACKUP_DIR}"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
OUT_FILE="${BACKUP_DIR}/bingo_${TIMESTAMP}.sql.gz"

pg_dump "${LOCAL_URL}" --no-owner --no-privileges | gzip > "${OUT_FILE}"

echo "$(date -Iseconds) - backed up to ${OUT_FILE} ($(du -h "${OUT_FILE}" | cut -f1))"

# Prune anything older than KEEP_DAYS
find "${BACKUP_DIR}" -name "bingo_*.sql.gz" -mtime +${KEEP_DAYS} -delete
