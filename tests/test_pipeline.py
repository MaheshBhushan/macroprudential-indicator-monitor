"""End-to-end tests against PostgreSQL using the captured fixtures (no network)."""

from __future__ import annotations

from decimal import Decimal

from conftest import FIXTURES, fixture_catalog, ingest_vintage

from mim.sources import FetchResult, FixtureFetcher, parse_sdmx_csv

US_GAP = "BIS.WS_CREDIT_GAP.Q.US.P.A.C"
GB_GAP = "BIS.WS_CREDIT_GAP.Q.GB.P.A.C"
US_RPP_REAL = "BIS.WS_SPP.Q.US.R.628"
DE_DEBT = "ESTAT.gov_10q_ggdebt.Q.GD.S13.PC_GDP.DE"


def _fixture_rows(vintage: str) -> int:
    return sum(
        len(parse_sdmx_csv((FIXTURES / vintage / f"{s.series_id}.csv").read_text()).observations)
        for s in fixture_catalog()
    )


def _one(conn, sql, *params):
    return conn.execute(sql, params).fetchone()


def test_first_vintage_loads_every_observation(conn):
    s = ingest_vintage(conn, "vintage1")
    assert (s.series_ok, s.series_failed) == (len(fixture_catalog()), 0)
    assert s.inserted == _fixture_rows("vintage1")
    assert (s.revised, s.withdrawn) == (0, 0)
    assert s.flags == {}


def test_credit_gap_view(conn):
    ingest_vintage(conn, "vintage1")
    row = _one(conn, "select credit_gap_pp, ccyb_band, indicative_ccyb_pct from mim.v_credit_gap"
                     " where country = 'US' and period = '2026-Q1'")
    assert row == (Decimal("-11.3204"), "below_2pp", Decimal("0.00"))


def test_property_price_growth_matches_fixture(conn):
    ingest_vintage(conn, "vintage1")
    obs = {o.period: o.value for o in parse_sdmx_csv(
        (FIXTURES / "vintage1" / f"{US_RPP_REAL}.csv").read_text()).observations}
    expected = round(100 * (obs["2026-Q2"] / obs["2025-Q2"] - 1), 2)
    (got,) = _one(conn, "select real_yoy_pct from mim.v_property_price_growth"
                        " where country = 'US' and period = '2026-Q2'")
    assert float(got) == expected


def test_debt_service_deviation_from_own_mean(conn):
    ingest_vintage(conn, "vintage1")
    obs = [o.value for o in parse_sdmx_csv(
        (FIXTURES / "vintage1" / "BIS.WS_DSR.Q.US.H.csv").read_text()).observations]
    dsr, avg, dev = _one(conn, "select dsr_pct, long_run_avg_pct, deviation_pp from"
                               " mim.v_debt_service where country = 'US' and sector ="
                               " 'households' and period = '2026-Q1'")
    assert float(dsr) == obs[-1]
    assert float(avg) == round(sum(obs) / len(obs), 2)
    assert float(dev) == round(obs[-1] - sum(obs) / len(obs), 2)


def test_second_vintage_records_revisions_withdrawals_and_new_periods(conn):
    ingest_vintage(conn, "vintage1")
    s = ingest_vintage(conn, "vintage2")
    # revised: US gap 2026-Q1 value, US real RPP 2022-Q4 -> missing, DE debt 2024-Q1 status
    assert s.revised == 3
    assert s.withdrawn == 1  # GB gap 2021-Q2
    assert s.inserted == 4  # 3 new vintages for revised obs + US gap 2026-Q2
    assert s.flags == {"gap_in_series": 1, "missing_value": 1, "revised": 3, "withdrawn": 1}

    revisions = conn.execute(
        "select series_id, period, previous_value, revised_value, previous_status,"
        " revised_status from mim.v_revisions order by series_id, period"
    ).fetchall()
    assert revisions == [
        ("BIS.WS_CREDIT_GAP.Q.US.P.A.C", "2026-Q1", Decimal("-11.3204"), Decimal("-10.8204"),
         "A", "A"),
        ("BIS.WS_SPP.Q.US.R.628", "2022-Q4", Decimal("156.4894"), None, "A", "M"),
        ("ESTAT.gov_10q_ggdebt.Q.GD.S13.PC_GDP.DE", "2024-Q1", Decimal("61.5"),
         Decimal("61.5"), "p", None),
    ]


