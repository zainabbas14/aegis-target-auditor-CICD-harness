from __future__ import annotations

import json
import logging
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from auditor.api.app import REQUEST_ID_HEADER, create_app
from auditor.config import Settings
from auditor.ports import LedgerReader
from tests.mock_data.fixtures import SCENARIOS

SETTINGS = Settings(env="test", log_format="console", documents_dir=Path(tempfile.mkdtemp()), _env_file=None)


@pytest.fixture
def client(ledger: LedgerReader) -> Iterator[TestClient]:
    with TestClient(create_app(SETTINGS, ledger=ledger, llm=None)) as c:
        yield c


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.scenario_id)
def test_audit_endpoint_matches_golden(scenario: object, client: TestClient, built: Path) -> None:
    sid = scenario.scenario_id  # type: ignore[attr-defined]
    body = json.loads((built / "requests" / f"{sid}.json").read_text())
    resp = client.post("/v1/audits", json=body)
    assert resp.status_code == 200
    assert resp.json()["verdict"] == scenario.expected_verdict.value  # type: ignore[attr-defined]


def test_findings_serialise_with_severity(client: TestClient, built: Path) -> None:
    body = json.loads((built / "requests" / "S07_remit_to_changed.json").read_text())
    (finding,) = client.post("/v1/audits", json=body).json()["findings"]
    assert finding["code"] == "REMIT_TO_MISMATCH"
    assert finding["severity"] == "BLOCK"


def test_malformed_invoice_is_422_not_500(client: TestClient) -> None:
    resp = client.post("/v1/audits", json={"invoice_number": "X", "total": "-5"})
    assert resp.status_code == 422


def test_request_id_is_propagated_or_generated(client: TestClient) -> None:
    assert client.get("/healthz", headers={REQUEST_ID_HEADER: "abc-123"}).headers[REQUEST_ID_HEADER] == "abc-123"
    generated = client.get("/healthz").headers[REQUEST_ID_HEADER]
    assert len(generated) == 32


def test_oversized_request_id_is_replaced(client: TestClient) -> None:
    rid = client.get("/healthz", headers={REQUEST_ID_HEADER: "x" * 500}).headers[REQUEST_ID_HEADER]
    assert rid != "x" * 500


def test_readyz_ok(client: TestClient) -> None:
    assert client.get("/readyz").json() == {"status": "ready"}


class _DownLedger:
    """A ledger whose backend is unreachable."""

    def ping(self) -> None:
        raise ConnectionError("db down")

    def close(self) -> None: ...

    def __getattr__(self, name: str) -> object:
        def _fail(*_: object, **__: object) -> object:
            raise ConnectionError("db down")

        return _fail


def test_readyz_503_when_ledger_down() -> None:
    with TestClient(create_app(SETTINGS, ledger=_DownLedger(), llm=None)) as c:  # type: ignore[arg-type]
        assert c.get("/readyz").status_code == 503


def test_internal_errors_do_not_leak(built: Path, caplog: pytest.LogCaptureFixture) -> None:
    body = json.loads((built / "requests" / "S01_clean_services.json").read_text())
    with TestClient(create_app(SETTINGS, ledger=_DownLedger(), llm=None), raise_server_exceptions=False) as c:  # type: ignore[arg-type]
        resp = c.post("/v1/audits", json=body)
    assert resp.status_code == 500
    assert "db down" not in resp.text
    assert resp.json()["request_id"] == resp.headers[REQUEST_ID_HEADER]
    assert any(r.levelno == logging.ERROR for r in caplog.records)
