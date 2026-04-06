#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# docker-teardown.sh — Remove job-matcher-pr Docker stack
# Config files in ./config/ are always preserved.
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

echo "This will stop and remove all job-matcher-pr containers."
echo "Config files in ./config/ will NOT be deleted."
echo ""
read -r -p "Continue? [y/N] " confirm
if [[ "$(echo "$confirm" | tr '[:upper:]' '[:lower:]')" != "y" ]]; then
  echo "Aborted."
  exit 0
fi

echo "==> Stopping containers..."
docker compose down
echo "    Containers removed"

echo ""
read -r -p "Also delete the PostgreSQL data volume? This permanently destroys all job data. [y/N] " confirm_vol
if [[ "$(echo "$confirm_vol" | tr '[:upper:]' '[:lower:]')" == "y" ]]; then
  docker compose down -v
  echo "    Data volume removed"
else
  echo "    Data volume preserved"
fi

echo ""
echo "Done. Config files in $PROJECT_DIR/config/ were not deleted."
echo "To fully remove images: docker rmi \$(docker images --format '{{.Repository}}:{{.Tag}}' | grep 'job-matcher-pr')"
