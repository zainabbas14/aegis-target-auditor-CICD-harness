"""Contract agent: read the signed PDF, reconcile its rate card against the ERP mirror."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from auditor.agents.contracts import ContractReader, DriftCode, read_and_reconcile
from auditor.api.app import create_app
from auditor.config import Settings
from auditor.domain.models import RateCardEntry, VendorRecord
from auditor.llm.ports import LLMUnavailableError
from auditor.ports import LedgerReader
from tests.fakes import FakeLLM
from tests.mock_data.fixtures import CONTRACTS

REFS = [c.contract_ref for c in CONTRACTS]


class DriftedLedger:
    """A real ledger whose rate-card mirror has been edited (the drift we want to catch)."""

    def __init__(
        self,
        inner: LedgerReader,
        rates: dict[str, tuple[RateCardEntry, ...]] | None = None,
        vendor: VendorRecord | bool | None = True,
    ) -> None:
        self._inner, self._rates, self._vendor = inner, rates or {}, vendor

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def rate_card(self, contract_ref: str) -> tuple[RateCardEntry, ...]:
        return self._rates.get(contract_ref, self._inner.rate_card(contract_ref))

    def vendor_by_tax_id(self, tax_id: str) -> VendorRecord | None:
        return self._inner.vendor_by_tax_id(tax_id) if self._vendor is True else None


def _pdf(built: Path, ref: str) -> bytes:
    return (built / "contracts" / f"{ref}.pdf").read_bytes()


@pytest.mark.parametrize("ref", REFS)
def test_signed_contracts_match_the_erp_mirror(ref: str, built: Path, ledger: LedgerReader) -> None:
    report = read_and_reconcile(ContractReader(FakeLLM(built)), ledger, _pdf(built, ref))
    assert report.status == "CONSISTENT", report.drifts
    assert report.contract_ref == ref
    assert report.lines_checked == 3


def test_typo_in_erp_ceiling_is_reported(built: Path, ledger: LedgerReader) -> None:
    ref = "MSA-HDS-2025-014"
    erp = list(ledger.rate_card(ref))
    erp = [
        e.model_copy(update={"max_unit_price": Decimal("102.00")}) if e.item_code == "SENIOR_ENG" else e for e in erp
    ]
    report = read_and_reconcile(
        ContractReader(FakeLLM(built)),
        DriftedLedger(ledger, {ref: tuple(erp)}),
        _pdf(built, ref),
    )
    assert report.status == "DRIFT"
    (drift,) = report.drifts
    assert drift.code is DriftCode.RATE_MISMATCH
    assert (drift.item_code, drift.contract_value, drift.erp_value) == ("SENIOR_ENG", "120.00", "102.00")


def test_lines_missing_on_either_side(built: Path, ledger: LedgerReader) -> None:
    ref = "MSA-KFC-2025-003"
    erp = [e for e in ledger.rate_card(ref) if e.item_code != "FUEL_SURCH"]
    erp.append(
        RateCardEntry(item_code="DETENTION", description="Detention", unit="hour", max_unit_price=Decimal("45.00"))
    )
    report = read_and_reconcile(
        ContractReader(FakeLLM(built)),
        DriftedLedger(ledger, {ref: tuple(erp)}),
        _pdf(built, ref),
    )
    assert {(d.code, d.item_code) for d in report.drifts} == {
        (DriftCode.MISSING_IN_CONTRACT, "DETENTION"),
        (DriftCode.MISSING_IN_ERP, "FUEL_SURCH"),
    }


def test_unknown_supplier_is_reported(built: Path, ledger: LedgerReader) -> None:
    report = read_and_reconcile(
        ContractReader(FakeLLM(built)),
        DriftedLedger(ledger, vendor=False),
        _pdf(built, REFS[0]),
    )
    assert DriftCode.SUPPLIER_MISMATCH in {d.code for d in report.drifts}


def test_hallucinated_rate_makes_the_contract_unreadable(built: Path, ledger: LedgerReader) -> None:
    llm = FakeLLM(built)

    def inflate(c):  # type: ignore[no-untyped-def]
        lines = [
            ln.model_copy(update={"max_unit_price": "150.00"}) if ln.item_code == "SENIOR_ENG" else ln
            for ln in c.rate_card
        ]
        return c.model_copy(update={"rate_card": lines})

    llm.contract_override = inflate
    report = read_and_reconcile(ContractReader(llm), ledger, _pdf(built, "MSA-HDS-2025-014"))
    assert report.status == "UNREADABLE"
    assert report.unreadable_reason == "ungrounded"
    assert "max_unit_price" in (report.unreadable_detail or "")


def test_llm_failure_degrades_to_unreadable(built: Path, ledger: LedgerReader) -> None:
    llm = FakeLLM(built)
    llm.contract_override = LLMUnavailableError("transient_failure")
    report = read_and_reconcile(ContractReader(llm), ledger, _pdf(built, REFS[0]))
    assert (report.status, report.unreadable_reason) == ("UNREADABLE", "llm_transient_failure")
    no_llm = read_and_reconcile(ContractReader(None), ledger, _pdf(built, REFS[0]))
    assert no_llm.unreadable_reason == "llm_not_configured"


def test_reconcile_endpoint(built: Path, sqlite_ledger: LedgerReader, tmp_path: Path) -> None:
    settings = Settings(env="test", log_format="console", documents_dir=tmp_path, _env_file=None)
    with TestClient(create_app(settings, ledger=sqlite_ledger, llm=FakeLLM(built))) as client:
        ok = client.post(
            "/v1/contracts/reconcile",
            files={"file": ("c.pdf", _pdf(built, "SUPPLY-MIS-2024-007"), "application/pdf")},
        )
        assert ok.status_code == 200
        assert ok.json()["status"] == "CONSISTENT"
        bad = client.post("/v1/contracts/reconcile", files={"file": ("c.pdf", b"not a pdf", "application/pdf")})
        assert bad.status_code == 415
