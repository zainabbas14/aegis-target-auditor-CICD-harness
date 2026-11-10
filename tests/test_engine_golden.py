"""Every golden scenario, end to end through production code.

The invoice is built from the golden *extraction* (what is printed on the
PDF), with item codes removed for layouts that don't print them - exactly
what a perfect Document Parser would hand over. When the LLM parser lands in
M3, this same harness runs on its real output instead.
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
from auditor.rules.engine import audit
from tests.mock_data.fixtures import SCENARIOS


def _golden(built: Path) -> dict[str, dict[str, Any]]:
    data = json.loads((built / "expected_outcomes.json").read_text())
    return {s["scenario_id"]: s for s in data["scenarios"]}


def as_parser_would_see_it(golden: dict[str, Any]) -> ExtractedInvoice:
    extraction = dict(golden["expected_extraction"])
    if not golden["item_codes_printed"]:
        extraction["line_items"] = [{**ln, "item_code": None} for ln in extraction["line_items"]]
    return ExtractedInvoice.model_validate(extraction)


@pytest.mark.parametrize("scenario_id", [s.scenario_id for s in SCENARIOS])
def test_golden_scenario(scenario_id: str, built: Path, ledger: SqliteLedger) -> None:
    golden = _golden(built)[scenario_id]
    result = audit(as_parser_would_see_it(golden), ledger)

    assert {f.code.value for f in result.findings} == set(golden["expected_findings"]), [
        f.message for f in result.findings
    ]
    assert result.verdict == Verdict(golden["expected_verdict"])


def test_rate_overcharge_evidence_is_quantified(built: Path, ledger: SqliteLedger) -> None:
    result = audit(as_parser_would_see_it(_golden(built)["S03_rate_overcharge"]), ledger)
    (finding,) = result.findings
    assert finding.evidence["item_code"] == "SENIOR_ENG"  # resolved from description alone
    assert finding.evidence["overcharge"] == "2400.00"


def test_ledger_connection_is_read_only(ledger: SqliteLedger) -> None:
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        ledger._conn.execute("DELETE FROM historical_payments")


def test_void_payments_are_not_duplicates(db: sqlite3.Connection, ledger: SqliteLedger) -> None:
    void = db.execute(
        "SELECT vendor_id, invoice_number FROM historical_payments WHERE status='VOID' LIMIT 1"
    ).fetchone()
    assert void is not None, "seed data should contain at least one VOID payment"
    assert ledger.payments_by_invoice_number(void["vendor_id"], void["invoice_number"]) == ()
