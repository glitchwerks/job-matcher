# Profile Scoring Instructions & Test Profile Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "Scoring Instructions" section to the Profile page with: (1) an editable textarea for `scoring_notes` with a dedicated Save button, and (2) a separate Test button that scores a selected listing against the current saved profile and shows a diff-style before/after breakdown of matched skills, missing skills, concerns, and verdict.

**Architecture:** `GET /profile` extended to load `scoring_notes` from `profile.json` and recent listings. `POST /profile/scoring-notes` saves only (fast, no LLM). `POST /api/test-profile` runs the LLM call in a `ThreadPoolExecutor` thread so Flask's worker pool is not blocked. Result is an HTMX fragment showing a structured diff between the stored scoring data and the new result. Save and Test are intentionally decoupled.

**Tech Stack:** Flask, HTMX 1.9.10, Jinja2, SQLite (via db.py), ingest.py scoring pipeline, providers.py, `concurrent.futures.ThreadPoolExecutor`

---

## Design Decisions & Rationale

| Decision | Rationale |
|---|---|
| Save and Test are separate buttons | Save is a fast file write; Test is a slow LLM call. Coupling them means untested instructions get persisted if the API call fails, and the user can't save a draft without triggering a billable call. |
| LLM call runs in `ThreadPoolExecutor` | The ingest pattern uses `subprocess.Popen` to avoid blocking Flask workers, but that adds polling infrastructure unsuitable for a single live call. `ThreadPoolExecutor` keeps the request thread free while the LLM call runs, with no subprocess overhead. **This tool runs on localhost for a single user** — exhausting the worker pool is not a realistic concern, but blocking for 10–30 s would freeze all page interactions (feed, dismiss, bookmark) for that duration. A thread is the right tradeoff. |
| Diff-style breakdown instead of score delta | LLM scores are non-deterministic; a 6→7 delta is noise. Showing which skills moved in/out of `matched_skills`/`missing_skills` and which concerns appeared/disappeared is concrete, instruction-attributable signal. |
| Both model names displayed | If the stored score was produced by a different model than the test, the comparison is less meaningful. Always show `model_used` for both, with a warning if they differ. |
| Single form variant | Avoid two code paths for one action. If no listings exist, show the form disabled with an explanatory message — the user sees a consistent UI on first run. |
| Atomic file write via `os.replace` | `json.dump` to a file handle can leave a truncated `profile.json` on Windows if interrupted. Write to a temp file in the same directory then `os.replace` for atomic swap. |
| `scoring_notes` not in raw JSON editor | The raw editor already shows `config.json`, not `profile.json`. These are separate files. No stripping needed — this was confirmed dead code in adversarial review. |
| `dead_providers` passed as fresh `set()` per request | `score_listing_with_fallback` takes a mutable `dead_providers` set designed for batch ingest runs (accumulates bad providers across hundreds of calls). In a single-call web context, a fresh `set()` per request is correct — each test is independent, and there is no multi-call session to persist auth failures across. The alternative (calling `provider.complete()` directly) would bypass the retry and fallback logic that `score_listing_with_fallback` provides. Fresh `set()` is intentional, not an oversight. |

---

## File Map

| File | Change |
|---|---|
| `app.py` | Add `_PROFILE_PATH` constant; extend imports (`build_provider_chain`, `score_listing_with_fallback`, `ThreadPoolExecutor`); extend GET `/profile`; add POST `/profile/scoring-notes` (save only, atomic write); add POST `/api/test-profile` (threaded LLM call, diff result) |
| `templates/profile.html` | Add HTMX script tag; add Scoring Instructions section with separate Save and Test forms |
| `templates/_test_result.html` | **New** — diff-style HTMX partial: +/- indicators on skills/concerns, both model names, mismatch warning |
| `static/style.css` | Append test result panel CSS and diff indicators |
| `tests/test_profile_scoring.py` | **New** — TDD tests for both routes |

---

## Task 1: Add constant and imports to `app.py`

**Files:** Modify `app.py`

- [ ] Find the path constants block (near `_CONFIG_PATH`, `_KEYS_PATH`). Add immediately after `_PROVIDERS_PATH`:
  ```python
  _PROFILE_PATH: str = os.path.join(os.path.dirname(__file__), "profile.json")
  ```

- [ ] Find `from providers import _PROVIDER_CLASS_MAP`. Change to:
  ```python
  from providers import _PROVIDER_CLASS_MAP, build_provider_chain
  ```

