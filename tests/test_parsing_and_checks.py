from __future__ import annotations

from datetime import date

import pytest
from conftest import AS_OF, FIXTURES

from mim.catalog import Series, default_catalog
from mim.checks import check_fetch, check_series
from mim.periods import format_period, ordinal, parse_period, period_end
from mim.sources import FetchResult, FixtureFetcher, parse_sdmx_csv, source_url

V1 = FIXTURES / "vintage1"


def _series(freq="Q"):
    return Series("BIS", "WS_TEST", "Q.XX", "XX", "credit_gap", "pp", freq, "test series")


def _csv(rows):
    return "TIME_PERIOD,OBS_VALUE,OBS_STATUS\n" + "".join(f"{p},{v},{s}\n" for p, v, s in rows)


def _flags(series, body, as_of=AS_OF):
    return [(f.flag, f.period) for f in check_series(series, parse_sdmx_csv(body), as_of)]


@pytest.mark.parametrize(
    "period,freq,start",
    [("2024-Q3", "Q", date(2024, 7, 1)), ("2024-11", "M", date(2024, 11, 1)),
     ("2024", "A", date(2024, 1, 1))],
)
def test_parse_period(period, freq, start):
    assert parse_period(period) == (freq, start)
    assert format_period(freq, ordinal(freq, start)) == period


@pytest.mark.parametrize("bad", ["2024-Q5", "2024-13", "24-Q1", "2024W01", ""])
def test_parse_period_rejects(bad):
    with pytest.raises(ValueError):
        parse_period(bad)


def test_ordinals_are_consecutive_across_year_end():
    assert ordinal("Q", date(2025, 1, 1)) - ordinal("Q", date(2024, 10, 1)) == 1
    assert ordinal("M", date(2025, 1, 1)) - ordinal("M", date(2024, 12, 1)) == 1
    assert period_end("Q", date(2024, 10, 1)) == date(2024, 12, 31)
    assert period_end("M", date(2024, 2, 1)) == date(2024, 2, 29)


def test_parse_bis_fixture():
    parsed = parse_sdmx_csv((V1 / "BIS.WS_CREDIT_GAP.Q.US.P.A.C.csv").read_text())
    assert len(parsed.observations) == 29
    first, last = parsed.observations[0], parsed.observations[-1]
    assert (first.period, first.value, first.obs_status) == ("2019-Q1", -6.34, "A")
    assert (last.period, last.value) == ("2026-Q1", -11.3204)


def test_parse_ecb_fixture_monthly_with_provisional_status():
    parsed = parse_sdmx_csv((V1 / "ECB.BSI.M.U2.Y.U.A20.A.I.U2.2250.Z01.A.csv").read_text())
    assert {o.freq for o in parsed.observations} == {"M"}
    assert parsed.observations[-1].period == "2026-09"
    assert parsed.observations[-1].obs_status == "P"


def test_parse_eurostat_fixture_reads_flag_and_last_update():
    parsed = parse_sdmx_csv((V1 / "ESTAT.gov_10q_ggdebt.Q.GD.S13.PC_GDP.DE.csv").read_text())
    assert parsed.source_last_update == "21/10/26 11:00:00"
    by_period = {o.period: o for o in parsed.observations}
    assert by_period["2024-Q1"].value == 61.5
    assert by_period["2024-Q1"].obs_status == "p"


def test_parse_missing_tokens_and_bad_periods():
    parsed = parse_sdmx_csv(_csv([("2024-Q1", "", "M"), ("2024-Q2", "NaN", "M"),
                                  ("2024-Q3", "1.5", "A"), ("oops", "2", "A")]))
    assert [o.value for o in parsed.observations] == [None, None, 1.5]
    assert parsed.unparseable_periods == ["oops"]


def test_parse_rejects_non_sdmx_payload():
    with pytest.raises(ValueError):
        parse_sdmx_csv("<html>maintenance</html>")


def test_fixture_fetcher_missing_file_is_404():
    s = _series()
    result = FixtureFetcher(V1).fetch(s)
    assert (result.ok, result.http_status) == (False, 404)
    assert [f.flag for f in check_fetch(s, result)] == ["no_data"]
    assert [f.flag for f in check_fetch(s, FetchResult("u", 503, None, "HTTP 503"))] == [
        "fetch_failed"
    ]


def test_catalog_ids_unique_and_urls_well_formed():
    catalog = default_catalog()
    assert len({s.series_id for s in catalog}) == len(catalog)
    urls = {s.source: source_url(s) for s in catalog}
    assert urls["BIS"].startswith("https://stats.bis.org/api/v1/data/")
    assert urls["ECB"].startswith("https://data-api.ecb.europa.eu/service/data/")
    assert urls["ESTAT"].startswith("https://ec.europa.eu/eurostat/api/")


def test_clean_series_has_no_flags():
    body = _csv([("2026-Q1", "1", "A"), ("2026-Q2", "2", "A")])
    assert _flags(_series(), body) == []


def test_gap_missing_value_and_duplicate_flags():
    body = _csv([("2025-Q3", "1", "A"), ("2026-Q1", "", "M"), ("2026-Q2", "2", "A"),
                 ("2026-Q2", "3", "A")])
    flags = _flags(_series(), body)
    assert ("gap_in_series", "2025-Q4") in flags
    assert ("missing_value", "2026-Q1") in flags
    assert ("duplicate_period", "2026-Q2") in flags


def test_frequency_checks():
    monthly = _csv([("2026-08", "1", "A"), ("2026-09", "2", "A")])
    assert ("frequency_mismatch", None) in _flags(_series("Q"), monthly)
    mixed = _csv([("2026-Q1", "1", "A"), ("2026-Q2", "2", "A"), ("2026-06", "3", "A")])
    assert ("mixed_frequency", None) in _flags(_series("Q"), mixed)


def test_staleness_threshold():
    body = _csv([("2025-Q4", "1", "A")])  # period ends 2025-12-31
    assert _flags(_series(), body, as_of=date(2026, 10, 27)) == []  # 300 days
    assert ("stale_series", "2025-Q4") in _flags(_series(), body, as_of=date(2026, 10, 28))


def test_empty_response_is_no_data():
    assert _flags(_series(), "TIME_PERIOD,OBS_VALUE\n") == [("no_data", None)]
