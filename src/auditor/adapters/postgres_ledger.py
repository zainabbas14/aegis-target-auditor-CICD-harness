"""Read-only Postgres implementation of LedgerReader.

Read-only is enforced in three independent layers:
  1. the app connects as ``auditor_ro`` - a role with SELECT grants only;
  2. that role defaults to read-only transactions;
  3. this pool sets ``default_transaction_read_only=on`` per connection anyway,
     so even pointing it at an admin URL by mistake cannot write.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from types import TracebackType
from typing import Any, LiteralString, Self

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from auditor.domain.enums import PaymentStatus
from auditor.domain.models import (
    PaymentRecord,
    PurchaseOrderRecord,
    RateCardEntry,
    VendorRecord,
    cents_to_money,
    money_to_cents,
)

logger = logging.getLogger(__name__)

_PAYMENT_SELECT: LiteralString = (
    "SELECT invoice_number, vendor_id, po_number, amount_cents, payment_date, status, external_ref "
    "FROM historical_payments "
)


def _payment(row: dict[str, Any]) -> PaymentRecord:
    return PaymentRecord(
        invoice_number=row["invoice_number"],
        vendor_id=row["vendor_id"],
        po_number=row["po_number"],
        amount=cents_to_money(row["amount_cents"]),
        payment_date=row["payment_date"],
        status=PaymentStatus(row["status"]),
        external_ref=row["external_ref"],
    )


class PostgresLedger:
    def __init__(self, conninfo: str, *, min_size: int = 1, max_size: int = 5, timeout_s: float = 5.0) -> None:
        self._pool = ConnectionPool(
            conninfo,
            min_size=min_size,
            max_size=max_size,
            timeout=timeout_s,
            kwargs={"options": "-c default_transaction_read_only=on", "autocommit": True},
            open=True,
            name="ledger",
        )
        logger.info("Postgres ledger pool opened (min=%d, max=%d)", min_size, max_size)

    def close(self) -> None:
        self._pool.close()
        logger.info("Postgres ledger pool closed")

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()

    def _one(self, sql: LiteralString, params: tuple[object, ...]) -> dict[str, Any] | None:
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    def _all(self, sql: LiteralString, params: tuple[object, ...]) -> list[dict[str, Any]]:
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def ping(self) -> None:
        self._one("SELECT 1", ())

    def vendor_by_tax_id(self, tax_id: str) -> VendorRecord | None:
        row = self._one(
            "SELECT vendor_id, legal_name, tax_id, remit_account, contract_ref, status FROM vendors WHERE tax_id = %s",
            (tax_id,),
        )
        return VendorRecord(**row) if row else None

    def purchase_order(self, po_number: str) -> PurchaseOrderRecord | None:
        row = self._one(
            "SELECT po.po_number, po.vendor_id, po.status, b.approved_budget_cents, b.consumed_cents "
            "FROM purchase_orders po JOIN po_balances b USING (po_number) WHERE po.po_number = %s",
            (po_number,),
        )
        if row is None:
            return None
        return PurchaseOrderRecord(
            po_number=row["po_number"],
            vendor_id=row["vendor_id"],
            status=row["status"],
            approved_budget=cents_to_money(row["approved_budget_cents"]),
            consumed=cents_to_money(row["consumed_cents"]),
        )

    def rate_card(self, contract_ref: str) -> tuple[RateCardEntry, ...]:
        rows = self._all(
            "SELECT item_code, description, unit, max_unit_price_cents FROM contract_rates "
            "WHERE contract_ref = %s ORDER BY item_code",
            (contract_ref,),
        )
        return tuple(
            RateCardEntry(
                item_code=r["item_code"],
                description=r["description"],
                unit=r["unit"],
                max_unit_price=cents_to_money(r["max_unit_price_cents"]),
            )
            for r in rows
        )

    def payments_by_invoice_number(self, vendor_id: str, invoice_number: str) -> tuple[PaymentRecord, ...]:
        rows = self._all(
            _PAYMENT_SELECT + "WHERE vendor_id = %s AND invoice_number = %s AND status <> 'VOID'",
            (vendor_id, invoice_number),
        )
        return tuple(_payment(r) for r in rows)

    def payments_by_amount(self, vendor_id: str, amount: Decimal, start: date, end: date) -> tuple[PaymentRecord, ...]:
        rows = self._all(
            _PAYMENT_SELECT + "WHERE vendor_id = %s AND amount_cents = %s AND status <> 'VOID' "
            "AND payment_date BETWEEN %s AND %s ORDER BY payment_date",
            (vendor_id, money_to_cents(amount), start, end),
        )
        return tuple(_payment(r) for r in rows)
