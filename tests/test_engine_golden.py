"""Every golden scenario, end to end through production code, on every backend.

The request body is the build's parser-view JSON: exactly what a perfect
Document Parser would hand over (no item codes where the layout prints none).
When the LLM parser lands in M3, this same harness runs on its real output.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from auditor.adapters.sqlite_ledger import SqliteLedger
from auditor.domain.enums import Verdict
from auditor.domain.models import ExtractedInvoice
from auditor.ports import LedgerReader
from auditor.rules.engine import audit
from tests.mock_data.fixtures import SCENARIOS

SCENARIO_IDS = [s.scenario_id for s in SCENARIOS]


def golden(built: Path, scenario_id: str) -> dict[str, Any]:
    data = json.loads((built / "expected_outcomes.json").read_text())
    return next(s for s in data["scenarios"] if s["scenario_id"] == scenario_id)


def request_body(built: Path, scenario_id: str) -> dict[str, Any]:
    body: dict[str, Any] = json.loads((built / "requests" / f"{scenario_id}.json").read_text())
    return body


@pytest.mark.parametrize("scenario_id", SCENARIO_IDS)
def test_golden_scenario(scenario_id: str, built: Path, ledger: LedgerReader) -> None:
    g = golden(built, scenario_id)
    result = audit(ExtractedInvoice.model_validate(request_body(built, scenario_id)), ledger)

    assert {f.code.value for f in result.findings} == set(g["expected_findings"]), [f.message for f in result.findings]
    assert result.verdict == Verdict(g["expected_verdict"])


def test_rate_overcharge_evidence_is_quantified(built: Path, ledger: LedgerReader) -> None:
    inv = ExtractedInvoice.model_validate(request_body(built, "S03_rate_overcharge"))
    (finding,) = audit(inv, ledger).findings
    assert finding.evidence["item_code"] == "SENIOR_ENG"  # resolved from description alone
    assert finding.evidence["overcharge"] == "2400.00"


def test_void_payments_are_not_duplicates(db: sqlite3.Connection, ledger: LedgerReader) -> None:
    void = db.execute(
        "SELECT vendor_id, invoice_number FROM historical_payments WHERE status='VOID' LIMIT 1"
    ).fetchone()
    assert void is not None, "seed data should contain at least one VOID payment"
    assert ledger.payments_by_invoice_number(void["vendor_id"], void["invoice_number"]) == ()


def test_sqlite_connection_is_read_only(sqlite_ledger: SqliteLedger) -> None:
    with sqlite_ledger._connect() as conn, pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("DELETE FROM historical_payments")