- [ ] In the same import block, add:
  ```python
  from ingest import score_listing_with_fallback
  from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
  ```

- [ ] Verify no import errors:
  ```
  cd "I:\Web Development\job_matcher"
  python -c "import app; print('import OK')"
  ```
  Expected: `import OK`

- [ ] Commit:
  ```
  git add "I:/Web Development/job_matcher/app.py"
  git commit -m "$(cat <<'EOF'
  Add _PROFILE_PATH, build_provider_chain, and ThreadPoolExecutor imports

  Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
  EOF
  )"
  ```

---

## Task 2: Write failing tests

**Files:** Create `tests/test_profile_scoring.py`

- [ ] Create `I:\Web Development\job_matcher\tests\test_profile_scoring.py`:

```python
"""
tests/test_profile_scoring.py — Tests for the Scoring Instructions feature.

Covers:
  GET  /profile                — scoring_notes textarea populated; recent listings passed
  POST /profile/scoring-notes  — atomic save to profile.json; no LLM call
  POST /api/test-profile       — threaded LLM call; diff result HTML fragment
"""

from __future__ import annotations
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as app_module
from app import app as flask_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def client():
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


@pytest.fixture()
def tmp_profile_path(tmp_path, monkeypatch):
    path = str(tmp_path / "profile.json")
    monkeypatch.setattr(app_module, "_PROFILE_PATH", path)
    return path


@pytest.fixture()
def tmp_db_path(tmp_path, monkeypatch):
    import db as db_module
    path = str(tmp_path / "test.db")
    monkeypatch.setattr(app_module, "DB_PATH", path)
    db_module.init_db(db_path=path)
    return path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _insert_listing(tmp_db_path, source_id, title="Dev", company="Corp",
                    score=7.0, matched=None, missing=None, concerns=None,
                    model_used="anthropic/claude-haiku"):
    import db as db_module
    conn = db_module.get_connection(tmp_db_path)
    conn.execute(
        """INSERT INTO listings
           (source, source_id, title, company, location, url, description,
            score, matched_skills, missing_skills, concerns, model_used,
            seen, bookmarked, applied, fetched_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("test", source_id, title, company, "Remote",
         "https://example.com", "Python required.",
         score,
         json.dumps(matched or ["Python", "SQL"]),
         json.dumps(missing or ["Kubernetes"]),
         json.dumps(concerns or ["Mid-level role"]),
         model_used,
         1, 0, 0,
         "2026-01-01T00:00:00Z"),
    )
    conn.commit()
    conn.close()
    return db_module.get_all_scored(db_path=tmp_db_path)[0]


# ---------------------------------------------------------------------------
# GET /profile
# ---------------------------------------------------------------------------

class TestProfileGet:
    def test_scoring_notes_textarea_populated(self, client, tmp_profile_path, tmp_db_path):
        with open(tmp_profile_path, "w", encoding="utf-8") as f:
            json.dump({"scoring_notes": ["Prefer Azure.", "Avoid AWS."]}, f)
        resp = client.get("/profile")
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "Prefer Azure." in body
        assert "Avoid AWS." in body

    def test_dropdown_shows_score(self, client, tmp_profile_path, tmp_db_path):
        _insert_listing(tmp_db_path, "src-x", title="Backend Dev", score=6.5)
        resp = client.get("/profile")
        assert resp.status_code == 200
        assert "6.5" in resp.data.decode()

    def test_missing_profile_json_does_not_crash(self, client, tmp_profile_path, tmp_db_path):
        # profile.json does not exist — should load with empty scoring_notes
        assert not os.path.exists(tmp_profile_path)
        resp = client.get("/profile")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# POST /profile/scoring-notes (save only)
# ---------------------------------------------------------------------------

class TestSaveScoringNotes:
    def test_writes_array_to_profile_json(self, client, tmp_profile_path):
        resp = client.post("/profile/scoring-notes", data={
            "scoring_notes": "Prioritize Azure.\nAWS is acceptable.",
        })
        assert resp.status_code == 302
        with open(tmp_profile_path, encoding="utf-8") as f:
            saved = json.load(f)
        assert saved["scoring_notes"] == ["Prioritize Azure.", "AWS is acceptable."]

    def test_filters_blank_lines(self, client, tmp_profile_path):
        client.post("/profile/scoring-notes", data={
            "scoring_notes": "Line one\n\n   \nLine two\n",
        })
        with open(tmp_profile_path, encoding="utf-8") as f:
            saved = json.load(f)
        assert saved["scoring_notes"] == ["Line one", "Line two"]

    def test_redirects_to_profile_anchor(self, client, tmp_profile_path):
        resp = client.post("/profile/scoring-notes",
                           data={"scoring_notes": "test"},
                           follow_redirects=False)
        assert resp.status_code == 302
        location = resp.headers.get("Location", "")
        assert "/profile" in location
        assert "scoring-notes" in location

    def test_creates_file_if_missing(self, client, tmp_profile_path):
        assert not os.path.exists(tmp_profile_path)
        client.post("/profile/scoring-notes", data={"scoring_notes": "new note"})
        assert os.path.exists(tmp_profile_path)

    def test_preserves_other_profile_fields(self, client, tmp_profile_path):
        with open(tmp_profile_path, "w", encoding="utf-8") as f:
            json.dump({"primary_skills": ["Python"], "seniority": "Senior",
                       "scoring_notes": ["old"]}, f)
        client.post("/profile/scoring-notes", data={"scoring_notes": "new note"})
        with open(tmp_profile_path, encoding="utf-8") as f:
            saved = json.load(f)
        assert saved["primary_skills"] == ["Python"]
        assert saved["seniority"] == "Senior"
        assert saved["scoring_notes"] == ["new note"]

    def test_empty_textarea_saves_empty_array(self, client, tmp_profile_path):
        resp = client.post("/profile/scoring-notes", data={"scoring_notes": ""})
        assert resp.status_code == 302
        with open(tmp_profile_path, encoding="utf-8") as f:
            saved = json.load(f)
        assert saved["scoring_notes"] == []


# ---------------------------------------------------------------------------
# POST /api/test-profile (test only — no save, threaded LLM call)
# ---------------------------------------------------------------------------

class TestProfileScoringOnly:
    def test_missing_listing_id_returns_400(self, client, tmp_profile_path, tmp_db_path):
        resp = client.post("/api/test-profile", data={})
        assert resp.status_code == 400
        assert b"No listing selected" in resp.data

    def test_invalid_listing_id_returns_400(self, client, tmp_profile_path, tmp_db_path):
        resp = client.post("/api/test-profile", data={"listing_id": "abc"})
        assert resp.status_code == 400
        assert b"Invalid listing ID" in resp.data

    def test_nonexistent_listing_returns_404(self, client, tmp_profile_path, tmp_db_path):
        resp = client.post("/api/test-profile", data={"listing_id": "99999"})
        assert resp.status_code == 404
        assert b"Listing not found" in resp.data

    def test_missing_profile_json_returns_500(self, client, tmp_profile_path, tmp_db_path):
        row = _insert_listing(tmp_db_path, "src-a")
        assert not os.path.exists(tmp_profile_path)
        resp = client.post("/api/test-profile", data={"listing_id": str(row["id"])})
        assert resp.status_code == 500
        assert b"Failed to load profile.json" in resp.data

    def test_no_providers_returns_400(self, client, tmp_profile_path, tmp_db_path, monkeypatch):
        row = _insert_listing(tmp_db_path, "src-b")
        with open(tmp_profile_path, "w", encoding="utf-8") as f:
            json.dump({"scoring_notes": []}, f)
        monkeypatch.setattr(app_module, "build_provider_chain", lambda _: [])
        resp = client.post("/api/test-profile", data={"listing_id": str(row["id"])})
        assert resp.status_code == 400
        assert b"No LLM providers configured" in resp.data

    def test_returns_diff_html_fragment(self, client, tmp_profile_path, tmp_db_path, monkeypatch):
        """Result HTML must include old and new skills/concerns for diff display."""
        row = _insert_listing(
            tmp_db_path, "src-c",
            title="Senior Engineer", company="TechCo",
            score=6.0,
            matched=["Python", "SQL"],
            missing=["Kubernetes"],
            concerns=["Mid-level role"],
            model_used="anthropic/claude-haiku",
        )
        with open(tmp_profile_path, "w", encoding="utf-8") as f:
            json.dump({"primary_skills": ["Python"], "scoring_notes": []}, f)
        fake_result = {
            "score": 8,
            "matched_skills": ["Python", "SQL", "Azure"],   # Azure is new
            "missing_skills": [],                            # Kubernetes gone
            "concerns": ["Mid-level role"],                  # unchanged
            "verdict": "Strong match with cloud alignment.",
            "model_used": "anthropic/claude-haiku",
            "tokens_input": 100,
            "tokens_output": 50,
        }
        monkeypatch.setattr(app_module, "build_provider_chain", lambda _: ["fake"])
        monkeypatch.setattr(app_module, "score_listing_with_fallback",
                            lambda *a, **kw: fake_result)
        resp = client.post("/api/test-profile", data={"listing_id": str(row["id"])})
        assert resp.status_code == 200
        body = resp.data.decode()
        # Scores present
        assert "6" in body   # old score
        assert "8" in body   # new score
        # Skills diff
        assert "Azure" in body       # new skill
        assert "Kubernetes" in body  # removed skill
        assert "Python" in body      # unchanged skill
        # Verdict
        assert "Strong match with cloud alignment." in body
        # Model shown
        assert "anthropic/claude-haiku" in body

    def test_model_mismatch_warning_shown(self, client, tmp_profile_path, tmp_db_path, monkeypatch):
        """A warning must appear when old and new scores used different models."""
        row = _insert_listing(
            tmp_db_path, "src-d",
            score=6.0,
            model_used="openai/gpt-4o-mini",
        )
        with open(tmp_profile_path, "w", encoding="utf-8") as f:
            json.dump({"scoring_notes": []}, f)
        fake_result = {
            "score": 7,
            "matched_skills": ["Python"],
            "missing_skills": [],
            "concerns": [],
            "verdict": "OK.",
            "model_used": "anthropic/claude-haiku",   # different model
            "tokens_input": 10,
            "tokens_output": 5,
        }
        monkeypatch.setattr(app_module, "build_provider_chain", lambda _: ["fake"])
        monkeypatch.setattr(app_module, "score_listing_with_fallback",
                            lambda *a, **kw: fake_result)
        resp = client.post("/api/test-profile", data={"listing_id": str(row["id"])})
        assert resp.status_code == 200
        body = resp.data.decode()
        # Both model names present
        assert "openai/gpt-4o-mini" in body
        assert "anthropic/claude-haiku" in body
        # Warning text present
        assert "model" in body.lower()  # "Models differ" or similar

    def test_all_providers_fail_returns_500(self, client, tmp_profile_path, tmp_db_path, monkeypatch):
        row = _insert_listing(tmp_db_path, "src-e")
        with open(tmp_profile_path, "w", encoding="utf-8") as f:
            json.dump({"scoring_notes": []}, f)
        monkeypatch.setattr(app_module, "build_provider_chain", lambda _: ["fake"])
        monkeypatch.setattr(app_module, "score_listing_with_fallback",
                            lambda *a, **kw: None)
        resp = client.post("/api/test-profile", data={"listing_id": str(row["id"])})
        assert resp.status_code == 500
        assert b"Scoring failed" in resp.data
```

