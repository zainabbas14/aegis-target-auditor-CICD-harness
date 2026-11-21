"""Deterministic audit core.

Two phases, deliberately separated:

    gather_facts(invoice, ledger) -> LedgerFacts     # I/O, read-only
    evaluate(invoice, facts)      -> AuditResult     # pure function, no I/O

In the agent graph (M3) the SQL agent's job is to produce LedgerFacts and the
Risk stage calls ``evaluate``. Because ``evaluate`` is pure, every rule is
unit-testable without a database, and the verdict is reproducible from the
logged inputs alone - which is what an auditor will ask for.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from datetime import timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from auditor.domain.enums import FindingCode, verdict_for
from auditor.domain.models import (
    AuditResult,
    ExtractedInvoice,
    ExtractedLineItem,
    Finding,
    LedgerFacts,
    RateCardEntry,
)
from auditor.ports import LedgerReader

logger = logging.getLogger(__name__)

RULES_VERSION = "2026.11-m1"


class RuleConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    near_duplicate_window_days: int = Field(default=30, gt=0)


# --------------------------------------------------------------------------- #
# Phase 1: fact gathering
# --------------------------------------------------------------------------- #
def gather_facts(inv: ExtractedInvoice, ledger: LedgerReader, cfg: RuleConfig | None = None) -> LedgerFacts:
    cfg = cfg or RuleConfig()
    vendor = ledger.vendor_by_tax_id(inv.vendor_tax_id)
    po = ledger.purchase_order(inv.po_number)
    if vendor is None:
        return LedgerFacts(vendor=None, purchase_order=po)

    window = timedelta(days=cfg.near_duplicate_window_days)
    return LedgerFacts(
        vendor=vendor,
        purchase_order=po,
        rate_card=ledger.rate_card(vendor.contract_ref) if vendor.contract_ref else (),
        same_number_payments=ledger.payments_by_invoice_number(vendor.vendor_id, inv.invoice_number),
        same_amount_payments=ledger.payments_by_amount(
            vendor.vendor_id, inv.total, inv.invoice_date - window, inv.invoice_date + window
        ),
    )


# --------------------------------------------------------------------------- #
# Phase 2: pure checks. Each returns zero or more Findings.
# --------------------------------------------------------------------------- #
Check = Callable[[ExtractedInvoice, LedgerFacts], Iterable[Finding]]


def _fmt(v: Decimal) -> str:
    return f"{v:,.2f}"


def check_vendor(inv: ExtractedInvoice, facts: LedgerFacts) -> Iterable[Finding]:
    if facts.vendor is None:
        yield Finding(
            code=FindingCode.VENDOR_UNKNOWN,
            message=f"No vendor on file with tax ID {inv.vendor_tax_id} (invoice names '{inv.vendor_name}').",
            evidence={"vendor_tax_id": inv.vendor_tax_id, "vendor_name": inv.vendor_name},
        )


def check_arithmetic(inv: ExtractedInvoice, _: LedgerFacts) -> Iterable[Finding]:
    for i, ln in enumerate(inv.line_items, start=1):
        expected = (ln.quantity * ln.unit_price).quantize(Decimal("0.01"))
        if expected != ln.amount:
            yield Finding(
                code=FindingCode.ARITHMETIC_MISMATCH,
                message=f"Line {i}: {ln.quantity} x {_fmt(ln.unit_price)} = {_fmt(expected)}, "
                f"invoice states {_fmt(ln.amount)}.",
                evidence={"line": str(i), "computed": str(expected), "stated": str(ln.amount)},
            )
    line_sum = sum((ln.amount for ln in inv.line_items), Decimal("0"))
    if line_sum != inv.subtotal:
        yield Finding(
            code=FindingCode.ARITHMETIC_MISMATCH,
            message=f"Line items sum to {_fmt(line_sum)} but stated subtotal is {_fmt(inv.subtotal)} "
            f"(difference {_fmt(inv.subtotal - line_sum)}).",
            evidence={"line_sum": str(line_sum), "stated_subtotal": str(inv.subtotal)},
        )
    if inv.subtotal + inv.tax != inv.total:
        yield Finding(
            code=FindingCode.ARITHMETIC_MISMATCH,
            message=f"Subtotal {_fmt(inv.subtotal)} + tax {_fmt(inv.tax)} != total {_fmt(inv.total)}.",
            evidence={"subtotal": str(inv.subtotal), "tax": str(inv.tax), "total": str(inv.total)},
        )


def _normalise(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def resolve_rate_entry(line: ExtractedLineItem, rate_card: tuple[RateCardEntry, ...]) -> RateCardEntry | None:
    """Deterministic resolution only. Exact code match, else unique prefix match
    on the normalised description. Anything ambiguous returns None and is
    escalated - an LLM resolver (M3) may propose a mapping, never silently apply one.
    """
    if line.item_code is not None:
        return next((e for e in rate_card if e.item_code == line.item_code), None)
    desc = _normalise(line.description)
    candidates = [e for e in rate_card if desc == (n := _normalise(e.description)) or desc.startswith(n + " ")]
    return candidates[0] if len(candidates) == 1 else None


def check_rates(inv: ExtractedInvoice, facts: LedgerFacts) -> Iterable[Finding]:
    if facts.vendor is None:
        return
    if not facts.rate_card:
        yield Finding(
            code=FindingCode.NO_CONTRACT,
            message=f"No contract rate card on file for {facts.vendor.legal_name}; prices cannot be verified.",
            evidence={"vendor_id": facts.vendor.vendor_id},
        )
        return
    for i, ln in enumerate(inv.line_items, start=1):
        entry = resolve_rate_entry(ln, facts.rate_card)
        if entry is None:
            yield Finding(
                code=FindingCode.UNRESOLVED_LINE_ITEM,
                message=f"Line {i} ('{ln.description}') could not be matched to the contract rate card.",
                evidence={"line": str(i), "description": ln.description, "item_code": ln.item_code or ""},
            )
        elif ln.unit_price > entry.max_unit_price:
            over = (ln.unit_price - entry.max_unit_price) * ln.quantity
            yield Finding(
                code=FindingCode.RATE_EXCEEDS_CONTRACT,
                message=f"Line {i} ({entry.item_code}): billed {_fmt(ln.unit_price)}/{entry.unit} vs contract "
                f"ceiling {_fmt(entry.max_unit_price)}/{entry.unit}; overcharge {_fmt(over)}.",
                evidence={
                    "line": str(i),
                    "item_code": entry.item_code,
                    "billed_unit_price": str(ln.unit_price),
                    "max_unit_price": str(entry.max_unit_price),
                    "overcharge": str(over.quantize(Decimal("0.01"))),
                },
            )


def check_duplicates(inv: ExtractedInvoice, facts: LedgerFacts) -> Iterable[Finding]:
    if facts.same_number_payments:
        p = facts.same_number_payments[0]
        yield Finding(
            code=FindingCode.DUPLICATE_INVOICE,
            message=f"Invoice {inv.invoice_number} already recorded as {p.status} "
            f"({_fmt(p.amount)} on {p.payment_date.isoformat()}).",
            evidence={
                "prior_status": p.status,
                "prior_amount": str(p.amount),
                "prior_date": p.payment_date.isoformat(),
            },
        )
        return  # an exact duplicate subsumes the fuzzy signal
    similar = [p for p in facts.same_amount_payments if p.invoice_number != inv.invoice_number]
    if similar:
        p = similar[0]
        gap = abs((inv.invoice_date - p.payment_date).days)
        yield Finding(
            code=FindingCode.POSSIBLE_DUPLICATE,
            message=f"Same vendor and amount ({_fmt(inv.total)}) as {p.invoice_number}, "
            f"{p.status} on {p.payment_date.isoformat()} ({gap} days apart).",
            evidence={
                "prior_invoice": p.invoice_number,
                "prior_date": p.payment_date.isoformat(),
                "days_apart": str(gap),
            },
        )


def check_purchase_order(inv: ExtractedInvoice, facts: LedgerFacts) -> Iterable[Finding]:
    po = facts.purchase_order
    if po is None:
        yield Finding(
            code=FindingCode.PO_UNKNOWN,
            message=f"Purchase order {inv.po_number} not found.",
            evidence={"po_number": inv.po_number},
        )
        return
    if facts.vendor is not None and po.vendor_id != facts.vendor.vendor_id:
        yield Finding(
            code=FindingCode.PO_VENDOR_MISMATCH,
            message=f"PO {po.po_number} belongs to vendor {po.vendor_id}, not {facts.vendor.vendor_id}.",
            evidence={"po_vendor": po.vendor_id, "invoice_vendor": facts.vendor.vendor_id},
        )
        return  # budget of someone else's PO is meaningless
    if inv.total > po.remaining:
        yield Finding(
            code=FindingCode.PO_BUDGET_EXCEEDED,
            message=f"Invoice total {_fmt(inv.total)} exceeds remaining PO budget {_fmt(po.remaining)} "
            f"on {po.po_number}.",
            evidence={"total": str(inv.total), "remaining": str(po.remaining), "po_number": po.po_number},
        )


def check_remit_to(inv: ExtractedInvoice, facts: LedgerFacts) -> Iterable[Finding]:
    if facts.vendor is not None and inv.remit_account != facts.vendor.remit_account:
        yield Finding(
            code=FindingCode.REMIT_TO_MISMATCH,
            message="Remit-to account on invoice differs from vendor master record. "
            "Verify by call-back to a contact on file before any payment.",
            # Only show tails of account identifiers in evidence/logs.
            evidence={"invoice_account_tail": inv.remit_account[-4:], "on_file_tail": facts.vendor.remit_account[-4:]},
        )


CHECKS: tuple[Check, ...] = (
    check_vendor,
    check_arithmetic,
    check_rates,
    check_duplicates,
    check_purchase_order,
    check_remit_to,
)


def evaluate(inv: ExtractedInvoice, facts: LedgerFacts) -> AuditResult:
    findings = tuple(
        sorted((f for check in CHECKS for f in check(inv, facts)), key=lambda f: (f.code.value, f.message))
    )
    result = AuditResult(
        invoice_number=inv.invoice_number,
        verdict=verdict_for({f.code for f in findings}),
        findings=findings,
        rules_version=RULES_VERSION,
    )
    logger.info(
        "Audited %s: verdict=%s findings=%s",
        inv.invoice_number,
        result.verdict,
        [f.code.value for f in findings],
    )
    return result


def audit(inv: ExtractedInvoice, ledger: LedgerReader, cfg: RuleConfig | None = None) -> AuditResult:
    """Convenience: the full deterministic path. This IS the fallback in M3."""
    return evaluate(inv, gather_facts(inv, ledger, cfg))
