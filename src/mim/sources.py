"""Fetch SDMX CSV from BIS, ECB and Eurostat (live or from fixture files) and parse it."""

from __future__ import annotations

import csv
import hashlib
import io
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

from .catalog import Series
from .periods import parse_period

BASE_URLS = {
    "BIS": "https://stats.bis.org/api/v1/data/{flow}/{key}?format=csv",
    "ECB": "https://data-api.ecb.europa.eu/service/data/{flow}/{key}?format=csvdata",
    "ESTAT": (
        "https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/{flow}/{key}"
        "?format=SDMX-CSV"
    ),
}

_MISSING_TOKENS = {"", "NaN", "nan", "NA", ":"}


def source_url(series: Series) -> str:
    return BASE_URLS[series.source].format(flow=series.dataflow, key=series.key)


@dataclass(frozen=True)
class FetchResult:
    url: str
    http_status: int | None
    body: str | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.http_status == 200 and self.body is not None

    @property
    def sha256(self) -> str | None:
        return hashlib.sha256(self.body.encode()).hexdigest() if self.body is not None else None


class Fetcher(Protocol):
    def fetch(self, series: Series) -> FetchResult: ...


class HttpFetcher:
    """Live fetcher. 404 means the source has no data for the key; 429/5xx are retried."""

    def __init__(self, timeout: float = 60.0, retries: int = 3, backoff: float = 2.0):
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff

    def fetch(self, series: Series) -> FetchResult:
        url = source_url(series)
        req = urllib.request.Request(
            url, headers={"User-Agent": "macroprudential-indicator-monitor/0.1"}
        )
        last_error = None
        status = None
        for attempt in range(self.retries):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return FetchResult(url, resp.status, resp.read().decode("utf-8-sig"))
            except urllib.error.HTTPError as exc:
                status, last_error = exc.code, f"HTTP {exc.code}"
                if exc.code not in (429, 500, 502, 503, 504):
                    break
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = str(exc)
            time.sleep(self.backoff * (attempt + 1))
        return FetchResult(url, status, None, last_error)


class FixtureFetcher:
    """Reads `<series_id>.csv` from a directory; a missing file behaves like an HTTP 404."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def path_for(self, series: Series) -> Path:
        return self.directory / f"{series.series_id}.csv"

    def fetch(self, series: Series) -> FetchResult:
        path = self.path_for(series)
        url = f"fixture://{path.name}"
        if not path.exists():
            return FetchResult(url, 404, None, "HTTP 404")
        return FetchResult(url, 200, path.read_text(encoding="utf-8-sig"))


@dataclass(frozen=True)
class Observation:
    period: str
    freq: str
    period_start: date
    value: float | None
    obs_status: str | None


@dataclass(frozen=True)
class ParsedSeries:
    observations: list[Observation]
    source_last_update: str | None
    unparseable_periods: list[str]


def parse_sdmx_csv(body: str) -> ParsedSeries:
    """Parse BIS `csv`, ECB `csvdata` and Eurostat `SDMX-CSV` responses (single series)."""
    reader = csv.DictReader(io.StringIO(body))
    fields = reader.fieldnames or []
    if "TIME_PERIOD" not in fields or "OBS_VALUE" not in fields:
        raise ValueError(f"not an SDMX CSV payload; columns={fields[:8]}")
    # BIS and ECB call the observation status OBS_STATUS; Eurostat calls it OBS_FLAG.
    status_col = next((c for c in ("OBS_STATUS", "OBS_FLAG") if c in fields), None)
    obs: list[Observation] = []
    bad: list[str] = []
    last_update = None
    for row in reader:
        period = (row["TIME_PERIOD"] or "").strip()
        try:
            freq, start = parse_period(period)
        except ValueError:
            bad.append(period)
            continue
        raw = (row["OBS_VALUE"] or "").strip()
        value = None if raw in _MISSING_TOKENS else float(raw)
        status = ((row.get(status_col) or "").strip() or None) if status_col else None
        obs.append(Observation(period, freq, start, value, status))
        if row.get("LAST UPDATE"):
            last_update = row["LAST UPDATE"].strip()
    obs.sort(key=lambda o: (o.period_start, o.period))
    return ParsedSeries(obs, last_update, bad)
