from __future__ import annotations

import json
import logging

import pytest
from pydantic import ValidationError

from auditor.config import Settings
from auditor.logging_setup import JsonFormatter, request_id_var


def make(**kw: object) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[arg-type]


def test_postgres_requires_database_url() -> None:
    with pytest.raises(ValidationError, match="AUDITOR_DATABASE_URL"):
        make(ledger_backend="postgres")


def test_prod_forbids_sqlite() -> None:
    with pytest.raises(ValidationError, match="not permitted"):
        make(env="prod")


def test_database_url_never_renders() -> None:
    s = make(ledger_backend="postgres", database_url="postgresql://u:hunter2@db/erp")
    assert "hunter2" not in repr(s)
    assert "hunter2" not in s.model_dump_json()


def test_env_vars_are_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUDITOR_NEAR_DUPLICATE_WINDOW_DAYS", "14")
    assert make().near_duplicate_window_days == 14


def test_json_log_line_has_request_id_and_extras() -> None:
    token = request_id_var.set("rid-42")
    try:
        record = logging.makeLogRecord({"msg": "hello %s", "args": ("world",), "levelname": "INFO", "duration_ms": 3})
        record.request_id = request_id_var.get()
        line = json.loads(JsonFormatter().format(record))
    finally:
        request_id_var.reset(token)
    assert line["msg"] == "hello world"
    assert line["request_id"] == "rid-42"
    assert line["duration_ms"] == 3
