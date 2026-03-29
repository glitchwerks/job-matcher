"""
tests/test_prefilter.py — Unit tests for prefilter() in ingest.py.

prefilter() returns None when a listing passes all checks, and a non-empty
string describing the first failing check when a listing is rejected.
"""

import sys
import os

# Ensure the project root is on the path so we can import ingest directly.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ingest import prefilter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_listing(
    title: str = "Software Engineer",
    salary_min: float | None = None,
    salary_max: float | None = None,
    contract_time: str = "",
    contract_type: str = "",
) -> dict:
    """Return a minimal listing dict with only the fields prefilter() reads."""
    return {
        "title": title,
        "salary_min": salary_min,
        "salary_max": salary_max,
        "contract_time": contract_time,
        "contract_type": contract_type,
    }


def make_config(
    title_include: list[str] | None = None,
    title_exclude: list[str] | None = None,
    salary_min: float | None = None,
    require_contract_time: str | None = None,
    require_contract_type: str | None = None,
    include_prefilter: bool = True,
) -> dict:
    """Return a config dict with a prefilter section.

    Pass include_prefilter=False to simulate a config with no prefilter key.
    """
    if not include_prefilter:
        return {}

    pf: dict = {}
    if title_include is not None:
        pf["title_include"] = title_include
    if title_exclude is not None:
        pf["title_exclude"] = title_exclude
    if salary_min is not None:
        pf["salary_min"] = salary_min
    if require_contract_time is not None:
        pf["require_contract_time"] = require_contract_time
    if require_contract_type is not None:
        pf["require_contract_type"] = require_contract_type

    return {"prefilter": pf}


# ---------------------------------------------------------------------------
# Title include tests
# ---------------------------------------------------------------------------

def test_title_include_pass():
    """Listing passes when its title contains one of the include patterns."""
    listing = make_listing(title="Senior Python Engineer")
    config = make_config(title_include=["python", "django"])
    assert prefilter(listing, config) is None


def test_title_include_miss():
    """Listing is rejected when its title matches none of the include patterns."""
    listing = make_listing(title="Java Developer")
    config = make_config(title_include=["python", "django"])
    result = prefilter(listing, config)
    assert result is not None
    assert "title_include" in result


def test_title_include_case_insensitive():
    """Include patterns are matched case-insensitively."""
    listing = make_listing(title="PYTHON DEVELOPER")
    config = make_config(title_include=["python"])
    assert prefilter(listing, config) is None


# ---------------------------------------------------------------------------
# Title exclude tests
# ---------------------------------------------------------------------------

def test_title_exclude_match():
    """Listing is rejected when its title contains an exclude pattern."""
    listing = make_listing(title="Senior Java Engineer")
    config = make_config(title_exclude=["java"])
    result = prefilter(listing, config)
    assert result is not None
    assert "title_exclude" in result


def test_title_exclude_no_match_passes():
    """Listing passes when its title contains none of the exclude patterns."""
    listing = make_listing(title="Senior Python Engineer")
    config = make_config(title_exclude=["java", "cobol"])
    assert prefilter(listing, config) is None


def test_title_exclude_takes_priority_over_include():
    """Exclude is checked after include; a title that matches both is rejected.

    The listing title "Python/Java Engineer" satisfies the include pattern
    "python" but also triggers the exclude pattern "java". prefilter() checks
    include first then exclude, so the listing is still rejected.
    """
    listing = make_listing(title="Python/Java Engineer")
    config = make_config(title_include=["python"], title_exclude=["java"])
    result = prefilter(listing, config)
    assert result is not None
    assert "title_exclude" in result


def test_title_exclude_case_insensitive():
    """Exclude patterns are matched case-insensitively."""
    listing = make_listing(title="JAVA DEVELOPER")
    config = make_config(title_exclude=["java"])
    result = prefilter(listing, config)
    assert result is not None


# ---------------------------------------------------------------------------
# Salary floor tests
# ---------------------------------------------------------------------------

def test_salary_floor_pass():
    """Listing passes when salary_max is at or above the configured floor."""
    listing = make_listing(salary_max=100_000)
    config = make_config(salary_min=80_000)
    assert prefilter(listing, config) is None


def test_salary_floor_exact_boundary_passes():
    """Listing passes when salary_max exactly equals the floor."""
    listing = make_listing(salary_max=80_000)
    config = make_config(salary_min=80_000)
    assert prefilter(listing, config) is None


