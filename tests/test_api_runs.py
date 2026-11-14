"""HTTP surface of the agent workflow."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from auditor.api.app import create_app
from auditor.config import Settings
from auditor.payments import SimulatedPaymentGateway
from tests.fakes import FakeLLM


@pytest.fixture
def setup(built: Path, sqlite_ledger, tmp_path: Path) -> Iterator[tuple[TestClient, SimulatedPaymentGateway]]:  # type: ignore[no-untyped-def]
    settings = Settings(
        env="test", log_format="console", documents_dir=tmp_path / "docs", max_upload_bytes=200_000, _env_file=None
    )
    payments = SimulatedPaymentGateway()
    app = create_app(settings, ledger=sqlite_ledger, llm=FakeLLM(built), payments=payments)
    with TestClient(app) as client:
        yield client, payments


def _upload(client: TestClient, built: Path, scenario_id: str):  # type: ignore[no-untyped-def]
    pdf = (built / "invoices" / f"{scenario_id}.pdf").read_bytes()
    return client.post("/v1/runs", files={"file": (f"{scenario_id}.pdf", pdf, "application/pdf")})


def test_upload_approve_pay_over_http(setup, built: Path) -> None:  # type: ignore[no-untyped-def]
    client, payments = setup
    created = _upload(client, built, "S01_clean_services")
    assert created.status_code == 201
    run = created.json()
    assert run["status"] == "AWAITING_APPROVAL"
    assert client.get(f"/v1/runs/{run['run_id']}").json()["status"] == "AWAITING_APPROVAL"

    decided = client.post(f"/v1/runs/{run['run_id']}/decision", json={"approved": True, "approver": "alice"})
    assert decided.status_code == 200
    assert decided.json()["status"] == "PAID"
    assert payments.execution_count == 1

    again = client.post(f"/v1/runs/{run['run_id']}/decision", json={"approved": True, "approver": "alice"})
    assert again.status_code == 409
    assert payments.execution_count == 1


def test_blocked_run_cannot_be_approved(setup, built: Path) -> None:  # type: ignore[no-untyped-def]
    client, _ = setup
    run = _upload(client, built, "S07_remit_to_changed").json()
    assert run["status"] == "BLOCKED"
    resp = client.post(f"/v1/runs/{run['run_id']}/decision", json={"approved": True, "approver": "mallory"})
    assert resp.status_code == 409


def test_non_pdf_rejected(setup) -> None:  # type: ignore[no-untyped-def]
    client, _ = setup
    resp = client.post("/v1/runs", files={"file": ("x.pdf", b"MZ\x90\x00 definitely not a pdf", "application/pdf")})
    assert resp.status_code == 415


def test_oversized_upload_rejected(setup) -> None:  # type: ignore[no-untyped-def]
    client, _ = setup
    resp = client.post("/v1/runs", files={"file": ("big.pdf", b"%PDF-" + b"0" * 300_000, "application/pdf")})
    assert resp.status_code == 413


def test_unknown_and_malformed_run_ids(setup) -> None:  # type: ignore[no-untyped-def]
    client, _ = setup
    assert client.get(f"/v1/runs/{'0' * 32}").status_code == 404
    assert client.get("/v1/runs/../../etc/passwd").status_code == 404
    assert client.get("/v1/runs/not-a-run-id").status_code == 422


def test_decision_requires_an_approver(setup, built: Path) -> None:  # type: ignore[no-untyped-def]
    client, _ = setup
    run = _upload(client, built, "S01_clean_services").json()
    resp = client.post(f"/v1/runs/{run['run_id']}/decision", json={"approved": True, "approver": "  "})
    assert resp.status_code == 422


def test_mapping_review_over_http(setup, built: Path) -> None:  # type: ignore[no-untyped-def]
    client, _ = setup
    run = _upload(client, built, "S09_unmatched_line_item").json()
    assert run["status"] == "AWAITING_MAPPING_REVIEW"
    assert run["mapping_proposals"][0]["proposed_code"] == "SENIOR_ENG"
    url = f"/v1/runs/{run['run_id']}/mappings"

    bad = client.post(url, json={"reviewer": "ana", "decisions": [{"line": 1, "item_code": "NOT_A_CODE"}]})
    assert bad.status_code == 422
    assert "rate card" in bad.json()["detail"]

    ok = client.post(url, json={"reviewer": "ana", "decisions": [{"line": 1, "item_code": "SENIOR_ENG"}]})
    assert ok.status_code == 200
    assert ok.json()["status"] == "AWAITING_APPROVAL"

    again = client.post(url, json={"reviewer": "ana", "decisions": [{"line": 1, "item_code": "SENIOR_ENG"}]})
    assert again.status_code == 409
    keyer = client.post(f"/v1/runs/{run['run_id']}/decision", json={"approved": True, "approver": "ana"})
    assert keyer.status_code == 403
