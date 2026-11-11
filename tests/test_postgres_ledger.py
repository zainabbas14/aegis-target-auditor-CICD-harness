"""Postgres-specific guarantees. Parity with SQLite is covered by the
backend-parametrised tests in test_engine_golden.py."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.postgres


def test_pool_refuses_writes_even_with_admin_credentials(pg_dsn: str) -> None:
    import psycopg  # noqa: PLC0415

    from auditor.adapters.postgres_ledger import PostgresLedger  # noqa: PLC0415

    with (
        PostgresLedger(pg_dsn) as led,
        led._pool.connection() as conn,
        pytest.raises(psycopg.errors.ReadOnlySqlTransaction),
    ):
        conn.execute("DELETE FROM historical_payments")


def test_ping(pg_dsn: str) -> None:
    from auditor.adapters.postgres_ledger import PostgresLedger  # noqa: PLC0415

    with PostgresLedger(pg_dsn) as led:
        led.ping()
