#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# docker-setup.sh — One-time setup for job-matcher-pr on a Linux VM
# Run from /opt/job-matcher-pr/ after cloning the repo
# ---------------------------------------------------------------------------

# Require root for chown operations.
if [[ $EUID -ne 0 ]]; then
  echo "ERROR: This script must be run as root (e.g. sudo ./scripts/docker-setup.sh)" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# If this script fails partway through, it is safe to re-run — all steps check
# whether the target file/directory already exists before acting, and will skip
# anything already configured.

echo "==> Checking Docker..."
if ! command -v docker &>/dev/null; then
  echo "ERROR: Docker is not installed. Install Docker Engine first." >&2
  exit 1
fi
if ! docker compose version &>/dev/null; then
  echo "ERROR: Docker Compose plugin not found." >&2
  exit 1
fi
echo "    Docker $(docker --version | cut -d' ' -f3 | tr -d ',')"
echo "    Compose $(docker compose version --short)"

if ! docker ps &>/dev/null; then
  echo "ERROR: Cannot connect to Docker daemon. Is Docker running?" >&2
  exit 1
fi

echo "==> Setting up directories..."
mkdir -p "$PROJECT_DIR/config"
mkdir -p "$PROJECT_DIR/config-dev"
mkdir -p "$PROJECT_DIR/logs"
mkdir -p "$PROJECT_DIR/logs-dev"
# uid 1000 = appuser defined in the Dockerfile — required so the web container
# can write to config/ (the /settings UI saves providers.json at runtime).
chown -R 1000:1000 "$PROJECT_DIR/config"
chown -R 1000:1000 "$PROJECT_DIR/config-dev"
echo "    config/ and config-dev/ owned by uid 1000"

# ---------------------------------------------------------------------------
# .env.dev
# ---------------------------------------------------------------------------
echo "==> Creating .env.dev from .env.dev.example..."
if [[ ! -f "$PROJECT_DIR/.env.dev" ]]; then
  cp "$PROJECT_DIR/.env.dev.example" "$PROJECT_DIR/.env.dev"
  echo "    Created .env.dev — IMPORTANT: edit it and set a strong POSTGRES_PASSWORD before continuing"
  echo ""
  echo "    nano $PROJECT_DIR/.env.dev"
  echo ""
  read -r -p "Press Enter when .env.dev is configured, or Ctrl+C to abort..."
  if grep -q "your-strong-password-here\|changeme" "$PROJECT_DIR/.env.dev" 2>/dev/null; then
    echo "ERROR: POSTGRES_PASSWORD in .env.dev still contains the example value. Set a real password first." >&2
    exit 1
  fi
  PW=$(grep '^POSTGRES_PASSWORD=' "$PROJECT_DIR/.env.dev" | cut -d= -f2)
  if [[ ${#PW} -lt 12 ]]; then
    echo "ERROR: POSTGRES_PASSWORD in .env.dev must be at least 12 characters." >&2
    exit 1
  fi
else
  echo "    .env.dev already exists, skipping"
fi

# ---------------------------------------------------------------------------
# .env.prod
# ---------------------------------------------------------------------------
echo "==> Creating .env.prod from .env.prod.example..."
if [[ ! -f "$PROJECT_DIR/.env.prod" ]]; then
  cp "$PROJECT_DIR/.env.prod.example" "$PROJECT_DIR/.env.prod"
  echo "    Created .env.prod — IMPORTANT: edit it and set a strong POSTGRES_PASSWORD before continuing"
  echo ""
  echo "    nano $PROJECT_DIR/.env.prod"
  echo ""
  read -r -p "Press Enter when .env.prod is configured, or Ctrl+C to abort..."
  if grep -q "your-strong-password-here\|changeme" "$PROJECT_DIR/.env.prod" 2>/dev/null; then
    echo "ERROR: POSTGRES_PASSWORD in .env.prod still contains the example value. Set a real password first." >&2
    exit 1
  fi
  PW=$(grep '^POSTGRES_PASSWORD=' "$PROJECT_DIR/.env.prod" | cut -d= -f2)
  if [[ ${#PW} -lt 12 ]]; then
    echo "ERROR: POSTGRES_PASSWORD in .env.prod must be at least 12 characters." >&2
    exit 1
  fi
else
  echo "    .env.prod already exists, skipping"
fi

# ---------------------------------------------------------------------------
# Config files — copy examples into both config/ and config-dev/
# ---------------------------------------------------------------------------
echo "==> Copying example config files..."
shopt -s nullglob
for example in "$PROJECT_DIR/config/"*.example.json; do
  base="$(basename "${example%.example.json}.json")"

  target_prod="$PROJECT_DIR/config/$base"
  if [[ ! -f "$target_prod" ]]; then
    cp "$example" "$target_prod"
    chown 1000:1000 "$target_prod"
    echo "    Created config/$base"
  else
    echo "    config/$base already exists, skipping"
  fi

  target_dev="$PROJECT_DIR/config-dev/$base"
  if [[ ! -f "$target_dev" ]]; then
    cp "$example" "$target_dev"
    chown 1000:1000 "$target_dev"
    echo "    Created config-dev/$base"
  else
    echo "    config-dev/$base already exists, skipping"
  fi
done
shopt -u nullglob

# ---------------------------------------------------------------------------
# GHCR login
# ---------------------------------------------------------------------------
echo "==> Logging in to GitHub Container Registry..."
if [[ -n "${GHCR_TOKEN:-}" && -n "${GHCR_USERNAME:-}" ]]; then
  echo "$GHCR_TOKEN" | docker login ghcr.io -u "$GHCR_USERNAME" --password-stdin
else
  echo "    Enter your GitHub username and a Personal Access Token with read:packages scope"
  echo "    (Or set GHCR_USERNAME and GHCR_TOKEN env vars to skip this prompt)"
  docker login ghcr.io
fi

# ---------------------------------------------------------------------------
# Pull and start both stacks
# ---------------------------------------------------------------------------
echo "==> Pulling dev stack images..."
docker compose -f "$PROJECT_DIR/docker-compose.dev.yml" pull

echo "==> Starting dev stack (port 5000)..."
docker compose -f "$PROJECT_DIR/docker-compose.dev.yml" up -d

echo "==> Pulling prod stack images..."
docker compose -f "$PROJECT_DIR/docker-compose.prod.yml" pull

echo "==> Starting prod stack (port 5001)..."
docker compose -f "$PROJECT_DIR/docker-compose.prod.yml" up -d

echo ""
echo "Both stacks are running!"
echo ""
echo "Next steps:"
echo "  Dev  (port 5000): http://<this-vm-ip>:5000/settings — configure dev API keys"
echo "  Prod (port 5001): http://<this-vm-ip>:5001/settings — configure prod API keys"
echo ""
echo "  Run your first dev ingest:"
echo "    docker compose -f docker-compose.dev.yml exec web python ingest.py --hours 48"
echo ""
echo "  Check status: ./scripts/docker-status.sh"
