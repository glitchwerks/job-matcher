# Docker Migration Plan — job-matcher-pr

## Context

The app currently deploys via a Windows-native stack (NSSM service + Task Scheduler + SQLite). Supporting two independent versions (public + private repos) is broken because paths are hardcoded throughout scripts, CI/CD, and config. Docker eliminates hardcoded paths: each repo becomes an independent compose stack differentiated by environment variables, named volumes, and port numbers — no path collisions possible.

**Infrastructure target:** Ubuntu 24.04 VM via Hyper-V on existing Windows Server 2019 host. PostgreSQL replaces SQLite (existing data is disposable). GitHub Container Registry (ghcr.io) for images.

---

## Architecture

```
[web container]  Flask/waitress — always running, port 5000
    └─ subprocess.Popen([sys.executable, "ingest.py"])  ← UI-triggered, works unchanged
[db container]   PostgreSQL 16 — named volume pgdata
[scheduler]      Ofelia — job-exec into web container at @daily
```

Single Dockerfile. Same image for both web and ingest (different CMD). The UI-triggered subprocess spawn requires both in the same container — this is preserved.

**Multi-repo:** Each repo gets its own compose stack with a unique `container_name` prefix and port (5000 public, 5001 private). Named volumes are scoped by project name. No hardcoded host paths anywhere.

---

## Milestone: `feat-docker-deploy`

### Issue 1 — Infrastructure: Provision Linux VM
**Labels:** `infrastructure`, `ops`  
Manual, one-time. No code changes.

- Create Ubuntu 24.04 VM in Hyper-V on Windows Server 2019
- Assign static LAN IP (used in `DEPLOY_SSH_HOST` secret)
- Install Docker Engine + Compose plugin via official apt repo
- Add deploy user to `docker` group
- Generate SSH keypair: private → `DEPLOY_SSH_KEY` GitHub secret; public → `~/.ssh/authorized_keys` on VM
- Open port 5000 inbound (ufw)
- Create `/opt/job-matcher-pr/` with `./config/` subdirectory, owned by uid 1000
- Acceptance: `ssh deploy@<vm> docker ps` succeeds from client machine

---

### Issue 2 — Database: Rewrite db.py for PostgreSQL
**Labels:** `database`, `breaking-change`  
**File:** `db.py` (full rewrite of connection layer and schema init)  
**File:** `requirements.txt` (add `psycopg2-binary`)  
**File:** `tests/test_db.py` (update `TempDB` fixture)  
**File:** `.github/workflows/ci.yml` (add `services: postgres`)

#### Connection layer

Replace `sqlite3` with a `_Conn` wrapper around psycopg2 that mimics `sqlite3.Connection`. This keeps **all 28 call sites in db.py unchanged** — `conn.execute(sql, params)`, `conn.commit()`, `conn.close()`, and `with conn:` all continue to work.

```python
class _Conn:
    def __init__(self, conn):
        self._conn = conn
        self._cursor = conn.cursor()
    def execute(self, sql, params=None):
        self._cursor.execute(sql, params or ())
        return self._cursor
    def commit(self): self._conn.commit()
    def close(self): self._cursor.close(); self._conn.close()
    def __enter__(self): return self
    def __exit__(self, *a): self.close()

def get_connection(db_path=None):  # db_path kept for signature compat, ignored
    raw = psycopg2.connect(_DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return _Conn(raw)
```

Replace `_DEFAULT_DB_PATH` with:
```python
_DATABASE_URL: str = os.environ.get(
    "DATABASE_URL",
    "postgresql://jobmatcher:jobmatcher@localhost:5432/jobmatcher"
)
```

#### SQL changes

| SQLite | PostgreSQL |
|--------|------------|
| `INTEGER PRIMARY KEY AUTOINCREMENT` | `SERIAL PRIMARY KEY` |
| `INSERT OR REPLACE INTO location_geocache` | `INSERT ... ON CONFLICT (location_text) DO UPDATE SET lat=EXCLUDED.lat, lon=EXCLUDED.lon, cached_at=CURRENT_TIMESTAMP` |
| `PRAGMA table_info(listings)` | `SELECT column_name FROM information_schema.columns WHERE table_name='listings'` |
| `?` positional params | `%s` |
| `:name` named params | `%(name)s` |
| `sqlite3.Row` row factory | `psycopg2.extras.RealDictCursor` (already returns dicts) |

#### Schema init (`init_db`)

Remove all A/B/C/D migration paths — data is disposable. Replace with a single `CREATE TABLE IF NOT EXISTS` for `listings` and `location_geocache`. Use `SERIAL PRIMARY KEY`. Keep JSON columns as `TEXT` (app still does `json.dumps`/`json.loads` — no JSONB change needed). Keep boolean flags as `INTEGER DEFAULT 0` for zero caller changes.

#### Tests

Add `services: postgres:16` to `ci.yml`. Update `TempDB` fixture in `test_db.py` to connect to the CI Postgres service via `DATABASE_URL` env var (set in the workflow). Remove the `tempfile.NamedTemporaryFile` pattern from `TempDB`.

