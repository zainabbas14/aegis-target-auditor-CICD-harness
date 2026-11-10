"""Typed contracts between pipeline stages.

ExtractedInvoice  -- what the Document Parser read off the page (M3: LLM output)
LedgerFacts       -- what the SQL stage established from the ERP
AuditResult       -- what the rule engine concluded

Design rule: models validate SHAPE, never business SEMANTICS. An invoice whose
subtotal doesn't match its lines must still parse cleanly. Otherwise the
pipeline would reject the exact fraud it exists to detect with a
ValidationError instead of a Finding.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, computed_field

from auditor.domain.enums import SEVERITY, FindingCode, PaymentStatus, Severity, Verdict

CENT = Decimal("0.01")


def _two_dp(v: Decimal) -> Decimal:
    if v != v.quantize(CENT):
        raise ValueError(f"monetary value {v} has more than 2 decimal places")
    return v


Money = Annotated[Decimal, Field(ge=0, allow_inf_nan=False), AfterValidator(_two_dp)]
Quantity = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]


def cents_to_money(cents: int) -> Decimal:
    return (Decimal(cents) / 100).quantize(CENT)


def money_to_cents(value: Decimal) -> int:
    return int(_two_dp(value) * 100)


class Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


# --------------------------------------------------------------------------- #
# Stage 1 output: the parsed document
# --------------------------------------------------------------------------- #
class ExtractedLineItem(Model):
    item_code: str | None = Field(default=None, description="Only present if printed on the invoice")
    description: str = Field(min_length=1)
    quantity: Quantity
    unit_price: Money
    amount: Money


class ExtractedInvoice(Model):
    invoice_number: str = Field(min_length=1)
    vendor_name: str
    vendor_tax_id: str = Field(min_length=1)
    po_number: str = Field(min_length=1)
    invoice_date: date
    due_date: date | None = None
    remit_account: str = Field(min_length=1)
    line_items: tuple[ExtractedLineItem, ...] = Field(min_length=1)
    subtotal: Money
    tax: Money = Decimal("0.00")
    total: Money
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")


# --------------------------------------------------------------------------- #
# Stage 2 output: ledger facts (read-only view of the ERP)
# --------------------------------------------------------------------------- #
class VendorRecord(Model):
    vendor_id: str
    legal_name: str
    tax_id: str
    remit_account: str
    contract_ref: str | None
    status: str


class PurchaseOrderRecord(Model):
    po_number: str
    vendor_id: str
    approved_budget: Money
    consumed: Money
    status: str

    @computed_field  # type: ignore[prop-decorator]
    @property
    def remaining(self) -> Decimal:
        return self.approved_budget - self.consumed


class RateCardEntry(Model):
    item_code: str
    description: str
    unit: str
    max_unit_price: Money


class PaymentRecord(Model):
    invoice_number: str
    vendor_id: str
    po_number: str | None
    amount: Money
    payment_date: date
    status: PaymentStatus


class LedgerFacts(Model):
    """Everything the rules need, gathered up front. Rules never touch the DB."""

    vendor: VendorRecord | None
    purchase_order: PurchaseOrderRecord | None
    rate_card: tuple[RateCardEntry, ...] = ()
    same_number_payments: tuple[PaymentRecord, ...] = ()
    same_amount_payments: tuple[PaymentRecord, ...] = ()


# --------------------------------------------------------------------------- #
# Stage 3 output: the audit decision
# --------------------------------------------------------------------------- #
class Finding(Model):
    code: FindingCode
    message: str
    # Structured, JSON-safe evidence for the LLM explainer and the audit log.
    evidence: dict[str, str] = Field(default_factory=dict)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def severity(self) -> Severity:
        return SEVERITY[self.code]


class AuditResult(Model):
    invoice_number: str
    verdict: Verdict
    findings: tuple[Finding, ...]
    rules_version: str

    @property
    def codes(self) -> frozenset[FindingCode]:
        return frozenset(f.code for f in self.findings)
