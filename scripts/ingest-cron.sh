#!/usr/bin/env bash
# ingest-cron.sh — Run daily ingest via host cron — no Docker socket required.
#
# Add to crontab with: crontab -e
# Suggested schedule (2am daily):
#   0 2 * * * /opt/job-matcher-pr/scripts/ingest-cron.sh >> /opt/job-matcher-pr/logs/ingest-cron.log 2>&1
#
# The `docker compose exec -T` command runs ingest.py inside the already-running
# web container. The -T flag disables pseudo-TTY allocation, which is required
# in non-interactive cron environments.

set -euo pipefail
cd "$(dirname "$0")/.."
docker compose exec -T web python ingest.py --hours 25
