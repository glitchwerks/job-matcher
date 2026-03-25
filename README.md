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

**4. Create your config file**

```bash
# bash / macOS / Linux
cp config.example.json config.json

# PowerShell
Copy-Item config.example.json config.json
```

Also copy the example profile:

```bash
# bash / macOS / Linux
cp profile.example.json profile.json

# PowerShell
Copy-Item profile.example.json profile.json
```

**5. Fill in `config.json`**

Open `config.json` and set the following. All three top-level API keys are required — the script will exit immediately if any are missing or empty.

| Key | Required | Notes |
|---|---|---|
| `adzuna_app_id` | Yes | From your Adzuna developer dashboard |
| `adzuna_app_key` | Yes | From your Adzuna developer dashboard |
| `anthropic_api_key` | Yes | `sk-ant-...` key from Anthropic console |
| `search.country` | Yes | Adzuna country code: `us`, `gb`, `au`, etc. |
| `search.what` | Yes | Keyword query sent to Adzuna, e.g. `"software engineer"` |
| `search.where` | No | Location filter, e.g. `"miami"`. Omit or leave empty for nationwide. |
| `search.salary_min` | No | Minimum salary in local currency (USD for `us`). Listings with no salary data are allowed through regardless. |
| `search.results_per_page` | Yes | Max 50 (Adzuna API limit) |
| `search.max_pages` | Yes | Number of pages to fetch per run. 5 pages at 50 results = up to 250 raw listings. |
| `scoring.threshold` | Yes | Minimum score (0–10) for a listing to appear in the feed |
| `scoring.model` | Yes | Anthropic model ID. Default: `claude-haiku-4-5-20251001` |
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

## Automating ingestion (optional)

**cron (macOS / Linux)**

To run ingestion nightly at 07:00:

```cron
0 7 * * * /usr/bin/python3 /path/to/job_aggregator/ingest.py >> /path/to/job_aggregator/ingest.log 2>&1
```

**Windows Task Scheduler**

```powershell
schtasks /create /tn "JobMatcherIngest" /tr "python C:\path\to\job_aggregator\ingest.py" /sc daily /st 07:00
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
