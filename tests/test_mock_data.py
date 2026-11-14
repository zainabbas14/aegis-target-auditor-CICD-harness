"""Guard rails for the mock data itself.

If the fixtures are wrong, every downstream eval is meaningless. These tests
prove that the PDFs contain what the golden file says they contain, that the
scenario mix covers every business finding, and that the build is
reproducible. Whether the *engine* reproduces the golden verdicts is tested in
test_engine_golden.py.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pdfplumber
import pytest

from auditor.domain.enums import BUSINESS_FINDINGS
from auditor.rules.engine import RuleConfig
from tests.mock_data.build import build
from tests.mock_data.fixtures import SCENARIO_PAYMENTS, SCENARIOS, Scenario, contract_by_ref, vendor_by_id


def test_scenario_mix(built: Path) -> None:
    golden = json.loads((built / "expected_outcomes.json").read_text())
    verdicts = [s["expected_verdict"] for s in golden["scenarios"]]
    assert verdicts.count("APPROVE_PENDING_HUMAN") >= 2
    assert verdicts.count("BLOCK") >= 3
    covered = {f for s in golden["scenarios"] for f in s["expected_findings"]}
    assert covered >= {f.value for f in BUSINESS_FINDINGS}, "every business finding needs a scenario"


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.scenario_id)
def test_invoice_pdf_contains_ground_truth(scenario: Scenario, built: Path) -> None:
    golden = next(
        g
        for g in json.loads((built / "expected_outcomes.json").read_text())["scenarios"]
        if g["scenario_id"] == scenario.scenario_id
    )["expected_extraction"]
    with pdfplumber.open(built / "invoices" / f"{scenario.scenario_id}.pdf") as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    for key in ("invoice_number", "po_number", "vendor_tax_id", "remit_account"):
        assert golden[key] in text, f"{key}={golden[key]!r} missing"
    assert f"{Decimal(golden['total']):,.2f}" in text


def test_contract_pdfs_contain_rate_ceilings(built: Path) -> None:
    for ref in {v.contract_ref for v in map(vendor_by_id, ("V-1001", "V-1002", "V-1003"))}:
        assert ref is not None
        with pdfplumber.open(built / "contracts" / f"{ref}.pdf") as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        for rate in contract_by_ref(ref).rate_card:
            assert rate.item_code in text
            assert f"{rate.max_unit_price:,.2f}" in text


def _digest(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.suffix == ".db":
            conn = sqlite3.connect(p)
            out[p.name] = hashlib.sha256("\n".join(conn.iterdump()).encode()).hexdigest()
            conn.close()
        elif p.is_file():
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def test_build_is_reproducible(built: Path, tmp_path: Path) -> None:
    build(tmp_path)
    assert _digest(tmp_path) == _digest(built)


def test_no_noise_in_near_duplicate_windows(db: sqlite3.Connection) -> None:
    """Noise must never accidentally create a finding the golden file doesn't expect."""
    window = timedelta(days=RuleConfig().near_duplicate_window_days)
    start = min(s.invoice.invoice_date for s in SCENARIOS) - window
    end = max(s.invoice.invoice_date for s in SCENARIOS) + window
    rows = db.execute(
        "SELECT invoice_number FROM historical_payments WHERE vendor_id IN ('V-1001','V-1002','V-1003') "
        "AND payment_date BETWEEN ? AND ?",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    assert {r["invoice_number"] for r in rows} <= {p.invoice_number for p in SCENARIO_PAYMENTS}
