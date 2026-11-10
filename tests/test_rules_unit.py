"""Pure-function tests: no database, no files. Each rule in isolation."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from auditor.domain.enums import SEVERITY, FindingCode, PaymentStatus, Verdict, verdict_for
from auditor.domain.models import (
    ExtractedInvoice,
    ExtractedLineItem,
    LedgerFacts,
    PaymentRecord,
    PurchaseOrderRecord,
    RateCardEntry,
    VendorRecord,
)
from auditor.rules.engine import evaluate, resolve_rate_entry

D = Decimal
VENDOR = VendorRecord(
    vendor_id="V-1", legal_name="Test Co", tax_id="TX-1", remit_account="ACCT-0001", contract_ref="C-1", status="ACTIVE"
)
CARD = (
    RateCardEntry(item_code="SENIOR_ENG", description="Senior Data Engineer", unit="hour", max_unit_price=D("120.00")),
    RateCardEntry(item_code="DATA_ENG", description="Data Engineer", unit="hour", max_unit_price=D("105.00")),
)
PO = PurchaseOrderRecord(
    po_number="PO-1", vendor_id="V-1", approved_budget=D("10000.00"), consumed=D("0.00"), status="OPEN"
)


def invoice(**overrides: Any) -> ExtractedInvoice:
    base: dict[str, Any] = {
        "invoice_number": "INV-1",
        "vendor_name": "Test Co",
        "vendor_tax_id": "TX-1",
        "po_number": "PO-1",
        "invoice_date": date(2026, 10, 1),
        "remit_account": "ACCT-0001",
        "line_items": [
            {
                "item_code": "DATA_ENG",
                "description": "Data Engineer",
                "quantity": "10",
                "unit_price": "100.00",
                "amount": "1000.00",
            },
        ],
        "subtotal": "1000.00",
        "total": "1000.00",
    }
    return ExtractedInvoice.model_validate(base | overrides)


def facts(**overrides: Any) -> LedgerFacts:
    base: dict[str, Any] = {"vendor": VENDOR, "purchase_order": PO, "rate_card": CARD}
    return LedgerFacts.model_validate(base | overrides)


def codes(inv: ExtractedInvoice, f: LedgerFacts) -> set[FindingCode]:
    return {x.code for x in evaluate(inv, f).findings}


# ---- baseline ---------------------------------------------------------------
def test_clean_invoice_routes_to_human_approval() -> None:
    result = evaluate(invoice(), facts())
    assert result.findings == ()
    assert result.verdict is Verdict.APPROVE_PENDING_HUMAN


# ---- fail-closed integrity --------------------------------------------------
def test_unknown_vendor_blocks() -> None:
    assert FindingCode.VENDOR_UNKNOWN in codes(invoice(), facts(vendor=None, rate_card=()))


def test_unknown_po_blocks() -> None:
    assert codes(invoice(), facts(purchase_order=None)) == {FindingCode.PO_UNKNOWN}


def test_po_belonging_to_other_vendor_blocks_and_skips_budget() -> None:
    other = PO.model_copy(update={"vendor_id": "V-999", "approved_budget": D("1.00")})
    assert codes(invoice(), facts(purchase_order=other)) == {FindingCode.PO_VENDOR_MISMATCH}


def test_missing_contract_blocks_rather_than_skipping_price_check() -> None:
    assert codes(invoice(), facts(rate_card=())) == {FindingCode.NO_CONTRACT}


# ---- arithmetic -------------------------------------------------------------
def test_line_level_math_error_is_caught_even_if_totals_agree() -> None:
    inv = invoice(
        line_items=[
            {
                "item_code": "DATA_ENG",
                "description": "Data Engineer",
                "quantity": "10",
                "unit_price": "100.00",
                "amount": "1100.00",
            }
        ],
        subtotal="1100.00",
        total="1100.00",
    )
    assert codes(inv, facts()) == {FindingCode.ARITHMETIC_MISMATCH}


def test_tax_total_mismatch() -> None:
    assert codes(invoice(tax="50.00", total="1060.00"), facts()) == {FindingCode.ARITHMETIC_MISMATCH}


# ---- rates ------------------------------------------------------------------
def test_price_at_ceiling_passes_and_one_cent_over_fails() -> None:
    at = invoice(
        line_items=[
            {"item_code": "DATA_ENG", "description": "x", "quantity": "1", "unit_price": "105.00", "amount": "105.00"}
        ],
        subtotal="105.00",
        total="105.00",
    )
    over = invoice(
        line_items=[
            {"item_code": "DATA_ENG", "description": "x", "quantity": "1", "unit_price": "105.01", "amount": "105.01"}
        ],
        subtotal="105.01",
        total="105.01",
    )
    assert codes(at, facts()) == set()
    assert codes(over, facts()) == {FindingCode.RATE_EXCEEDS_CONTRACT}


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("Senior Data Engineer - Oct 2026", "SENIOR_ENG"),
        ("Data Engineer - Sep 2026", "DATA_ENG"),  # 'data engineer' is inside 'senior data engineer' - prefix wins
        ("DATA ENGINEER", "DATA_ENG"),
        ("Engineering consultancy", None),  # no match -> escalate
        ("Principal Data Engineer", None),  # contains a card phrase but isn't it -> escalate, don't guess
    ],
)
def test_description_resolution(description: str, expected: str | None) -> None:
    line = ExtractedLineItem(description=description, quantity=D("1"), unit_price=D("1.00"), amount=D("1.00"))
    entry = resolve_rate_entry(line, CARD)
    assert (entry.item_code if entry else None) == expected


def test_unknown_item_code_is_unresolved_not_ignored() -> None:
    inv = invoice(
        line_items=[
            {"item_code": "GOLD_PLATING", "description": "x", "quantity": "1", "unit_price": "1.00", "amount": "1.00"}
        ],
        subtotal="1.00",
        total="1.00",
    )
    result = evaluate(inv, facts())
    assert {f.code for f in result.findings} == {FindingCode.UNRESOLVED_LINE_ITEM}
    assert result.verdict is Verdict.REVIEW


# ---- duplicates -------------------------------------------------------------
def _payment(number: str, amount: str = "1000.00") -> PaymentRecord:
    return PaymentRecord(
        invoice_number=number,
        vendor_id="V-1",
        po_number="PO-1",
        amount=D(amount),
        payment_date=date(2026, 9, 20),
        status=PaymentStatus.PAID,
    )


def test_exact_duplicate_subsumes_possible_duplicate() -> None:
    f = facts(same_number_payments=[_payment("INV-1")], same_amount_payments=[_payment("INV-1"), _payment("INV-0")])
    assert codes(invoice(), f) == {FindingCode.DUPLICATE_INVOICE}


def test_same_amount_different_number_is_review_only() -> None:
    result = evaluate(invoice(), facts(same_amount_payments=[_payment("INV-0")]))
    assert result.verdict is Verdict.REVIEW
    assert result.findings[0].evidence["days_apart"] == "11"


# ---- remit-to ---------------------------------------------------------------
def test_remit_change_blocks_and_evidence_never_contains_full_account() -> None:
    result = evaluate(invoice(remit_account="ATTACKER-ACCT-9999"), facts())
    (f,) = result.findings
    assert f.code is FindingCode.REMIT_TO_MISMATCH
    assert "ATTACKER" not in str(f.evidence)
    assert f.evidence["invoice_account_tail"] == "9999"


# ---- domain invariants ------------------------------------------------------
def test_every_code_has_a_severity() -> None:
    assert set(SEVERITY) == set(FindingCode)


def test_worst_severity_wins() -> None:
    assert verdict_for({FindingCode.POSSIBLE_DUPLICATE, FindingCode.REMIT_TO_MISMATCH}) is Verdict.BLOCK


def test_inconsistent_invoice_still_parses() -> None:
    """Shape validation must not reject the fraud we're trying to detect."""
    inv = invoice(subtotal="9999.99", total="1.00")
    assert inv.subtotal == D("9999.99")


@pytest.mark.parametrize("bad", ["-1.00", "1.005", "NaN"])
def test_money_shape_is_enforced(bad: str) -> None:
    with pytest.raises(ValidationError):
        invoice(total=bad)
