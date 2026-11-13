"""Tableau deliverables: a .hyper extract of the indicator panel and a .tds PostgreSQL data source.

Requires the optional `tableau` extra (tableauhyperapi, tableaudocumentapi).
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse
from xml.sax.saxutils import quoteattr

import psycopg

# (column, hyper type name, nullable) for mim.v_indicator_panel
PANEL_COLUMNS = [
    ("series_id", "text", False),
    ("source", "text", False),
    ("country", "text", False),
    ("indicator", "text", False),
    ("unit", "text", False),
    ("expected_freq", "text", False),
    ("period_start", "date", False),
    ("period", "text", True),
    ("value", "double", True),
    ("obs_status", "text", True),
    ("is_missing_period", "bool", False),
    ("is_missing_value", "bool", False),
    ("is_revised_last_run", "bool", False),
    ("series_stale", "bool", False),
    ("frequency_issue", "bool", False),
]


def export_hyper(conn: psycopg.Connection, out: Path) -> int:
    """Write mim.v_indicator_panel to `out` as Extract.Extract; return the row count read back."""
    from tableauhyperapi import (
        NOT_NULLABLE,
        NULLABLE,
        Connection,
        CreateMode,
        HyperProcess,
        Inserter,
        SqlType,
        TableDefinition,
        TableName,
        Telemetry,
    )

    types = {"text": SqlType.text(), "date": SqlType.date(), "double": SqlType.double(),
             "bool": SqlType.bool()}
    table = TableDefinition(
        TableName("Extract", "Extract"),
        [TableDefinition.Column(name, types[t], NULLABLE if nullable else NOT_NULLABLE)
         for name, t, nullable in PANEL_COLUMNS],
    )
    cols = ", ".join(name for name, _, _ in PANEL_COLUMNS)
    rows = conn.execute(
        f"select {cols} from mim.v_indicator_panel order by series_id, period_start"
    ).fetchall()
    rows = [tuple(float(v) if PANEL_COLUMNS[i][1] == "double" and v is not None else v
                  for i, v in enumerate(r)) for r in rows]

    out.parent.mkdir(parents=True, exist_ok=True)
    with HyperProcess(telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU) as hyper:
        with Connection(hyper.endpoint, str(out), CreateMode.CREATE_AND_REPLACE) as hc:
            hc.catalog.create_schema("Extract")
            hc.catalog.create_table(table)
            with Inserter(hc, table) as ins:
                ins.add_rows(rows)
                ins.execute()
            return hc.execute_scalar_query(f"SELECT COUNT(*) FROM {table.table_name}")


def read_hyper_summary(path: Path) -> list[tuple]:
    """Re-open an extract and return (indicator, rows, missing periods, missing values)."""
    from tableauhyperapi import Connection, HyperProcess, Telemetry

    with HyperProcess(telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU) as hyper:
        with Connection(hyper.endpoint, str(path)) as hc:
            return hc.execute_list_query(
                'SELECT "indicator", COUNT(*), '
                'SUM(CASE WHEN "is_missing_period" THEN 1 ELSE 0 END), '
                'SUM(CASE WHEN "is_missing_value" THEN 1 ELSE 0 END) '
                'FROM "Extract"."Extract" GROUP BY "indicator" ORDER BY "indicator"'
            )


_TDS_TEMPLATE = """<?xml version='1.0' encoding='utf-8' ?>
<datasource formatted-name='mim_indicator_panel' inline='true' version='18.1' \
xmlns:user='http://www.tableausoftware.com/xml/user'>
  <connection class='federated'>
    <named-connections>
      <named-connection caption={server} name='postgres.mim'>
        <connection authentication='username-password' class='postgres' dbname={dbname} \
odbc-native-protocol='' one-time-sql='' port={port} server={server} sslmode='' \
username={username} />
      </named-connection>
    </named-connections>
    <relation connection='postgres.mim' name='v_indicator_panel' \
table='[mim].[v_indicator_panel]' type='table' />
  </connection>
  <aliases enabled='yes' />
{columns}
</datasource>
"""

_TDS_TYPES = {"text": ("string", "dimension", "nominal"), "date": ("date", "dimension", "ordinal"),
              "double": ("real", "measure", "quantitative"),
              "bool": ("boolean", "dimension", "nominal")}


def write_tds(dsn: str, out: Path) -> None:
    """Write a Tableau data source (.tds) pointing at mim.v_indicator_panel. The password is
    never written; Tableau prompts for it."""
    u = urlparse(dsn)
    columns = "\n".join(
        f"  <column datatype='{_TDS_TYPES[t][0]}' name='[{name}]' role='{_TDS_TYPES[t][1]}' "
        f"type='{_TDS_TYPES[t][2]}' />"
        for name, t, _ in PANEL_COLUMNS
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        _TDS_TEMPLATE.format(
            server=quoteattr(u.hostname or "localhost"),
            port=quoteattr(str(u.port or 5432)),
            dbname=quoteattr(u.path.lstrip("/") or "mim"),
            username=quoteattr(u.username or "mim"),
            columns=columns,
        ),
        encoding="utf-8",
    )


def inspect_tds(path: Path) -> dict:
    """Load a .tds with Tableau's Document API and return what it sees."""
    from tableaudocumentapi import Datasource

    ds = Datasource.from_file(str(path))
    conn = ds.connections[0]
    return {
        "connection_class": conn.dbclass,
        "server": conn.server,
        "port": conn.port,
        "dbname": conn.dbname,
        "username": conn.username,
        "fields": sorted(f.id for f in ds.fields.values()),
    }
