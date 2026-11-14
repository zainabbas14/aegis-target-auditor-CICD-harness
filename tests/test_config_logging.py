from __future__ import annotations

import json
import logging

import pytest
from pydantic import ValidationError

from auditor.auth import Role, parse_api_keys
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


# --------------------------------------------------------------------------- #
# M6: authentication and production requirements
# --------------------------------------------------------------------------- #

PROD_OK = {
    "env": "prod",
    "ledger_backend": "postgres",
    "database_url": "postgresql://u:p@db/erp",
    "checkpointer": "postgres",
    "state_database_url": "postgresql://s:p@db/erp",
    "payments_database_url": "postgresql://w:p@db/erp",
    "api_keys": "ap.manager:approver:" + "k" * 32,
}


def test_prod_settings_ok() -> None:
    make(**PROD_OK)


@pytest.mark.parametrize("missing", ["payments_database_url", "api_keys"])
def test_prod_requires_payment_write_back_and_auth(missing: str) -> None:
    with pytest.raises(ValidationError, match=missing.upper()):
        make(**{k: v for k, v in PROD_OK.items() if k != missing})


@pytest.mark.parametrize(
    "keys",
    ["ap.manager:approver:change-me-approver-xxxxxxxx", "ap.manager:approver:short-but-16-chars"],
    ids=["placeholder", "too-short-for-prod"],
)
def test_prod_rejects_weak_keys(keys: str) -> None:
    with pytest.raises(ValidationError, match="at least"):
        make(**{**PROD_OK, "api_keys": keys})


def test_parse_api_keys() -> None:
    principals = parse_api_keys("bea:clerk:aaaaaaaaaaaaaaaa, ana:analyst+clerk:bbbbbbbbbbbbbbbb")
    by_id = {p.id: p for p in principals.values()}
    assert by_id["ana"].roles == {Role.ANALYST, Role.CLERK}
    assert "aaaaaaaaaaaaaaaa" not in str(principals)  # stored by hash only


@pytest.mark.parametrize(
    ("raw", "error"),
    [
        ("bea:clerk", "expected id:role"),
        ("bea:janitor:aaaaaaaaaaaaaaaa", "unknown role"),
        ("bea:clerk:aaaaaaaaaaaaaaaa,BEA:analyst:bbbbbbbbbbbbbbbb", "duplicate principal"),
        ("bea:clerk:aaaaaaaaaaaaaaaa,ana:analyst:aaaaaaaaaaaaaaaa", "reused"),
        ("bea:clerk:short", "at least 16"),
    ],
)
def test_parse_api_keys_rejects(raw: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        parse_api_keys(raw)


def test_api_keys_never_render() -> None:
    s = make(api_keys="bea:clerk:super-secret-key-123456")
    assert "super-secret" not in repr(s)
