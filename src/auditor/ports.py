"""Ports: what the core needs from the outside world, nothing more.

The rule engine and (later) the agent tools depend on this Protocol, not on
sqlite3. Postgres, an HTTP ERP API, or a test fake can all satisfy it.
Note there are no write methods: the audit path is read-only by construction.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Protocol

from auditor.domain.models import PaymentRecord, PurchaseOrderRecord, RateCardEntry, VendorRecord


class LedgerReader(Protocol):
    def ping(self) -> None:
        """Raise if the backend is unreachable. Used by the readiness probe."""

    def close(self) -> None: ...

    def vendor_by_tax_id(self, tax_id: str) -> VendorRecord | None: ...

    def purchase_order(self, po_number: str) -> PurchaseOrderRecord | None: ...

    def rate_card(self, contract_ref: str) -> tuple[RateCardEntry, ...]: ...

    def payments_by_invoice_number(self, vendor_id: str, invoice_number: str) -> tuple[PaymentRecord, ...]: ...

    def payments_by_amount(
        self, vendor_id: str, amount: Decimal, start: date, end: date
    ) -> tuple[PaymentRecord, ...]: ...


class PaymentConflictError(Exception):
    """Another run - or the ERP itself - already holds a payment for this vendor + invoice number."""


class PaymentLedgerWriter(Protocol):
    """The ONLY write path into the ledger: record payments this system makes.

    Reserve-then-pay: ``reserve`` inserts a PENDING row before any money moves, so the
    invoice is immediately visible to every other run's duplicate check, and the
    (vendor, invoice number) unique constraint arbitrates races. ``mark_paid`` flips it
    to PAID with the gateway's reference. Both are idempotent per ``external_ref``
    (the run id), because a LangGraph node may re-execute after a crash.
    """

    def reserve(
        self,
        *,
        external_ref: str,
        vendor_id: str,
        invoice_number: str,
        po_number: str | None,
        amount: Decimal,
        payment_date: date,
    ) -> None:
        """Raise ``PaymentConflictError`` if someone else already holds this invoice."""

    def mark_paid(self, *, external_ref: str, payment_ref: str) -> None: ...

    def close(self) -> None: ...
