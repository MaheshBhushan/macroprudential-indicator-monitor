"""Command line entry point: `mim init-db | ingest | report | tableau`."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import psycopg

from .catalog import default_catalog
from .ingest import database_url, init_db, run_ingest
from .sources import FixtureFetcher, HttpFetcher


def _print_table(cur: psycopg.Cursor, title: str) -> None:
    rows = cur.fetchall()
    names = [d.name for d in cur.description]
    cells = [[("" if v is None else str(v)) for v in r] for r in rows]
    widths = [max([len(n)] + [len(c[i]) for c in cells]) for i, n in enumerate(names)]
    print(f"\n{title}")
    print("  ".join(n.ljust(w) for n, w in zip(names, widths, strict=True)))
    for c in cells:
        print("  ".join(v.ljust(w) for v, w in zip(c, widths, strict=True)))


REPORT_QUERIES = [
    ("Ingest runs",
     "select run_id, mode, as_of, series_ok, series_failed, obs_inserted, obs_revised, "
     "obs_withdrawn from mim.ingest_run order by run_id"),
    ("Latest reading per country (mim.v_latest_snapshot)",
     "select * from mim.v_latest_snapshot order by country"),
    ("Credit-to-GDP gap, latest 4 quarters per country (mim.v_credit_gap)",
     "select country, period, credit_gap_pp, ccyb_band, indicative_ccyb_pct from ("
     " select *, row_number() over (partition by country order by period_start desc) rn"
     " from mim.v_credit_gap where credit_gap_pp is not null) t where rn <= 4"
     " order by country, period"),
    ("Debt service ratios, latest quarter (mim.v_debt_service)",
     "select distinct on (country, sector) country, sector, period, dsr_pct, long_run_avg_pct,"
     " deviation_pp from mim.v_debt_service where dsr_pct is not null"
     " order by country, sector, period_start desc"),
    ("Bank loan growth, latest month (mim.v_bank_loan_growth)",
     "select distinct on (borrower) country, borrower, period, loan_growth_yoy_pct"
     " from mim.v_bank_loan_growth order by borrower, period_start desc"),
    ("Quality flags in latest run",
     "select flag, severity, count(*) as n from mim.quality_flag"
     " where run_id = (select max(run_id) from mim.ingest_run) group by 1, 2 order by 1"),
    ("Warning/error flags in latest run (first 30)",
     "select series_id, period, flag, severity, detail from mim.quality_flag"
     " where run_id = (select max(run_id) from mim.ingest_run) and severity <> 'info'"
     " order by series_id, flag_id limit 30"),
    ("Revisions recorded across all runs (mim.v_revisions)",
     "select series_id, period, previous_value, revised_value, revision, revised_in_run"
     " from mim.v_revisions order by revised_in_run, series_id, period_start limit 20"),
    ("Indicator panel coverage (mim.v_indicator_panel)",
     "select indicator, count(*) as rows, count(*) filter (where is_missing_period) as"
     " missing_periods, count(*) filter (where is_missing_value) as missing_values,"
     " count(distinct series_id) filter (where series_stale) as stale_series"
     " from mim.v_indicator_panel group by indicator order by indicator"),
]


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="mim", description=__doc__)
    p.add_argument("--dsn", default=None, help="PostgreSQL URL (default: $MIM_DATABASE_URL)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db", help="create schema, merge function and views")
    ing = sub.add_parser("ingest", help="pull every catalog series and merge a new vintage")
    ing.add_argument("--fixtures", type=Path, help="read <series_id>.csv files instead of HTTP")
    ing.add_argument("--as-of", type=date.fromisoformat, default=None,
                     help="reference date for staleness checks (default: today)")
    sub.add_parser("report", help="print indicator and quality summaries")
    tab = sub.add_parser("tableau", help="write .hyper extract and .tds data source")
    tab.add_argument("--out-dir", type=Path, default=Path("tableau"))
    args = p.parse_args(argv)

    dsn = args.dsn or database_url()
    with psycopg.connect(dsn, autocommit=True) as conn:
        if args.cmd == "init-db":
            init_db(conn)
            print("schema mim ready")
        elif args.cmd == "ingest":
            catalog = default_catalog()
            if args.fixtures:
                fetcher = FixtureFetcher(args.fixtures)
                catalog = [s for s in catalog if fetcher.path_for(s).exists()]
                mode = "fixture"
            else:
                fetcher, mode = HttpFetcher(), "live"
            s = run_ingest(conn, catalog, fetcher, mode=mode, as_of=args.as_of or date.today())
            print(f"run {s.run_id}: {s.series_ok} series ok, {s.series_failed} unusable; "
                  f"{s.inserted} vintages inserted, {s.revised} revised, {s.withdrawn} withdrawn")
            print("flags: " + ", ".join(f"{k}={v}" for k, v in s.flags.items()))
        elif args.cmd == "report":
            with conn.cursor() as cur:
                for title, sql in REPORT_QUERIES:
                    cur.execute(sql)
                    _print_table(cur, title)
        elif args.cmd == "tableau":
            from .tableau import export_hyper, inspect_tds, read_hyper_summary, write_tds

            hyper_path = args.out_dir / "mim_indicator_panel.hyper"
            tds_path = args.out_dir / "mim_indicator_panel.tds"
            n = export_hyper(conn, hyper_path)
            print(f"wrote {hyper_path}: {n} rows (count read back through Hyper)")
            for ind, rows, mp, mv in read_hyper_summary(hyper_path):
                print(f"  {ind}: rows={rows} missing_periods={mp} missing_values={mv}")
            write_tds(dsn, tds_path)
            info = inspect_tds(tds_path)
            print(f"wrote {tds_path}: class={info['connection_class']} server={info['server']} "
                  f"port={info['port']} db={info['dbname']} fields={len(info['fields'])}")


if __name__ == "__main__":
    main()
