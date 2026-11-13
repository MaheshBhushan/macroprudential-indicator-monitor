"""Per-pull data quality checks: frequency, gaps, missing values, duplicates and staleness."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta

from .catalog import Series
from .periods import format_period, ordinal, period_end
from .sources import FetchResult, ParsedSeries

# Days after the end of the latest period before a series counts as stale. BIS credit and DSR
# series are published with a lag of roughly two quarters, hence the generous quarterly allowance.
STALENESS_DAYS = {"M": 120, "Q": 300, "A": 550}


@dataclass(frozen=True)
class Flag:
    series_id: str
    period: str | None
    flag: str
    severity: str  # info | warning | error
    detail: str


def check_fetch(series: Series, result: FetchResult) -> list[Flag]:
    if result.ok:
        return []
    kind = "no_data" if result.http_status == 404 else "fetch_failed"
    sev = "warning" if kind == "no_data" else "error"
    return [Flag(series.series_id, None, kind, sev, result.error or "no body")]


def check_series(series: Series, parsed: ParsedSeries, as_of: date) -> list[Flag]:
    sid = series.series_id
    flags: list[Flag] = []
    obs = parsed.observations

    for p in parsed.unparseable_periods:
        flags.append(Flag(sid, None, "unparseable_period", "error", f"TIME_PERIOD={p!r}"))
    if not obs:
        flags.append(Flag(sid, None, "no_data", "warning", "response contained no observations"))
        return flags

    freqs = Counter(o.freq for o in obs)
    observed = freqs.most_common(1)[0][0]
    if len(freqs) > 1:
        flags.append(
            Flag(sid, None, "mixed_frequency", "error", f"frequencies seen: {dict(freqs)}")
        )
    if observed != series.expected_freq:
        flags.append(
            Flag(
                sid,
                None,
                "frequency_mismatch",
                "error",
                f"expected {series.expected_freq}, observed {observed}",
            )
        )

    same_freq = [o for o in obs if o.freq == observed]
    counts = Counter(o.period for o in same_freq)
    for period, n in sorted(counts.items()):
        if n > 1:
            flags.append(Flag(sid, period, "duplicate_period", "error", f"{n} rows"))

    present = {ordinal(observed, o.period_start) for o in same_freq}
    lo, hi = min(present), max(present)
    for idx in range(lo, hi + 1):
        if idx not in present:
            flags.append(
                Flag(sid, format_period(observed, idx), "gap_in_series", "warning",
                     "period absent between first and last observation")
            )

    for o in obs:
        if o.value is None:
            flags.append(
                Flag(sid, o.period, "missing_value", "warning", f"obs_status={o.obs_status}")
            )

    latest = max(same_freq, key=lambda o: o.period_start)
    allowance = STALENESS_DAYS[observed]
    end = period_end(observed, latest.period_start)
    if as_of - end > timedelta(days=allowance):
        flags.append(
            Flag(sid, latest.period, "stale_series", "warning",
                 f"latest period ended {end.isoformat()}, {(as_of - end).days} days before "
                 f"{as_of.isoformat()} (allowance {allowance})")
        )
    return flags
