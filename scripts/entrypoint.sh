#!/usr/bin/env bash
# entrypoint.sh — Docker container entrypoint for job-matcher-pr web service.
#
# Reads the database password from the Docker secret file and constructs
# DATABASE_URL before handing off to the main process. This keeps the
# plaintext password out of environment variables (not visible via
# `docker inspect` or /proc/1/environ).
#
# Requires POSTGRES_USER and POSTGRES_DB to be set as environment variables
# in docker-compose.yml (they contain no secret material).

set -euo pipefail

SECRET_FILE="/run/secrets/db_password"

if [ -f "$SECRET_FILE" ]; then
    DB_PASS=$(cat "$SECRET_FILE")
    export DATABASE_URL="postgresql://${POSTGRES_USER}:${DB_PASS}@db:5432/${POSTGRES_DB}"
else
    echo "WARNING: $SECRET_FILE not found — falling back to DATABASE_URL env var" >&2
fi

exec "$@"
