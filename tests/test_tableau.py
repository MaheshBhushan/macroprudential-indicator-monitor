from __future__ import annotations

import pytest
from conftest import ingest_vintage

from mim.tableau import PANEL_COLUMNS, inspect_tds, write_tds

pytest.importorskip("tableaudocumentapi")


def test_tds_loads_in_tableau_document_api(tmp_path):
    path = tmp_path / "panel.tds"
    write_tds("postgresql://reader:secret@db.example:6543/mimdb", path)
    info = inspect_tds(path)
    assert info["connection_class"] == "postgres"
    assert (info["server"], info["port"], info["dbname"], info["username"]) == (
        "db.example", "6543", "mimdb", "reader")
    assert info["fields"] == sorted(f"[{name}]" for name, _, _ in PANEL_COLUMNS)
    assert "secret" not in path.read_text()


def test_hyper_extract_round_trip(conn, tmp_path):
    pytest.importorskip("tableauhyperapi")
    from mim.tableau import export_hyper, read_hyper_summary

    ingest_vintage(conn, "vintage1")
    ingest_vintage(conn, "vintage2")
    out = tmp_path / "panel.hyper"
    n = export_hyper(conn, out)
    assert n == conn.execute("select count(*) from mim.v_indicator_panel").fetchone()[0]
    summary = {ind: (rows, mp, mv) for ind, rows, mp, mv in read_hyper_summary(out)}
    assert summary["credit_gap"][1] == 1  # withdrawn GB quarter
    assert summary["rpp_real_index"][2] == 1  # US 2022-Q4 turned missing
