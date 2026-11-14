"""SQLite payment write-back (dev and tests; production uses Postgres)."""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from datetime import date
from decimal import Decimal
from pathlib import Path

from auditor.domain.models import money_to_cents
from auditor.ports import PaymentConflictError

logger = logging.getLogger(__name__)


class SqlitePaymentLedger:
    def __init__(self, db_path: Path) -> None:
        if not db_path.is_file():
            raise FileNotFoundError(f"ledger database not found: {db_path}")
        self._path = db_path

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, isolation_level=None)  # autocommit; one statement per call

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
        with closing(self._connect()) as conn:
            try:
                conn.execute(
                    "INSERT INTO historical_payments (invoice_number, vendor_id, po_number, amount_cents, "
                    "payment_date, status, external_ref) VALUES (?, ?, ?, ?, ?, 'PENDING', ?)",
                    (
                        invoice_number,
                        vendor_id,
                        po_number,
                        money_to_cents(amount),
                        payment_date.isoformat(),
                        external_ref,
                    ),
                )
            except sqlite3.IntegrityError:
                row = conn.execute(
                    "SELECT external_ref FROM historical_payments WHERE vendor_id = ? AND invoice_number = ?",
                    (vendor_id, invoice_number),
                ).fetchone()
                if row is None or row[0] != external_ref:
                    raise PaymentConflictError(f"{vendor_id}/{invoice_number} is already recorded") from None
        logger.info("Payment reserved in ledger", extra={"external_ref": external_ref})

    def mark_paid(self, *, external_ref: str, payment_ref: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE historical_payments SET status = 'PAID', payment_ref = ? WHERE external_ref = ?",
                (payment_ref, external_ref),
            )

    def close(self) -> None:
        """Nothing pooled."""
