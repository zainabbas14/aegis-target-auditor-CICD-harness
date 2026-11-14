"""Single source of truth for the auditor's mock data.

Every artifact (invoice PDFs, contract PDFs, the ERP database, and the golden
expected-outcomes file) is rendered from the specs in this module. Nothing is
hand-edited downstream, so the three data modalities cannot drift apart.

Domain enums are imported from ``auditor.domain``: test data speaks the
production vocabulary, never a parallel copy of it.

Monetary values are ``Decimal`` here and integer minor units (cents) in the DB.
Floats never touch money.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from auditor.domain.enums import FindingCode, PaymentStatus, Verdict, verdict_for

CENT = Decimal("0.01")


def money(value: Decimal) -> Decimal:
    """Quantize to cents using half-up rounding (invoice convention)."""
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------------- #
# Master data
# --------------------------------------------------------------------------- #
class Vendor(_Frozen):
    vendor_id: str
    legal_name: str
    tax_id: str
    address_lines: tuple[str, ...]
    remit_account: str  # obviously-fake account identifier
    contract_ref: str | None
    layout: str = Field(description="Invoice template variant; prevents parser overfitting")


class RateCardLine(_Frozen):
    item_code: str
    description: str
    unit: str
    max_unit_price: Decimal


class Contract(_Frozen):
    contract_ref: str
    vendor_id: str
    title: str
    effective_date: date
    expiry_date: date
    payment_terms_days: int
    currency: str = "USD"
    rate_card: tuple[RateCardLine, ...]

    def rate_for(self, item_code: str) -> RateCardLine:
        for line in self.rate_card:
            if line.item_code == item_code:
                return line
        raise KeyError(f"{item_code} not on rate card {self.contract_ref}")


class PurchaseOrder(_Frozen):
    po_number: str
    vendor_id: str
    issued_on: date
    headroom: Decimal = Field(
        description="Budget remaining AFTER all seeded payments. approved_budget is "
        "derived at seed time as sum(consumed payments) + headroom."
    )


class Payment(_Frozen):
    invoice_number: str
    vendor_id: str
    po_number: str | None
    amount: Decimal
    payment_date: date
    status: PaymentStatus = PaymentStatus.PAID


# --------------------------------------------------------------------------- #
# Invoices & scenarios
# --------------------------------------------------------------------------- #
class InvoiceLine(_Frozen):
    item_code: str
    description: str
    quantity: Decimal
    unit_price: Decimal

    @computed_field  # type: ignore[prop-decorator]
    @property
    def amount(self) -> Decimal:
        return money(self.quantity * self.unit_price)


class InvoiceSpec(_Frozen):
    invoice_number: str
    vendor_id: str
    po_number: str
    invoice_date: date
    terms_days: int
    lines: tuple[InvoiceLine, ...]
    tax_rate: Decimal = Decimal("0")
    # Fault-injection hooks. None = print the arithmetically correct value.
    printed_subtotal_override: Decimal | None = None
    remit_account_override: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def due_date(self) -> date:
        return self.invoice_date + timedelta(days=self.terms_days)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def true_subtotal(self) -> Decimal:
        return money(sum((ln.amount for ln in self.lines), Decimal("0")))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def printed_subtotal(self) -> Decimal:
        return self.printed_subtotal_override or self.true_subtotal

    @computed_field  # type: ignore[prop-decorator]
    @property
    def printed_tax(self) -> Decimal:
        # Tax is computed on the *printed* subtotal, as a sloppy/malicious
        # vendor system would. The total is therefore internally consistent
        # with the header, only the line items betray the error.
        return money(self.printed_subtotal * self.tax_rate)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def printed_total(self) -> Decimal:
        return money(self.printed_subtotal + self.printed_tax)


class Scenario(_Frozen):
    scenario_id: str
    title: str
    invoice: InvoiceSpec
    expected_verdict: Verdict
    expected_findings: frozenset[FindingCode]
    rationale: str

    @model_validator(mode="after")
    def _verdict_consistent(self) -> Scenario:
        derived = verdict_for(self.expected_findings)
        if derived is not self.expected_verdict:
            raise ValueError(f"{self.scenario_id}: findings imply {derived}, spec says {self.expected_verdict}")
        return self


# --------------------------------------------------------------------------- #
# The world
# --------------------------------------------------------------------------- #
BUYER_NAME = "Arclight Holdings (Pvt) Ltd"
BUYER_ADDRESS = ("Accounts Payable", "Plot 14, Sector 7", "Karachi 75500, Pakistan")

D = Decimal

VENDORS: tuple[Vendor, ...] = (
    Vendor(
        vendor_id="V-1001",
        legal_name="Halcyon Data Systems Ltd.",
        tax_id="HDS-TX-4471902",
        address_lines=("88 Quayside Road", "Leeds LS1 4AP", "United Kingdom"),
        remit_account="FAKE-GB00-HALC-0000-1001",
        contract_ref="MSA-HDS-2025-014",
        layout="services_classic",
    ),
    Vendor(
        vendor_id="V-1002",
        legal_name="Meridian Industrial Supply Inc.",
        tax_id="MIS-TX-0938815",
        address_lines=("1200 Foundry Ave, Suite 300", "Cleveland, OH 44114", "USA"),
        remit_account="FAKE-US00-MERI-0000-1002",
        contract_ref="SUPPLY-MIS-2024-007",
        layout="goods_modern",
    ),
    Vendor(
        vendor_id="V-1003",
        legal_name="Kestrel Freight Co.",
        tax_id="KFC-TX-5520467",
        address_lines=("Unit 9, Port Qasim Logistics Park", "Karachi 75020", "Pakistan"),
        remit_account="FAKE-PK00-KEST-0000-1003",
        contract_ref="MSA-KFC-2025-003",
        layout="freight_compact",
    ),
)

CONTRACTS: tuple[Contract, ...] = (
    Contract(
        contract_ref="MSA-HDS-2025-014",
        vendor_id="V-1001",
        title="Master Services Agreement - Data Engineering Services",
        effective_date=date(2025, 7, 1),
        expiry_date=date(2027, 6, 30),
        payment_terms_days=30,
        rate_card=(
            RateCardLine(
                item_code="SENIOR_ENG", description="Senior Data Engineer", unit="hour", max_unit_price=D("120.00")
            ),
            RateCardLine(item_code="DATA_ENG", description="Data Engineer", unit="hour", max_unit_price=D("105.00")),
            RateCardLine(
                item_code="SUPPORT_RET",
                description="Platform Support Retainer",
                unit="month",
                max_unit_price=D("4000.00"),
            ),
        ),
    ),
    Contract(
        contract_ref="SUPPLY-MIS-2024-007",
        vendor_id="V-1002",
        title="Supply Agreement - Warehouse Equipment and Consumables",
        effective_date=date(2024, 11, 1),
        expiry_date=date(2027, 10, 31),
        payment_terms_days=45,
        rate_card=(
            RateCardLine(
                item_code="PALLET_RACK",
                description="Selective pallet rack bay",
                unit="unit",
                max_unit_price=D("340.00"),
            ),
            RateCardLine(
                item_code="SAFETY_KIT", description="PPE safety kit (Class 2)", unit="unit", max_unit_price=D("85.00")
            ),
            RateCardLine(
                item_code="FORKLIFT_SVC",
                description="Forklift preventive service",
                unit="visit",
                max_unit_price=D("650.00"),
            ),
        ),
    ),
    Contract(
        contract_ref="MSA-KFC-2025-003",
        vendor_id="V-1003",
        title="Master Services Agreement - Freight and Haulage",
        effective_date=date(2025, 3, 1),
        expiry_date=date(2027, 2, 28),
        payment_terms_days=30,
        rate_card=(
            RateCardLine(
                item_code="FTL_LANE",
                description="Full truckload, KHI-LHE lane",
                unit="trip",
                max_unit_price=D("2200.00"),
            ),
            RateCardLine(
                item_code="LTL_LANE",
                description="Less-than-truckload, KHI-LHE lane",
                unit="trip",
                max_unit_price=D("780.00"),
            ),
            RateCardLine(item_code="FUEL_SURCH", description="Fuel surcharge", unit="trip", max_unit_price=D("180.00")),
        ),
    ),
)

PURCHASE_ORDERS: tuple[PurchaseOrder, ...] = (
    PurchaseOrder(po_number="PO-2026-0418", vendor_id="V-1001", issued_on=date(2026, 1, 12), headroom=D("50000.00")),
    PurchaseOrder(po_number="PO-2026-0422", vendor_id="V-1002", issued_on=date(2026, 1, 20), headroom=D("38000.00")),
    PurchaseOrder(po_number="PO-2026-0431", vendor_id="V-1003", issued_on=date(2026, 2, 3), headroom=D("11400.00")),
    PurchaseOrder(po_number="PO-2026-0457", vendor_id="V-1002", issued_on=date(2026, 6, 1), headroom=D("2500.00")),
)

# Payments that specific scenarios depend on. Background noise is added by
# seed_db.py with a fixed RNG seed and guaranteed not to collide with these.
SCENARIO_PAYMENTS: tuple[Payment, ...] = (
    Payment(
        invoice_number="KFC-9981",
        vendor_id="V-1003",
        po_number="PO-2026-0431",
        amount=D("8800.00"),
        payment_date=date(2026, 9, 18),
    ),
    Payment(
        invoice_number="HDS-10249",
        vendor_id="V-1001",
        po_number="PO-2026-0418",
        amount=D("4000.00"),
        payment_date=date(2026, 10, 9),
    ),
)

_HDS, _MIS, _KFC = "V-1001", "V-1002", "V-1003"


def _ln(code: str, desc: str, qty: str, price: str) -> InvoiceLine:
    return InvoiceLine(item_code=code, description=desc, quantity=D(qty), unit_price=D(price))


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        scenario_id="S01_clean_services",
        title="Clean pass - services within rate card",
        invoice=InvoiceSpec(
            invoice_number="HDS-10233",
            vendor_id=_HDS,
            po_number="PO-2026-0418",
            invoice_date=date(2026, 10, 2),
            terms_days=30,
            lines=(
                _ln("SENIOR_ENG", "Senior Data Engineer - Sep 2026", "60", "120.00"),
                _ln("DATA_ENG", "Data Engineer - Sep 2026", "40", "105.00"),
            ),
        ),
        expected_verdict=Verdict.APPROVE_PENDING_HUMAN,
        expected_findings=frozenset(),
        rationale="All rates at contract ceiling (not above), new invoice number, PO headroom ample.",
    ),
    Scenario(
        scenario_id="S02_clean_goods_with_tax",
        title="Clean pass - goods with sales tax",
        invoice=InvoiceSpec(
            invoice_number="MIS-55810",
            vendor_id=_MIS,
            po_number="PO-2026-0422",
            invoice_date=date(2026, 10, 6),
            terms_days=45,
            tax_rate=D("0.05"),
            lines=(
                _ln("PALLET_RACK", "Selective pallet rack bay", "20", "340.00"),
                _ln("SAFETY_KIT", "PPE safety kit (Class 2)", "30", "82.50"),
            ),
        ),
        expected_verdict=Verdict.APPROVE_PENDING_HUMAN,
        expected_findings=frozenset(),
        rationale="Safety kits billed BELOW ceiling - an under-charge must not be flagged.",
    ),
    Scenario(
        scenario_id="S03_rate_overcharge",
        title="Rate anomaly - hourly rate above contract ceiling",
        invoice=InvoiceSpec(
            invoice_number="HDS-10241",
            vendor_id=_HDS,
            po_number="PO-2026-0418",
            invoice_date=date(2026, 10, 14),
            terms_days=30,
            lines=(_ln("SENIOR_ENG", "Senior Data Engineer - Oct 2026 (1st half)", "80", "150.00"),),
        ),
        expected_verdict=Verdict.BLOCK,
        expected_findings=frozenset({FindingCode.RATE_EXCEEDS_CONTRACT}),
        rationale="150.00/hr vs 120.00/hr ceiling in Exhibit A: overcharge of 2,400.00 on 80 hrs.",
    ),
    Scenario(
        scenario_id="S04_exact_duplicate",
        title="Duplicate billing - invoice number already PAID",
        invoice=InvoiceSpec(
            invoice_number="KFC-9981",
            vendor_id=_KFC,
            po_number="PO-2026-0431",
            invoice_date=date(2026, 10, 20),
            terms_days=30,
            lines=(_ln("FTL_LANE", "Full truckload, KHI-LHE lane", "4", "2200.00"),),
        ),
        expected_verdict=Verdict.BLOCK,
        expected_findings=frozenset({FindingCode.DUPLICATE_INVOICE}),
        rationale="KFC-9981 for 8,800.00 was PAID on 2026-09-18. Re-dated resubmission.",
    ),
    Scenario(
        scenario_id="S05_arithmetic_mismatch",
        title="Math mismatch - subtotal does not equal sum of lines",
        invoice=InvoiceSpec(
            invoice_number="MIS-55902",
            vendor_id=_MIS,
            po_number="PO-2026-0422",
            invoice_date=date(2026, 10, 21),
            terms_days=45,
            tax_rate=D("0.05"),
            lines=(
                _ln("SAFETY_KIT", "PPE safety kit (Class 2)", "40", "85.00"),
                _ln("FORKLIFT_SVC", "Forklift preventive service", "2", "650.00"),
            ),
            printed_subtotal_override=D("4970.00"),
        ),
        expected_verdict=Verdict.BLOCK,
        expected_findings=frozenset({FindingCode.ARITHMETIC_MISMATCH}),
        rationale="Lines sum to 4,700.00; header says 4,970.00 (digit transposition). Tax and "
        "total are consistent with the wrong header, so only line-level recomputation catches it.",
    ),
    Scenario(
        scenario_id="S06_po_budget_exceeded",
        title="PO budget exceeded",
        invoice=InvoiceSpec(
            invoice_number="MIS-55917",
            vendor_id=_MIS,
            po_number="PO-2026-0457",
            invoice_date=date(2026, 10, 23),
            terms_days=45,
            tax_rate=D("0.05"),
            lines=(_ln("PALLET_RACK", "Selective pallet rack bay", "10", "340.00"),),
        ),
        expected_verdict=Verdict.BLOCK,
        expected_findings=frozenset({FindingCode.PO_BUDGET_EXCEEDED}),
        rationale="Total 3,570.00 against 2,500.00 remaining on PO-2026-0457.",
    ),
    Scenario(
        scenario_id="S07_remit_to_changed",
        title="Bank-detail change - remit-to differs from vendor master (BEC pattern)",
        invoice=InvoiceSpec(
            invoice_number="KFC-10012",
            vendor_id=_KFC,
            po_number="PO-2026-0431",
            invoice_date=date(2026, 10, 26),
            terms_days=30,
            lines=(
                _ln("LTL_LANE", "Less-than-truckload, KHI-LHE lane", "3", "780.00"),
                _ln("FUEL_SURCH", "Fuel surcharge", "3", "180.00"),
            ),
            remit_account_override="FAKE-AE00-XXXX-7731-0090",
        ),
        expected_verdict=Verdict.BLOCK,
        expected_findings=frozenset({FindingCode.REMIT_TO_MISMATCH}),
        rationale="Everything else is clean. Remit-to account is not the one on file - the "
        "classic business-email-compromise signal. Must be verified out-of-band.",
    ),
    Scenario(
        scenario_id="S08_near_duplicate",
        title="Possible duplicate - same vendor and amount, new number, 5 days apart",
        invoice=InvoiceSpec(
            invoice_number="HDS-10250",
            vendor_id=_HDS,
            po_number="PO-2026-0418",
            invoice_date=date(2026, 10, 14),
            terms_days=30,
            lines=(_ln("SUPPORT_RET", "Platform Support Retainer - Oct 2026", "1", "4000.00"),),
        ),
        expected_verdict=Verdict.REVIEW,
        expected_findings=frozenset({FindingCode.POSSIBLE_DUPLICATE}),
        rationale="HDS-10249 (4,000.00) paid 5 days earlier. A monthly retainer billed twice in "
        "one month is suspicious but not proof - soft finding, route to analyst.",
    ),
    Scenario(
        scenario_id="S09_unmatched_line_item",
        title="Unmatched line - abbreviated description the rules can't map to the rate card",
        invoice=InvoiceSpec(
            invoice_number="HDS-10262",
            vendor_id=_HDS,
            po_number="PO-2026-0418",
            invoice_date=date(2026, 10, 28),
            terms_days=30,
            lines=(_ln("SENIOR_ENG", "Sr. Data Eng. consulting - Oct 2026 (2nd half)", "70", "120.00"),),
        ),
        expected_verdict=Verdict.REVIEW,
        expected_findings=frozenset({FindingCode.UNRESOLVED_LINE_ITEM}),
        rationale="The layout prints no codes and 'Sr. Data Eng.' doesn't match 'Senior Data Engineer'. The rules "
        "escalate instead of guessing; once a human confirms SENIOR_ENG the rate (120.00) is at ceiling and clean.",
    ),
)


def vendor_by_id(vendor_id: str) -> Vendor:
    return next(v for v in VENDORS if v.vendor_id == vendor_id)


def contract_by_ref(ref: str) -> Contract:
    return next(c for c in CONTRACTS if c.contract_ref == ref)


def reserved_invoice_numbers() -> frozenset[str]:
    """Invoice numbers background noise must never reuse."""
    return frozenset({s.invoice.invoice_number for s in SCENARIOS} | {p.invoice_number for p in SCENARIO_PAYMENTS})
