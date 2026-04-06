#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# docker-status.sh — Health check for job-matcher-pr Docker stacks (dev + prod)
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# ---------------------------------------------------------------------------
# Container Status
# ---------------------------------------------------------------------------
echo "=== Container Status ==="
echo ""
echo "--- Dev Stack (port 5000) ---"
docker compose -f "$PROJECT_DIR/docker-compose.dev.yml" ps
echo ""
echo "--- Prod Stack (port 5001) ---"
docker compose -f "$PROJECT_DIR/docker-compose.prod.yml" ps
echo ""

# ---------------------------------------------------------------------------
# Recent Logs
# ---------------------------------------------------------------------------
echo "=== Recent Web Logs (last 20 lines) ==="
echo ""
echo "--- DEV ---"
docker compose -f "$PROJECT_DIR/docker-compose.dev.yml" logs web --tail=20 --no-log-prefix
echo ""
echo "--- PROD ---"
docker compose -f "$PROJECT_DIR/docker-compose.prod.yml" logs web --tail=20 --no-log-prefix
echo ""

# ---------------------------------------------------------------------------
# Database Stats — dev
# ---------------------------------------------------------------------------
echo "=== Database Stats ==="
echo ""
echo "--- DEV (jobmatcher_dev) ---"
if docker compose -f "$PROJECT_DIR/docker-compose.dev.yml" ps --status running --services 2>/dev/null | grep -q "^db$"; then
  DEV_POSTGRES_USER=$(grep '^POSTGRES_USER=' "$PROJECT_DIR/.env.dev" 2>/dev/null | cut -d= -f2 || echo "jobmatcher")
  DEV_POSTGRES_DB=$(grep '^POSTGRES_DB=' "$PROJECT_DIR/.env.dev" 2>/dev/null | cut -d= -f2 || echo "jobmatcher_dev")
  docker compose -f "$PROJECT_DIR/docker-compose.dev.yml" exec -T db psql -U "${DEV_POSTGRES_USER}" -d "${DEV_POSTGRES_DB}" -c \
    "SELECT COUNT(*) AS total_listings, COUNT(score) AS scored, MAX(fetched_at) AS last_fetch FROM listings;" \
    2>/dev/null || echo "    (Could not query dev database)"
else
  echo "    dev db container is not running"
fi
echo ""

# ---------------------------------------------------------------------------
# Database Stats — prod
# ---------------------------------------------------------------------------
echo "--- PROD (jobmatcher_prod) ---"
if docker compose -f "$PROJECT_DIR/docker-compose.prod.yml" ps --status running --services 2>/dev/null | grep -q "^db$"; then
  PROD_POSTGRES_USER=$(grep '^POSTGRES_USER=' "$PROJECT_DIR/.env.prod" 2>/dev/null | cut -d= -f2 || echo "jobmatcher")
  PROD_POSTGRES_DB=$(grep '^POSTGRES_DB=' "$PROJECT_DIR/.env.prod" 2>/dev/null | cut -d= -f2 || echo "jobmatcher_prod")
  docker compose -f "$PROJECT_DIR/docker-compose.prod.yml" exec -T db psql -U "${PROD_POSTGRES_USER}" -d "${PROD_POSTGRES_DB}" -c \
    "SELECT COUNT(*) AS total_listings, COUNT(score) AS scored, MAX(fetched_at) AS last_fetch FROM listings;" \
    2>/dev/null || echo "    (Could not query prod database)"
else
  echo "    prod db container is not running"
fi
echo ""

# ---------------------------------------------------------------------------
# Environment summary
# ---------------------------------------------------------------------------
echo "=== Environment ==="
echo ""
echo "--- DEV (.env.dev) ---"
if [[ -f "$PROJECT_DIR/.env.dev" ]]; then
  DEV_PG_DB=$(grep '^POSTGRES_DB=' "$PROJECT_DIR/.env.dev" 2>/dev/null | cut -d= -f2 || echo "jobmatcher_dev")
  DEV_FLASK_DEBUG=$(grep '^FLASK_DEBUG=' "$PROJECT_DIR/.env.dev" 2>/dev/null | cut -d= -f2 || echo "0")
  echo "    DATABASE_URL:  postgresql://***:***@db:5432/${DEV_PG_DB}"
  echo "    FLASK_DEBUG:   ${DEV_FLASK_DEBUG}"
else
  echo "    .env.dev not found"
fi
echo ""
echo "--- PROD (.env.prod) ---"
if [[ -f "$PROJECT_DIR/.env.prod" ]]; then
  PROD_PG_DB=$(grep '^POSTGRES_DB=' "$PROJECT_DIR/.env.prod" 2>/dev/null | cut -d= -f2 || echo "jobmatcher_prod")
  PROD_FLASK_DEBUG=$(grep '^FLASK_DEBUG=' "$PROJECT_DIR/.env.prod" 2>/dev/null | cut -d= -f2 || echo "0")
  echo "    DATABASE_URL:  postgresql://***:***@db:5432/${PROD_PG_DB}"
  echo "    FLASK_DEBUG:   ${PROD_FLASK_DEBUG}"
else
  echo "    .env.prod not found"
fi
