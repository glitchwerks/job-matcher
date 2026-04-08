# Contributing to Job Matcher

Thanks for your interest in contributing! This document covers how to get the project running locally, the conventions used, and how to submit a good PR.

---

## Setting Up for Development

**Prerequisites:** Python 3.11+, [uv](https://github.com/astral-sh/uv)

```powershell
git clone https://github.com/cbeaulieu-gt/job-matcher.git
cd job-matcher

uv venv
.venv\Scripts\Activate.ps1

uv pip install -r requirements.txt
```

Copy the example config files — these are gitignored and must never be committed:

```powershell
Copy-Item config\config.example.json config\config.json
Copy-Item config\providers.example.json config\providers.json
Copy-Item config\profile.example.json config\profile.json
```

**Database:** The app requires PostgreSQL. Set `DATABASE_URL` before running the web server or tests:

```powershell
# Option A: spin up the dev database with Docker Compose (recommended)
docker compose -f docker-compose.dev.yml up -d
$env:DATABASE_URL = "postgresql://jobmatcher:<password>@localhost:5432/jobmatcher"
# (<password> is in .env.dev — copy .env.dev.example to .env.dev first)

# Option B: use an existing local PostgreSQL instance
$env:DATABASE_URL = "postgresql://<user>:<password>@localhost:5432/<dbname>"
```

Run the web UI:

```powershell
python app.py   # http://localhost:5000
```

Run the test suite:

```powershell
pytest
```

---

## Project Layout

```
ingest.py          CLI pipeline — fetch, filter, scrape, score, store
app.py             Flask web server — feed, settings, stats
db.py              All SQLite access
providers/         LLM provider backends (Anthropic, OpenAI, Gemini)
sources/           Job board API clients
templates/         Jinja2 HTML templates
static/            CSS and any static assets
config/            Example config files (*.example.json are committed; others are not)
tests/             Pytest test suite
docs/              Design docs, style guide, plans
scripts/           Linux/Docker deployment helpers (docker-setup.sh, docker-status.sh, docker-teardown.sh)
```

---

## Conventions

### Code style
- Python: follow existing style (PEP 8, type hints on public functions, docstrings on public classes/methods)
- No new dependencies without discussion — the project deliberately keeps the dependency footprint small
- All database access goes through `db.py`; `app.py` and `ingest.py` must not issue raw SQL

### UI
- All HTML/CSS changes must follow `docs/STYLE_GUIDE.md` — read it before touching templates or `static/style.css`
- Never hard-code hex values; always use a CSS custom property from `:root`
- HTMX for interactivity; no JS framework

### Tests
- Add or update tests for every code change
- Bug fixes must include a regression test that would have caught the bug
- Run `pytest` before opening a PR

### Commits
- One logical change per commit
- Commit messages: imperative mood, present tense (`fix:`, `feat:`, `chore:`, `docs:`, `test:`)
- Reference the issue number in the commit message: `fixes #N` or `closes #N`

---

## Branching and PRs

1. **Create a branch off `main`** — name it `feat/short-description`, `fix/short-description`, or `chore/short-description`
2. **Open an issue first** if one doesn't exist — PRs without a linked issue may be asked to add one
3. **Keep PRs focused** — one issue per PR; avoid bundling unrelated changes
4. **Fill in the PR template** — summary, test plan, and checklist
5. **Ensure CI passes** before requesting review

---

## Reporting Issues

Use the issue templates:
- **Bug report** — for unexpected behaviour, errors, or incorrect scores
- **Feature request** — for new sources, UI improvements, or pipeline changes

Please search existing issues before opening a new one.

---

## Questions?

Open a [discussion](https://github.com/cbeaulieu-gt/job-matcher/discussions) or comment on an existing issue. PRs are welcome — no contribution is too small.
