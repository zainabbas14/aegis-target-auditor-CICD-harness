"""Render synthetic vendor contracts to PDF.

Clause structure follows the CUAD label taxonomy (Parties, Effective Date,
Expiration Date, Renewal Term, Governing Law, Audit Rights, Cap on Liability,
Termination for Convenience, Price Restrictions, ...) so the contract agent
can later be evaluated on real CUAD documents with the same extraction schema.

The pricing ceiling lives in Exhibit A, *not* in the body - mirroring real
agreements where the operative number is several pages away from the clause
that makes it binding.
"""

from __future__ import annotations

import logging
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Flowable, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from tests.mock_data.fixtures import BUYER_NAME, Contract, vendor_by_id

logger = logging.getLogger(__name__)

_SS = getSampleStyleSheet()
_TITLE = ParagraphStyle("t", parent=_SS["Title"], fontSize=15, leading=19)
_H = ParagraphStyle("h", parent=_SS["Heading3"], fontSize=10.5, spaceBefore=8, spaceAfter=3)
_P = ParagraphStyle("p", parent=_SS["Normal"], fontSize=9.5, leading=13.5, alignment=TA_JUSTIFY, spaceAfter=5)
_C = ParagraphStyle("c", parent=_P, alignment=TA_CENTER)


def _long_date(d: object) -> str:
    return d.strftime("%B %-d, %Y")  # type: ignore[attr-defined]


def _clauses(c: Contract, supplier: str) -> list[tuple[str, list[str]]]:
    return [
        ("1. Definitions", [
            f'1.1 "Agreement" means this {c.title.split(" - ")[0]} together with all Exhibits and '
            "Statements of Work executed hereunder.",
            '1.2 "Deliverables" means all goods, services and work product supplied by Supplier '
            "under this Agreement.",
            '1.3 "Rate Card" means the schedule of maximum unit prices set out in Exhibit A.',
        ]),
        ("2. Term", [
            f"2.1 This Agreement commences on {_long_date(c.effective_date)} (the \"Effective Date\") "
            f"and, unless terminated earlier, continues until {_long_date(c.expiry_date)}.",
            "2.2 The parties may renew this Agreement for successive twelve (12) month periods by "
            "mutual written agreement executed no later than sixty (60) days before expiry.",
        ]),
        ("3. Fees and Pricing", [
            "3.1 In consideration of the Deliverables, Customer shall pay Supplier the fees "
            "properly invoiced in accordance with this Agreement.",
            "3.2 <b>Price Restrictions.</b> Fees charged for any item listed on the Rate Card shall "
            "not exceed the maximum unit price stated in Exhibit A. Rates are fixed for the Term and "
            "may be varied only by a written amendment signed by authorised signatories of both "
            "parties. Any amount invoiced in excess of the Rate Card is not payable.",
            "3.3 Items not listed on the Rate Card require a prior written quotation accepted by "
            "Customer's procurement function and referenced on the relevant Purchase Order.",
        ]),
        ("4. Invoicing and Payment", [
            "4.1 Supplier shall invoice in arrears. Each invoice must state a unique invoice "
            "number, the Customer Purchase Order number, an itemised breakdown of quantities and "
            "unit prices, applicable taxes shown separately, and Supplier's tax registration number.",
            f"4.2 Undisputed amounts are payable within {c.payment_terms_days} days of receipt of a "
            f"valid invoice, in {c.currency}, to the bank account registered in Customer's vendor master "
            "file.",
            "4.3 <b>Change of bank details.</b> Customer shall not be obliged to pay to any account "
            "other than the account on record. Any request to change remittance details must be "
            "submitted on Supplier letterhead and will be verified by Customer by telephone call-back "
            "to a contact on record before taking effect.",
            "4.4 Supplier shall not submit any invoice for amounts previously invoiced. Customer may "
            "set off any duplicate or erroneous payment against future sums due.",
            "4.5 Customer may withhold payment of any amount disputed in good faith, provided it "
            "notifies Supplier of the dispute within fifteen (15) days of receipt of the invoice.",
        ]),
        ("5. Audit Rights", [
            "5.1 Supplier shall maintain complete and accurate records relating to fees charged "
            "for a period of six (6) years and shall permit Customer or its auditors to inspect such "
            "records on ten (10) business days' notice. If an audit reveals overcharging exceeding "
            "five percent (5%) in any period, Supplier shall bear the cost of the audit.",
        ]),
        ("6. Limitation of Liability", [
            "6.1 Except for liability arising from fraud, wilful misconduct or breach of "
            "confidentiality, each party's aggregate liability under this Agreement shall not exceed "
            "the total fees paid or payable in the twelve (12) months preceding the claim.",
        ]),
        ("7. Termination", [
            "7.1 Either party may terminate this Agreement for material breach not remedied within "
            "thirty (30) days of written notice.",
            "7.2 Customer may terminate this Agreement for convenience on ninety (90) days' written "
            "notice, subject to payment for Deliverables properly supplied up to the termination date.",
        ]),
        ("8. Governing Law", [
            "8.1 This Agreement is governed by the laws of the Islamic Republic of Pakistan. The "
            "courts at Karachi shall have exclusive jurisdiction.",
        ]),
    ]


