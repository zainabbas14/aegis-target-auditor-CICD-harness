from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from auditor.adapters.sqlite_ledger import SqliteLedger
from tests.mock_data.build import build


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
def ledger(built: Path) -> Iterator[SqliteLedger]:
    with SqliteLedger(built / "enterprise_erp.db") as led:
        yield led
