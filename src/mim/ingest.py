"""Ingestion: fetch every catalog series, run checks, stage, and merge into vintages."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from importlib.resources import files

import psycopg

from .catalog import Series
from .checks import Flag, check_fetch, check_series
from .sources import Fetcher, FetchResult, ParsedSeries, parse_sdmx_csv

DEFAULT_DSN = "postgresql://mim:mim@localhost:55473/mim"


def database_url() -> str:
    return os.environ.get("MIM_DATABASE_URL", DEFAULT_DSN)


def _sql(name: str) -> str:
    return files("mim").joinpath("sql", name).read_text()


def init_db(conn: psycopg.Connection) -> None:
    with conn.transaction():
        conn.execute(_sql("schema.sql"))
        conn.execute(_sql("views.sql"))


def upsert_catalog(conn: psycopg.Connection, catalog: list[Series]) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            """
            insert into mim.series (series_id, source, dataflow, series_key, country, indicator,
                                    unit, expected_freq, description)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            on conflict (series_id) do update set
                source = excluded.source, dataflow = excluded.dataflow,
                series_key = excluded.series_key, country = excluded.country,
                indicator = excluded.indicator, unit = excluded.unit,
                expected_freq = excluded.expected_freq, description = excluded.description
            """,
            [
                (s.series_id, s.source, s.dataflow, s.key, s.country, s.indicator, s.unit,
                 s.expected_freq, s.description)
                for s in catalog
            ],
        )


@dataclass(frozen=True)
class RunSummary:
    run_id: int
    series_ok: int
    series_failed: int
    inserted: int
    revised: int
    withdrawn: int
    flags: dict[str, int]


def _process(series: Series, result: FetchResult, as_of: date):
    flags: list[Flag] = check_fetch(series, result)
    parsed: ParsedSeries | None = None
    if result.ok:
        try:
            parsed = parse_sdmx_csv(result.body)
        except ValueError as exc:
            flags.append(Flag(series.series_id, None, "unparseable_payload", "error", str(exc)))
        else:
            flags.extend(check_series(series, parsed, as_of))
    return parsed, flags


def run_ingest(
    conn: psycopg.Connection,
    catalog: list[Series],
    fetcher: Fetcher,
    *,
    mode: str,
    as_of: date,
    workers: int = 4,
) -> RunSummary:
    """Fetch, check and merge one pull of `catalog`; all writes happen in one transaction."""
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(fetcher.fetch, catalog))

    with conn.transaction():
        upsert_catalog(conn, catalog)
        run_id = conn.execute(
            "insert into mim.ingest_run (mode, as_of) values (%s, %s) returning run_id",
            (mode, as_of),
        ).fetchone()[0]
        conn.execute("truncate mim.stg_observation")

        all_flags: list[Flag] = []
        ok = failed = 0
        with conn.cursor() as cur:
            for series, result in zip(catalog, results, strict=True):
                parsed, flags = _process(series, result, as_of)
                all_flags.extend(flags)
                usable = parsed is not None and parsed.observations
                ok += bool(usable)
                failed += not usable
                cur.execute(
                    """
                    insert into mim.series_pull (run_id, series_id, url, http_status, ok,
                        n_observations, content_sha256, source_last_update, error)
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (run_id, series.series_id, result.url, result.http_status, bool(usable),
                     len(parsed.observations) if parsed else None, result.sha256,
                     parsed.source_last_update if parsed else None, result.error),
                )
                if not usable:
                    continue
                # Duplicates are flagged by the checks; keep the last row per period.
                rows = {o.period: o for o in parsed.observations}
                with cur.copy(
                    "copy mim.stg_observation (series_id, period, period_start, value, obs_status)"
                    " from stdin"
                ) as copy:
                    for o in rows.values():
                        copy.write_row(
                            (series.series_id, o.period, o.period_start, o.value, o.obs_status)
                        )

            inserted, revised, withdrawn = conn.execute(
                "select * from mim.merge_staging(%s)", (run_id,)
            ).fetchone()
            cur.executemany(
                """
                insert into mim.quality_flag (run_id, series_id, period, flag, severity, detail)
                values (%s, %s, %s, %s, %s, %s)
                """,
                [(run_id, f.series_id, f.period, f.flag, f.severity, f.detail) for f in all_flags],
            )
            conn.execute(
                """
                update mim.ingest_run set finished_at = now(), series_ok = %s,
                    series_failed = %s, obs_inserted = %s, obs_revised = %s, obs_withdrawn = %s
                where run_id = %s
                """,
                (ok, failed, inserted, revised, withdrawn, run_id),
            )
            counts = dict(
                conn.execute(
                    "select flag, count(*) from mim.quality_flag where run_id = %s "
                    "group by flag order by flag",
                    (run_id,),
                ).fetchall()
            )
        conn.execute("truncate mim.stg_observation")
    return RunSummary(run_id, ok, failed, inserted, revised, withdrawn, counts)
