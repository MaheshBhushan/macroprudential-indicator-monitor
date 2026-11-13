"""SDMX period strings: parsing, frequency inference and ordinal arithmetic."""

from __future__ import annotations

import re
from datetime import date

_PATTERNS = (
    ("Q", re.compile(r"^(\d{4})-Q([1-4])$")),
    ("M", re.compile(r"^(\d{4})-(\d{2})$")),
    ("A", re.compile(r"^(\d{4})$")),
)

MONTHS_PER_PERIOD = {"A": 12, "Q": 3, "M": 1}


def parse_period(period: str) -> tuple[str, date]:
    """Return (frequency, first day of period) for an SDMX TIME_PERIOD string."""
    for freq, pattern in _PATTERNS:
        m = pattern.match(period)
        if not m:
            continue
        year = int(m.group(1))
        if freq == "Q":
            return freq, date(year, 3 * (int(m.group(2)) - 1) + 1, 1)
        if freq == "M":
            month = int(m.group(2))
            if not 1 <= month <= 12:
                break
            return freq, date(year, month, 1)
        return freq, date(year, 1, 1)
    raise ValueError(f"unrecognised period {period!r}")


def ordinal(freq: str, start: date) -> int:
    """Monotone integer index so consecutive periods differ by exactly 1."""
    return (start.year * 12 + start.month - 1) // MONTHS_PER_PERIOD[freq]


def format_period(freq: str, idx: int) -> str:
    months = idx * MONTHS_PER_PERIOD[freq]
    year, month0 = divmod(months, 12)
    if freq == "A":
        return f"{year}"
    if freq == "Q":
        return f"{year}-Q{month0 // 3 + 1}"
    return f"{year}-{month0 + 1:02d}"


def period_end(freq: str, start: date) -> date:
    months = start.year * 12 + start.month - 1 + MONTHS_PER_PERIOD[freq]
    y, m = divmod(months, 12)
    return date.fromordinal(date(y, m + 1, 1).toordinal() - 1)
