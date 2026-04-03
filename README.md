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

The database file `jobs.db` is created automatically on the first run.

---

## Starting the web UI

```bash
python app.py
```

Then open `http://localhost:5000` in your browser.

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
schtasks /create /tn "JobMatcherIngest" /tr "python C:\Apps\job_matcher\ingest.py" /sc daily /st 07:00
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

## Native deployment (Windows Server)

Use this approach if you want the web UI running as a Windows service and ingestion triggered by Task Scheduler.

### Quick start

`scripts/setup.ps1` automates all the manual steps described in this section. Run it once as Administrator from the project root:

```powershell
.\scripts\setup.ps1
```

What it does:

- Prompts for the data directory path and daily ingest time (infrastructure only — no credential prompts)
- Sets system environment variables (`DB_PATH`, `FLASK_DEBUG`)
- Creates the data directory and a `logs/` subfolder
- Copies `config/keys.example.json` → `config/keys.json` and restricts its ACL to the current user
- Copies `config/config.example.json` → `config/config.json` (if absent)
- Registers the `JobMatcher` Windows service (waitress via NSSM) set to auto-start
- Registers the `JobMatcherIngest` daily Task Scheduler task
- Opens Windows Firewall inbound TCP port 5000 so the UI is reachable from the network

After the script completes, navigate to `http://localhost:5000/settings` to configure your LLM provider API keys and any job source credentials (including Adzuna App ID and App Key).

### Prerequisites

- Python venv already set up and `uv pip install -r requirements.txt` run
- `config/profile.json` present in the project (`config/config.json` is created from the example by the script if absent)
- [NSSM](https://nssm.cc/download) downloaded and either on `PATH` or referenced by full path

### Environment variables (reference)

Set the database path as a machine-level environment variable so both the service and the scheduled task pick it up automatically. Adzuna credentials are optional — only set them if you are using the Adzuna source:

```powershell
[System.Environment]::SetEnvironmentVariable("DB_PATH", "C:\path\to\data\jobs.db", "Machine")

# Optional — only needed if using the Adzuna source
[System.Environment]::SetEnvironmentVariable("ADZUNA_APP_ID", "your_id", "Machine")
[System.Environment]::SetEnvironmentVariable("ADZUNA_APP_KEY", "your_key", "Machine")
```

Restart your terminal after setting machine-level variables for them to take effect.

LLM provider API keys (Anthropic, OpenAI, Gemini) are managed through `config/providers.json` and the `/settings` UI — do not set them as environment variables.

### Web service — NSSM (reference)

Register waitress as a Windows service named `JobMatcher`:

```powershell
nssm install JobMatcher "C:\Apps\job_matcher\venv\Scripts\waitress-serve.exe"
nssm set JobMatcher AppParameters "--host=0.0.0.0 --port=5000 app:app"
nssm set JobMatcher AppDirectory "C:\Apps\job_matcher"
nssm set JobMatcher Start SERVICE_AUTO_START
nssm start JobMatcher

# Allow inbound connections from the network (run as Administrator)
New-NetFirewallRule -DisplayName "Job Matcher Web UI" -Direction Inbound -Protocol TCP -LocalPort 5000 -Action Allow
```

Service management:

```powershell
nssm stop JobMatcher
nssm restart JobMatcher
nssm status JobMatcher
```

### Scheduled ingest — Task Scheduler (reference)

Create a daily ingest task running at 6am:

```powershell
$action = New-ScheduledTaskAction `
    -Execute "C:\Apps\job_matcher\venv\Scripts\python.exe" `
    -Argument "ingest.py --hours 25" `
    -WorkingDirectory "C:\Apps\job_matcher"

$trigger = New-ScheduledTaskTrigger -Daily -At 6am

Register-ScheduledTask -TaskName "JobMatcherIngest" `
    -Action $action `
    -Trigger $trigger `
    -RunLevel Highest `
    -Force
```

Run the task manually at any time:

```powershell
Start-ScheduledTask -TaskName "JobMatcherIngest"
```

### Data directory (reference)

Create the directory that will hold the database and point `DB_PATH` at it:

```powershell
New-Item -ItemType Directory -Force -Path "C:\path\to\data"
```

The SQLite database (`jobs.db`) is created there automatically on the first run.

### API keys (LLM providers)

LLM provider keys (Anthropic, OpenAI, Gemini, etc.) are stored in `config/providers.json` alongside job source credentials. The file is gitignored and never committed. After running `scripts/setup.ps1`, navigate to `http://localhost:5000/settings` to enter your API keys — the Settings UI validates each key before saving.

### Ops commands

```powershell
# Show service status, last task run, and DB row count
.\scripts\status.ps1

# Remove the service and scheduled task cleanly
.\scripts\teardown.ps1

# Run ingest immediately without waiting for the scheduled trigger
Start-ScheduledTask -TaskName "JobMatcherIngest"
```

---

## Automated deployment (self-hosted runner)

Pushing to `main` triggers an automatic deployment on the homelab server via a self-hosted GitHub Actions runner.

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
