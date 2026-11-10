"""Guard rails for the mock data itself.

If the fixtures are wrong, every downstream eval is meaningless. These tests
prove that (a) a plain-SQL reference oracle reproduces exactly the expected
findings for every scenario, (b) the PDFs actually contain what the golden
file says they contain, and (c) the build is reproducible.

The oracle below is intentionally dumb and deterministic. It is also the
starting point for the agent graph's rule-based fallback path.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pdfplumber
import pytest

from tests.mock_data.build import build
from tests.mock_data.fixtures import (
    SCENARIOS,
    FindingCode,
    Scenario,
    contract_by_ref,
    vendor_by_id,
)
from tests.mock_data.seed_db import to_cents

NEAR_DUP_WINDOW = timedelta(days=30)


@pytest.fixture(scope="session")
def built(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("mock")
    build(out)
    return out


@pytest.fixture(scope="session")
def db(built: Path) -> sqlite3.Connection:
    # Read-only URI: the oracle has no business writing to the ledger.
    conn = sqlite3.connect(f"file:{built / 'enterprise_erp.db'}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def oracle(s: Scenario, db: sqlite3.Connection) -> set[FindingCode]:
    inv = s.invoice
    vendor = vendor_by_id(inv.vendor_id)
    findings: set[FindingCode] = set()
    total_cents = to_cents(inv.printed_total)

    line_sum = sum((ln.amount for ln in inv.lines), Decimal("0"))
    if line_sum != inv.printed_subtotal or inv.printed_subtotal + inv.printed_tax != inv.printed_total:
        findings.add(FindingCode.ARITHMETIC_MISMATCH)

    assert vendor.contract_ref is not None
    contract = contract_by_ref(vendor.contract_ref)
    if any(ln.unit_price > contract.rate_for(ln.item_code).max_unit_price for ln in inv.lines):
        findings.add(FindingCode.RATE_EXCEEDS_CONTRACT)

    if db.execute(
        "SELECT 1 FROM historical_payments WHERE vendor_id=? AND invoice_number=? AND status!='VOID'",
        (inv.vendor_id, inv.invoice_number),
    ).fetchone():
        findings.add(FindingCode.DUPLICATE_INVOICE)
    elif db.execute(
        "SELECT 1 FROM historical_payments WHERE vendor_id=? AND amount_cents=? AND status!='VOID' "
        "AND invoice_number!=? AND payment_date BETWEEN ? AND ?",
        (inv.vendor_id, total_cents, inv.invoice_number,
         (inv.invoice_date - NEAR_DUP_WINDOW).isoformat(), inv.invoice_date.isoformat()),
    ).fetchone():
        findings.add(FindingCode.POSSIBLE_DUPLICATE)

    remaining = db.execute(
        "SELECT remaining_cents FROM po_balances WHERE po_number=?", (inv.po_number,)
    ).fetchone()["remaining_cents"]
    if total_cents > remaining:
        findings.add(FindingCode.PO_BUDGET_EXCEEDED)

    on_file = db.execute(
        "SELECT remit_account FROM vendors WHERE vendor_id=?", (inv.vendor_id,)
    ).fetchone()["remit_account"]
    if (inv.remit_account_override or vendor.remit_account) != on_file:
        findings.add(FindingCode.REMIT_TO_MISMATCH)

    return findings


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.scenario_id)
def test_oracle_reproduces_expected_findings(scenario: Scenario, db: sqlite3.Connection) -> None:
    assert oracle(scenario, db) == set(scenario.expected_findings)


def test_scenario_mix(built: Path) -> None:
    golden = json.loads((built / "expected_outcomes.json").read_text())
    verdicts = [s["expected_verdict"] for s in golden["scenarios"]]
    assert verdicts.count("APPROVE_PENDING_HUMAN") >= 2
    assert verdicts.count("BLOCK") >= 3
    covered = {f for s in golden["scenarios"] for f in s["expected_findings"]}
    assert covered == {f.value for f in FindingCode}, "every finding code needs a scenario"


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.scenario_id)
def test_invoice_pdf_contains_ground_truth(scenario: Scenario, built: Path) -> None:
    golden = next(
        g for g in json.loads((built / "expected_outcomes.json").read_text())["scenarios"]
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
            assert rate.item_code in text and f"{rate.max_unit_price:,.2f}" in text


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
    earliest = min(s.invoice.invoice_date for s in SCENARIOS) - NEAR_DUP_WINDOW
    rows = db.execute(
        "SELECT invoice_number FROM historical_payments WHERE vendor_id IN ('V-1001','V-1002','V-1003') "
        "AND payment_date >= ?", (earliest.isoformat(),),
    ).fetchall()
    assert {r["invoice_number"] for r in rows} <= {"KFC-9981", "HDS-10249"}
    assert date.fromisoformat("2026-01-01") < earliest
