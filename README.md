# Job Matcher

A locally-run job search tool that pulls listings from the Adzuna API, scores each one against your personal skills profile using Claude Haiku, and surfaces ranked results in a browser-based feed. Run the ingestion script on demand or on a schedule, then open the UI to review, bookmark, or dismiss listings.

---

## Prerequisites

- Python 3.11+
- An [Adzuna API account](https://developer.adzuna.com/) (free tier is sufficient)
- An [Anthropic API key](https://console.anthropic.com/)

---

## Setup

**1. Clone or download the repo**

```bash
git clone <repo-url>
cd job_aggregator
```

**2. Create and activate a virtual environment**

```bash
# bash / macOS / Linux
python -m venv .venv
source .venv/bin/activate

# PowerShell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

**3. Install dependencies**

```bash
pip install -r requirements.txt
```

**4. Copy the config files**

```bash
# bash / macOS / Linux
cp config.example.json config.json
cp keys.example.json keys.json
cp profile.example.json profile.json

# PowerShell
Copy-Item config.example.json config.json
Copy-Item keys.example.json keys.json
Copy-Item profile.example.json profile.json
```

`keys.json` holds LLM provider API keys and model selection. You can fill it in directly or configure it through the `/settings` UI after starting the web server.

**5. Fill in `config.json`**

Open `config.json` and set the following. Both Adzuna keys are required — the script will exit immediately if either is missing or empty. LLM provider keys (Anthropic etc.) are configured separately via `keys.json` and the `/settings` UI.

| Key | Required | Notes |
|---|---|---|
| `adzuna_app_id` | Yes | From your Adzuna developer dashboard |
| `adzuna_app_key` | Yes | From your Adzuna developer dashboard |
| `search.country` | Yes | Adzuna country code: `us`, `gb`, `au`, etc. |
| `search.what` | Yes | Keyword query sent to Adzuna, e.g. `"software engineer"` |
| `search.where` | No | Location filter, e.g. `"miami"`. Omit or leave empty for nationwide. |
| `search.salary_min` | No | Minimum salary in local currency (USD for `us`). Listings with no salary data are allowed through regardless. |
| `search.results_per_page` | Yes | Max 50 (Adzuna API limit) |
| `search.max_pages` | Yes | Number of pages to fetch per run. 5 pages at 50 results = up to 250 raw listings. |
| `scoring.threshold` | Yes | Minimum score (0–10) for a listing to appear in the feed |
| `prefilter.title_include` | No | Listing title must match at least one of these (case-insensitive substring). Omit to allow all titles. |
| `prefilter.title_exclude` | No | Listing title must match none of these. |
| `prefilter.require_contract_time` | No | e.g. `"full_time"`. Set to `null` to skip this check. |
| `prefilter.require_contract_type` | No | e.g. `"permanent"`. Set to `null` to skip this check. |

**6. Edit `profile.json` to match your skills**

See [Customising your profile](#customising-your-profile) below.

---

## Running an ingestion

```bash
python ingest.py
```

The pipeline runs in five stages:

1. **Fetch** — pages through Adzuna results up to `search.max_pages`
2. **Pre-filter** — drops listings that fail title keyword, salary, or contract type checks
3. **Dedup** — skips any listing already present in `jobs.db` (safe to re-run repeatedly)
4. **Scrape** — follows each listing's redirect URL to retrieve the full job description; falls back to the Adzuna snippet if scraping fails
5. **Score** — sends each description plus your profile to Claude Haiku and stores the structured result

### Re-scoring existing listings

To re-evaluate all previously scored listings against an updated `profile.json` without fetching new listings:

```bash
python ingest.py --rescore
```

Scores, matched/missing skills, concerns, and verdicts are overwritten in place. Dismissed, bookmarked, and applied status is preserved.

### Overriding config and profile paths

By default, `ingest.py` reads `config.json` and `profile.json` from the current directory. You can override either with:

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

- **Feed** (`/`) — listings scored at or above `scoring.threshold`, sorted by score descending, with dismissed listings hidden. Filterable by score, job type, remote-only, and title/company search.
- **Bookmarks** (`/bookmarks`) — listings you have saved for later review
- **Applied** (`/applied`) — listings you have marked as applied; excluded from the main feed
- **Stats** (`/stats`) — cumulative token usage and estimated API cost, broken down by day

Each listing card shows the score, matched and missing skills, concerns, and Haiku's one-sentence verdict alongside the job title, company, location, salary, and a link to the original posting. Bookmark and dismiss actions update instantly without a page reload.

The web server and ingestion script are fully decoupled — `app.py` does not need to be running when you run `ingest.py`, and vice versa.

---

## Automating ingestion manually

**cron (macOS / Linux)**

To run ingestion nightly at 07:00:

```cron
0 7 * * * /usr/bin/python3 /path/to/job_aggregator/ingest.py >> /path/to/job_aggregator/ingest.log 2>&1
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
| `search.what` | The keyword query sent to Adzuna. Keep it broad — pre-filtering and scoring do the narrowing. |
| `search.where` | Location. Use a city name or leave empty for nationwide results. |
| `search.salary_min` | Listings below this salary max are dropped (only when the listing has salary data). |
| `scoring.threshold` | Raise to see only strong matches; lower to see more listings in the feed. |
| `prefilter.title_include` | Whitelist — at least one pattern must appear in the title. |
| `prefilter.title_exclude` | Blacklist — any match causes the listing to be dropped before scoring. |

Title patterns are case-insensitive substring matches, not regex.

---

## Customising your profile

`profile.json` is injected verbatim into the Claude Haiku scoring prompt. Edit it to reflect your actual experience — the more accurate it is, the more useful the scores will be.

| Field | Format | Purpose |
|---|---|---|
| `primary_skills` | Array of strings: `"<skill>, <years>yr, <active\|dormant>"` | Skills Haiku uses to find matches and flag gaps. Years and recency help it weight recent experience more heavily. |
| `anti_preferences` | Array of strings | Roles or technologies you want flagged as concerns even if you technically have the skills. |
| `seniority` | String | e.g. `"Senior / Staff"`. Used to flag seniority mismatches. |
| `preferred_industries` | Array of strings | Optional. Haiku uses this to note industry fit. |
| `location_preference` | String | e.g. `"remote or Miami, FL"`. Haiku uses this to flag location concerns. |

Changes to `profile.json` take effect on the next ingestion run. Previously scored listings are not rescored automatically.

---

## Native deployment (Windows Server)

Use this approach if you want the web UI running as a Windows service and ingestion triggered by Task Scheduler.

### Quick start

`scripts/setup.ps1` automates all the manual steps described in this section. Run it once as Administrator from the project root:

```powershell
.\scripts\setup.ps1
```

What it does:

- Prompts for your Adzuna credentials and the data directory path
- Sets system environment variables (`DB_PATH`, `ADZUNA_APP_ID`, `ADZUNA_APP_KEY`, `FLASK_DEBUG`)
- Creates the data directory and a `logs/` subfolder
- Copies `keys.example.json` → `keys.json` and restricts its ACL to the current user
- Registers the `JobMatcher` Windows service (gunicorn via NSSM) set to auto-start
- Registers the `JobMatcherIngest` daily Task Scheduler task

After the script completes, start the service with `nssm start JobMatcher` and then navigate to `http://localhost:5000/settings` to enter your LLM provider API keys.

### Prerequisites

- Python venv already set up and `pip install -r requirements.txt` run
- `config.json` and `profile.json` present in the project root
- [NSSM](https://nssm.cc/download) downloaded and either on `PATH` or referenced by full path

### Environment variables (reference)

Set Adzuna credentials and the database path as machine-level environment variables so both the service and the scheduled task pick them up automatically:

```powershell
[System.Environment]::SetEnvironmentVariable("DB_PATH", "C:\path\to\data\jobs.db", "Machine")
[System.Environment]::SetEnvironmentVariable("ADZUNA_APP_ID", "your_id", "Machine")
[System.Environment]::SetEnvironmentVariable("ADZUNA_APP_KEY", "your_key", "Machine")
```

Restart your terminal after setting machine-level variables for them to take effect.

LLM provider API keys are managed separately through `keys.json` and the `/settings` UI — do not set them as environment variables.

### Web service — NSSM (reference)

Register gunicorn as a Windows service named `JobMatcher`:

```powershell
nssm install JobMatcher "C:\Apps\job_matcher\venv\Scripts\gunicorn.exe"
nssm set JobMatcher AppParameters "app:app --bind 0.0.0.0:5000 --workers 2"
nssm set JobMatcher AppDirectory "C:\Apps\job_matcher"
nssm set JobMatcher Start SERVICE_AUTO_START
nssm start JobMatcher
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

LLM provider keys (Anthropic, OpenAI, Gemini, etc.) are stored in `keys.json` at the project root and managed through the `/settings` UI — they are not set as environment variables. `keys.json` is gitignored and never committed. After running `scripts/setup.ps1`, navigate to `http://localhost:5000/settings` to enter your API keys.

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
git pull → pip install → nssm restart JobMatcher
```

### One-time runner setup

The self-hosted runner must be registered on the server before automated deployments will work. After running `scripts/setup.ps1`, follow the GitHub Actions runner setup steps documented at the bottom of that script. The steps cover downloading the runner, configuring it with the `self-hosted` label, and installing it as a Windows service so it persists across reboots.

### Secrets and config

`config.json`, `keys.json`, and `profile.json` are gitignored and are never touched by the workflow. API keys live only in `keys.json` on the server — the deployment workflow does not use or require any GitHub Actions secrets for application credentials.

### Required secrets

Add these in **Settings → Secrets and variables → Actions** for the CI failure diagnosis workflows to work:

| Secret | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key used by Claude to diagnose CI failures |
| `GH_PAT` | GitHub Personal Access Token with `repo` scope — needed to post PR comments and push auto-fix commits |
