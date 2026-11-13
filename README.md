# Macroprudential Indicator Monitor

Python and SQL pipeline that pulls public credit, property-price and debt series from the BIS,
ECB and Eurostat statistical APIs into PostgreSQL. Every pull is stored as a vintage, so revisions
between pulls can be queried. Each pull runs frequency, gap, missing-value, duplicate and staleness
checks, and the results are stored as flags. SQL views compute the indicators (credit-to-GDP gap
with the Basel III buffer band, property price growth, debt service ratios, total credit,
government debt, bank loan growth). The tool also writes a Tableau-ready `.hyper` extract and a
`.tds` data source.

## Data sources

The catalog in `src/mim/catalog.py` defines 62 series:

| Indicator | Source / dataflow | Key | Countries |
|---|---|---|---|
| `credit_gap` | BIS `WS_CREDIT_GAP` | `Q.{c}.P.A.C` | US GB DE FR ES IT JP CA |
| `total_credit_gdp` | BIS `WS_TC` | `Q.{c}.P.A.M.770.A` | same |
| `rpp_real_index`, `rpp_nominal_index` | BIS `WS_SPP` | `Q.{c}.R.628`, `Q.{c}.N.628` | same |
| `dsr_private`, `dsr_households`, `dsr_nfc` | BIS `WS_DSR` | `Q.{c}.P` / `H` / `N` | same |
| `gov_debt_gdp` | Eurostat `gov_10q_ggdebt` | `Q.GD.S13.PC_GDP.{c}` | DE FR ES IT |
| `hh_loan_growth`, `nfc_loan_growth` | ECB `BSI` | `M.U2.Y.U.A20.A.I.U2.2250/2240.Z01.A` | euro area |

All three APIs are queried for SDMX CSV (`src/mim/sources.py`). No API key is needed.

## Architecture

```
catalog.py ──> sources.py (HttpFetcher | FixtureFetcher) ──> parse_sdmx_csv
                                                          └─> checks.py (flags)
ingest.py: one transaction per run
   series_pull (URL, HTTP status, sha256, source LAST UPDATE)
   COPY -> mim.stg_observation -> mim.merge_staging(run_id)   [PL/pgSQL, set-based]
   quality_flag (check flags + revised/withdrawn flags written by the merge)
views.sql: observation_current, v_revisions, v_credit_gap, v_property_price_growth,
           v_debt_service, v_total_credit, v_government_debt, v_bank_loan_growth,
           v_series_quality, v_indicator_panel, v_latest_snapshot
tableau.py: v_indicator_panel -> .hyper (Hyper API), .tds (PostgreSQL live connection)
```

### Vintages and revisions

`mim.observation_vintage` holds one row for each (series, period, vintage), with
`valid_from_run` and `valid_to_run`. When a run sees a different value or status for a period,
the open row is closed and a new row is inserted, and a `revised` flag records the old and new
value. When a successful pull no longer contains a period, the open row is closed and a
`withdrawn` flag is written. A failed fetch (non-200, unparseable body) marks the pull as not ok,
and the merge skips that series, so an outage never withdraws data. Use
`mim.observations_as_of(run_id)` to see the dataset as it stood after any run, and `mim.v_revisions`
to list every change between consecutive vintages.

### Quality flags

| Flag | Severity | Raised when |
|---|---|---|
| `no_data` | warning | HTTP 404 or an empty response |
| `fetch_failed` | error | other HTTP errors or network errors after 3 attempts (429/5xx retried) |
| `unparseable_payload`, `unparseable_period` | error | body is not SDMX CSV, or `TIME_PERIOD` is not `YYYY`, `YYYY-Qn` or `YYYY-MM` |
| `frequency_mismatch` | error | observed frequency differs from the catalog's expected frequency |
| `mixed_frequency` | error | more than one frequency in a single series |
| `duplicate_period` | error | the same period appears twice |
| `gap_in_series` | warning | a period is absent between the first and last observation |
| `missing_value` | warning | a period is present but `OBS_VALUE` is empty or NaN |
| `stale_series` | warning | latest period ended more than 120 (M) / 300 (Q) / 550 (A) days before the run date |
| `revised` | info | value or status changed since the previous vintage |
| `withdrawn` | warning | a previously published period disappeared |