def test_salary_floor_fail():
    """Listing is rejected when salary_max is below the configured floor."""
    listing = make_listing(salary_max=50_000)
    config = make_config(salary_min=80_000)
    result = prefilter(listing, config)
    assert result is not None
    assert "salary" in result


def test_salary_missing_passes_regardless_of_floor():
    """Listing with no salary_max passes even when a floor is configured.

    The spec says listings with no salary data are always allowed through
    so as not to exclude roles that don't advertise compensation.
    """
    listing = make_listing(salary_max=None)
    config = make_config(salary_min=80_000)
    assert prefilter(listing, config) is None


def test_salary_floor_from_search_section():
    """Floor is also read from config.search.salary_min when prefilter.salary_min absent."""
    listing = make_listing(salary_max=50_000)
    config = {
        "search": {"salary_min": 80_000},
        "prefilter": {},
    }
    result = prefilter(listing, config)
    assert result is not None
    assert "salary" in result


# ---------------------------------------------------------------------------
# Contract time tests
# ---------------------------------------------------------------------------

def test_contract_time_match_passes():
    """Listing passes when contract_time matches the required value."""
    listing = make_listing(contract_time="full_time")
    config = make_config(require_contract_time="full_time")
    assert prefilter(listing, config) is None


def test_contract_time_mismatch_rejected():
    """Listing is rejected when contract_time doesn't match required value."""
    listing = make_listing(contract_time="part_time")
    config = make_config(require_contract_time="full_time")
    result = prefilter(listing, config)
    assert result is not None
    assert "contract_time" in result


def test_contract_time_case_insensitive():
    """contract_time comparison is case-insensitive."""
    listing = make_listing(contract_time="Full_Time")
    config = make_config(require_contract_time="full_time")
    assert prefilter(listing, config) is None


def test_contract_time_null_config_skips_check():
    """When require_contract_time is absent from config, any contract_time passes."""
    listing = make_listing(contract_time="part_time")
    config = make_config()  # no require_contract_time key
    assert prefilter(listing, config) is None


def test_contract_time_empty_passes_when_filter_set():
    """Listing with empty contract_time passes even when require_contract_time is set.

    Empty means the field is unknown (many job sources don't populate it); it
    should never be rejected as a mismatch.
    """
    listing = make_listing(contract_time="")
    config = make_config(require_contract_time="full_time")
    assert prefilter(listing, config) is None


# ---------------------------------------------------------------------------
# Contract type tests
# ---------------------------------------------------------------------------

def test_contract_type_match_passes():
    """Listing passes when contract_type matches the required value."""
    listing = make_listing(contract_type="permanent")
    config = make_config(require_contract_type="permanent")
    assert prefilter(listing, config) is None


def test_contract_type_mismatch_rejected():
    """Listing is rejected when contract_type doesn't match required value."""
    listing = make_listing(contract_type="contract")
    config = make_config(require_contract_type="permanent")
    result = prefilter(listing, config)
    assert result is not None
    assert "contract_type" in result


def test_contract_type_case_insensitive():
    """contract_type comparison is case-insensitive."""
    listing = make_listing(contract_type="Permanent")
    config = make_config(require_contract_type="permanent")
    assert prefilter(listing, config) is None


def test_contract_type_null_config_skips_check():
    """When require_contract_type is absent from config, any contract_type passes."""
    listing = make_listing(contract_type="contract")
    config = make_config()  # no require_contract_type key
    assert prefilter(listing, config) is None


def test_contract_type_empty_passes_when_filter_set():
    """Listing with empty contract_type passes even when require_contract_type is set.

    Empty means the field is unknown (many job sources don't populate it); it
    should never be rejected as a mismatch.
    """
    listing = make_listing(contract_type="")
    config = make_config(require_contract_type="permanent")
    assert prefilter(listing, config) is None


# ---------------------------------------------------------------------------
# All filters disabled
# ---------------------------------------------------------------------------

def test_no_prefilter_section_passes():
    """Listing passes when config has no prefilter section at all."""
    listing = make_listing(title="Anything", salary_max=10_000, contract_time="contract")
    config = make_config(include_prefilter=False)
    assert prefilter(listing, config) is None


def test_empty_prefilter_section_passes():
    """Listing passes when prefilter section exists but has no active checks."""
    listing = make_listing(title="Anything", salary_max=10_000)
    config = {"prefilter": {}}
    assert prefilter(listing, config) is None
