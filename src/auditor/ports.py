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
    def vendor_by_tax_id(self, tax_id: str) -> VendorRecord | None: ...

    def purchase_order(self, po_number: str) -> PurchaseOrderRecord | None: ...

    def rate_card(self, contract_ref: str) -> tuple[RateCardEntry, ...]: ...

    def payments_by_invoice_number(self, vendor_id: str, invoice_number: str) -> tuple[PaymentRecord, ...]: ...

    def payments_by_amount(
        self, vendor_id: str, amount: Decimal, start: date, end: date
    ) -> tuple[PaymentRecord, ...]: ...