def _exhibit_a(c: Contract) -> list[Flowable]:
    rows = [["Item Code", "Description", "Unit", f"Max Unit Price ({c.currency})"]] + [
        [r.item_code, r.description, r.unit, f"{r.max_unit_price:,.2f}"] for r in c.rate_card
    ]
    t = Table(rows, colWidths=[32 * mm, 80 * mm, 22 * mm, 40 * mm])
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey), ("ALIGN", (3, 0), (3, -1), "RIGHT"),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8E8E8")),
    ]))
    return [
        Paragraph("EXHIBIT A - RATE CARD", _TITLE),
        Paragraph(f"Forming part of Agreement ref. {c.contract_ref}", _C),
        Spacer(1, 6 * mm), t, Spacer(1, 5 * mm),
        Paragraph("All prices exclude applicable sales tax/VAT, which shall be shown separately on "
                  "each invoice. Prices are ceilings; Supplier may invoice below them.", _P),
    ]


def render_contract(c: Contract, out_path: Path) -> Path:
    supplier = vendor_by_id(c.vendor_id)
    story: list[Flowable] = [
        Paragraph(c.title.upper(), _TITLE),
        Paragraph(f"Agreement Reference: {c.contract_ref}", _C),
        Spacer(1, 6 * mm),
        Paragraph(
            f"This Agreement is made between <b>{BUYER_NAME}</b> (\"Customer\") and "
            f"<b>{supplier.legal_name}</b>, {', '.join(supplier.address_lines)}, tax registration "
            f"{supplier.tax_id} (\"Supplier\").", _P),
    ]
    for heading, paras in _clauses(c, supplier.legal_name):
        story.append(Paragraph(heading, _H))
        story.extend(Paragraph(p, _P) for p in paras)

    sig = Table(
        [[f"For and on behalf of {BUYER_NAME}", f"For and on behalf of {supplier.legal_name}"],
         ["Signature: ______________________", "Signature: ______________________"],
         ["Name / Title: ___________________", "Name / Title: ___________________"],
         [f"Date: {_long_date(c.effective_date)}", f"Date: {_long_date(c.effective_date)}"]],
        colWidths=[90 * mm, 90 * mm],
    )
    sig.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 8.5), ("TOPPADDING", (0, 1), (-1, -1), 7)]))
    story += [Spacer(1, 8 * mm), sig, PageBreak(), *_exhibit_a(c)]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(out_path), pagesize=LETTER, leftMargin=20 * mm, rightMargin=20 * mm,
        topMargin=18 * mm, bottomMargin=18 * mm, title=c.title, author=BUYER_NAME, invariant=True,
    )
    doc.build(story)
    logger.debug("Rendered contract %s -> %s", c.contract_ref, out_path)
    return out_path