#### credentials.py

No changes needed. The `if sys.platform == "win32":` spin-lock block is dead on Linux but harmless.

---

### Issue 3 — Fix ingest.py log path
**Labels:** `bug`  
**File:** `ingest.py` — `_configure_file_logging()` only

Currently derives log dir from `DB_PATH`. With Postgres, `DB_PATH` is unset.

```python
# Replace this:
db_abs  = os.path.abspath(os.environ.get("DB_PATH", "jobs.db"))
log_dir = os.path.join(os.path.dirname(db_abs), "logs")

# With this:
log_dir = os.environ.get(
    "LOG_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
)
```

In Docker: `LOG_DIR=/app/logs` set in `docker-compose.yml`. Logs are ephemeral across container restarts (acceptable for personal use). Mount `/app/logs` as a volume if persistence is needed later.

---

### Issue 4 — Dockerfile
**Labels:** `docker`  
**File:** `Dockerfile` (new)  
**File:** `.dockerignore` (new)

Multi-stage build. Builder installs deps; runtime is lean.

```dockerfile
FROM python:3.11-slim AS builder
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends gcc && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.11-slim AS runtime
RUN groupadd --gid 1000 appuser && useradd --uid 1000 --gid appuser --shell /bin/bash --create-home appuser
COPY --from=builder /install /usr/local
WORKDIR /app
COPY --chown=appuser:appuser . .
RUN mkdir -p /app/logs && chown appuser:appuser /app/logs
USER appuser
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/')" || exit 1
EXPOSE 5000
CMD ["waitress-serve", "--host=0.0.0.0", "--port=5000", "app:app"]
```

`.dockerignore` must exclude all user secrets:
```
.venv/
venv/
__pycache__/
*.pyc
config/config.json
config/keys.json
config/profile.json
config/providers.json
jobs.db
logs/
.git/
.github/
tests/
docs/
scripts/
*.md
.env
```

---

### Issue 5 — docker-compose.yml
**Labels:** `docker`  
**File:** `docker-compose.yml` (new)  
**File:** `.env.example` (new, committed)  
**File:** `.gitignore` (add `/.env`, `docker-compose.override.yml`)

```yaml
services:
  db:
    image: postgres:16-alpine
    container_name: job-matcher-pr-db
    volumes:
      - pgdata:/var/lib/postgresql/data
    environment:
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: ${POSTGRES_DB}
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER}"]
      interval: 10s
      timeout: 5s
      retries: 5
    restart: unless-stopped

  web:
    image: ghcr.io/cbeaulieu-gt/job-matcher-pr:latest
    container_name: job-matcher-pr-web
    depends_on:
      db:
        condition: service_healthy
    ports:
      - "5000:5000"
    volumes:
      - ./config:/app/config:rw
    environment:
      DATABASE_URL: postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@db:5432/${POSTGRES_DB}
      FLASK_DEBUG: "0"
      LOG_DIR: "/app/logs"
    restart: unless-stopped
    labels:
      ofelia.enabled: "true"
      ofelia.job-exec.daily-ingest.schedule: "@daily"
      ofelia.job-exec.daily-ingest.command: "python ingest.py --hours 25"

  scheduler:
    image: mcuadros/ofelia:latest
    container_name: job-matcher-pr-scheduler
    depends_on:
      - web
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    restart: unless-stopped
    command: daemon --docker

volumes:
  pgdata:
```

`.env.example`:
```env
POSTGRES_USER=jobmatcher
POSTGRES_PASSWORD=changeme
POSTGRES_DB=jobmatcher
```

**Ofelia note:** Docker socket is mounted read-only. This is the standard Ofelia pattern; acceptable trade-off for personal-use single-tenant deployment. Ofelia uses the `container_name: job-matcher-pr-web` label to target the correct container for `job-exec`.

**`./config` volume:** Must be owned by uid 1000 on the VM so the Settings UI can write `providers.json`. `docker-setup.sh` handles this.

---

### Issue 6 — Replace deploy.yml
**Labels:** `ci-cd`  
**File:** `.github/workflows/deploy.yml` (full replacement)

**New GitHub secrets required:**
- `DEPLOY_SSH_HOST` — VM IP or hostname
- `DEPLOY_SSH_USER` — e.g. `deploy`
- `DEPLOY_SSH_KEY` — PEM private key

**New workflow: two jobs**

`build-and-push`:
1. Checkout
2. `docker/login-action@v3` with `registry: ghcr.io`, `password: ${{ secrets.GITHUB_TOKEN }}`
3. `docker/setup-buildx-action@v3`
4. `docker/build-push-action@v5` — tags `:latest` and `:<sha>`, GHA cache

`deploy` (depends on `build-and-push`):
1. `appleboy/ssh-action@v1` with the three SSH secrets
2. On VM: `cd /opt/job-matcher-pr && docker compose pull && docker compose up -d --remove-orphans && docker system prune -f --filter "until=24h"`

