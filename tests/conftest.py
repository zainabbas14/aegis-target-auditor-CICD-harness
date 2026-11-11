from __future__ import annotations

import os
import sqlite3
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from auditor.adapters.sqlite_ledger import SqliteLedger
from auditor.ports import LedgerReader
from tests.mock_data.build import build

REPO = Path(__file__).resolve().parents[1]
PG_ADMIN_ENV = "AUDITOR_TEST_DATABASE_URL"


@pytest.fixture(scope="session")
def built(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A fresh, isolated build of all mock data - never the committed copy."""
    out = tmp_path_factory.mktemp("mock")
    build(out)
    return out


@pytest.fixture(scope="session")
def db(built: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(f"file:{built / 'enterprise_erp.db'}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def sqlite_ledger(built: Path) -> SqliteLedger:
    return SqliteLedger(built / "enterprise_erp.db")


@pytest.fixture(scope="session")
def pg_dsn(built: Path) -> Iterator[str]:
    """A throwaway database initialised with the SAME scripts docker compose uses."""
    admin = os.environ.get(PG_ADMIN_ENV)
    if not admin:
        pytest.skip(f"{PG_ADMIN_ENV} not set")
    import psycopg  # noqa: PLC0415
    from psycopg import sql  # noqa: PLC0415
    from psycopg.conninfo import make_conninfo  # noqa: PLC0415

    name = f"auditor_test_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    dsn = make_conninfo(admin, dbname=name)
    try:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute((REPO / "db/postgres/001_schema.sql").read_text())
            conn.execute((built / "postgres/002_seed.sql").read_text())
        yield dsn
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture(scope="session", params=["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)])
def ledger(request: pytest.FixtureRequest, sqlite_ledger: SqliteLedger) -> Iterator[LedgerReader]:
    """Every ledger-backed test runs against BOTH backends."""
    if request.param == "sqlite":
        yield sqlite_ledger
        return
    from auditor.adapters.postgres_ledger import PostgresLedger  # noqa: PLC0415

    led = PostgresLedger(request.getfixturevalue("pg_dsn"))
    yield led
    led.close()
