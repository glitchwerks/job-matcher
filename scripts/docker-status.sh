#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# docker-status.sh — Health check for job-matcher-pr Docker stack
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

echo "=== Container Status ==="
docker compose ps
echo ""

echo "=== Recent Web Logs (last 20 lines) ==="
docker compose logs web --tail=20 --no-log-prefix
echo ""

echo "=== Database Stats ==="
if docker compose ps --status running --services 2>/dev/null | grep -q "^db$"; then
  POSTGRES_USER=$(grep '^POSTGRES_USER=' .env 2>/dev/null | cut -d= -f2 || echo "jobmatcher")
  POSTGRES_DB=$(grep '^POSTGRES_DB=' .env 2>/dev/null | cut -d= -f2 || echo "jobmatcher")
  docker compose exec -T db psql -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -c \
    "SELECT COUNT(*) AS total_listings, COUNT(score) AS scored, MAX(fetched_at) AS last_fetch FROM listings;" \
    2>/dev/null || echo "    (Could not query database)"
else
  echo "    db container is not running"
fi
echo ""

echo "=== Environment ==="
if [[ -f .env ]]; then
  echo "    DATABASE_URL: postgresql://***:***@db:5432/${POSTGRES_DB:-jobmatcher}"
  FLASK_DEBUG=$(grep '^FLASK_DEBUG=' .env 2>/dev/null | cut -d= -f2 || echo "0")
  echo "    FLASK_DEBUG:  ${FLASK_DEBUG}"
else
  echo "    .env not found"
fi
