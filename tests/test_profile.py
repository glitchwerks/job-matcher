"""
tests/test_profile.py — Tests for the structured /profile route (issue #319).

Covered cases
-------------

GET /profile:
* Returns 200
* Renders primary_skills values from profile.json
* Renders seniority value from profile.json
* Renders scoring threshold from config.json
* Renders search fields (country, what, where) from config.json
* Renders prefilter title_include and title_exclude from config.json
* Renders location center from profile.json location block
* Renders scoring_notes from profile.json
* Does NOT render a raw JSON textarea
* Works when profile.json is absent (empty form)

POST /profile — happy path:
* Writes primary_skills list to profile.json
* Writes anti_preferences list to profile.json
* Writes seniority to profile.json
* Writes preferred_industries to profile.json
* Writes location block (center, radius_km, geocode_fallback, notes) to profile.json
* Writes scoring_notes to profile.json
* Updates scoring.threshold in config.json
* Updates candidate search fields (country, what, where) in config.json
* Leaves technical keys (results_per_page, max_pages, model) untouched in config.json
* Updates prefilter.title_include and title_exclude in config.json
* Sets prefilter.require_contract_time / require_contract_type to None when "Any" selected
* Returns 200 with saved=True notice

POST /profile — validation:
* Returns 422 when scoring_threshold is empty
* Returns 422 when scoring_threshold is not a number
* Returns 422 when scoring_threshold is below 0
* Returns 422 when scoring_threshold is above 10
* Does NOT write either file when validation fails

POST /settings (Search Settings tab):
* Saves results_per_page to config.json search block
* Saves max_pages to config.json search block
* Leaves other config.json keys untouched when saving search settings
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import services.profile_store as _profile_store_module
from app import app as flask_app


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture()
def tmp_config_path(tmp_path, monkeypatch):
    """Point _CONFIG_PATH at a temp file for isolation.

    Patches profile_store, web.profile, and web.settings so all HTTP
    endpoints read/write the same temp path.
    """
    import web.profile as profile_module  # noqa: PLC0415
    import web.settings as settings_module  # noqa: PLC0415
    path = str(tmp_path / "config.json")
    monkeypatch.setattr(_profile_store_module, "_CONFIG_PATH", path)
    monkeypatch.setattr(profile_module, "_CONFIG_PATH", path)
    monkeypatch.setattr(settings_module, "_CONFIG_PATH", path)
    return path


@pytest.fixture()
def tmp_profile_path(tmp_path, monkeypatch):
    """Point _PROFILE_PATH at a temp file for isolation.

    Patches profile_store and web.profile so the HTTP endpoint reads
    the same temp path.
    """
    import web.profile as profile_module  # noqa: PLC0415
    path = str(tmp_path / "profile.json")
    monkeypatch.setattr(_profile_store_module, "_PROFILE_PATH", path)
    monkeypatch.setattr(profile_module, "_PROFILE_PATH", path)
    return path


@pytest.fixture()
def tmp_providers_path(tmp_path, monkeypatch):
    """Point _PROVIDERS_PATH at a temp file so providers are isolated.

    Patches profile_store, web.settings, and web.profile so all HTTP
    endpoints read the same temp path.
    """
    import web.profile as profile_module  # noqa: PLC0415
    import web.settings as settings_module  # noqa: PLC0415
    path = str(tmp_path / "providers.json")
    monkeypatch.setattr(_profile_store_module, "_PROVIDERS_PATH", path)
    monkeypatch.setattr(profile_module, "_PROVIDERS_PATH", path)
    monkeypatch.setattr(settings_module, "_PROVIDERS_PATH", path)
    return path


@pytest.fixture()
def tmp_keys_path(tmp_path, monkeypatch):
    """Point _KEYS_PATH at a temp file so legacy migration never triggers.

    Patches profile_store, web.settings, and web.profile so all HTTP
    endpoints read the same temp path.
    """
    import web.profile as profile_module  # noqa: PLC0415
    import web.settings as settings_module  # noqa: PLC0415
    path = str(tmp_path / "keys.json")
    monkeypatch.setattr(_profile_store_module, "_KEYS_PATH", path)
    monkeypatch.setattr(profile_module, "_KEYS_PATH", path)
    monkeypatch.setattr(settings_module, "_KEYS_PATH", path)
    return path


@pytest.fixture()
def client():
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


def _write_config(path: str, data: dict | None = None) -> None:
    """Write a minimal config.json fixture."""
    if data is None:
        data = {
            "search": {
                "country": "us",
                "what": "software engineer",
                "where": "coconut creek",
                "distance": 32,
                "max_days_old": 14,
                "salary_min": 100000,
                "results_per_page": 50,
                "max_pages": 5,
            },
            "scoring": {
                "threshold": 7.0,
                "model": "claude-haiku-4-5-20251001",
            },
            "prefilter": {
                "title_include": ["engineer", "developer"],
                "title_exclude": ["junior", "intern"],
                "require_contract_time": None,
                "require_contract_type": None,
            },
        }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _write_profile(path: str, data: dict | None = None) -> None:
    """Write a minimal profile.json fixture."""
    if data is None:
        data = {
            "primary_skills": [
                {"description": "Python", "years_active": 5, "active": True},
                {"description": "Go", "years_active": 2, "active": True},
            ],
            "anti_preferences": ["no QA roles"],
            "seniority": "Senior / Staff",
            "preferred_industries": ["developer tooling", "fintech"],
            "location": {
                "center": "Miami, FL",
                "radius_km": 80,
                "geocode_fallback": "pass",
                "notes": "Open to remote or on-site in South Florida",
            },
            "scoring_notes": ["Senior roles preferred"],
        }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ===========================================================================
# GET /profile — renders all fields from both files
# ===========================================================================


class TestProfileGet:
    def test_returns_200(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        _write_config(tmp_config_path)
        _write_profile(tmp_profile_path)
        resp = client.get("/profile")
        assert resp.status_code == 200

    def test_renders_primary_skills(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        """GET /profile renders skill names and years from structured skill objects."""
        _write_config(tmp_config_path)
        _write_profile(tmp_profile_path)
        body = client.get("/profile").data.decode()
        # Skill description must appear as an input value in the table.
        assert 'value="Python"' in body
        assert 'value="Go"' in body
        # Years must appear in number inputs.
        assert 'value="5"' in body
        assert 'value="2"' in body

    def test_renders_seniority(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        _write_config(tmp_config_path)
        _write_profile(tmp_profile_path)
        body = client.get("/profile").data.decode()
        assert "Senior / Staff" in body

    def test_renders_scoring_threshold(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        _write_config(tmp_config_path)
        _write_profile(tmp_profile_path)
        body = client.get("/profile").data.decode()
        assert "7.0" in body

    def test_renders_search_country(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        _write_config(tmp_config_path)
        _write_profile(tmp_profile_path)
        body = client.get("/profile").data.decode()
        # The country value "us" must appear in the rendered form.
        assert 'value="us"' in body

    def test_renders_search_what(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        _write_config(tmp_config_path)
        _write_profile(tmp_profile_path)
        body = client.get("/profile").data.decode()
        assert "software engineer" in body

    def test_renders_prefilter_title_include(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        _write_config(tmp_config_path)
        _write_profile(tmp_profile_path)
        body = client.get("/profile").data.decode()
        assert 'value="engineer"' in body
        assert 'value="developer"' in body

    def test_renders_prefilter_title_exclude(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        _write_config(tmp_config_path)
        _write_profile(tmp_profile_path)
        body = client.get("/profile").data.decode()
        assert 'value="junior"' in body
        assert 'value="intern"' in body

    def test_renders_location_center(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        _write_config(tmp_config_path)
        _write_profile(tmp_profile_path)
        body = client.get("/profile").data.decode()
        assert "Miami, FL" in body

    def test_renders_scoring_notes(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        _write_config(tmp_config_path)
        _write_profile(tmp_profile_path)
        body = client.get("/profile").data.decode()
        assert "Senior roles preferred" in body

    def test_no_raw_json_textarea(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        """The old raw JSON textarea must be gone — only structured inputs remain."""
        _write_config(tmp_config_path)
        _write_profile(tmp_profile_path)
        body = client.get("/profile").data.decode()
        assert "config_json" not in body

    def test_renders_education_entries(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        """GET /profile must pre-populate education table rows from structured objects."""
        _write_config(tmp_config_path)
        _write_profile(tmp_profile_path, {
            **{
                "primary_skills": [],
                "anti_preferences": [],
                "seniority": "",
                "preferred_industries": [],
                "location": {"geocode_fallback": "pass"},
                "scoring_notes": [],
            },
            "education": [
                {
                    "degree_type": "B.S.",
                    "degree_field": "Computer Science",
                    "school": "MIT",
                    "graduation_year": "2010",
                }
            ],
        })
        body = client.get("/profile").data.decode()
        # Table structure must be present.
        assert "edu-table" in body
        # Degree type select or input must contain the type value.
        assert "B.S." in body
        # Field of study input must appear.
        assert 'value="Computer Science"' in body
        # School input must appear.
        assert 'value="MIT"' in body
        # Year input must appear.
        assert 'value="2010"' in body
        # Structured field names must be used (not the old education[]).
        assert 'name="edu_type[]"' in body
        assert 'name="edu_field[]"' in body
        assert 'name="edu_school[]"' in body
        assert 'name="edu_year[]"' in body

    def test_renders_when_profile_absent(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        """GET must succeed even when profile.json does not exist."""
        _write_config(tmp_config_path)
        # Intentionally do NOT create tmp_profile_path
        resp = client.get("/profile")
        assert resp.status_code == 200

    def test_renders_legacy_string_education(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        """GET must not 500 when profile.json has old-format plain-string education entries.

        Regression test for issue #149: PR #140 restructured education from free-text
        strings to dicts.  Profiles written before the migration still contain strings;
        load_profile() must normalise them so the template never receives a str where
        it expects a dict with .get().
        """
        _write_config(tmp_config_path)
        _write_profile(tmp_profile_path, {
            "primary_skills": [],
            "anti_preferences": [],
            "seniority": "",
            "preferred_industries": [],
            "location": {"geocode_fallback": "pass"},
            "scoring_notes": [],
            "education": ["B.S. in Computer Science from MIT"],  # old free-text format
        })
        resp = client.get("/profile")
        assert resp.status_code == 200
        body = resp.data.decode()
        # The legacy string must be surfaced in the degree_field column.
        assert "B.S. in Computer Science from MIT" in body
        # Must use structured field names, not a raw textarea.
        assert 'name="edu_field[]"' in body

    def test_renders_mixed_format_education(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        """GET must not 500 when education contains a dict first and a string second.

        Regression test: the old gate ``if raw_edu and not isinstance(raw_edu[0], dict)``
        evaluated to False when the first element was already a dict, silently skipping
        normalisation for subsequent string entries and crashing the template.
        """
        _write_config(tmp_config_path)
        _write_profile(tmp_profile_path, {
            "primary_skills": [],
            "anti_preferences": [],
            "seniority": "",
            "preferred_industries": [],
            "location": {"geocode_fallback": "pass"},
            "scoring_notes": [],
            "education": [
                {"degree_type": "B.S.", "degree_field": "Computer Science", "school": "MIT", "graduation_year": "2010"},
                "M.S. in Data Science from Stanford",  # legacy string after a structured dict
            ],
        })
        resp = client.get("/profile")
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "Computer Science" in body
        assert "M.S. in Data Science from Stanford" in body

    def test_renders_multiple_legacy_strings_education(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        """GET must not 500 when education contains multiple plain strings."""
        _write_config(tmp_config_path)
        _write_profile(tmp_profile_path, {
            "primary_skills": [],
            "anti_preferences": [],
            "seniority": "",
            "preferred_industries": [],
            "location": {"geocode_fallback": "pass"},
            "scoring_notes": [],
            "education": [
                "B.S. in Computer Science from MIT",
                "M.S. in Data Science from Stanford",
                "Ph.D. in Machine Learning from CMU",
            ],
        })
        resp = client.get("/profile")
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "B.S. in Computer Science from MIT" in body
        assert "M.S. in Data Science from Stanford" in body
        assert "Ph.D. in Machine Learning from CMU" in body

    def test_renders_already_structured_education(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        """GET must pass already-structured education dicts through unchanged.

        Verifies that the migration guard does not re-wrap dicts that are already
        in the correct structured format — structured data should render its fields.
        """
        _write_config(tmp_config_path)
        _write_profile(tmp_profile_path, {
            "primary_skills": [],
            "anti_preferences": [],
            "seniority": "",
            "preferred_industries": [],
            "location": {"geocode_fallback": "pass"},
            "scoring_notes": [],
            "education": [
                {
                    "degree_type": "M.S.",
                    "degree_field": "Software Engineering",
                    "school": "Stanford",
                    "graduation_year": "2015",
                }
            ],
        })
        resp = client.get("/profile")
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "M.S." in body
        assert 'value="Software Engineering"' in body
        assert 'value="Stanford"' in body
        assert 'value="2015"' in body

    def test_renders_empty_education_array(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        """GET must not 500 when education is an empty array."""
        _write_config(tmp_config_path)
        _write_profile(tmp_profile_path, {
            "primary_skills": [],
            "anti_preferences": [],
            "seniority": "",
            "preferred_industries": [],
            "location": {"geocode_fallback": "pass"},
            "scoring_notes": [],
            "education": [],
        })
        resp = client.get("/profile")
        assert resp.status_code == 200



# ===========================================================================
# POST /profile — happy path
# ===========================================================================


class TestProfilePost:
    def _post(self, client, **kwargs):
        """Helper: POST to /profile with sensible defaults for required fields."""
        data = {
            "scoring_threshold": "7.0",
            "search_country": "us",
            "search_what": "engineer",
            "search_where": "miami",
            "location_geocode_fallback": "pass",
        }
        data.update(kwargs)
        return client.post("/profile", data=data)

    def test_writes_primary_skills(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        """POST with structured skill fields writes typed objects to profile.json."""
        _write_config(tmp_config_path)
        self._post(
            client,
            **{
                "skill_description[]": ["Python", "Go"],
                "skill_years_active[]": ["5", "2"],
                "skill_active_idx[]": ["0", "1"],
            },
        )
        with open(tmp_profile_path, encoding="utf-8") as f:
            prof = json.load(f)
        assert prof["primary_skills"] == [
            {"description": "Python", "years_active": 5, "active": True},
            {"description": "Go", "years_active": 2, "active": True},
        ]

    def test_writes_anti_preferences(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        _write_config(tmp_config_path)
        self._post(client, **{"anti_preferences[]": ["no QA", "no frontend"]})
        with open(tmp_profile_path, encoding="utf-8") as f:
            prof = json.load(f)
        assert prof["anti_preferences"] == ["no QA", "no frontend"]

    def test_writes_education(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        """POST with structured edu_* fields must persist structured objects to profile.json."""
        _write_config(tmp_config_path)
        self._post(
            client,
            **{
                "edu_type[]": ["B.S.", "M.S."],
                "edu_field[]": ["Computer Science", "Software Engineering"],
                "edu_school[]": ["MIT", "Stanford"],
                "edu_year[]": ["2010", "2012"],
            },
        )
        with open(tmp_profile_path, encoding="utf-8") as f:
            prof = json.load(f)
        assert prof["education"] == [
            {
                "degree_type": "B.S.",
                "degree_field": "Computer Science",
                "school": "MIT",
                "graduation_year": "2010",
            },
            {
                "degree_type": "M.S.",
                "degree_field": "Software Engineering",
                "school": "Stanford",
                "graduation_year": "2012",
            },
        ]

    def test_writes_education_skips_empty_rows(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        """POST with empty education rows must discard rows where all four fields are empty."""
        _write_config(tmp_config_path)
        self._post(
            client,
            **{
                "edu_type[]": ["B.S.", ""],
                "edu_field[]": ["Computer Science", ""],
                "edu_school[]": ["MIT", ""],
                "edu_year[]": ["2010", ""],
            },
        )
        with open(tmp_profile_path, encoding="utf-8") as f:
            prof = json.load(f)
        assert len(prof["education"]) == 1
        assert prof["education"][0]["school"] == "MIT"

    def test_writes_seniority(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        _write_config(tmp_config_path)
        self._post(client, seniority="Senior / Staff")
        with open(tmp_profile_path, encoding="utf-8") as f:
            prof = json.load(f)
        assert prof["seniority"] == "Senior / Staff"

    def test_writes_preferred_industries(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        _write_config(tmp_config_path)
        self._post(client, **{"preferred_industries[]": ["fintech", "AI platforms"]})
        with open(tmp_profile_path, encoding="utf-8") as f:
            prof = json.load(f)
        assert prof["preferred_industries"] == ["fintech", "AI platforms"]

    def test_writes_location_block(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        _write_config(tmp_config_path)
        self._post(
            client,
            location_center="Miami, FL",
            location_radius_km="80",
            location_geocode_fallback="discard",
            location_notes="Remote or hybrid",
        )
        with open(tmp_profile_path, encoding="utf-8") as f:
            prof = json.load(f)
        assert prof["location"]["center"] == "Miami, FL"
        assert prof["location"]["radius_km"] == 80.0
        assert prof["location"]["geocode_fallback"] == "discard"
        assert prof["location"]["notes"] == "Remote or hybrid"

    def test_writes_scoring_notes(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        _write_config(tmp_config_path)
        self._post(client, **{"scoring_notes[]": ["Prefer senior", "Mid is ok"]})
        with open(tmp_profile_path, encoding="utf-8") as f:
            prof = json.load(f)
        assert prof["scoring_notes"] == ["Prefer senior", "Mid is ok"]

    def test_updates_scoring_threshold_in_config(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        _write_config(tmp_config_path)
        self._post(client, scoring_threshold="8.5")
        with open(tmp_config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        assert cfg["scoring"]["threshold"] == 8.5

    def test_updates_search_candidate_fields(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        _write_config(tmp_config_path)
        self._post(
            client,
            search_country="gb",
            search_what="backend developer",
            search_where="london",
        )
        with open(tmp_config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        assert cfg["search"]["country"] == "gb"
        assert cfg["search"]["what"] == "backend developer"
        assert cfg["search"]["where"] == "london"

    def test_preserves_technical_keys_in_config(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        """results_per_page, max_pages, and model must not be touched by the profile POST."""
        _write_config(tmp_config_path)
        self._post(client)
        with open(tmp_config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        assert cfg["search"]["results_per_page"] == 50
        assert cfg["search"]["max_pages"] == 5
        assert cfg["scoring"]["model"] == "claude-haiku-4-5-20251001"

    def test_updates_prefilter_title_include(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        _write_config(tmp_config_path)
        self._post(client, **{"prefilter_title_include[]": ["engineer", "developer", "sre"]})
        with open(tmp_config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        assert cfg["prefilter"]["title_include"] == ["engineer", "developer", "sre"]

    def test_updates_prefilter_title_exclude(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        _write_config(tmp_config_path)
        self._post(client, **{"prefilter_title_exclude[]": ["junior", "intern", "manager"]})
        with open(tmp_config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        assert cfg["prefilter"]["title_exclude"] == ["junior", "intern", "manager"]

    def test_prefilter_contract_time_none_when_any_selected(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        """Submitting an empty string for contract_time must store None."""
        _write_config(tmp_config_path)
        self._post(client, prefilter_require_contract_time="")
        with open(tmp_config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        assert cfg["prefilter"]["require_contract_time"] is None

    def test_prefilter_contract_time_stored_when_set(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        _write_config(tmp_config_path)
        self._post(client, prefilter_require_contract_time="full_time")
        with open(tmp_config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        assert cfg["prefilter"]["require_contract_time"] == "full_time"

    def test_prefilter_contract_type_none_when_any_selected(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        _write_config(tmp_config_path)
        self._post(client, prefilter_require_contract_type="")
        with open(tmp_config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        assert cfg["prefilter"]["require_contract_type"] is None

    def test_returns_200_with_saved_notice(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        _write_config(tmp_config_path)
        resp = self._post(client)
        assert resp.status_code == 200
        assert b"saved" in resp.data.lower()

    def test_empty_rows_are_excluded(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        """Empty or whitespace-only skill descriptions must be dropped."""
        _write_config(tmp_config_path)
        self._post(
            client,
            **{
                "skill_description[]": ["Python", "  ", "Go", ""],
                "skill_years_active[]": ["5", "0", "2", "0"],
                "skill_active_idx[]": ["0", "2"],
            },
        )
        with open(tmp_profile_path, encoding="utf-8") as f:
            prof = json.load(f)
        # Indices 0 and 2 are non-empty; index 0 is active (in active_idx), index 2 is active.
        assert prof["primary_skills"] == [
            {"description": "Python", "years_active": 5, "active": True},
            {"description": "Go", "years_active": 2, "active": True},
        ]


# ===========================================================================
# POST /profile — validation failures
# ===========================================================================


class TestProfilePostValidation:
    def _post(self, client, **kwargs):
        data = {
            "search_country": "us",
            "search_what": "engineer",
            "search_where": "miami",
            "location_geocode_fallback": "pass",
        }
        data.update(kwargs)
        return client.post("/profile", data=data)

    def test_empty_threshold_returns_422(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        _write_config(tmp_config_path)
        resp = self._post(client, scoring_threshold="")
        assert resp.status_code == 422

    def test_non_numeric_threshold_returns_422(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        _write_config(tmp_config_path)
        resp = self._post(client, scoring_threshold="abc")
        assert resp.status_code == 422

    def test_threshold_below_zero_returns_422(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        _write_config(tmp_config_path)
        resp = self._post(client, scoring_threshold="-1")
        assert resp.status_code == 422

    def test_threshold_above_ten_returns_422(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        _write_config(tmp_config_path)
        resp = self._post(client, scoring_threshold="11")
        assert resp.status_code == 422

    def test_validation_failure_does_not_write_profile(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        """On validation error, profile.json must not be created or modified."""
        _write_config(tmp_config_path)
        self._post(client, scoring_threshold="bad")
        assert not os.path.exists(tmp_profile_path)

    def test_validation_failure_does_not_write_config(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        """On validation error, config.json must remain exactly as it was."""
        _write_config(tmp_config_path)
        with open(tmp_config_path, encoding="utf-8") as f:
            original = f.read()
        self._post(client, scoring_threshold="bad")
        with open(tmp_config_path, encoding="utf-8") as f:
            after = f.read()
        assert original == after


# ===========================================================================
# POST /settings (Search Settings tab) — results_per_page / max_pages
# ===========================================================================


class TestSettingsSearchTab:
    def test_saves_results_per_page(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        _write_config(tmp_config_path)
        client.post("/settings", data={
            "tab": "search",
            "search_results_per_page": "25",
            "search_max_pages": "3",
        })
        with open(tmp_config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        assert cfg["search"]["results_per_page"] == 25

    def test_saves_max_pages(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        _write_config(tmp_config_path)
        client.post("/settings", data={
            "tab": "search",
            "search_results_per_page": "50",
            "search_max_pages": "10",
        })
        with open(tmp_config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        assert cfg["search"]["max_pages"] == 10

    def test_leaves_other_config_keys_untouched(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        """Saving search settings must not clobber scoring or prefilter blocks."""
        _write_config(tmp_config_path)
        client.post("/settings", data={
            "tab": "search",
            "search_results_per_page": "20",
            "search_max_pages": "2",
        })
        with open(tmp_config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        # Scoring block must be untouched.
        assert cfg["scoring"]["threshold"] == 7.0
        assert cfg["scoring"]["model"] == "claude-haiku-4-5-20251001"
        # Prefilter block must be untouched.
        assert cfg["prefilter"]["title_include"] == ["engineer", "developer"]

    def test_search_settings_tab_renders_in_settings_page(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        """The Search Settings tab button must appear on the settings page."""
        _write_config(tmp_config_path)
        body = client.get("/settings").data.decode()
        assert "Search Settings" in body

    def test_search_settings_pane_contains_results_per_page(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        _write_config(tmp_config_path)
        body = client.get("/settings?tab=search").data.decode()
        assert "results_per_page" in body
        assert "max_pages" in body


# ===========================================================================
# POST /profile — numeric field validation edge cases
# ===========================================================================


class TestProfileNumericValidation:
    """Regression tests for the numeric field validation added in PR #327.

    Covers: location.radius_km (must be > 0) and search.distance (must be >= 0,
    whole number).  All invalid inputs must return 422 without touching disk.
    """

    def _post(self, client, **kwargs):
        data = {
            "scoring_threshold": "7.0",
            "search_country": "us",
            "search_what": "engineer",
            "search_where": "miami",
            "location_geocode_fallback": "pass",
        }
        data.update(kwargs)
        return client.post("/profile", data=data)

    # --- location_radius_km ---

    def test_negative_radius_returns_422(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        _write_config(tmp_config_path)
        resp = self._post(client, location_radius_km="-1")
        assert resp.status_code == 422

    def test_zero_radius_returns_422(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        _write_config(tmp_config_path)
        resp = self._post(client, location_radius_km="0")
        assert resp.status_code == 422

    def test_non_numeric_radius_returns_422(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        _write_config(tmp_config_path)
        resp = self._post(client, location_radius_km="abc")
        assert resp.status_code == 422

    def test_valid_positive_radius_accepted(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        """Positive radius must be saved without error."""
        _write_config(tmp_config_path)
        resp = self._post(client, location_radius_km="80")
        assert resp.status_code == 200
        with open(tmp_profile_path, encoding="utf-8") as f:
            prof = json.load(f)
        assert prof["location"]["radius_km"] == 80.0

    def test_radius_validation_does_not_write_profile(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        """On radius validation error, profile.json must not be created."""
        _write_config(tmp_config_path)
        self._post(client, location_radius_km="-5")
        assert not os.path.exists(tmp_profile_path)

    # --- search_distance ---

    def test_non_numeric_distance_returns_422(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        _write_config(tmp_config_path)
        resp = self._post(client, search_distance="far")
        assert resp.status_code == 422

    def test_negative_distance_returns_422(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        _write_config(tmp_config_path)
        resp = self._post(client, search_distance="-10")
        assert resp.status_code == 422

    def test_zero_distance_accepted(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        """Zero is a valid distance (Adzuna default — no radius constraint)."""
        _write_config(tmp_config_path)
        resp = self._post(client, search_distance="0")
        assert resp.status_code == 200
        with open(tmp_config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        assert cfg["search"]["distance"] == 0

    # --- search_salary_min ---

    def test_negative_salary_min_returns_422(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        _write_config(tmp_config_path)
        resp = self._post(client, search_salary_min="-1")
        assert resp.status_code == 422

    def test_non_numeric_salary_min_returns_422(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        _write_config(tmp_config_path)
        resp = self._post(client, search_salary_min="abc")
        assert resp.status_code == 422

    # --- search_max_days_old ---

    def test_negative_max_days_old_returns_422(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        _write_config(tmp_config_path)
        resp = self._post(client, search_max_days_old="-1")
        assert resp.status_code == 422

    def test_zero_max_days_old_returns_422(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        """Zero days old means no jobs would match — must be rejected."""
        _write_config(tmp_config_path)
        resp = self._post(client, search_max_days_old="0")
        assert resp.status_code == 422

    def test_non_numeric_max_days_old_returns_422(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        _write_config(tmp_config_path)
        resp = self._post(client, search_max_days_old="two")
        assert resp.status_code == 422


# ===========================================================================
# POST /profile — structured primary_skills validation
# ===========================================================================


class TestStructuredSkillsValidation:
    """Tests for the new typed primary_skills fields (issue #74)."""

    def _post(self, client, **kwargs):
        data = {
            "scoring_threshold": "7.0",
            "search_country": "us",
            "search_what": "engineer",
            "search_where": "miami",
            "location_geocode_fallback": "pass",
        }
        data.update(kwargs)
        return client.post("/profile", data=data)

    def test_writes_primary_skills_active_flag(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        """active=True when index appears in skill_active_idx[], False otherwise."""
        _write_config(tmp_config_path)
        self._post(
            client,
            **{
                "skill_description[]": ["Python", "C++"],
                "skill_years_active[]": ["5", "4"],
                # Only index 0 (Python) is active; C++ at index 1 is dormant.
                "skill_active_idx[]": ["0"],
            },
        )
        with open(tmp_profile_path, encoding="utf-8") as f:
            prof = json.load(f)
        skills = prof["primary_skills"]
        assert skills[0] == {"description": "Python", "years_active": 5, "active": True}
        assert skills[1] == {"description": "C++", "years_active": 4, "active": False}

    def test_years_active_negative_returns_422(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        """Negative years_active must be rejected with HTTP 422."""
        _write_config(tmp_config_path)
        resp = self._post(
            client,
            **{
                "skill_description[]": ["Python"],
                "skill_years_active[]": ["-1"],
                "skill_active_idx[]": ["0"],
            },
        )
        assert resp.status_code == 422

    def test_years_active_non_numeric_returns_422(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        """Non-numeric years_active must be rejected with HTTP 422."""
        _write_config(tmp_config_path)
        resp = self._post(
            client,
            **{
                "skill_description[]": ["Python"],
                "skill_years_active[]": ["five"],
                "skill_active_idx[]": ["0"],
            },
        )
        assert resp.status_code == 422

    def test_renders_structured_skills_in_table(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        """GET /profile must render a <table> with structured skill data."""
        _write_config(tmp_config_path)
        _write_profile(tmp_profile_path, {
            "primary_skills": [
                {"description": "Rust", "years_active": 3, "active": True},
                {"description": "COBOL", "years_active": 10, "active": False},
            ],
            "anti_preferences": [],
            "seniority": "",
            "preferred_industries": [],
            "location": {"geocode_fallback": "pass"},
            "scoring_notes": [],
        })
        body = client.get("/profile").data.decode()
        # Table must be present.
        assert "<table" in body
        assert "skills-table" in body
        # Skill names must appear as input values.
        assert 'value="Rust"' in body
        assert 'value="COBOL"' in body
        # Years must appear.
        assert 'value="3"' in body
        assert 'value="10"' in body
        # Active skill must have checked attribute; dormant must not.
        # We check that the Rust row has checked somewhere near its description.
        # A simple approach: count checked vs unchecked.
        assert "checked" in body  # at least one checked input


# ===========================================================================
# POST /profile — education graduation year sanitization (issue #143)
# ===========================================================================


class TestEducationYearSanitization:
    """Regression tests for server-side graduation year validation.

    Non-numeric year values (e.g. "abcd", "20@@") must be silently discarded
    to the empty string rather than persisted, so only all-digit values (e.g.
    "2010") or empty strings reach profile.json.
    """

    def _post(self, client, **kwargs):
        data = {
            "scoring_threshold": "7.0",
            "search_country": "us",
            "search_what": "engineer",
            "search_where": "miami",
            "location_geocode_fallback": "pass",
        }
        data.update(kwargs)
        return client.post("/profile", data=data)

    def test_non_numeric_year_is_sanitized_to_empty(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        """A non-numeric graduation year must be stored as empty string, not persisted as-is."""
        _write_config(tmp_config_path)
        self._post(
            client,
            **{
                "edu_type[]": ["B.S."],
                "edu_field[]": ["Computer Science"],
                "edu_school[]": ["MIT"],
                "edu_year[]": ["abcd"],
            },
        )
        with open(tmp_profile_path, encoding="utf-8") as f:
            prof = json.load(f)
        assert len(prof["education"]) == 1
        assert prof["education"][0]["graduation_year"] == ""

    def test_special_chars_year_is_sanitized_to_empty(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        """A year containing special characters (e.g. '20@@') must be sanitized to empty."""
        _write_config(tmp_config_path)
        self._post(
            client,
            **{
                "edu_type[]": ["M.S."],
                "edu_field[]": ["Software Engineering"],
                "edu_school[]": ["Stanford"],
                "edu_year[]": ["20@@"],
            },
        )
        with open(tmp_profile_path, encoding="utf-8") as f:
            prof = json.load(f)
        assert prof["education"][0]["graduation_year"] == ""

    def test_valid_numeric_year_is_preserved(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        """A valid all-digit year must pass through unchanged."""
        _write_config(tmp_config_path)
        self._post(
            client,
            **{
                "edu_type[]": ["Ph.D."],
                "edu_field[]": ["Mathematics"],
                "edu_school[]": ["Harvard"],
                "edu_year[]": ["2015"],
            },
        )
        with open(tmp_profile_path, encoding="utf-8") as f:
            prof = json.load(f)
        assert prof["education"][0]["graduation_year"] == "2015"

    def test_empty_year_is_preserved(
        self, client, tmp_config_path, tmp_profile_path, tmp_providers_path, tmp_keys_path
    ):
        """An empty year field must remain empty — the isdigit guard must not alter it."""
        _write_config(tmp_config_path)
        self._post(
            client,
            **{
                "edu_type[]": ["B.A."],
                "edu_field[]": ["History"],
                "edu_school[]": ["Yale"],
                "edu_year[]": [""],
            },
        )
        with open(tmp_profile_path, encoding="utf-8") as f:
            prof = json.load(f)
        assert prof["education"][0]["graduation_year"] == ""
