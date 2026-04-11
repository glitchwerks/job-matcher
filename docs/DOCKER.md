# Docker Deployment

This guide covers deploying Job Matcher as a Docker Compose stack on a Linux VM. It assumes familiarity with Docker basics but no prior knowledge of this project.

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Quick Start](#quick-start)
3. [Environment Configuration](#environment-configuration)
4. [Stack Architecture](#stack-architecture)
5. [First-Time Server Setup](#first-time-server-setup)
6. [Day-to-Day Operations](#day-to-day-operations)
7. [CI/CD Pipeline](#cicd-pipeline)
8. [Required GitHub Secrets](#required-github-secrets)
9. [Scheduled Ingestion](#scheduled-ingestion)
10. [Backups](#backups)
11. [Troubleshooting](#troubleshooting)
12. [Teardown](#teardown)
13. [Migrating from Windows NSSM Deployment](#migrating-from-windows-nssm-deployment)

---

## Prerequisites

- **Docker Engine** 24+ with the **Docker Compose plugin** (`docker compose`, not the legacy `docker-compose`)
- A **Linux VM** (Ubuntu 22.04 LTS recommended) reachable on your network
- **GHCR access** — a GitHub account with `read:packages` scope, or a Personal Access Token with that scope
- Ports **5000** (dev) and **5001** (prod) open in the VM's firewall

Verify Docker is ready before running the setup script:

```bash
docker --version
docker compose version
```

---

## Quick Start

```bash
# 1. Clone the repo into /opt (recommended path — scripts reference it)
sudo git clone https://github.com/cbeaulieu-gt/job-matcher-pr.git /opt/job-matcher-pr
cd /opt/job-matcher-pr

# 2. Run the one-time setup script (requires root for chown)
sudo ./scripts/docker-setup.sh
```

The script is interactive — it will pause and ask you to set passwords before continuing. See [First-Time Server Setup](#first-time-server-setup) for a detailed walkthrough.

After the script completes, both stacks are running:

- **Dev** — `http://<vm-ip>:5000/settings` — configure dev API keys
- **Prod** — `http://<vm-ip>:5001/settings` — configure prod API keys

---

## Environment Configuration

Each stack reads from its own env file: `.env.dev` for dev and `.env.prod` for prod. Copy from the examples to get started:

```bash
cp .env.dev.example .env.dev
cp .env.prod.example .env.prod
```

### Variables

| Variable | Required | Description |
|---|---|---|
| `POSTGRES_USER` | Yes | PostgreSQL username. Default: `jobmatcher`. |
| `POSTGRES_PASSWORD` | Yes | PostgreSQL password. Must be at least 12 characters. The setup script will reject placeholder values (`changeme`, `your-strong-password-here`). Use a randomly generated password. |
| `POSTGRES_DB` | Yes | Database name. Dev value: `jobmatcher_dev`. Prod value: `jobmatcher_prod`. |

> **Note:** `DATABASE_URL` is not set in the env file. It is assembled by Docker Compose variable substitution in the `environment:` block of each Compose file (e.g. `postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@db:5432/jobmatcher_prod`). The password is visible via `docker inspect` on the web container.

Additional variables that are set in the Compose file itself (not the env file):

| Variable | Value | Description |
|---|---|---|
| `FLASK_DEBUG` | `"0"` | Always disabled in both stacks. Do not enable in production. |
| `LOG_DIR` | `"/app/logs"` | Log directory inside the container — mounted from `./logs` or `./logs-dev` on the host. |
| `APP_ENV` | `dev` or `prod` | Identifies which stack is running. |
| `APP_VERSION` | `${APP_VERSION:-latest}` or `${APP_VERSION:-local}` | Set by CI to the commit SHA; falls back to `latest`/`local` if unset. |

---

## Stack Architecture

Two independent Compose stacks run on the same host, each fully isolated.

```
Host VM
├── job-matcher-pr-dev  (Compose project)
│   ├── web             → ghcr.io/.../job-matcher-pr:<sha>  → port 5000
│   ├── db              → postgres:16-alpine  → pgdata_dev volume
│   └── scheduler       → mcuadros/ofelia (cron executor)
│
└── job-matcher-pr-prod (Compose project)
    ├── web             → ghcr.io/.../job-matcher-pr:latest  → port 5001
    ├── db              → postgres:16-alpine  → pgdata_prod volume
    └── scheduler       → mcuadros/ofelia (cron executor)
```

### Isolation

| Aspect | Dev | Prod |
|---|---|---|
| Compose project name | `job-matcher-pr-dev` | `job-matcher-pr-prod` |
| Port | `5000` | `5001` |
| Database | `jobmatcher_dev` | `jobmatcher_prod` |
| Data volume | `pgdata_dev` | `pgdata_prod` |
| Config directory | `./config-dev/` | `./config/` |
| Log directory | `./logs-dev/` | `./logs/` |
| Image tag | SHA tag (branch builds) | `latest` (main builds) |
| Env file | `.env.dev` | `.env.prod` |

The two stacks share a Docker network namespace on the host but each Compose project uses its own internal network, so containers in one stack cannot reach containers in the other by service name.

### Services

**`web`** — Flask application served by waitress on port 5000 (internal). Runs as `appuser` (uid 1000, gid 1000). Mounts `./config[-dev]/` read-write because the `/settings` UI writes `providers.json` at runtime.

**`db`** — PostgreSQL 16. Exposes port 5432 only within the Compose network. Data is persisted in a named Docker volume. The `web` service waits for a healthy `db` before starting (`condition: service_healthy`).

**`scheduler`** — [Ofelia](https://github.com/mcuadros/ofelia) reads `ofelia.job-exec.*` labels from the `web` container and runs `python ingest.py --hours 25` inside it once daily (`@daily`). It requires Docker socket access (mounted read-only at `/var/run/docker.sock:ro`) to exec into the web container. See the security note in the Compose files for implications.

### The application image

The image is built in two stages (`Dockerfile`):

1. **Builder** — installs Python dependencies into a `/install` prefix
2. **Runtime** — copies dependencies from the builder, adds a non-root `appuser`, sets up the healthcheck, and configures the entrypoint

The entrypoint (`scripts/entrypoint.sh`) performs any pre-start setup before handing off to the main process. `DATABASE_URL` is injected by Docker Compose, not constructed by the entrypoint.

---

## First-Time Server Setup

Run `sudo ./scripts/docker-setup.sh` from `/opt/job-matcher-pr/`. The script is idempotent — re-running it after a partial failure is safe.

### What the script does, step by step

1. **Checks Docker** — verifies Docker Engine, the Compose plugin, and daemon connectivity.

2. **Creates directories** — `config/`, `config-dev/`, `logs/`, and `logs-dev/`. Sets ownership of `config/` and `config-dev/` to uid 1000 (`appuser`) so the web container can write `providers.json` at runtime.

3. **Creates `.env.dev`** — copies `.env.dev.example` and pauses so you can set a real `POSTGRES_PASSWORD`. Rejects placeholder values and passwords shorter than 12 characters.

4. **Creates `.env.prod`** — same process for the prod env file.

5. **Copies example config files** — for each `config/*.example.json`, creates `config/<name>.json` and `config-dev/<name>.json` if they do not exist. Sets ownership to uid 1000. This gives both stacks a working `config.json`, `profile.json`, and `providers.json` to start from.

6. **Logs in to GHCR** — runs `docker login ghcr.io`. If `GHCR_USERNAME` and `GHCR_TOKEN` are set in the environment, uses them non-interactively. Otherwise prompts for credentials.

7. **Pulls and starts both stacks** — runs `docker compose pull` then `docker compose up -d` for both dev and prod.

8. **Prints cron instructions** — outputs the exact `crontab -e` lines to add for scheduled ingest and database backups (see [Scheduled Ingestion](#scheduled-ingestion)).

### After setup

Open the Settings page to enter your API keys:

```
http://<vm-ip>:5000/settings   ← dev
http://<vm-ip>:5001/settings   ← prod
```

The Settings UI validates each key before saving it to `config[-dev]/providers.json`.

Run your first ingest manually to confirm everything works:

```bash
docker compose -p job-matcher-pr-dev -f docker-compose.dev.yml exec web python ingest.py --hours 48
```

---

## Day-to-Day Operations

### Check status

```bash
./scripts/docker-status.sh
```

This prints container health, the last 20 log lines from each web container, a live database row count for each stack, and a summary of each stack's environment variables (passwords are masked).

### View logs

```bash
# Tail web container logs (live)
docker compose -p job-matcher-pr-prod -f docker-compose.prod.yml logs web -f

# Last 50 lines, no prefix
docker compose -p job-matcher-pr-dev -f docker-compose.dev.yml logs web --tail=50 --no-log-prefix

# Ingest log written to the host log directory
tail -f logs/ingest-cron.log
```

### Run ingest manually

```bash
# Prod — standard run
docker compose -p job-matcher-pr-prod -f docker-compose.prod.yml exec web python ingest.py --hours 25

# Prod — re-score all existing listings against an updated profile
docker compose -p job-matcher-pr-prod -f docker-compose.prod.yml exec web python ingest.py --rescore

# Dev — verbose output
docker compose -p job-matcher-pr-dev -f docker-compose.dev.yml exec web python ingest.py --hours 48 --verbose
```

### Restart a stack

```bash
# Restart just the web container (e.g. after editing config files)
docker compose -p job-matcher-pr-prod -f docker-compose.prod.yml restart web

# Full stack restart
docker compose -p job-matcher-pr-prod -f docker-compose.prod.yml down
docker compose -p job-matcher-pr-prod --env-file .env.prod -f docker-compose.prod.yml up -d
```

### Pull a new image manually

CI handles this automatically on push to main. To pull and redeploy manually:

```bash
docker compose -p job-matcher-pr-prod --env-file .env.prod -f docker-compose.prod.yml pull
docker compose -p job-matcher-pr-prod --env-file .env.prod -f docker-compose.prod.yml up -d --remove-orphans
```

### Open a psql shell

```bash
# Prod
docker compose -p job-matcher-pr-prod -f docker-compose.prod.yml exec db psql -U jobmatcher jobmatcher_prod

# Dev
docker compose -p job-matcher-pr-dev -f docker-compose.dev.yml exec db psql -U jobmatcher jobmatcher_dev
```

---

## CI/CD Pipeline

The pipeline is defined in `.github/workflows/deploy.yml` and runs on the self-hosted Linux runner registered on the VM.

### Flow

```
git push origin <branch>
        │
        ▼
CI workflow runs (tests + linting)
        │ passes
        ▼
Deploy workflow triggers (workflow_run on CI success)
        │
        ├── build-and-push job (runs on ubuntu-latest, GitHub-hosted)
        │   ├── Builds Docker image via docker/build-push-action
        │   ├── Non-main branch → pushes ghcr.io/.../job-matcher-pr:<sha>
        │   └── main branch     → pushes :latest and :<sha>
        │
        ├── deploy-dev job (non-main branches, self-hosted runner)
        │   ├── Pulls SHA-tagged image
        │   └── Deploys dev stack with APP_VERSION=<sha>
        │
        └── deploy-prod job (main only, self-hosted runner)
            ├── Pulls :latest image
            └── Deploys prod stack with APP_VERSION=<sha>
```

### Key details

- The deploy jobs run on `[self-hosted, linux]` — the GitHub Actions runner must be installed and running on the VM (see [First-Time Server Setup](#first-time-server-setup)).
- CI must pass before any auto-deploy job runs (`if: github.event.workflow_run.conclusion == 'success'`).
- Deploys use `up -d --remove-orphans` which replaces containers in-place with zero downtime for the database.
- After each deploy, old images are pruned to reclaim disk space.
- The deploy workflow does **not** SSH into the VM. Instead, the self-hosted runner executes `docker compose` commands directly on the host. This means no `DEPLOY_SSH_*` secrets are needed for the current deployment model.

### Manual deploy (workflow_dispatch)

Any branch can be deployed on demand from the GitHub Actions UI without waiting for a push-triggered CI run:

1. Go to **Actions → Deploy** in the GitHub repository.
2. Click **Run workflow**, select the target branch from the dropdown, and click **Run workflow**.

The manual path runs these jobs in sequence:

```
workflow_dispatch (branch selected in UI)
        │
        ▼
manual-test job — runs pytest with a Postgres service container
        │ passes (lint not required)
        ▼
build-and-push-manual job — builds + pushes image to GHCR
        │   Non-main → ghcr.io/.../job-matcher-pr:<sha> and :<short-sha>-<branch>
        │   main     → :latest, :<sha>, and :<short-sha>-main
        ▼
deploy-dev-manual  (if branch != main) → dev stack, port 5000
deploy-prod-manual (if branch == main) → prod stack, port 5001
```

If `pytest` fails the deploy jobs are skipped. Lint (ruff) is not a gate for manual deploys — see issue #197 for rationale.

### Registering the self-hosted runner

The runner must be registered once on the VM. Follow the GitHub documentation at `https://github.com/<owner>/<repo>/settings/actions/runners` to download and configure the runner with the `self-hosted` and `linux` labels. Install it as a systemd service so it restarts automatically:

```bash
sudo ./svc.sh install
sudo ./svc.sh start
```

---

## Required GitHub Secrets

The deploy workflow itself uses only `GITHUB_TOKEN` (automatically provided by GitHub Actions) to authenticate with GHCR — no additional secrets are required for the build and deploy jobs.

The following secrets are used by other workflows (CI failure diagnosis):

| Secret | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key used by Claude to diagnose CI failures automatically |
| `GH_PAT` | GitHub Personal Access Token with `repo` scope — used to post PR comments and push auto-fix commits |

These are added in **Settings → Secrets and variables → Actions** on the GitHub repository.

---

## Scheduled Ingestion

Ingestion is scheduled in two independent ways: via Ofelia inside the container, and optionally via host cron.

### Ofelia (container-side, built in)

Both Compose files include an Ofelia `scheduler` service. It reads labels on the `web` container:

```yaml
ofelia.job-exec.daily-ingest-prod.schedule: "@daily"
ofelia.job-exec.daily-ingest-prod.command: "python ingest.py --hours 25"
```

Ofelia executes `docker compose exec` inside the running web container once per day at midnight UTC. No host configuration is required — this starts automatically when the stack comes up.

### Host cron (alternative/additional)

`scripts/ingest-cron.sh` is a lightweight wrapper that runs ingest inside the prod web container via `docker compose exec -T`. The `-T` flag disables pseudo-TTY allocation, which is required for non-interactive cron environments.

To schedule it, add to the host crontab with `crontab -e`:

```cron
# Run ingest daily at 2am
0 2 * * * /opt/job-matcher-pr/scripts/ingest-cron.sh >> /opt/job-matcher-pr/logs/ingest-cron.log 2>&1
```

If you use host cron, consider disabling Ofelia to avoid double-runs. Remove the `scheduler` service from the Compose file or simply remove the `ofelia.*` labels from the `web` service.

---

## Backups

The PostgreSQL data volumes (`pgdata_dev`, `pgdata_prod`) are the only copies of your data. `docker compose down -v` destroys them permanently.

### Manual backup

```bash
./scripts/backup.sh
```

Creates a timestamped SQL dump in `./backups/` and removes backups older than the 10 most recent.

### Automated daily backup

Add to `crontab -e`:

```cron
30 1 * * * /opt/job-matcher-pr/scripts/backup.sh >> /opt/job-matcher-pr/logs/backup.log 2>&1
```

### Restore from backup

```bash
docker compose -p job-matcher-pr-prod exec -T db psql -U jobmatcher jobmatcher_prod < backups/jobs_YYYYMMDD_HHMMSS.sql
```

Replace `jobs_YYYYMMDD_HHMMSS.sql` with the filename of the backup to restore.

---

## Troubleshooting

### Container won't start

Check logs for the failing service:

```bash
docker compose -p job-matcher-pr-prod -f docker-compose.prod.yml logs web
docker compose -p job-matcher-pr-prod -f docker-compose.prod.yml logs db
```

Common causes:
- **Missing env file** — `.env.prod` does not exist. Run `cp .env.prod.example .env.prod` and edit it.
- **Wrong password in env file** — `POSTGRES_PASSWORD` in `.env.prod` does not match what the database was initialized with. The volume must be re-created (destructive) or the password reset via `psql`.
- **Port already in use** — another process is bound to port 5000 or 5001. Find it with `ss -tlnp | grep 500`.

### Database connection refused

The web container starts before `db` is healthy. The Compose file uses `condition: service_healthy` on the `db` service, so the web container should wait automatically. If the db healthcheck is failing:

```bash
docker compose -p job-matcher-pr-prod -f docker-compose.prod.yml ps
docker compose -p job-matcher-pr-prod -f docker-compose.prod.yml exec db pg_isready -U jobmatcher
```

### Log directory permission errors (issue #80)

The `config/` and `config-dev/` directories must be owned by uid 1000 (`appuser`) so the web container can write `providers.json`. The `logs/` and `logs-dev/` directories must also be writable. If the web container exits with a permission error on startup:

```bash
sudo chown -R 1000:1000 /opt/job-matcher-pr/config
sudo chown -R 1000:1000 /opt/job-matcher-pr/config-dev
sudo chown -R 1000:1000 /opt/job-matcher-pr/logs
sudo chown -R 1000:1000 /opt/job-matcher-pr/logs-dev
```

### GHCR authentication failures

The deploy workflow uses `GITHUB_TOKEN` for GHCR pushes (build job, GitHub-hosted runner) and the self-hosted runner uses the same token for pulls. If the pull step fails with `unauthorized`:

- Verify the repository's GHCR package visibility is set to **public** or the runner's GitHub token has `read:packages` scope.
- Re-run the docker login step manually on the VM: `docker login ghcr.io`

### Image not updating after push to main

1. Confirm CI passed: check the Actions tab on GitHub.
2. Confirm the deploy workflow triggered and the `deploy-prod` job ran on the self-hosted runner.
3. Check the runner is online: `sudo systemctl status actions.runner.*`
4. Pull manually: `docker compose -p job-matcher-pr-prod --env-file .env.prod -f docker-compose.prod.yml pull`

### Ingest not running on schedule

Check whether Ofelia is running and inspect its logs:

```bash
docker compose -p job-matcher-pr-prod -f docker-compose.prod.yml ps scheduler
docker compose -p job-matcher-pr-prod -f docker-compose.prod.yml logs scheduler
```

If using host cron instead, verify the crontab entry:

```bash
crontab -l
cat /opt/job-matcher-pr/logs/ingest-cron.log
```

---

## Teardown

```bash
./scripts/docker-teardown.sh
```

The script is interactive and asks for confirmation separately for dev and prod. For each stack, it also asks whether to delete the data volume.

### What is preserved vs deleted

| Item | Behavior |
|---|---|
| `./config/` and `./config-dev/` | Always preserved — not touched by teardown |
| `./logs/` and `./logs-dev/` | Always preserved |
| `./backups/` | Always preserved |
| Containers | Removed on confirmation |
| `pgdata_dev` / `pgdata_prod` volumes | Removed only if explicitly confirmed — **this destroys all job data** |
| Docker images | Not removed by default — remove manually if desired |

To remove images after teardown:

```bash
docker rmi $(docker images --format '{{.Repository}}:{{.Tag}}' | grep 'job-matcher-pr')
```

---

## Migrating from Windows NSSM Deployment

The Windows-native deployment used SQLite (`jobs.db`) and NSSM + Task Scheduler. The Docker deployment uses PostgreSQL. There is no automatic migration path for the database — the schema is compatible but the data formats differ.

### Steps to migrate

1. **Export your bookmarks and applied listings** from the Windows instance while it is still running (the UI does not have an export feature; query the SQLite database directly if needed).

2. **Complete [First-Time Server Setup](#first-time-server-setup)** on the Linux VM.

3. **Copy your config files** from the Windows instance to the Linux VM:
   - `config/config.json` → `/opt/job-matcher-pr/config/config.json`
   - `config/profile.json` → `/opt/job-matcher-pr/config/profile.json`
   - `config/providers.json` → `/opt/job-matcher-pr/config/providers.json` (contains API keys)

   Set correct ownership after copying:
   ```bash
   sudo chown 1000:1000 /opt/job-matcher-pr/config/*.json
   ```

4. **Run the first ingest** on the Docker stack to populate the PostgreSQL database:
   ```bash
   docker compose -p job-matcher-pr-prod -f docker-compose.prod.yml exec web python ingest.py --hours 48
   ```

5. **Stop the Windows service** once you have confirmed the Docker stack is working correctly:
   ```powershell
   nssm stop JobMatcher
   nssm remove JobMatcher confirm
   ```

The CI/CD pipeline's self-hosted runner workflows (`deploy.yml`) reference the Docker stack. The Windows-era `deploy.yml` used a different flow (git pull + nssm restart). Ensure the runner registered on the VM is the one used by the current workflow.