- [ ] Run tests to confirm they all fail (expected — routes don't exist yet):
  ```
  cd "I:\Web Development\job_matcher"
  python -m pytest tests/test_profile_scoring.py -v 2>&1
  ```
  Expected: failures with `404` or `AttributeError`.

---

## Task 3: Extend GET `/profile` in `app.py`

**Files:** Modify `app.py`

- [ ] In the `profile()` view function, locate the block starting with `# Always re-read from disk`. Replace from that comment through the `return render_template(...)` call with:

  ```python
  # Always re-read from disk (after write or on GET) for the textarea.
  cfg_display = load_config(_CONFIG_PATH)
  masked = _mask_config_keys(cfg_display)
  # Note: scoring_notes lives in profile.json (separate file) — not in config.json.
  # No stripping needed; these are different files.
  config_json_str = json.dumps(masked, indent=2)

  # Scoring notes — load from profile.json, join lines for textarea display.
  scoring_notes_text = ""
  try:
      with open(_PROFILE_PATH, encoding="utf-8") as _f:
          _profile_data = json.load(_f)
      scoring_notes_text = "\n".join(_profile_data.get("scoring_notes", []))
  except (FileNotFoundError, json.JSONDecodeError):
      pass

  # Recent scored listings for the Test Profile dropdown (up to 20).
  # Score included in display so user can pick borderline test cases.
  recent_listings = db.get_all_scored(db_path=DB_PATH)[:20]

  return render_template(
      "profile.html",
      view="profile",
      config_json=config_json_str,
      saved=saved,
      error=error,
      scoring_notes_text=scoring_notes_text,
      recent_listings=recent_listings,
  ), status_code
  ```

- [ ] Run GET tests:
  ```
  python -m pytest tests/test_profile_scoring.py::TestProfileGet -v
  ```
  Expected: all 3 pass.

---

## Task 4: Implement `POST /profile/scoring-notes` (save only)

**Files:** Modify `app.py`

Save is fast and side-effect-free — file write only, no LLM call. Uses atomic write to prevent truncated JSON on Windows if interrupted.

- [ ] Add after the `settings_config_redirect` route:

  ```python
  @app.route("/profile/scoring-notes", methods=["POST"])
  def save_scoring_notes():
      """Save scoring_notes array to profile.json.

      Save only — no LLM call. Use /api/test-profile to score.

      Writes atomically via a temp file + os.replace to prevent a truncated
      profile.json if the process is interrupted mid-write on Windows.
      """
      raw = request.form.get("scoring_notes", "")
      lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]

      try:
          with open(_PROFILE_PATH, encoding="utf-8") as f:
              profile_data = json.load(f)
      except (FileNotFoundError, json.JSONDecodeError):
          profile_data = {}

      profile_data["scoring_notes"] = lines

      # Atomic write: write to .tmp then replace, so profile.json is never
      # partially written if the process is interrupted.
      tmp_path = _PROFILE_PATH + ".tmp"
      with open(tmp_path, "w", encoding="utf-8") as f:
          json.dump(profile_data, f, indent=2)
      os.replace(tmp_path, _PROFILE_PATH)

      return redirect(url_for("profile") + "#scoring-notes")
  ```

- [ ] Run save tests:
  ```
  python -m pytest tests/test_profile_scoring.py::TestSaveScoringNotes -v
  ```
  Expected: all 6 pass.

---

## Task 5: Implement `POST /api/test-profile` (threaded LLM call)

**Files:** Modify `app.py`

The LLM call runs in a `ThreadPoolExecutor` thread with a 60-second timeout. This keeps Flask's worker threads free during the scoring call — without this, a 10–30 s API response would block the worker pool and freeze all other page interactions (feed, dismiss, bookmark) for that duration. A subprocess-based approach (as used by `/ingest/trigger`) would require a polling endpoint and adds infrastructure unsuitable for a single ad-hoc call. A thread is the right tradeoff for this tool's single-user localhost context.

- [ ] Add immediately after `save_scoring_notes`:

  ```python
  # Module-level executor — reused across requests, avoids thread-per-request overhead.
  _score_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-profile")


  @app.route("/api/test-profile", methods=["POST"])
  def test_profile_scoring():
      """Score a listing against the current saved profile.json.

      Does NOT save anything. Does NOT update any DB record.

      The LLM call runs in a ThreadPoolExecutor thread so this request handler
      returns promptly and does not block Flask worker threads. Timeout: 60 s.

      Returns an HTML fragment rendered by _test_result.html, including a
      diff-style breakdown of matched/missing skills and concerns vs the
      stored scoring data.
      """
      listing_id_raw = request.form.get("listing_id", "").strip()
      if not listing_id_raw:
          return "<p class='save-error'>No listing selected.</p>", 400
      try:
          listing_id = int(listing_id_raw)
      except ValueError:
          return "<p class='save-error'>Invalid listing ID.</p>", 400

      listing = db.get_listing_by_id(listing_id, db_path=DB_PATH)
      if listing is None:
          return "<p class='save-error'>Listing not found.</p>", 404

      try:
          with open(_PROFILE_PATH, encoding="utf-8") as f:
              profile_data = json.load(f)
      except (FileNotFoundError, json.JSONDecodeError) as e:
          return f"<p class='save-error'>Failed to load profile.json: {e}</p>", 500

      try:
          providers_data = _load_providers_safe()
          chain = build_provider_chain(providers_data)
      except Exception as e:
          return f"<p class='save-error'>Failed to load LLM providers: {e}</p>", 500

      if not chain:
          return "<p class='save-error'>No LLM providers configured. Check Settings.</p>", 400

      # Run the LLM call in a thread — keeps this Flask worker free.
      future = _score_executor.submit(
          score_listing_with_fallback, listing, profile_data, chain, set()
      )
      try:
          result = future.result(timeout=60)
      except FuturesTimeoutError:
          return "<p class='save-error'>Scoring timed out (60 s). Try again.</p>", 504
      except Exception as e:
          return f"<p class='save-error'>Scoring error: {e}</p>", 500

      if result is None:
          return "<p class='save-error'>Scoring failed — all providers returned errors.</p>", 500

      return render_template("_test_result.html", listing=listing, result=result)
  ```

- [ ] Run all new tests:
  ```
  python -m pytest tests/test_profile_scoring.py -v
  ```
  Expected: all tests pass.

- [ ] Commit:
  ```
  git add "I:/Web Development/job_matcher/app.py"
  git add "I:/Web Development/job_matcher/tests/test_profile_scoring.py"
  git commit -m "$(cat <<'EOF'
  Add scoring notes save route and threaded test-profile endpoint

  - Extend GET /profile to pass scoring_notes_text and scored recent listings
  - POST /profile/scoring-notes: save-only with atomic write via os.replace
  - POST /api/test-profile: LLM call in ThreadPoolExecutor (60s timeout)
    to avoid blocking Flask worker threads during 10-30s API call
  - TDD tests covering save, diff HTML output, model mismatch warning

  Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
  EOF
  )"
  ```

---

## Task 6: Create `_test_result.html` partial

**Files:** Create `templates/_test_result.html`

Shows a diff-style breakdown: skills/concerns that are new (`+`), removed (`-`), or unchanged. Both model names shown. Warning if models differ.

Template receives:
- `listing` — DB listing dict: `listing.score`, `listing.matched_skills`, `listing.missing_skills`, `listing.concerns`, `listing.model_used`
- `result` — new scoring dict: `result.score`, `result.matched_skills`, etc.

- [ ] Create `I:\Web Development\job_matcher\templates\_test_result.html`:

```html
{#- _test_result.html — HTMX partial returned by POST /api/test-profile.
    Swapped into #test-result on the Profile page.
    Shows a diff-style before/after breakdown rather than a simple score delta,
    because LLM scores are non-deterministic — structured field changes are
    more reliable indicators of whether instructions had the intended effect. -#}

{% if result.score >= 7 %}{% set new_tier = "high" %}
{% elif result.score >= 5 %}{% set new_tier = "mid" %}
{% else %}{% set new_tier = "low" %}{% endif %}

{% if listing.score is not none %}
  {% if listing.score >= 7 %}{% set old_tier = "high" %}
  {% elif listing.score >= 5 %}{% set old_tier = "mid" %}
  {% else %}{% set old_tier = "low" %}{% endif %}
{% endif %}

{% set models_differ = listing.model_used and listing.model_used != result.model_used %}

{# Compute diffs as sets for template logic #}
{% set old_matched = listing.matched_skills or [] %}
{% set new_matched = result.matched_skills or [] %}
{% set old_missing = listing.missing_skills or [] %}
{% set new_missing = result.missing_skills or [] %}
{% set old_concerns = listing.concerns or [] %}
{% set new_concerns = result.concerns or [] %}

<div class="test-result">
  <div class="test-result-header">
    <strong>{{ listing.title | title }}</strong> &mdash; {{ listing.company }}
  </div>

  {# Score comparison row #}
  <div class="score-delta">
    {% if listing.score is not none %}
    <span class="score-delta-label">Before</span>
    <span class="score-badge tier-{{ old_tier }}">{{ listing.score }}/10</span>
    <small class="model-badge">{{ listing.model_used or "unknown model" }}</small>
    <span class="score-delta-arrow">&rarr;</span>
    {% endif %}
    <span class="score-delta-label">Test</span>
    <span class="score-badge tier-{{ new_tier }}">{{ result.score }}/10</span>
    <small class="model-badge">{{ result.model_used }}</small>
  </div>

  {% if models_differ %}
  <p class="test-result-warning">
    ⚠ Models differ — score delta may reflect model variance, not instruction changes.
  </p>
  {% endif %}

  {# Matched skills diff #}
  {% set all_matched = (old_matched + new_matched) | unique | list %}
  {% if all_matched %}
  <div class="test-result-section">
    <strong>Matched skills</strong>
    <span class="chip-list">
      {% for skill in all_matched %}
        {% if skill in new_matched and skill not in old_matched %}
          <span class="chip matched chip-added" title="New">+&thinsp;{{ skill }}</span>
        {% elif skill in old_matched and skill not in new_matched %}
          <span class="chip chip-removed" title="Removed">-&thinsp;{{ skill }}</span>
        {% else %}
          <span class="chip matched">{{ skill }}</span>
        {% endif %}
      {% endfor %}
    </span>
  </div>
  {% endif %}

  {# Missing skills diff #}
  {% set all_missing = (old_missing + new_missing) | unique | list %}
  {% if all_missing %}
  <div class="test-result-section">
    <strong>Missing skills</strong>
    <span class="chip-list">
      {% for skill in all_missing %}
        {% if skill in new_missing and skill not in old_missing %}
          <span class="chip missing chip-added" title="New">+&thinsp;{{ skill }}</span>
        {% elif skill in old_missing and skill not in new_missing %}
          <span class="chip chip-removed" title="Resolved">-&thinsp;{{ skill }}</span>
        {% else %}
          <span class="chip missing">{{ skill }}</span>
        {% endif %}
      {% endfor %}
    </span>
  </div>
  {% endif %}

  {# Concerns diff #}
  {% set all_concerns = (old_concerns + new_concerns) | unique | list %}
  {% if all_concerns %}
  <div class="test-result-section">
    <strong>Concerns</strong>
    <ul>
      {% for c in all_concerns %}
        {% if c in new_concerns and c not in old_concerns %}
          <li class="concern-added">+&thinsp;{{ c }}</li>
        {% elif c in old_concerns and c not in new_concerns %}
          <li class="concern-removed">-&thinsp;{{ c }}</li>
        {% else %}
          <li>{{ c }}</li>
        {% endif %}
      {% endfor %}
    </ul>
  </div>
  {% endif %}

  {# Verdict — show both if they differ #}
  <div class="test-result-section">
    <strong>Verdict</strong>
    {% if listing.verdict and listing.verdict != result.verdict %}
    <p class="verdict-before"><em>Before:</em> {{ listing.verdict }}</p>
    <p class="verdict-after"><em>After:</em> {{ result.verdict }}</p>
    {% else %}
    <p>{{ result.verdict }}</p>
    {% endif %}
  </div>
</div>
```

---

## Task 7: Update `profile.html`

**Files:** Modify `templates/profile.html`

- [ ] After `<link rel="stylesheet" href="/static/style.css">`, add:
  ```html
  <script src="https://unpkg.com/htmx.org@1.9.10" crossorigin="anonymous"></script>
  ```

- [ ] Locate the closing `</div>` that closes `.page-wrap`. Insert immediately before it:

```html
  {# ── Scoring Instructions ───────────────────────────────────── #}
  <section id="scoring-notes" class="settings-section">
    <h2 class="settings-section-title">Scoring Instructions</h2>
    <p class="settings-label">
      These instructions are injected into the LLM scoring prompt alongside
      your profile. One instruction per line. Saved to <code>profile.json</code>.
    </p>

    {# Save form — plain POST, no LLM call #}
    <form class="settings-form" method="POST" action="/profile/scoring-notes">
      <textarea
        name="scoring_notes"
        class="settings-input"
        rows="8"
        spellcheck="false"
        autocomplete="off"
        placeholder="e.g. Prioritize Azure roles. AWS is acceptable but downweight if required as a skill.">{{ scoring_notes_text }}</textarea>
      <button type="submit" class="btn btn-save">Save Instructions</button>
    </form>

    <hr style="border-color: var(--border-subtle); margin: 24px 0;">

    <h3 class="settings-section-title">Test Against a Listing</h3>
    <p class="settings-label">
      Score a listing against the <em>saved</em> profile to see how your
      instructions affect the result. Does not modify stored scores.
    </p>
    <p class="settings-label" style="color: var(--text-muted); font-size: 0.8rem;">
      Each test makes a live LLM API call — token costs apply.
    </p>

    {# Test form — HTMX POST, LLM call, result swapped inline #}
    <form
      class="settings-form"
      hx-post="/api/test-profile"
      hx-target="#test-result"
      hx-swap="innerHTML">
      <select
        name="listing_id"
        class="settings-input settings-select"
        {% if not recent_listings %}disabled{% endif %}>
        {% if recent_listings %}
          {% for l in recent_listings %}
          <option value="{{ l.id }}">
            {{ l.title | title }} &mdash; {{ l.company }}
            {% if l.score is not none %} ({{ l.score }}/10){% endif %}
          </option>
          {% endfor %}
        {% else %}
          <option value="">No scored listings yet — run an ingestion first</option>
        {% endif %}
      </select>
      <div style="display: flex; align-items: center; gap: 12px; margin-top: 8px;">
        <button
          type="submit"
          class="btn"
          {% if not recent_listings %}disabled title="Run an ingestion first"{% endif %}>
          Test Profile
        </button>
        <span class="htmx-indicator validate-spinner" id="test-profile-spinner">
          scoring&hellip;
        </span>
      </div>
    </form>

    <div id="test-result" style="margin-top: 16px;"></div>
  </section>
```

**Note:** Save and Test are separate forms with separate submit buttons. Saving does not trigger a test; testing uses the last saved version of `profile.json`. This is intentional — see design decisions at the top of this plan.

---

## Task 8: Add CSS

**Files:** Modify `static/style.css`

- [ ] Append to the end of `I:\Web Development\job_matcher\static\style.css`:

```css
/* ------------------------------------------------------------
   Profile page — test result panel
   ------------------------------------------------------------ */
.test-result {
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  padding: 16px 20px;
  margin-top: 12px;
}

.test-result-header {
  font-family: var(--font-ui);
  font-size: 0.85rem;
  margin-bottom: 10px;
}

.score-delta {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}

.score-delta-label {
  font-family: var(--font-ui);
  font-size: 0.72rem;
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.score-delta-arrow {
  font-size: 1rem;
  color: var(--text-muted);
}

.test-result-warning {
  font-family: var(--font-ui);
  font-size: 0.78rem;
  color: var(--text-muted);
  margin: 0 0 10px 0;
  padding: 6px 10px;
  background: color-mix(in srgb, var(--bg-surface) 80%, orange 20%);
  border-radius: var(--radius-sm);
}

.test-result-section {
  margin-top: 10px;
  font-family: var(--font-ui);
  font-size: 0.82rem;
  line-height: 1.6;
}

.test-result-section ul {
  margin: 4px 0 0 16px;
  padding: 0;
}

.test-result-section li {
  margin-bottom: 2px;
}

/* Diff indicators */
.chip-added {
  outline: 1px solid var(--color-high);
  font-weight: 600;
}

.chip-removed {
  background: var(--bg-base);
  color: var(--text-muted);
  text-decoration: line-through;
  opacity: 0.7;
}

.concern-added {
  color: var(--color-low, #c0392b);
  font-weight: 500;
}

.concern-removed {
  color: var(--text-muted);
  text-decoration: line-through;
  opacity: 0.7;
}

.verdict-before {
  color: var(--text-muted);
  font-size: 0.80rem;
  margin: 2px 0;
}

.verdict-after {
  font-size: 0.82rem;
  margin: 2px 0;
}

.model-badge {
  font-family: var(--font-mono);
  font-size: 0.70rem;
  color: var(--text-muted);
}
```

- [ ] Commit:
  ```
  git add "I:/Web Development/job_matcher/templates/_test_result.html"
  git add "I:/Web Development/job_matcher/templates/profile.html"
  git add "I:/Web Development/job_matcher/static/style.css"
  git commit -m "$(cat <<'EOF'
  Add Scoring Instructions UI to Profile page

  - Separate Save and Test buttons (decoupled by design)
  - _test_result.html: diff-style breakdown with +/- skill/concern indicators
  - Model name shown for both stored and new result; warning if models differ
  - Verdict shown as before/after if text changed
  - Disabled state on Test form when no listings exist
  - CSS for score delta, diff chips, concern indicators, model mismatch warning

  Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
  EOF
  )"
  ```

---

## Verification

### Automated tests
```
cd "I:\Web Development\job_matcher"
python -m pytest tests/ -v --tb=short
```
Expected: all pre-existing tests pass + all new tests green.

### Manual end-to-end checklist
Start the server: `python app.py`

- [ ] The **Profile** page loads without errors.
- [ ] "Scoring Instructions" section visible below config.json editor.
- [ ] Textarea shows current `scoring_notes` from `profile.json`, one per line.
- [ ] Listing dropdown shows title, company, and score (`7/10`).
- [ ] With no listings: dropdown is disabled, Test button is disabled, explanatory text shown.
- [ ] Edit instructions and click **Save Instructions** → page redirects to `/profile#scoring-notes`. Open `profile.json` on disk — confirms `scoring_notes` updated. No LLM call made.
- [ ] Click **Test Profile** (with listings present) → spinner appears → `#test-result` populates with before/after score row and diff breakdown. No page reload.
- [ ] Skills that are new in the test result show with `+` prefix and highlighted border.
- [ ] Skills that disappeared from the test result show struck through and faded.
- [ ] Concerns show same `+`/`-` treatment.
- [ ] Verdict shows before and after if text changed; single line if unchanged.
- [ ] If old and new `model_used` differ: warning appears below the score row.
- [ ] Confirm `listing.score` in DB is unchanged after testing.
- [ ] Browser DevTools → Network: Test is `POST /api/test-profile` XHR; Save is a normal form POST to `/profile/scoring-notes`.
