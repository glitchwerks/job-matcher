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
# uid 1000 = appuser defined in the Dockerfile — required so the web container
# can write to config/ (the /settings UI saves providers.json at runtime).
chown -R 1000:1000 "$PROJECT_DIR/config"
echo "    config/ owned by uid 1000"

echo "==> Creating .env from .env.example..."
if [[ ! -f "$PROJECT_DIR/.env" ]]; then
  cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
  echo "    Created .env — IMPORTANT: edit it and set a strong POSTGRES_PASSWORD before continuing"
  echo ""
  echo "    nano $PROJECT_DIR/.env"
  echo ""
  read -r -p "Press Enter when .env is configured, or Ctrl+C to abort..."
  if grep -q "your-strong-password-here\|changeme" "$PROJECT_DIR/.env" 2>/dev/null; then
    echo "ERROR: POSTGRES_PASSWORD in .env still contains the example value. Set a real password first." >&2
    exit 1
  fi
else
  echo "    .env already exists, skipping"
fi

echo "==> Copying example config files..."
for example in "$PROJECT_DIR/config/"*.example.json; do
  target="${example%.example.json}.json"
  target_name="$(basename "$target")"
  if [[ ! -f "$target" ]]; then
    cp "$example" "$target"
    chown 1000:1000 "$target"
    echo "    Created config/$target_name"
  else
    echo "    config/$target_name already exists, skipping"
  fi
done

echo "==> Logging in to GitHub Container Registry..."
if [[ -n "${GHCR_TOKEN:-}" && -n "${GHCR_USERNAME:-}" ]]; then
  echo "$GHCR_TOKEN" | docker login ghcr.io -u "$GHCR_USERNAME" --password-stdin
else
  echo "    Enter your GitHub username and a Personal Access Token with read:packages scope"
  echo "    (Or set GHCR_USERNAME and GHCR_TOKEN env vars to skip this prompt)"
  docker login ghcr.io
fi

echo "==> Pulling images..."
docker compose -f "$PROJECT_DIR/docker-compose.yml" pull

echo "==> Starting stack..."
docker compose -f "$PROJECT_DIR/docker-compose.yml" up -d

echo ""
echo "✓ Stack is running!"
echo ""
echo "Next steps:"
echo "  1. Open http://<this-vm-ip>:5000/settings to configure API keys"
echo "  2. Run your first ingest: docker compose exec web python ingest.py --hours 48"
echo "  3. Check status: ./scripts/docker-status.sh"
echo ""
echo "==> Setting up scheduled ingest (host cron)..."
chmod +x "$PROJECT_DIR/scripts/ingest-cron.sh"
echo "    Made scripts/ingest-cron.sh executable"
echo ""
echo "    To schedule daily ingest at 2am, run: crontab -e"
echo "    Then add this line:"
echo ""
echo "    0 2 * * * $PROJECT_DIR/scripts/ingest-cron.sh >> $PROJECT_DIR/logs/ingest-cron.log 2>&1"
echo ""
echo "    This runs ingest inside the web container via 'docker compose exec'."
echo "    No Docker socket mount is required."
echo ""
echo "==> Optional: automated daily backup"
chmod +x "$PROJECT_DIR/scripts/backup.sh"
echo "    Made scripts/backup.sh executable"
echo ""
echo "    To schedule a daily backup at 01:30, add to host crontab (crontab -e):"
echo ""
# Optional: automated daily backup (add to host crontab)
# 30 1 * * * /opt/job-matcher-pr/scripts/backup.sh >> /opt/job-matcher-pr/logs/backup.log 2>&1
echo "    30 1 * * * $PROJECT_DIR/scripts/backup.sh >> $PROJECT_DIR/logs/backup.log 2>&1"
echo ""
echo "    Backups are written to $PROJECT_DIR/backups/ and the 10 most recent are kept."
echo "    To restore: docker compose exec -T db psql -U jobmatcher jobmatcher < backups/jobs_YYYYMMDD_HHMMSS.sql"
