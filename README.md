# Job Matcher

A locally-run job search tool that aggregates listings from multiple sources (Adzuna, Remotive, RemoteOK, Arbeitnow, Himalayas, The Muse, USAJobs, Jobicy, Jooble), scores each one against your personal skills profile using an LLM, and surfaces ranked results in a browser-based feed. Run the ingestion script on demand or on a schedule, then open the UI to review, bookmark, or dismiss listings.

---

## Prerequisites

- Python 3.11+
- An LLM provider API key — [Anthropic](https://console.anthropic.com/), [OpenAI](https://platform.openai.com/), or [Google Gemini](https://aistudio.google.com/)
- An [Adzuna API account](https://developer.adzuna.com/) (optional — only needed if you want to use the Adzuna source)

---

## Setup

**1. Clone or download the repo**

```bash
git clone <repo-url>
cd job_matcher
```

**2. Create and activate a virtual environment**

```bash
# bash / macOS / Linux
uv venv
source .venv/bin/activate

# PowerShell (or: uv run python ...)
uv venv
.venv\Scripts\Activate.ps1
```

**3. Install dependencies**

```bash
uv pip install -r requirements.txt
```

**4. Copy the config files**

```bash
# bash / macOS / Linux
cp config/config.example.json config/config.json
cp config/providers.example.json config/providers.json
cp config/profile.example.json config/profile.json

# PowerShell
Copy-Item config\config.example.json config\config.json
Copy-Item config\providers.example.json config\providers.json
Copy-Item config\profile.example.json config\profile.json
```

`config/providers.json` is the unified credential store for all LLM providers (Anthropic, OpenAI, Gemini) and job sources (Adzuna, Jooble, etc.). The easiest way to configure it is through the `/settings` UI after starting the web server — it validates keys and saves them for you. You can also edit the file directly by following the structure in `config/providers.example.json`.

> **Migrating from `keys.json`?** If you have an existing `config/keys.json` from a previous install, it will be auto-migrated to `providers.json` on first run — no manual action needed.

**5. Fill in `config/config.json`**

`config/config.json` holds Adzuna-specific search parameters (`country`, `what`, `where`, etc.) and global scoring/filter options. Which sources are enabled and all source credentials (including Adzuna) are configured via the `/settings` UI after starting the web server.

The **Search Settings** tab in `/settings` now exposes `country`, `what`, `where`, `results_per_page`, and `max_pages` directly — you no longer need to edit `config.json` by hand for these fields. If Adzuna is enabled but any required search field is missing, a warning banner is shown on the Settings page and the ingest drawer blocks the start button until the gaps are filled.

LLM provider keys (Anthropic, OpenAI, Gemini) are configured via `config/providers.json` and the `/settings` UI.

**Adzuna source settings (optional — only needed if you want to use the Adzuna source)**

| Key | Required | Notes |
|---|---|---|
| `adzuna_app_id` | If using Adzuna | From your Adzuna developer dashboard |
| `adzuna_app_key` | If using Adzuna | From your Adzuna developer dashboard |
| `search.country` | If using Adzuna | Adzuna country code: `us`, `gb`, `au`, etc. (Adzuna-specific) |
| `search.what` | If using Adzuna | Keyword query sent to Adzuna, e.g. `"software engineer"` (Adzuna-specific; other sources use their own query logic) |
| `search.where` | No | Location filter for Adzuna, e.g. `"miami"`. Omit or leave empty for nationwide. (Adzuna-specific) |
| `search.results_per_page` | If using Adzuna | Max 50 (Adzuna API limit) |
| `search.max_pages` | If using Adzuna | Number of pages to fetch per run. 5 pages at 50 results = up to 250 raw listings. |

**Global settings**

| Key | Required | Notes |
|---|---|---|
| `scoring.threshold` | Yes | Minimum score (0–10) for a listing to appear in the feed |
| `search.salary_min` | No | Minimum salary in local currency. Listings with no salary data are allowed through regardless. Applies to all sources. |
| `prefilter.title_include` | No | Listing title must match at least one of these (case-insensitive substring). Omit to allow all titles. |
| `prefilter.title_exclude` | No | Listing title must match none of these. |
| `prefilter.require_contract_time` | No | e.g. `"full_time"`. Set to `null` to skip this check. |
| `prefilter.require_contract_type` | No | e.g. `"permanent"`. Set to `null` to skip this check. |

**6. Edit `config/profile.json` to match your skills**

See [Customising your profile](#customising-your-profile) below.

---

## Local Development (VS Code + Docker)

For developers working in VS Code with Docker Desktop, a setup script and task suite handle the full local workflow — including PostgreSQL via Docker Compose.

### First-time setup

Run the setup script once from a PowerShell terminal in the repo root (or any worktree):

```powershell
.\scripts\setup-local.ps1
```

The script is **idempotent** — safe to re-run at any time without clobbering existing config files. It:

1. Creates `.venv` via `uv venv` (falls back to `python -m venv` if `uv` is not installed)
2. Installs all Python dependencies via `uv pip install -r requirements.txt`
3. Copies `config/*.example.json` files to their non-example names (skips any that already exist)
4. Copies `.env.dev.example` → `.env.dev` (skips if it already exists)
5. Creates the `logs/` directory if missing
6. Prints next-steps guidance

After the script runs, open `config/providers.json` (or use the `/settings` UI) to add your LLM API keys, and edit `config/profile.json` to match your skills.

> **Important — set `SECRET_KEY` in `.env.dev` / `.env.prod` before starting Docker:**
> Flask uses `SECRET_KEY` to sign session cookies. `docker compose up` will now
> **refuse to start** if `SECRET_KEY` is missing or empty — the compose file uses
> the `:?` error syntax so the container exits immediately with a clear message
> rather than silently using an insecure value. The app itself also validates the
> key at startup and raises a `RuntimeError` if it is absent or starts with
> `changeme`. Generate a stable key once and add it to `.env.dev` (and `.env.prod`
> for production):
> ```powershell
> python -c "import secrets; print(secrets.token_hex(32))"
> # Paste the output as SECRET_KEY=<value> in .env.dev / .env.prod
> ```
> See `.env.dev.example` and `.env.prod.example` for the expected format.

### VS Code tasks

All common workflows are available as VS Code tasks. Open the Command Palette (`Ctrl+Shift+P`) and run **Tasks: Run Task**, or use `Ctrl+Shift+B` to trigger the default build task.

| Task | What it does |
|---|---|
| **Start Job Matcher** (`Ctrl+Shift+B`) | Starts the dev DB and web UI in parallel (default build task) |
| **Start Dev DB** | Spins up the PostgreSQL container via `docker-compose.dev.yml` |
| **Start Web UI** | Runs `app.py` with `DATABASE_URL` pre-set for the dev database |
| **Run Ingestion** | Runs `ingest.py --hours 24` against the dev database |
| **Rescore Listings** | Runs `ingest.py --rescore` to re-evaluate all stored listings |
| **Seed Demo DB** | Populates the database with demo data |
| **Start Web UI (Demo)** | Runs `app.py --demo` for a demo walkthrough |
| **Setup Local Dev** | Re-runs `scripts/setup-local.ps1` from within VS Code |

All tasks that talk to PostgreSQL have `DATABASE_URL` pre-configured for the default dev credentials (`changeme_dev`). If you changed `POSTGRES_PASSWORD` in `.env.dev`, update the password in the `env` block of each affected task in `.vscode/tasks.json`.

### Worktree support

The **Start Dev DB** task resolves the main git worktree root at runtime using `git worktree list`, so it correctly locates `docker-compose.dev.yml` regardless of whether you have the repo open from the main checkout (`job-matcher-pr/`) or from a worktree (`.worktrees/<branch>/`). The setup script does the same via `git rev-parse --show-toplevel`.

### Changing the default dev database password

The default dev password is `changeme_dev` (set in `.env.dev.example`). If you change `POSTGRES_PASSWORD` in your `.env.dev`:

1. Recreate the DB container so PostgreSQL picks up the new password:
   ```powershell
   docker compose -f docker-compose.dev.yml --env-file .env.dev -p job-matcher-pr-dev down -v
   docker compose -f docker-compose.dev.yml --env-file .env.dev -p job-matcher-pr-dev up -d db
   ```
2. Update `DATABASE_URL` in `.vscode/tasks.json` to use the new password.
3. Update your shell's `DATABASE_URL` export if you run `ingest.py` or `pytest` outside VS Code.

---

## Running natively (without Docker)

The app loads environment variables from a `.env` file at the repo root via
`python-dotenv`. Copy the template and fill in real values:

```powershell
Copy-Item .env.example .env
# Edit .env — at minimum generate a SECRET_KEY:
#   python -c "import secrets; print(secrets.token_hex(32))"
# You also need DATABASE_URL pointing at your local Postgres — the .env.example
# template includes a default that matches the Docker dev stack's credentials.
```

Then run:

```powershell
.venv\Scripts\python app.py
```

Docker is unaffected: `docker-compose.dev.yml` and `docker-compose.prod.yml`
read `.env.dev` / `.env.prod` via compose's `env_file:` directive, which
populates the container environment before Python starts. Variables set by the
parent process (shell, VSCode task `env:` block, compose `env_file:`) always
take precedence over `.env` — `load_dotenv(override=False)`.

---

## Running an ingestion

```bash
python ingest.py
```

The pipeline runs in five stages:

1. **Fetch** — fetches from enabled sources (Adzuna up to `search.max_pages`; other sources use their own pagination)
2. **Pre-filter** — drops listings that fail title keyword, salary, or contract type checks
3. **Dedup** — skips any listing already present in `jobs.db` (safe to re-run repeatedly)
4. **Scrape** — follows each listing's redirect URL to retrieve the full job description; falls back to the source snippet if scraping fails
5. **Score** — sends each description plus your profile to your configured LLM (Anthropic, OpenAI, or Google Gemini) and stores the structured result

### Re-scoring existing listings

To re-evaluate all previously scored listings against an updated `config/profile.json` without fetching new listings:

```bash
python ingest.py --rescore
```

Scores, matched/missing skills, concerns, and verdicts are overwritten in place. Dismissed, bookmarked, and applied status is preserved.

### Overriding config and profile paths

By default, `ingest.py` reads `config/config.json` and `config/profile.json`. You can override either with:

```bash
python ingest.py --config path/to/other_config.json
python ingest.py --profile path/to/other_profile.json
python ingest.py --rescore --profile path/to/other_profile.json
```

This is useful if you maintain separate profiles for different job searches (e.g. backend vs. SRE roles).

Log output is prefixed with the action taken for each listing:

```
INFO ingest: FILTERED   Senior Java Developer
INFO ingest: DUPE       Staff Engineer, Platform
INFO ingest: SCORED 8/10  Senior Backend Engineer
WARNING ingest: SCRAPE FALLBACK  Software Architect
```

At the end of each run a summary line is printed:

```
Run complete: 120 fetched | 74 pre-filtered | 12 dupes skipped | 34 scored (0 failed) | 2 scrape fallbacks | ~42,000 tok | ~$0.0034
```

In Docker deployments, the PostgreSQL database is created automatically by Docker Compose via `DATABASE_URL`. For local development runs (`python ingest.py`), a SQLite file `jobs.db` is created automatically on the first run.

---

## Starting the web UI

```bash
python app.py
```

Then open the web UI in your browser.

> **Security note:** Job Matcher is a localhost-only tool. All state-mutating
> requests (POST/PUT/PATCH/DELETE) are rejected with 403 if the `Origin` or
> `Referer` header does not point to `localhost` or `127.0.0.1`. Do not expose
> port 5000 to the public internet or an untrusted network.

- **Feed** (`/`) — listings scored at or above `scoring.threshold`, sorted by score descending, with dismissed listings hidden. Filterable by score, job type, remote-only, and title/company search.
- **Bookmarks** (`/bookmarks`) — listings you have saved for later review
- **Applied** (`/applied`) — listings you have marked as applied; excluded from the main feed
- **Stats** (`/stats`) — cumulative token usage and estimated API cost, broken down by day

Each listing card shows the score, matched and missing skills, concerns, and the LLM's one-sentence verdict alongside the job title, company, location, salary, and a link to the original posting. Bookmark and dismiss actions update instantly without a page reload.

The web server and ingestion script are fully decoupled — `app.py` does not need to be running when you run `ingest.py`, and vice versa.

---

## Automating ingestion manually

**cron (macOS / Linux)**

To run ingestion nightly at 07:00:

```cron
0 7 * * * /usr/bin/python3 /path/to/job_matcher/ingest.py >> /path/to/job_matcher/ingest.log 2>&1
```

**Windows Task Scheduler**

```powershell
schtasks /create /tn "JobMatcherIngest" /tr "python C:\Apps\job-matcher-pr\ingest.py" /sc daily /st 07:00
```

---

## Configuring search and filters

The keys you are most likely to tune after initial setup:

| Key | What it controls |
|---|---|
| `search.what` | The keyword query sent to Adzuna (Adzuna-specific; other sources use their own query logic). Keep it broad — pre-filtering and scoring do the narrowing. |
| `search.where` | Location filter for Adzuna (Adzuna-specific). Use a city name or leave empty for nationwide results. |
| `search.salary_min` | Listings below this salary max are dropped (only when the listing has salary data). |
| `scoring.threshold` | Raise to see only strong matches; lower to see more listings in the feed. |
| `prefilter.title_include` | Whitelist — at least one pattern must appear in the title. |
| `prefilter.title_exclude` | Blacklist — any match causes the listing to be dropped before scoring. |

Title patterns are case-insensitive substring matches, not regex.

Which sources are enabled, and any source-specific API keys or settings, are configured in the `/settings` UI — not in `config.json`.

---

## Customising your profile

`config/profile.json` is injected verbatim into the LLM scoring prompt. Edit it to reflect your actual experience — the more accurate it is, the more useful the scores will be.

| Field | Format | Purpose |
|---|---|---|
| `primary_skills` | Array of strings: `"<skill>, <years>yr, <active\|dormant>"` | Skills the LLM uses to find matches and flag gaps. Years and recency help it weight recent experience more heavily. |
| `anti_preferences` | Array of strings | Roles or technologies you want flagged as concerns even if you technically have the skills. |
| `seniority` | String | e.g. `"Senior / Staff"`. Used to flag seniority mismatches. |
| `preferred_industries` | Array of strings | Optional. Used to note industry fit. |
| `location` | Object | Nested location block. See fields below. |
| `location.center` | String | Geocodable string, e.g. `"Miami, FL"`. Used as the center point for the geospatial pre-filter. |
| `location.radius_km` | Number | Hard-filter radius in km. Listings outside this radius are dropped before LLM scoring. |
| `location.geocode_fallback` | `"pass"` or `"discard"` | What to do when a listing location cannot be geocoded. Defaults to `"pass"`. |
| `location.notes` | String | Free-text injected into the LLM scoring prompt (e.g. `"Open to remote or on-site / hybrid in South Florida"`). Auto-generated from `center` + `radius_km` when absent. |
| `scoring_notes` | String or array of strings | Optional freeform instructions injected verbatim into the LLM scoring prompt. Use this to tune scoring behaviour — e.g. `"Senior roles should score 1–2 points higher than equivalent Mid-level roles"` or `"Flag any role requiring on-site work as a concern"`. The LLM treats these as hard guidance alongside your skills and anti-preferences. |

Example `location` block:

```json
"location": {
  "center": "Miami, FL",
  "radius_km": 80,
  "geocode_fallback": "pass",
  "notes": "Open to remote or on-site / hybrid in South Florida"
}
```

> **Migrating from the old flat location fields?** The flat fields `location_preference`, `location_center`, `location_radius_km`, and `location_geocode_fallback` are no longer read. Update your `config/profile.json` to use the nested `location` block shown above.

Changes to `config/profile.json` take effect on the next ingestion run. Previously scored listings are not rescored automatically.

---

For legacy Windows deployment instructions, see [`docs/LEGACY_DEPLOYMENT.md`](docs/LEGACY_DEPLOYMENT.md).

---

## Automated deployment (self-hosted runner)

Pushing to `main` triggers an automatic deployment on the homelab server via a self-hosted GitHub Actions runner.

### Manual deploy from the Actions UI

You can deploy any branch on demand without waiting for a push-triggered CI run:

1. Go to **Actions → Deploy** in the GitHub repository.
2. Click **Run workflow** (top-right of the workflow list).
3. Select the branch you want to deploy from the dropdown and click **Run workflow**.

**What happens:**

- `pytest` runs first (PostgreSQL service spun up automatically). If tests fail, the deploy is cancelled.
- Lint (ruff) is **not** required for a manual deploy — only broken tests block it. Linting (`ruff`) is intentionally not a blocker for manual dispatches: a lint violation doesn't change runtime behavior, and gating dev testing on style rules slows down the feedback loop. Tests are still required because deploying code that doesn't pass its own test suite would waste the environment.
- The Docker image is built and pushed to GHCR for the selected branch.
- Branch → environment routing follows the same logic as auto-deploy:
  - `main` → prod stack (port 5001, `docker-compose.prod.yml`)
  - Any other branch → dev stack (port 5000, `docker-compose.dev.yml`)

### Deployment flow

```
git push origin main
        │
        ▼
GitHub Actions CI runs (tests + linting)
        │ passes
        ▼
deploy.yml triggers on the self-hosted runner
        │
        ▼
git pull → uv pip install → nssm restart JobMatcher
```

### One-time runner setup

The self-hosted runner must be registered on the server before automated deployments will work. After running `scripts/setup.ps1`, follow the GitHub Actions runner setup steps documented at the bottom of that script. The steps cover downloading the runner, configuring it with the `self-hosted` label, and installing it as a Windows service so it persists across reboots.

### Secrets and config

`config/config.json`, `config/providers.json`, and `config/profile.json` are gitignored and are never touched by the workflow. API keys live only in `config/providers.json` on the server — the deployment workflow does not use or require any GitHub Actions secrets for application credentials.

### Required secrets

Add these in **Settings → Secrets and variables → Actions** for the CI failure diagnosis workflows to work:

| Secret | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key used by Claude to diagnose CI failures |
| `GH_PAT` | GitHub Personal Access Token with `repo` scope — needed to post PR comments and push auto-fix commits |

---

## Docker deployment (Linux VM)

> **This is the primary and recommended deployment path.** Docker on Linux is the active deployment method. The native Windows deployment path has been retired — see [`docs/LEGACY_DEPLOYMENT.md`](docs/LEGACY_DEPLOYMENT.md) if you need those instructions.

Use this approach to run Job Matcher as a Docker Compose stack on a Linux VM. The stack consists of a `web` container (Flask + waitress), a `db` container (PostgreSQL), and a `scheduler` container (Ofelia) that runs ingestion daily.

For full documentation — stack architecture, environment variables, CI/CD pipeline, scheduled ingestion, backups, troubleshooting, teardown, and migration from Windows — see **[docs/DOCKER.md](docs/DOCKER.md)**.

### Quick start

```bash
# Clone into the recommended path (scripts reference /opt/job-matcher-pr)
sudo git clone https://github.com/cbeaulieu-gt/job-matcher-pr.git /opt/job-matcher-pr
cd /opt/job-matcher-pr

# One-time setup: creates directories, copies config examples, starts both stacks
sudo ./scripts/docker-setup.sh
```

After the script completes:

- **Dev** (port 5000): `http://<vm-ip>:5000/settings` — configure dev API keys
- **Prod** (port 5001): `http://<vm-ip>:5001/settings` — configure prod API keys

> **Fresh clone without GHCR access:** if you are starting the dev stack manually (outside `docker-setup.sh`), pass `--build` on the first run — `docker compose -p job-matcher-pr-dev --env-file .env.dev -f docker-compose.dev.yml up -d --build` — so compose builds the web image from the local Dockerfile instead of pulling from GHCR (which requires auth). Subsequent `up -d` calls reuse the cached build.

### Ops commands

```bash
# Check container health, logs, and database stats
./scripts/docker-status.sh

# Run ingest manually (prod)
docker compose -p job-matcher-pr-prod -f docker-compose.prod.yml exec web python ingest.py --hours 25

# Back up the prod database
./scripts/backup.sh

# Stop and remove stacks (interactive — prompts before deleting volumes)
./scripts/docker-teardown.sh
```
