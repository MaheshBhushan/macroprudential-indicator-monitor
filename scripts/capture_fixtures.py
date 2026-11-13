"""Capture test fixtures from the live APIs and derive a second, edited vintage.

tests/fixtures/vintage1/  responses from BIS, ECB, Eurostat (truncated by startPeriod, line
                          endings normalised to LF, otherwise verbatim).
tests/fixtures/vintage2/  vintage1 with documented, deterministic edits that simulate what a
                          later publication can look like: a revision, a new quarter, a
                          withdrawn period, a value turning missing and a status change.

Run: uv run python scripts/capture_fixtures.py            (capture + derive)
     uv run python scripts/capture_fixtures.py --derive-only   (rebuild vintage2 only)
Re-running vintage1 against live sources will pick up whatever the sources publish that day,
so the tests' expected values must be updated if you re-capture.
"""

from __future__ import annotations

import csv
import io
import shutil
import urllib.request
from pathlib import Path

from mim.catalog import default_catalog
from mim.sources import source_url

ROOT = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
V1, V2 = ROOT / "vintage1", ROOT / "vintage2"

FIXTURE_SERIES = {
    "BIS.WS_CREDIT_GAP.Q.US.P.A.C": "2019",
    "BIS.WS_CREDIT_GAP.Q.GB.P.A.C": "2019",
    "BIS.WS_TC.Q.US.P.A.M.770.A": "2019",
    "BIS.WS_SPP.Q.US.R.628": "2019",
    "BIS.WS_SPP.Q.US.N.628": "2019",
    "BIS.WS_DSR.Q.US.P": "2019",
    "BIS.WS_DSR.Q.US.H": "2019",
    "ESTAT.gov_10q_ggdebt.Q.GD.S13.PC_GDP.DE": "2019-Q1",
    "ECB.BSI.M.U2.Y.U.A20.A.I.U2.2250.Z01.A": "2023-01",
}


def capture() -> None:
    V1.mkdir(parents=True, exist_ok=True)
    by_id = {s.series_id: s for s in default_catalog()}
    for sid, start in FIXTURE_SERIES.items():
        url = f"{source_url(by_id[sid])}&startPeriod={start}"
        req = urllib.request.Request(url, headers={"User-Agent": "mim-fixtures"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8-sig").replace("\r\n", "\n")
        (V1 / f"{sid}.csv").write_text(body, encoding="utf-8", newline="")
        print(f"{sid}: {body.count(chr(10)) - 1} rows")


def _load(path: Path) -> tuple[list[str], list[dict]]:
    reader = csv.DictReader(io.StringIO(path.read_text(encoding="utf-8")))
    return list(reader.fieldnames or []), list(reader)


def _save(path: Path, fields: list[str], rows: list[dict]) -> None:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    path.write_text(buf.getvalue(), encoding="utf-8", newline="")


def derive_vintage2() -> None:
    if V2.exists():
        shutil.rmtree(V2)
    shutil.copytree(V1, V2)

    # 1. US credit gap: last quarter revised by +0.5 pp and one new quarter appended.
    p = V2 / "BIS.WS_CREDIT_GAP.Q.US.P.A.C.csv"
    fields, rows = _load(p)
    last = rows[-1]
    last["OBS_VALUE"] = f"{float(last['OBS_VALUE']) + 0.5:.4f}"
    year, q = int(last["TIME_PERIOD"][:4]), int(last["TIME_PERIOD"][-1])
    year, q = (year + 1, 1) if q == 4 else (year, q + 1)
    new = dict(last, TIME_PERIOD=f"{year}-Q{q}", OBS_VALUE=f"{float(last['OBS_VALUE']) + 0.1:.4f}")
    rows.append(new)
    _save(p, fields, rows)

    # 2. GB credit gap: 2021-Q2 no longer published (withdrawal, creates a gap).
    p = V2 / "BIS.WS_CREDIT_GAP.Q.GB.P.A.C.csv"
    fields, rows = _load(p)
    _save(p, fields, [r for r in rows if r["TIME_PERIOD"] != "2021-Q2"])

    # 3. US real property prices: 2022-Q4 value becomes missing (status M).
    p = V2 / "BIS.WS_SPP.Q.US.R.628.csv"
    fields, rows = _load(p)
    for r in rows:
        if r["TIME_PERIOD"] == "2022-Q4":
            r["OBS_VALUE"], r["OBS_STATUS"] = "", "M"
    _save(p, fields, rows)

    # 4. German government debt: provisional flag removed for 2024-Q1 (status-only revision).
    p = V2 / "ESTAT.gov_10q_ggdebt.Q.GD.S13.PC_GDP.DE.csv"
    fields, rows = _load(p)
    for r in rows:
        if r["TIME_PERIOD"] == "2024-Q1":
            r["OBS_FLAG"] = ""
    _save(p, fields, rows)


if __name__ == "__main__":
    import sys

    if "--derive-only" not in sys.argv:
        capture()
    derive_vintage2()
