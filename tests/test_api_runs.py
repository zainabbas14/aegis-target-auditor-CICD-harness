"""HTTP surface of the agent workflow: authentication, roles, write-back, audit log."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from auditor.adapters.sqlite_ledger import SqliteLedger
from auditor.adapters.sqlite_payments import SqlitePaymentLedger
from auditor.api.app import create_app
from auditor.audit_log import InMemoryAuditLog
from auditor.config import Settings
from auditor.payments import SimulatedPaymentGateway
from tests.fakes import FakeLLM
from tests.identities import API_KEYS, auth


@dataclass
class Api:
    client: TestClient
    payments: SimulatedPaymentGateway
    audit_log: InMemoryAuditLog
    built: Path

    def upload(self, scenario_id: str, who: str = "clerk.bea") -> Any:
        pdf = (self.built / "invoices" / f"{scenario_id}.pdf").read_bytes()
        return self.client.post(
            "/v1/runs", files={"file": (f"{scenario_id}.pdf", pdf, "application/pdf")}, headers=auth(who)
        )

    def decide(self, run_id: str, who: str, approved: bool = True) -> Any:
        return self.client.post(f"/v1/runs/{run_id}/decision", json={"approved": approved}, headers=auth(who))


@pytest.fixture
def api(built: Path, tmp_path: Path) -> Iterator[Api]:
    """Each test gets its own copy of the ledger, because payments are written back to it."""
    db = tmp_path / "erp.db"
    shutil.copy(built / "enterprise_erp.db", db)
    settings = Settings(
        env="test",
        log_format="console",
        documents_dir=tmp_path / "docs",
        max_upload_bytes=200_000,
        api_keys=SecretStr(API_KEYS),
        _env_file=None,
    )
    payments, log = SimulatedPaymentGateway(), InMemoryAuditLog()
    app = create_app(
        settings,
        ledger=SqliteLedger(db),
        llm=FakeLLM(built),
        payments=payments,
        audit_log=log,
        payment_ledger=SqlitePaymentLedger(db),
    )
    with TestClient(app) as client:
        yield Api(client, payments, log, built)


# --------------------------------------------------------------------------- #
# Happy path, with identities from the key
# --------------------------------------------------------------------------- #
def test_upload_approve_pay(api: Api) -> None:
    created = api.upload("S01_clean_services")
    assert created.status_code == 201
    run = created.json()
    assert run["status"] == "AWAITING_APPROVAL"
    assert api.client.get(f"/v1/runs/{run['run_id']}", headers=auth("ian")).json()["status"] == "AWAITING_APPROVAL"

    decided = api.decide(run["run_id"], "ap.manager")
    assert decided.status_code == 200
    assert decided.json()["status"] == "PAID"
    assert decided.json()["decision"]["approver"] == "ap.manager"  # from the key, not the body
    assert api.payments.execution_count == 1
    assert api.decide(run["run_id"], "ap.manager").status_code == 409


# --------------------------------------------------------------------------- #
# Authentication and roles
# --------------------------------------------------------------------------- #
def test_missing_or_bad_key_is_401(api: Api) -> None:
    pdf = (api.built / "invoices" / "S01_clean_services.pdf").read_bytes()
    files = {"file": ("x.pdf", pdf, "application/pdf")}
    assert api.client.post("/v1/runs", files=files).status_code == 401
    bad = api.client.post("/v1/runs", files=files, headers={"Authorization": "Bearer not-a-real-key"})
    assert bad.status_code == 401
    assert bad.headers["www-authenticate"] == "Bearer"
    assert api.client.get("/healthz").status_code == 200  # probes stay open


@pytest.mark.parametrize(
    ("who", "method", "path", "body"),
    [
        ("ap.manager", "post_file", "/v1/runs", None),  # approvers don't upload
        ("clerk.bea", "get", "/v1/audit-log/verify", None),
        ("clerk.bea", "get", "/v1/audit-log", None),
        ("ana", "post_file", "/v1/runs", None),
    ],
)
def test_roles_are_enforced(api: Api, who: str, method: str, path: str, body: Any) -> None:
    if method == "post_file":
        pdf = (api.built / "invoices" / "S01_clean_services.pdf").read_bytes()
        resp = api.client.post(path, files={"file": ("x.pdf", pdf, "application/pdf")}, headers=auth(who))
    else:
        resp = api.client.get(path, headers=auth(who))
    assert resp.status_code == 403


def test_clerk_cannot_approve(api: Api) -> None:
    run = api.upload("S01_clean_services").json()
    assert api.decide(run["run_id"], "clerk.bea").status_code == 403
    assert api.payments.execution_count == 0


def test_identity_cannot_be_smuggled_in_the_body(api: Api) -> None:
    run = api.upload("S01_clean_services").json()
    resp = api.client.post(
        f"/v1/runs/{run['run_id']}/decision",
        json={"approved": True, "approver": "someone.else"},
        headers=auth("ap.manager"),
    )
    assert resp.status_code == 422  # unknown field: identity comes only from the key


def test_four_eyes_compares_authenticated_identities(api: Api) -> None:
    """One person holding both roles still can't map the lines and then approve the payment."""
    run = api.upload("S09_unmatched_line_item", who="multi").json()
    mapped = api.client.post(
        f"/v1/runs/{run['run_id']}/mappings",
        json={"decisions": [{"line": 1, "item_code": "SENIOR_ENG"}]},
        headers=auth("multi"),
    )
    assert mapped.json()["status"] == "AWAITING_APPROVAL"
    assert mapped.json()["mapped_by"] == "multi"
    refused = api.decide(run["run_id"], "multi")
    assert refused.status_code == 403
    assert "mapped" in refused.json()["detail"]
    assert api.decide(run["run_id"], "ap.manager").json()["status"] == "PAID"