def test_point_in_time_query_returns_first_vintage(conn):
    r1 = ingest_vintage(conn, "vintage1").run_id
    r2 = ingest_vintage(conn, "vintage2").run_id
    q = ("select period, value from mim.observations_as_of(%s) where series_id = %s"
         " and period in ('2026-Q1', '2026-Q2') order by period")
    assert conn.execute(q, (r1, US_GAP)).fetchall() == [("2026-Q1", Decimal("-11.3204"))]
    assert conn.execute(q, (r2, US_GAP)).fetchall() == [
        ("2026-Q1", Decimal("-10.8204")), ("2026-Q2", Decimal("-10.7204"))]
    assert _one(conn, "select count(*) from mim.observations_as_of(%s) where series_id = %s"
                      " and period = '2021-Q2'", r1, GB_GAP) == (1,)
    assert _one(conn, "select count(*) from mim.observations_as_of(%s) where series_id = %s"
                      " and period = '2021-Q2'", r2, GB_GAP) == (0,)


def test_indicator_panel_surfaces_missing_data(conn):
    ingest_vintage(conn, "vintage1")
    ingest_vintage(conn, "vintage2")
    rows = conn.execute(
        "select series_id, period_start::text, is_missing_period, is_missing_value,"
        " is_revised_last_run from mim.v_indicator_panel"
        " where is_missing_period or is_missing_value or is_revised_last_run"
        " order by series_id, period_start"
    ).fetchall()
    assert rows == [
        (GB_GAP, "2021-04-01", True, False, False),
        (US_GAP, "2026-01-01", False, False, True),
        (US_RPP_REAL, "2022-10-01", False, True, True),
        (DE_DEBT, "2024-01-01", False, False, True),
    ]
    (n_rows,) = _one(conn, "select count(*) from mim.v_indicator_panel")
    (n_obs,) = _one(conn, "select count(*) from mim.observation_current")
    assert n_rows == n_obs + 1  # one calendar row for the withdrawn GB quarter


def test_reingesting_same_vintage_is_a_no_op(conn):
    ingest_vintage(conn, "vintage1")
    s = ingest_vintage(conn, "vintage1")
    assert (s.inserted, s.revised, s.withdrawn) == (0, 0, 0)


class _FailingFetcher:
    """Wraps a fixture fetcher and fails one series with HTTP 503."""

    def __init__(self, inner, fail_id):
        self.inner, self.fail_id = inner, fail_id

    def fetch(self, series):
        if series.series_id == self.fail_id:
            return FetchResult("https://example.invalid", 503, None, "HTTP 503")
        return self.inner.fetch(series)


def test_failed_fetch_does_not_withdraw_data(conn):
    ingest_vintage(conn, "vintage1")
    fetcher = _FailingFetcher(FixtureFetcher(FIXTURES / "vintage1"), US_GAP)
    s = ingest_vintage(conn, "vintage1", fetcher)
    assert (s.series_failed, s.withdrawn) == (1, 0)
    assert s.flags == {"fetch_failed": 1}
    (n,) = _one(conn, "select count(*) from mim.observation_current where series_id = %s", US_GAP)
    assert n == 29
    ok, status, flags = _one(conn, "select last_pull_ok, http_status, flags_last_run"
                                   " from mim.v_series_quality where series_id = %s", US_GAP)
    assert (ok, status, flags) == (False, 503, "fetch_failed")
