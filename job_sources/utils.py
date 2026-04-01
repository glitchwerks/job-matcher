"""
job_sources/utils.py — Shared utility functions for job source implementations.

These helpers are used by multiple source modules and are extracted here to
avoid code duplication across the package.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

# Regex to find numbers with optional k-suffix, e.g. "80,000", "120k", "50K"
_SALARY_NUMBER_RE = re.compile(r"[\d,]+(?:\.\d+)?[kK]?")


def parse_salary(raw: str) -> tuple[float | None, float | None]:
    """Parse a free-text salary string into (salary_min, salary_max).

    Handles patterns like:
      - "$80,000 - $120,000"
      - "€50k"
      - "100K-150K"
      - "" (empty → both None)

    Args:
        raw: Free-text salary string.

    Returns:
        A (salary_min, salary_max) tuple of floats, or (None, None) if the
        string is empty or no numeric values can be extracted.
    """
    if not raw or not raw.strip():
        return None, None

    matches = _SALARY_NUMBER_RE.findall(raw)
    if not matches:
        return None, None

    values: list[float] = []
    for m in matches:
        # Strip commas used as thousands separators.
        cleaned = m.replace(",", "")
        lower = cleaned.lower()
        if lower.endswith("k"):
            try:
                values.append(float(lower[:-1]) * 1000)
            except ValueError:
                continue
        else:
            try:
                values.append(float(cleaned))
            except ValueError:
                continue

    if not values:
        return None, None

    salary_min = values[0]
    salary_max = values[1] if len(values) >= 2 else salary_min
    return salary_min, salary_max


def strip_html(html: str) -> str:
    """Strip HTML tags from a string using BeautifulSoup.

    Args:
        html: HTML string to strip.

    Returns:
        Plain text with tags removed and whitespace normalised.
    """
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text(separator=" ", strip=True)