**GHCR package setup:** After first push, link package to repo and set visibility (private repos need explicit package linking in GitHub settings).

**Trigger:** Same as current — `workflow_run` on CI completing successfully on `main`.

Ensure repo Settings → Actions → General → Workflow permissions = "Read and write" for `GITHUB_TOKEN` to push packages.

---

### Issue 7 — Linux ops scripts
**Labels:** `ops`, `scripts`  
**Files:** `scripts/docker-setup.sh`, `scripts/docker-status.sh`, `scripts/docker-teardown.sh` (all new)

All: `#!/usr/bin/env bash`, `set -euo pipefail`, chmod +x. PowerShell scripts are **kept** (public repo may use the Windows path).

**`docker-setup.sh`:** Check Docker installed → create dirs → `chown 1000:1000 config/` → copy `.env.example` → copy example config files if absent → `docker login ghcr.io` → `docker compose pull` → `docker compose up -d` → print next steps (navigate to `:5000/settings`).

**`docker-status.sh`:** `docker compose ps` → last 20 web logs → psql row count query → print masked env vars from `.env`.

**`docker-teardown.sh`:** Confirm prompt → `docker compose down` → optional `-v` prompt for volume delete → note that `config/` is preserved.

---

## File Change Summary

| File | Action |
|------|--------|
| `db.py` | Rewrite connection layer + schema init for psycopg2 |
| `ingest.py` | `_configure_file_logging()`: add `LOG_DIR` env var (3 lines) |
| `requirements.txt` | Add `psycopg2-binary` |
| `tests/test_db.py` | Update `TempDB` fixture for Postgres |
| `.github/workflows/ci.yml` | Add `services: postgres:16` |
| `.github/workflows/deploy.yml` | Full replacement (GHCR build + SSH deploy) |
| `Dockerfile` | New |
| `.dockerignore` | New |
| `docker-compose.yml` | New |
| `.env.example` | New (committed) |
| `scripts/docker-setup.sh` | New |
| `scripts/docker-status.sh` | New |
| `scripts/docker-teardown.sh` | New |
| `.gitignore` | Add `/.env`, `docker-compose.override.yml` |
| `CLAUDE.md` | Add Docker deployment section |

**Unchanged:** `app.py`, `credentials.py`, all plugin files, templates, static assets, `publish.yml`, and all other workflows.

---

## Suggested Branch Strategy

```
main
 └─ feat-docker-deploy            ← primary feature branch
      ├─ feat-docker-deploy-db    ← db.py + test changes (Issues 2+3)
      ├─ feat-docker-deploy-image ← Dockerfile + compose (Issues 4+5)
      └─ feat-docker-deploy-cicd  ← deploy.yml + scripts (Issues 6+7)
```

Sub-branches merge into `feat-docker-deploy`; that merges into `main` once VM is verified.

---

## Verification

**After db.py rewrite:**
1. `pytest` — all tests pass against CI Postgres
2. `DATABASE_URL=... python -c "import db; db.init_db()"` — no errors, tables created
3. `DATABASE_URL=... python app.py` — server starts, `/` returns 200

**After Dockerfile + compose:**
1. `docker build -t job-matcher .` — succeeds (~300MB)
2. `docker compose up -d` — all three containers healthy
3. `curl http://localhost:5000/` — returns HTML
4. POST to `/ingest/trigger` — subprocess spawns, HTMX polls correctly
5. `docker compose exec web python ingest.py --hours 1` — runs cleanly

**After deploy.yml:**
1. Push trivial change to `main` → CI passes → Deploy triggers
2. GHCR shows new image tagged `:latest` and `:<sha>`
3. SSH step succeeds; `docker compose ps` on VM shows all healthy

**After cutover (Issue 1 + full stack):**
1. Configure API keys via Settings UI at `http://<vm>:5000/settings`
2. `docker compose exec web python ingest.py --hours 48` — listings appear in UI
3. Next day: `docker compose logs scheduler` confirms Ofelia fired `@daily`

---

## Risks

| Risk | Impact | Mitigation |
|------|--------|-----------|
| psycopg2 `?` → `%s` param style missed in a query | High — runtime crash | Integration test every `db.py` function against real Postgres before merge |
| Ofelia container name mismatch | Medium — silent scheduling failure | Explicit `container_name: job-matcher-pr-web`; verify with `docker compose logs scheduler` on first start |
| `./config` volume permissions (uid mismatch) | Medium — Settings UI can't save | `docker-setup.sh` must `chown 1000:1000 config/` |
| `GITHUB_TOKEN` lacks `packages: write` | High — GHCR push fails immediately | Set repo → Settings → Actions → Workflow permissions = "Read and write" before first push |
| Ingest subprocess doesn't inherit `DATABASE_URL` | Medium — UI-triggered ingest can't reach DB | `subprocess.Popen` inherits parent env by default; verify with one triggered run |