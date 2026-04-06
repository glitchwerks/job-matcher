"""
tests/test_geo_filter.py — Unit tests for the geospatial filter in ingest.py.

Tests cover:
- Listing within radius passes
- Listing outside radius is discarded
- Remote / worldwide listing always passes regardless of radius
- Listing with ungeocoded location respects fallback setting (pass / discard)
- Filter is skipped entirely when location_center is absent
- Filter is skipped when location_radius_km is absent
- Center not geocodable → filter skipped (fail-open)
- geo_filter() module-level helper API
- GeoFilter class with PostgreSQL geocache

All tests use the module-level geo_filter() helper (no network calls).
GeoFilter integration tests use the PostgreSQL geocache (requires DATABASE_URL).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db
from ingest import geo_filter, GeoFilter, _is_remote_location


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

# Miami, FL  (approx lat/lon used as the filter center in most tests)
_MIAMI = (25.7617, -80.1918)

# Fort Lauderdale, FL — ~45 km north of Miami (within 80 km radius)
_FORT_LAUDERDALE = (26.1224, -80.1373)

# Orlando, FL — ~380 km from Miami (outside 80 km radius)
_ORLANDO = (28.5383, -81.3792)

# A profile dict with geospatial filter enabled (nested schema).
_BASE_PROFILE = {
    "location": {
        "center": "Miami, FL",
        "radius_km": 80,
        "geocode_fallback": "pass",
    },
}


def _listing(location: str = "Fort Lauderdale, FL") -> dict:
    return {"title": "Software Engineer", "location": location}


def _geocache(
    *,
    center: tuple | None = _MIAMI,
    listing_loc: str = "Fort Lauderdale, FL",
    listing_coords: tuple | None = _FORT_LAUDERDALE,
) -> dict:
    """Build a geocache dict suitable for the geo_filter() helper."""
    cache = {}
    if center is not None:
        cache["Miami, FL"] = center
    if listing_coords is not None:
        cache[listing_loc] = listing_coords
    return cache


# ---------------------------------------------------------------------------
# _is_remote_location helper
# ---------------------------------------------------------------------------

def test_is_remote_location_detects_remote():
    assert _is_remote_location("Remote") is True


def test_is_remote_location_detects_worldwide():
    assert _is_remote_location("Worldwide") is True


def test_is_remote_location_case_insensitive():
    assert _is_remote_location("REMOTE - US only") is True
    assert _is_remote_location("worldwide / remote") is True


def test_is_remote_location_normal_city_false():
    assert _is_remote_location("Miami, FL") is False


def test_is_remote_location_empty_string_false():
    assert _is_remote_location("") is False


# ---------------------------------------------------------------------------
# geo_filter() — filter disabled
# ---------------------------------------------------------------------------

def test_geo_filter_disabled_when_no_center():
    """Filter is skipped entirely when location.center is absent."""
    profile = {"location": {"radius_km": 80}}
    listing = _listing("Orlando, FL")
    cache = {"Miami, FL": _MIAMI, "Orlando, FL": _ORLANDO}
    assert geo_filter(listing, profile, cache) is None


def test_geo_filter_disabled_when_no_radius():
    """Filter is skipped entirely when location.radius_km is absent."""
    profile = {"location": {"center": "Miami, FL"}}
    listing = _listing("Orlando, FL")
    cache = {"Miami, FL": _MIAMI, "Orlando, FL": _ORLANDO}
    assert geo_filter(listing, profile, cache) is None


def test_geo_filter_disabled_when_both_absent():
    """Filter is skipped when neither field is present."""
    listing = _listing("Orlando, FL")
    assert geo_filter(listing, {}, {}) is None


# ---------------------------------------------------------------------------
# geo_filter() — remote / worldwide listings
# ---------------------------------------------------------------------------

def test_geo_filter_remote_listing_passes():
    """Listing with 'Remote' location always passes even when outside radius."""
    profile = _BASE_PROFILE
    listing = _listing("Remote")
    cache = _geocache()  # no entry for "Remote" — doesn't matter
    assert geo_filter(listing, profile, cache) is None


def test_geo_filter_worldwide_listing_passes():
    """Listing with 'Worldwide' location always passes."""
    listing = _listing("Worldwide")
    cache = _geocache()
    assert geo_filter(listing, _BASE_PROFILE, cache) is None


def test_geo_filter_remote_substring_passes():
    """'Remote (US)' passes because it contains 'remote'."""
    listing = _listing("Remote (US)")
    cache = _geocache()
    assert geo_filter(listing, _BASE_PROFILE, cache) is None


def test_geo_filter_empty_location_passes():
    """An empty location string is treated the same as remote — passes."""
    listing = _listing("")
    cache = _geocache()
    assert geo_filter(listing, _BASE_PROFILE, cache) is None


# ---------------------------------------------------------------------------
# geo_filter() — within radius
# ---------------------------------------------------------------------------

def test_geo_filter_within_radius_passes():
    """Fort Lauderdale (~45 km from Miami) passes a 80 km radius."""
    listing = _listing("Fort Lauderdale, FL")
    cache = _geocache()
    assert geo_filter(listing, _BASE_PROFILE, cache) is None


def test_geo_filter_exact_center_passes():
    """A listing at the exact center coordinates always passes."""
    listing = _listing("Miami, FL")
    cache = {
        "Miami, FL": _MIAMI,
    }
    assert geo_filter(listing, _BASE_PROFILE, cache) is None


# ---------------------------------------------------------------------------
# geo_filter() — outside radius
# ---------------------------------------------------------------------------

def test_geo_filter_outside_radius_discarded():
    """Orlando (~380 km from Miami) is rejected by an 80 km radius."""
    listing = _listing("Orlando, FL")
    cache = {
        "Miami, FL": _MIAMI,
        "Orlando, FL": _ORLANDO,
    }
    result = geo_filter(listing, _BASE_PROFILE, cache)
    assert result is not None
    assert "geo_filter" in result
    assert "Orlando" in result


def test_geo_filter_rejection_message_contains_distance():
    """Rejection message includes a km distance figure."""
    listing = _listing("Orlando, FL")
    cache = {"Miami, FL": _MIAMI, "Orlando, FL": _ORLANDO}
    result = geo_filter(listing, _BASE_PROFILE, cache)
    assert result is not None
    assert "km" in result


def test_geo_filter_rejection_message_contains_radius():
    """Rejection message includes the configured radius."""
    listing = _listing("Orlando, FL")
    cache = {"Miami, FL": _MIAMI, "Orlando, FL": _ORLANDO}
    result = geo_filter(listing, _BASE_PROFILE, cache)
    assert result is not None
    assert "80" in result


# ---------------------------------------------------------------------------
# geo_filter() — ungeocoded location + fallback
# ---------------------------------------------------------------------------

def test_geo_filter_ungeocoded_fallback_pass():
    """Ungeocoded location passes when geocode_fallback='pass'."""
    profile = {"location": {**_BASE_PROFILE["location"], "geocode_fallback": "pass"}}
    listing = _listing("Unknown Small Town, XY")
    cache = {"Miami, FL": _MIAMI}  # no entry for the listing location
    assert geo_filter(listing, profile, cache) is None


def test_geo_filter_ungeocoded_fallback_discard():
    """Ungeocoded location is rejected when geocode_fallback='discard'."""
    profile = {"location": {**_BASE_PROFILE["location"], "geocode_fallback": "discard"}}
    listing = _listing("Unknown Small Town, XY")
    cache = {"Miami, FL": _MIAMI}
    result = geo_filter(listing, profile, cache)
    assert result is not None
    assert "geo_filter" in result
    assert "could not be geocoded" in result


def test_geo_filter_ungeocoded_fallback_defaults_to_pass():
    """When location.geocode_fallback is absent, ungeocoded locations pass."""
    profile = {
        "location": {
            "center": "Miami, FL",
            "radius_km": 80,
            # no geocode_fallback key
        }
    }
    listing = _listing("Unknown Small Town, XY")
    cache = {"Miami, FL": _MIAMI}
    assert geo_filter(listing, profile, cache) is None


def test_geo_filter_center_not_in_geocache_skips_filter():
    """When the center itself is absent from the geocache, the filter is skipped.

    This prevents silently discarding every listing when the center
    location string cannot be geocoded.
    """
    profile = _BASE_PROFILE
    listing = _listing("Orlando, FL")
    cache = {"Orlando, FL": _ORLANDO}  # center missing
    assert geo_filter(listing, profile, cache) is None


# ---------------------------------------------------------------------------
# GeoFilter class — integration tests using PostgreSQL geocache
# ---------------------------------------------------------------------------

def _prepopulate_geocache(entries: dict[str, tuple]) -> None:
    """Insert geocache entries into the PostgreSQL geocache for test setup."""
    with db.get_connection() as conn:
        for loc, (lat, lon) in entries.items():
            db.geocache_put(conn, loc, lat, lon)


def _cleanup_geocache(*location_texts: str) -> None:
    """Remove geocache entries by location_text after a test."""
    with db.get_connection() as conn:
        for loc in location_texts:
            conn.execute(
                "DELETE FROM location_geocache WHERE location_text = %s", (loc,)
            )


class TestGeoFilterClass:
    """Integration tests for GeoFilter using the PostgreSQL geocache.

    Each test cleans up its geocache entries in teardown_method.
    """

    def teardown_method(self):
        _cleanup_geocache(
            "Miami, FL", "Fort Lauderdale, FL", "Orlando, FL",
            "Nonexistent Place, ZZ",
        )

    def test_inactive_when_no_center(self):
        gf = GeoFilter(profile={"location": {"radius_km": 80}})
        assert gf.is_active is False

    def test_inactive_when_no_radius(self):
        gf = GeoFilter(profile={"location": {"center": "Miami, FL"}})
        assert gf.is_active is False

    def test_check_returns_none_when_inactive(self):
        gf = GeoFilter(profile={})
        listing = _listing("Orlando, FL")
        assert gf.check(listing) is None

    def test_remote_listing_passes_when_active(self):
        _prepopulate_geocache({"Miami, FL": _MIAMI})
        gf = GeoFilter(profile=_BASE_PROFILE)
        assert gf.check(_listing("Remote")) is None

    def test_within_radius_from_db_cache(self):
        """Listing within radius passes when coords come from the DB geocache."""
        _prepopulate_geocache({
            "Miami, FL": _MIAMI,
            "Fort Lauderdale, FL": _FORT_LAUDERDALE,
        })
        gf = GeoFilter(profile=_BASE_PROFILE)
        assert gf.check(_listing("Fort Lauderdale, FL")) is None

    def test_outside_radius_from_db_cache(self):
        """Listing outside radius is rejected when coords come from the DB geocache."""
        _prepopulate_geocache({
            "Miami, FL": _MIAMI,
            "Orlando, FL": _ORLANDO,
        })
        gf = GeoFilter(profile=_BASE_PROFILE)
        result = gf.check(_listing("Orlando, FL"))
        assert result is not None
        assert "geo_filter" in result

    def test_geocache_hit_counter(self):
        """DB cache hits are counted in gf.hits."""
        _prepopulate_geocache({
            "Miami, FL": _MIAMI,
            "Fort Lauderdale, FL": _FORT_LAUDERDALE,
        })
        gf = GeoFilter(profile=_BASE_PROFILE)
        # The center "Miami, FL" was resolved during __init__ via DB cache.
        # Now check a listing whose location is also in the DB cache.
        gf.check(_listing("Fort Lauderdale, FL"))
        assert gf.hits >= 1

    def test_ungeocoded_fallback_discard_via_class(self):
        """GeoFilter.check() respects geocode_fallback=discard for unresolvable locations."""
        _prepopulate_geocache({"Miami, FL": _MIAMI})
        profile = {"location": {**_BASE_PROFILE["location"], "geocode_fallback": "discard"}}
        gf = GeoFilter(profile=profile)
        result = gf.check(_listing("Nonexistent Place, ZZ"))
        assert result is not None
        assert "could not be geocoded" in result

    def test_ungeocoded_fallback_pass_via_class(self):
        """GeoFilter.check() lets unresolvable locations through when geocode_fallback=pass."""
        _prepopulate_geocache({"Miami, FL": _MIAMI})
        profile = {"location": {**_BASE_PROFILE["location"], "geocode_fallback": "pass"}}
        gf = GeoFilter(profile=profile)
        assert gf.check(_listing("Nonexistent Place, ZZ")) is None

    def test_geo_discarded_counter_increments(self):
        """geo_discarded counter increments when a listing is rejected by radius."""
        _prepopulate_geocache({
            "Miami, FL": _MIAMI,
            "Orlando, FL": _ORLANDO,
        })
        gf = GeoFilter(profile=_BASE_PROFILE)
        gf.check(_listing("Orlando, FL"))
        assert gf.geo_discarded == 1

    def test_in_memory_cache_prevents_repeated_db_reads(self):
        """Second check for same location uses in-memory cache, not DB."""
        _prepopulate_geocache({
            "Miami, FL": _MIAMI,
            "Fort Lauderdale, FL": _FORT_LAUDERDALE,
        })
        gf = GeoFilter(profile=_BASE_PROFILE)

        # First check — reads from DB (1 hit for Fort Lauderdale).
        gf.check(_listing("Fort Lauderdale, FL"))
        hits_after_first = gf.hits

        # Second check — reads from in-memory cache, no new DB hit.
        gf.check(_listing("Fort Lauderdale, FL"))
        assert gf.hits == hits_after_first  # no additional DB reads


# ---------------------------------------------------------------------------
# _generate_location_notes helper
# ---------------------------------------------------------------------------

from ingest import _generate_location_notes  # noqa: E402


def test_generate_location_notes_exact_format():
    result = _generate_location_notes("Miami, FL", 80)
    assert result == "Within 80 km of Miami, FL"


def test_generate_location_notes_with_center_and_radius():
    """Auto-generated notes include both the radius and center."""
    notes = _generate_location_notes("Miami, FL", 80)
    assert notes is not None
    assert "80" in notes
    assert "Miami, FL" in notes


def test_generate_location_notes_center_only():
    """When radius is absent, notes include the center without a distance."""
    notes = _generate_location_notes("Miami, FL", None)
    assert notes is not None
    assert "Miami, FL" in notes


def test_generate_location_notes_neither_set():
    """Returns None when neither center nor radius is given."""
    assert _generate_location_notes(None, None) is None


def test_generate_location_notes_radius_without_center():
    """Returns None when only radius is set — no center to reference."""
    assert _generate_location_notes(None, 80) is None


# ---------------------------------------------------------------------------
# Nested schema — new field reads
# ---------------------------------------------------------------------------

def test_geo_filter_nested_schema_passes_within_radius():
    """geo_filter reads center/radius_km from nested location block."""
    profile = {"location": {"center": "Miami, FL", "radius_km": 80}}
    listing = _listing("Fort Lauderdale, FL")
    cache = _geocache()
    assert geo_filter(listing, profile, cache) is None


def test_geo_filter_nested_schema_activates_filter():
    """geo_filter is active (reads center + radius_km) from the nested block.

    Rather than testing the radius rejection (which depends on geopy and is
    already covered by pre-existing tests), this verifies that the nested
    schema activates the filter — demonstrated by the fallback=discard path
    which does not require geopy.
    """
    profile = {"location": {"center": "Miami, FL", "radius_km": 80, "geocode_fallback": "discard"}}
    # Listing location present in profile but NOT in geocache → triggers fallback
    listing = _listing("Unknown Place, ZZ")
    cache = {"Miami, FL": _MIAMI}  # no entry for "Unknown Place, ZZ"
    result = geo_filter(listing, profile, cache)
    assert result is not None
    assert "could not be geocoded" in result


# ---------------------------------------------------------------------------
# Old flat fields are no longer read
# ---------------------------------------------------------------------------

def test_flat_location_fields_ignored_by_geo_filter():
    """A profile with only the old flat location fields disables the filter.

    This verifies that the migration broke the old schema — callers must
    update to the nested ``location`` block.
    """
    old_style_profile = {
        "location_center": "Miami, FL",
        "location_radius_km": 80,
        "location_geocode_fallback": "discard",
    }
    listing = _listing("Orlando, FL")
    cache = {"Miami, FL": _MIAMI, "Orlando, FL": _ORLANDO}
    # With flat fields the filter is not active, so Orlando passes.
    assert geo_filter(listing, old_style_profile, cache) is None


def test_flat_fields_do_not_activate_geo_filter_class():
    """GeoFilter.is_active is False when only old flat fields are present."""
    old_style_profile = {
        "location_center": "Miami, FL",
        "location_radius_km": 80,
    }
    gf = GeoFilter(profile=old_style_profile)
    assert gf.is_active is False