# --------------------------------------------------------------------------- #
# Payment write-back closes the cross-run duplicate gap
# --------------------------------------------------------------------------- #
def test_paid_invoice_is_blocked_as_duplicate_in_a_new_run(api: Api) -> None:
    first = api.upload("S01_clean_services").json()
    assert api.decide(first["run_id"], "ap.manager").json()["status"] == "PAID"

    second = api.upload("S01_clean_services", who="clerk.carl").json()
    assert second["status"] == "BLOCKED"
    assert {f["code"] for f in second["findings"]} == {"DUPLICATE_INVOICE"}
    assert api.payments.execution_count == 1


def test_two_pending_approvals_for_one_invoice_pay_once(api: Api) -> None:
    a = api.upload("S01_clean_services").json()
    b = api.upload("S01_clean_services", who="clerk.carl").json()
    assert a["status"] == b["status"] == "AWAITING_APPROVAL"  # neither paid yet
    assert api.decide(a["run_id"], "ap.manager").json()["status"] == "PAID"
    late = api.decide(b["run_id"], "ap.manager").json()
    assert late["status"] == "BLOCKED_AT_PAYMENT"
    assert {f["code"] for f in late["payment_recheck"]["findings"]} == {"DUPLICATE_INVOICE"}
    assert api.payments.execution_count == 1


# --------------------------------------------------------------------------- #
# Audit log
# --------------------------------------------------------------------------- #
def test_audit_log_records_who_did_what_including_refusals(api: Api) -> None:
    run = api.upload("S09_unmatched_line_item", who="multi").json()
    api.client.post(
        f"/v1/runs/{run['run_id']}/mappings",
        json={"decisions": [{"line": 1, "item_code": "SENIOR_ENG"}]},
        headers=auth("multi"),
    )
    api.decide(run["run_id"], "multi")  # refused: four-eyes
    api.decide(run["run_id"], "ap.manager")

    log = api.client.get("/v1/audit-log", params={"run_id": run["run_id"]}, headers=auth("ian")).json()
    assert [(e["actor"], e["action"]) for e in log] == [
        ("multi", "RUN_STARTED"),
        ("multi", "MAPPINGS_SUBMITTED"),
        ("multi", "PAYMENT_APPROVED_REFUSED"),
        ("ap.manager", "PAYMENT_APPROVED"),
    ]
    assert log[0]["outcome"]["status"] == "AWAITING_MAPPING_REVIEW"
    assert log[1]["details"]["decisions"] == [{"line": 1, "item_code": "SENIOR_ENG"}]
    assert log[3]["outcome"]["status"] == "PAID"
    assert log[3]["outcome"]["payment_id"].startswith("SIM-")
    assert api.client.get("/v1/audit-log/verify", headers=auth("ian")).json() == {
        "valid": True,
        "entries": 4,
        "first_bad_seq": None,
    }


def test_tampering_is_detected(api: Api) -> None:
    run = api.upload("S01_clean_services").json()
    api.decide(run["run_id"], "ap.manager")
    entries = api.audit_log._entries
    entries[0] = entries[0].model_copy(update={"actor": "someone.else"})  # rewrite history
    verdict = api.client.get("/v1/audit-log/verify", headers=auth("ian")).json()
    assert verdict == {"valid": False, "entries": 2, "first_bad_seq": 1}


# --------------------------------------------------------------------------- #
# Input validation (unchanged behaviour, now authenticated)
# --------------------------------------------------------------------------- #
def test_blocked_run_cannot_be_approved(api: Api) -> None:
    run = api.upload("S07_remit_to_changed").json()
    assert run["status"] == "BLOCKED"
    assert api.decide(run["run_id"], "ap.manager").status_code == 409


def test_non_pdf_and_oversized_uploads(api: Api) -> None:
    h = auth("clerk.bea")
    bad = api.client.post("/v1/runs", files={"file": ("x.pdf", b"MZ\x90\x00 not a pdf", "application/pdf")}, headers=h)
    assert bad.status_code == 415
    big = api.client.post(
        "/v1/runs", files={"file": ("b.pdf", b"%PDF-" + b"0" * 300_000, "application/pdf")}, headers=h
    )
    assert big.status_code == 413


def test_unknown_and_malformed_run_ids(api: Api) -> None:
    h = auth("ian")
    assert api.client.get(f"/v1/runs/{'0' * 32}", headers=h).status_code == 404
    assert api.client.get("/v1/runs/not-a-run-id", headers=h).status_code == 422


def test_mapping_review_over_http(api: Api) -> None:
    run = api.upload("S09_unmatched_line_item").json()
    assert run["status"] == "AWAITING_MAPPING_REVIEW"
    assert run["mapping_proposals"][0]["proposed_code"] == "SENIOR_ENG"
    url = f"/v1/runs/{run['run_id']}/mappings"
    h = auth("ana")
    bad = api.client.post(url, json={"decisions": [{"line": 1, "item_code": "NOT_A_CODE"}]}, headers=h)
    assert bad.status_code == 422
    assert "rate card" in bad.json()["detail"]
    ok = api.client.post(url, json={"decisions": [{"line": 1, "item_code": "SENIOR_ENG"}]}, headers=h)
    assert ok.json()["status"] == "AWAITING_APPROVAL"
    assert (
        api.client.post(url, json={"decisions": [{"line": 1, "item_code": "SENIOR_ENG"}]}, headers=h).status_code == 409
    )
    assert api.decide(run["run_id"], "ana").status_code == 403  # analysts don't approve
