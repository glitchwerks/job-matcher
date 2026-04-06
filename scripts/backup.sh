#!/usr/bin/env bash
# Backup the job-matcher-pr PostgreSQL database.
# Usage: ./scripts/backup.sh [backup_dir]
# Default backup_dir: ./backups
# Keeps the 10 most recent backups.
#
# To restore from a backup:
#   docker compose exec -T db psql -U jobmatcher jobmatcher < backups/jobs_YYYYMMDD_HHMMSS.sql

set -euo pipefail
cd "$(dirname "$0")/.."

BACKUP_DIR="${1:-./backups}"
mkdir -p "$BACKUP_DIR"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/jobs_${TIMESTAMP}.sql"

echo "Backing up database to $BACKUP_FILE..."
docker compose exec -T db pg_dump -U jobmatcher jobmatcher > "$BACKUP_FILE"
echo "Backup complete: $BACKUP_FILE ($(du -sh "$BACKUP_FILE" | cut -f1))"

# Rotate: keep only the 10 most recent backups
ls -t "$BACKUP_DIR"/jobs_*.sql 2>/dev/null | tail -n +11 | xargs -r rm --
echo "Old backups pruned. Remaining: $(ls "$BACKUP_DIR"/jobs_*.sql 2>/dev/null | wc -l)"
