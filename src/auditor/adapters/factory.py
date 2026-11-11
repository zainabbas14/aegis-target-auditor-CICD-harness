from __future__ import annotations

from auditor.config import Settings
from auditor.ports import LedgerReader


def build_ledger(settings: Settings) -> LedgerReader:
    """Composition root for the ledger. Imports are local so the SQLite path
    never needs psycopg installed, and vice versa."""
    if settings.ledger_backend == "postgres":
        from auditor.adapters.postgres_ledger import PostgresLedger  # noqa: PLC0415

        if settings.database_url is None:  # unreachable: Settings validator enforces it
            raise ValueError("database_url required for postgres backend")
        return PostgresLedger(
            settings.database_url.get_secret_value(),
            min_size=settings.db_pool_min,
            max_size=settings.db_pool_max,
        )
    from auditor.adapters.sqlite_ledger import SqliteLedger  # noqa: PLC0415

    return SqliteLedger(settings.sqlite_path)
