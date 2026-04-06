#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# docker-teardown.sh — Remove job-matcher-pr Docker stacks (dev and/or prod)
# Config files in ./config/ and ./config-dev/ are always preserved.
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "This will stop and remove job-matcher-pr containers."
echo "Config files in ./config/ and ./config-dev/ will NOT be deleted."
echo ""

# ---------------------------------------------------------------------------
# Dev stack
# ---------------------------------------------------------------------------
read -r -p "Tear down DEV stack (port 5000)? [y/N] " confirm_dev
if [[ "$(echo "$confirm_dev" | tr '[:upper:]' '[:lower:]')" == "y" ]]; then
  echo "==> Stopping dev containers..."

  read -r -p "Also delete DEV data volume (pgdata_dev)? This permanently destroys all dev job data. [y/N] " confirm_dev_vol
  if [[ "$(echo "$confirm_dev_vol" | tr '[:upper:]' '[:lower:]')" == "y" ]]; then
    docker compose -f "$PROJECT_DIR/docker-compose.dev.yml" down -v
    echo "    Dev containers and data volume removed"
  else
    docker compose -f "$PROJECT_DIR/docker-compose.dev.yml" down
    echo "    Dev containers removed"
    echo "    Dev data volume preserved"
  fi
else
  echo "    Dev stack skipped"
fi

echo ""

# ---------------------------------------------------------------------------
# Prod stack
# ---------------------------------------------------------------------------
read -r -p "Tear down PROD stack (port 5001)? [y/N] " confirm_prod
if [[ "$(echo "$confirm_prod" | tr '[:upper:]' '[:lower:]')" == "y" ]]; then
  echo "==> Stopping prod containers..."

  read -r -p "Also delete PROD data volume (pgdata_prod)? This permanently destroys all prod job data. [y/N] " confirm_prod_vol
  if [[ "$(echo "$confirm_prod_vol" | tr '[:upper:]' '[:lower:]')" == "y" ]]; then
    docker compose -f "$PROJECT_DIR/docker-compose.prod.yml" down -v
    echo "    Prod containers and data volume removed"
  else
    docker compose -f "$PROJECT_DIR/docker-compose.prod.yml" down
    echo "    Prod containers removed"
    echo "    Prod data volume preserved"
  fi
else
  echo "    Prod stack skipped"
fi

echo ""
echo "Done. Config files in $PROJECT_DIR/config/ and $PROJECT_DIR/config-dev/ were not deleted."
echo "To fully remove images: docker rmi \$(docker images --format '{{.Repository}}:{{.Tag}}' | grep 'job-matcher-pr')"
