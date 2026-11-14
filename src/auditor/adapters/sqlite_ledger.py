"""Read-only SQLite implementation of LedgerReader.

Read-only is enforced by the connection (``mode=ro``), not by convention:
even a bug or a prompt-injected tool call cannot write to the ledger.
Every query is parameterised; no string-built SQL exists in this module.

A short-lived connection per call: sqlite3 connections are not safe to share
across the API's worker threads, and opening a local file is cheap.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import Self

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

_PAYMENT_COLS = "invoice_number, vendor_id, po_number, amount_cents, payment_date, status, external_ref"


def _payment(row: sqlite3.Row) -> PaymentRecord:
    return PaymentRecord(
        invoice_number=row["invoice_number"],
        vendor_id=row["vendor_id"],
        po_number=row["po_number"],
        amount=cents_to_money(row["amount_cents"]),
        payment_date=date.fromisoformat(row["payment_date"]),
        status=PaymentStatus(row["status"]),
        external_ref=row["external_ref"],
    )


class SqliteLedger:
    def __init__(self, db_path: Path) -> None:
        if not db_path.is_file():
            raise FileNotFoundError(f"ledger database not found: {db_path}")
        self._uri = f"file:{db_path.resolve()}?mode=ro"
        logger.info("SQLite ledger configured (read-only): %s", db_path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self._uri, uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            yield conn

    def _one(self, sql: str, params: tuple[object, ...]) -> sqlite3.Row | None:
        with self._connect() as conn:
            row: sqlite3.Row | None = conn.execute(sql, params).fetchone()
            return row

    def _all(self, sql: str, params: tuple[object, ...]) -> list[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(sql, params).fetchall()

    def close(self) -> None:
        """Nothing pooled; present to satisfy the LedgerReader lifecycle."""

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()

    def ping(self) -> None:
        self._one("SELECT 1", ())

    def vendor_by_tax_id(self, tax_id: str) -> VendorRecord | None:
        row = self._one(
            "SELECT vendor_id, legal_name, tax_id, remit_account, contract_ref, status FROM vendors WHERE tax_id = ?",
            (tax_id,),
        )
        return VendorRecord(**dict(row)) if row else None

    def purchase_order(self, po_number: str) -> PurchaseOrderRecord | None:
        row = self._one(
            "SELECT po.po_number, po.vendor_id, po.status, b.approved_budget_cents, b.consumed_cents "
            "FROM purchase_orders po JOIN po_balances b USING (po_number) WHERE po.po_number = ?",
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
            "WHERE contract_ref = ? ORDER BY item_code",
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
            f"SELECT {_PAYMENT_COLS} FROM historical_payments "  # noqa: S608 - constant column list
            "WHERE vendor_id = ? AND invoice_number = ? AND status != 'VOID'",
            (vendor_id, invoice_number),
        )
        return tuple(_payment(r) for r in rows)

    def payments_by_amount(self, vendor_id: str, amount: Decimal, start: date, end: date) -> tuple[PaymentRecord, ...]:
        rows = self._all(
            f"SELECT {_PAYMENT_COLS} FROM historical_payments "  # noqa: S608 - constant column list
            "WHERE vendor_id = ? AND amount_cents = ? AND status != 'VOID' "
            "AND payment_date BETWEEN ? AND ? ORDER BY payment_date",
            (vendor_id, money_to_cents(amount), start.isoformat(), end.isoformat()),
        )
        return tuple(_payment(r) for r in rows)
