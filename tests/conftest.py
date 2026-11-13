from __future__ import annotations

import os
import uuid
from datetime import date
from pathlib import Path

import psycopg
import pytest

from mim.catalog import default_catalog
from mim.ingest import database_url, init_db, run_ingest
from mim.sources import FixtureFetcher

FIXTURES = Path(__file__).parent / "fixtures"
AS_OF = date(2026, 11, 11)  # date the fixtures were captured


def fixture_catalog():
    probe = FixtureFetcher(FIXTURES / "vintage1")
    return [s for s in default_catalog() if probe.path_for(s).exists()]


def ingest_vintage(conn, name: str, fetcher=None):
    return run_ingest(conn, fixture_catalog(), fetcher or FixtureFetcher(FIXTURES / name),
                      mode="fixture", as_of=AS_OF)


@pytest.fixture
def conn():
    """A throwaway database per test on the server in $MIM_DATABASE_URL.

    Set MIM_REQUIRE_DB=1 (CI does) to fail instead of skip when the server is unreachable."""
    admin_url = database_url()
    try:
        admin = psycopg.connect(admin_url, autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as exc:
        if os.environ.get("MIM_REQUIRE_DB") == "1":
            raise
        pytest.skip(f"PostgreSQL not reachable at {admin_url}: {exc}")
    name = f"mim_test_{uuid.uuid4().hex[:12]}"
    admin.execute(f'create database "{name}"')
    info = psycopg.conninfo.conninfo_to_dict(admin_url)
    info["dbname"] = name
    try:
        with psycopg.connect(**info, autocommit=True) as c:
            init_db(c)
            yield c
    finally:
        admin.execute(f'drop database "{name}" with (force)')
        admin.close()
