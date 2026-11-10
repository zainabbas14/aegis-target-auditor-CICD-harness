"""Render scenario invoices to PDF.

Each vendor gets a deliberately different template: different titles, column
orders, labels, date formats, and whether item codes are printed at all. A
parser that only works on one layout is a parser that fails in production.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4, LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Flowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from tests.mock_data.fixtures import BUYER_ADDRESS, BUYER_NAME, InvoiceSpec, Vendor, vendor_by_id

logger = logging.getLogger(__name__)

_SS = getSampleStyleSheet()
_BODY = ParagraphStyle("body", parent=_SS["Normal"], fontSize=9, leading=12)
_SMALL = ParagraphStyle("small", parent=_BODY, fontSize=8, textColor=colors.HexColor("#555555"))
_RIGHT = ParagraphStyle("right", parent=_BODY, alignment=TA_RIGHT)
_HEAD = ParagraphStyle("head", parent=_BODY, fontSize=14, leading=18, fontName="Helvetica-Bold")


def _m(value: Decimal, symbol: str = "") -> str:
    return f"{symbol}{value:,.2f}"


def _qty(value: Decimal) -> str:
    return f"{value.normalize():f}"


def _block(lines: tuple[str, ...] | list[str], style: ParagraphStyle = _BODY) -> Paragraph:
    return Paragraph("<br/>".join(lines), style)


def _totals_table(inv: InvoiceSpec, labels: tuple[str, str, str], symbol: str, width: float) -> Table:
    rows: list[list[str]] = [[labels[0], _m(inv.printed_subtotal, symbol)]]
    if inv.tax_rate:
        rows.append([f"{labels[1]} ({inv.tax_rate * 100:.0f}%)", _m(inv.printed_tax, symbol)])
    rows.append([labels[2], _m(inv.printed_total, symbol)])
    t = Table(rows, colWidths=[width * 0.6, width * 0.4], hAlign="RIGHT")
    t.setStyle(TableStyle([
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("LINEABOVE", (0, -1), (-1, -1), 0.8, colors.black),
    ]))
    return t


# --------------------------------------------------------------------------- #
# Layout 1: services_classic - no item codes, "Rate", ISO dates, Letter paper
# --------------------------------------------------------------------------- #
def _services_classic(inv: InvoiceSpec, v: Vendor, remit: str) -> tuple[list[Flowable], tuple[float, float]]:
    fmt: Callable[[date], str] = lambda d: d.isoformat()
    header = Table(
        [[_block([f"<b>{v.legal_name}</b>", *v.address_lines, f"Tax ID: {v.tax_id}"]),
          Paragraph("<font size=22><b>INVOICE</b></font>", _RIGHT)]],
        colWidths=[110 * mm, 70 * mm],
    )
    meta = Table(
        [["Invoice No.", inv.invoice_number], ["Invoice Date", fmt(inv.invoice_date)],
         ["Due Date", fmt(inv.due_date)], ["Customer PO", inv.po_number]],
        colWidths=[30 * mm, 45 * mm], hAlign="RIGHT",
    )
    meta.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 9), ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold")]))
    rows = [["Description", "Hours / Qty", "Rate", "Amount"]] + [
        [ln.description, _qty(ln.quantity), _m(ln.unit_price), _m(ln.amount)] for ln in inv.lines
    ]
    items = Table(rows, colWidths=[90 * mm, 25 * mm, 30 * mm, 35 * mm], repeatRows=1)
    items.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.black), ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F4F4F4")]),
    ]))
    story: list[Flowable] = [
        header, Spacer(1, 8 * mm),
        Table([[_block(["<b>Bill To</b>", BUYER_NAME, *BUYER_ADDRESS]), meta]], colWidths=[100 * mm, 80 * mm]),
        Spacer(1, 8 * mm), items, Spacer(1, 4 * mm),
        _totals_table(inv, ("Subtotal", "VAT", "Amount Due (USD)"), "", 75 * mm),
        Spacer(1, 10 * mm),
        _block([f"<b>Payment terms:</b> Net {inv.terms_days}",
                f"<b>Remit to account:</b> {remit}",
                "Please quote the invoice number on all remittances."], _SMALL),
    ]
    return story, LETTER


# --------------------------------------------------------------------------- #
# Layout 2: goods_modern - colour band, SKU first, "Tax Invoice", US dates, A4
# --------------------------------------------------------------------------- #
def _goods_modern(inv: InvoiceSpec, v: Vendor, remit: str) -> tuple[list[Flowable], tuple[float, float]]:
    fmt: Callable[[date], str] = lambda d: d.strftime("%m/%d/%Y")
    accent = colors.HexColor("#1F4E79")
    band = Table(
        [[Paragraph(f"<font color='white' size=14><b>{v.legal_name}</b></font>", _BODY),
          Paragraph("<font color='white' size=16><b>TAX INVOICE</b></font>", _RIGHT)]],
        colWidths=[115 * mm, 65 * mm],
    )
    band.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), accent),
                              ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    info = Table(
        [[_block([*v.address_lines, f"EIN/Tax Reg: {v.tax_id}"], _SMALL),
          _block(["<b>Sold To</b>", BUYER_NAME, *BUYER_ADDRESS]),
          _block([f"<b>Invoice #</b> {inv.invoice_number}", f"<b>Date</b> {fmt(inv.invoice_date)}",
                  f"<b>PO Ref</b> {inv.po_number}", f"<b>Terms</b> Net {inv.terms_days} days",
                  f"<b>Due</b> {fmt(inv.due_date)}"])]],
        colWidths=[60 * mm, 60 * mm, 60 * mm],
    )
    info.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    rows = [["SKU", "Item Description", "Qty", "Unit Price", "Line Total"]] + [
        [ln.item_code, ln.description, _qty(ln.quantity), _m(ln.unit_price, "$"), _m(ln.amount, "$")]
        for ln in inv.lines
    ]
    items = Table(rows, colWidths=[30 * mm, 70 * mm, 15 * mm, 30 * mm, 35 * mm], repeatRows=1)
    items.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), accent), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (2, 0), (-1, -1), "RIGHT"), ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#BBBBBB")),
    ]))
    story: list[Flowable] = [
        band, Spacer(1, 6 * mm), info, Spacer(1, 8 * mm), items, Spacer(1, 4 * mm),
        _totals_table(inv, ("Net Amount", "Sales Tax", "TOTAL DUE"), "$", 80 * mm),
        Spacer(1, 10 * mm),
        _block([f"Wire/ACH to: {remit}", f"Reference: {inv.invoice_number}",
                "Goods remain property of seller until paid in full."], _SMALL),
    ]
    return story, A4


# --------------------------------------------------------------------------- #
# Layout 3: freight_compact - qty first, codes, long-form dates, dense
# --------------------------------------------------------------------------- #
def _freight_compact(inv: InvoiceSpec, v: Vendor, remit: str) -> tuple[list[Flowable], tuple[float, float]]:
    fmt: Callable[[date], str] = lambda d: d.strftime("%d %b %Y")
    story: list[Flowable] = [
        Paragraph(f"<font size=12><b>{v.legal_name.upper()}</b></font>", _BODY),
        _block([", ".join(v.address_lines), f"NTN / Tax ID {v.tax_id}"], _SMALL),
        Spacer(1, 4 * mm),
        Paragraph("FREIGHT INVOICE", _HEAD),
        Paragraph(
            f"Inv: <b>{inv.invoice_number}</b> &nbsp;|&nbsp; Dated: {fmt(inv.invoice_date)} &nbsp;|&nbsp; "
            f"Pay by: {fmt(inv.due_date)} &nbsp;|&nbsp; Your PO: {inv.po_number}", _BODY),
        Spacer(1, 3 * mm),
        _block([f"Consignee: {BUYER_NAME}, {', '.join(BUYER_ADDRESS)}"], _SMALL),
        Spacer(1, 5 * mm),
    ]
    rows = [["Qty", "Code", "Service", "Rate", "Amount"]] + [
        [_qty(ln.quantity), ln.item_code, ln.description, _m(ln.unit_price), _m(ln.amount)] for ln in inv.lines
    ]
    items = Table(rows, colWidths=[15 * mm, 28 * mm, 82 * mm, 25 * mm, 30 * mm], repeatRows=1)
    items.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 8), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.black), ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.black),
        ("ALIGN", (3, 0), (-1, -1), "RIGHT"),
    ]))
    story += [
        items, Spacer(1, 3 * mm),
        _totals_table(inv, ("Sub-total", "Sales Tax", "NET PAYABLE (USD)"), "", 70 * mm),
        Spacer(1, 6 * mm),
        _block([f"Bank: {remit}", "E&amp;OE. Disputes must be raised within 7 days of invoice date."], _SMALL),
    ]
    return story, A4


_LAYOUTS = {
    "services_classic": _services_classic,
    "goods_modern": _goods_modern,
    "freight_compact": _freight_compact,
}


def render_invoice(inv: InvoiceSpec, out_path: Path) -> Path:
    vendor = vendor_by_id(inv.vendor_id)
    remit = inv.remit_account_override or vendor.remit_account
    story, pagesize = _LAYOUTS[vendor.layout](inv, vendor, remit)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(out_path), pagesize=pagesize,
        leftMargin=15 * mm, rightMargin=15 * mm, topMargin=15 * mm, bottomMargin=15 * mm,
        title=f"Invoice {inv.invoice_number}", author=vendor.legal_name,
        # Fixed timestamps keep the PDF bytes reproducible across builds.
        invariant=True,
    )
    doc.build(story)
    logger.debug("Rendered invoice %s -> %s", inv.invoice_number, out_path)
    return out_path