`mim.v_indicator_panel` is the long-format Tableau feed. It holds one row for each series and
each expected period between the series' first and last observation, with boolean columns
`is_missing_period`, `is_missing_value`, `is_revised_last_run`, `series_stale` and
`frequency_issue`.

## Quickstart

Requires Docker and [uv](https://docs.astral.sh/uv/).

```bash
docker compose up -d --wait          # PostgreSQL 16 on 127.0.0.1:55473 (user/pass/db: mim)
uv sync --all-extras
uv run mim init-db                   # schema, merge function, views (idempotent)
uv run mim ingest                    # live pull from BIS, ECB, Eurostat -> new run
uv run mim report                    # indicator and quality summaries
uv run mim tableau --out-dir tableau # tableau/mim_indicator_panel.hyper + .tds
```

Set `MIM_DATABASE_URL` to use another server. `mim ingest --fixtures tests/fixtures/vintage1`
ingests the committed fixtures instead of calling the APIs.

## Tests

```bash
docker compose up -d --wait
uv run pytest -v
uv run ruff check .
```

The tests use only the committed fixtures and make no network calls. Database tests create and
drop a throwaway database on the server in `MIM_DATABASE_URL`. They skip if no server is
reachable unless `MIM_REQUIRE_DB=1` is set, which CI sets. CI (`.github/workflows/ci.yml`) runs
ruff and pytest against a Postgres service container, then runs the CLI end to end on the fixtures.

Fixtures (`tests/fixtures/`):

- `vintage1/`: real responses for 9 series (7 BIS, 1 ECB, 1 Eurostat), captured on 2026-11-11
  by `scripts/capture_fixtures.py` and truncated by `startPeriod`.
- `vintage2/`: `vintage1` with four deterministic edits that simulate a later publication.
  These are synthetic edits, not real revisions: the US credit gap for 2026-Q1 is moved by
  +0.5 pp and a 2026-Q2 quarter is added; GB 2021-Q2 is removed; US real property prices for
  2022-Q4 become missing (status `M`); and the provisional flag on Eurostat DE 2024-Q1 is
  dropped.

## Measured results

All numbers below come from commands run on this machine. To reproduce, run the quickstart
commands. The live values will change as the sources publish new data.

Test suite (`uv run pytest -q`): **32 passed** (21 unit tests on parsing and checks, 9 PostgreSQL
pipeline tests, 2 Tableau tests).

Live ingestion (`uv run mim ingest`, run twice):

| run | as_of | series ok | series failed | vintages inserted | revised | withdrawn | flags |
|---|---|---|---|---|---|---|---|
| 1 | 2026-11-11 | 62 | 0 | 11425 | 0 | 0 | stale_series=2 |
| 2 | 2026-11-13 | 62 | 0 | 0 | 0 | 0 | stale_series=2 |

The sources published no revisions in the two days between the live pulls, so run 2 added no
vintages. Both stale flags are for Japan's residential property price series
(`BIS.WS_SPP.Q.JP.R.628`, `BIS.WS_SPP.Q.JP.N.628`): "latest period ended 2025-12-31, 317 days
before 2026-11-13 (allowance 300)".

`mim.v_latest_snapshot` after run 2 (from `uv run mim report`):

| country | credit gap period | credit-to-GDP gap (pp) | credit/GDP % | DSR private % | gov debt/GDP % | RPP period | real RPP y/y % |
|---|---|---|---|---|---|---|---|
| CA | 2026-Q1 | -15.2652 | 218.8 | 25.5 | | 2026-Q2 | -6.72 |
| DE | 2026-Q1 | -4.1759 | 134.8 | 12.2 | 63.4 | 2026-Q2 | -1.95 |
| ES | 2026-Q1 | -24.2876 | 119.4 | 11.7 | 101.4 | 2026-Q2 | 8.63 |
| FR | 2026-Q1 | -15.741 | 214.3 | 19.9 | 119.1 | 2026-Q2 | -2.84 |
| GB | 2026-Q1 | -20.1244 | 128.4 | 12.9 | | 2026-Q2 | 0.10 |
| IT | 2026-Q1 | -13.5622 | 94.2 | 9.2 | 139.9 | 2026-Q2 | 0.97 |
| JP | 2026-Q1 | 5.6702 | 173.7 | 15.5 | | 2025-Q4 | 2.43 |
| US | 2026-Q1 | -11.3204 | 139.7 | 13.8 | | 2026-Q2 | -2.60 |

Government debt comes from Eurostat and covers only DE, FR, ES and IT.

From `mim.v_credit_gap`, Japan is the only catalog country with a gap above the 2 pp Basel III
threshold. Its quarterly readings were 3.3488 (2025-Q2), 4.1848 (2025-Q3), 5.8727 (2025-Q4) and
5.6702 (2026-Q1). These map to indicative buffer add-ons of 0.42, 0.68, 1.21 and 1.15 %. Every
other country is in band `below_2pp`.

From `mim.v_debt_service` (2026-Q1, deviation from the series' own full-sample mean): the
non-financial corporate DSR is 57.3 % in Canada (+9.07 pp) and 58.2 % in France (+6.67 pp), and
32.4 % in Spain (-14.08 pp).

From `mim.v_bank_loan_growth` (ECB, 2026-09): euro area MFI loans to households grew 3.09 % y/y
and loans to non-financial corporations grew 4.29 % y/y.

Tableau export (`uv run mim tableau`): the extract holds 11425 rows. The row count was read back
through the Hyper API, and the extract has 0 missing periods and 0 missing values across all ten
indicators. The `.tds` loads in Tableau's Document API with class `postgres` and 15 fields.

Revision tracking on the fixtures (`mim ingest --fixtures tests/fixtures/vintage1` then
`.../vintage2`, both `--as-of 2026-11-11`, on a fresh database):

| run | vintages inserted | revised | withdrawn | flags |
|---|---|---|---|---|
| 1 | 280 | 0 | 0 | none |
| 2 | 4 | 3 | 1 | gap_in_series=1, missing_value=1, revised=3, withdrawn=1 |

`mim.v_revisions` then lists US credit gap 2026-Q1 -11.3204 -> -10.8204, US real RPP 2022-Q4
156.4894 -> NULL (status A -> M), and DE government debt 2024-Q1 61.5 -> 61.5 (status p -> NULL).
In `v_indicator_panel`, GB 2021-Q2 has `is_missing_period = true` and US real RPP 2022-Q4 has
`is_missing_value = true`.

## Tableau

`tableau/mim_indicator_panel.tds` is a Tableau data source that connects live to
`mim.v_indicator_panel` on the local PostgreSQL. It stores no password; Tableau asks for one.
`uv run mim tableau` regenerates it and writes `tableau/mim_indicator_panel.hyper`. That extract
is not committed because it is generated data. Both files are checked programmatically: the
extract is re-opened with the Tableau Hyper API and its rows are counted, and the `.tds` is
parsed with the Tableau Document API. To build views in Tableau, open either file and put
`period_start` on Columns, `value` on Rows, `indicator` and `country` on Filters, and the
`is_missing_*` flags on Color.

## Limitations

- **No Tableau workbook and no rendered views.** Tableau Desktop is not available on the Linux
  machine used to build this project, so no `.twb`/`.twbx` was produced and nothing was rendered
  in Tableau. What exists: the SQL indicator views, the `.hyper` extract (validated through the
  Hyper API) and the `.tds` data source (validated through the Document API). It is unverified
  whether Tableau Desktop or Server opens these files without warnings.
- No real source revisions have been observed yet. The two live pulls were two days apart and
  identical. The revision and withdrawal logic is tested on fixtures that contain synthetic,
  documented edits (see Fixtures).
- Staleness thresholds are fixed per frequency and do not follow each source's release calendar.
  For example, BIS property prices for Japan lag by more than 300 days and are flagged.
- The debt-service "deviation" is the gap to each series' own full-sample average. It is not the
  BIS early-warning methodology with country-specific thresholds.
- The indicative countercyclical buffer is the Basel III reference-guide mapping (2 pp to 10 pp of
  gap mapped linearly to 0 to 2.5 %). It is not any authority's actual buffer decision.
- Runs are manual (`mim ingest`). No scheduler is included.

## License

MIT
