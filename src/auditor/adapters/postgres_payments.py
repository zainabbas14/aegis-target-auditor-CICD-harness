"""Postgres payment write-back, as the ``auditor_payments`` role.

That role can SELECT and INSERT ``historical_payments`` and UPDATE only its
``status`` and ``payment_ref`` columns. It cannot delete, cannot touch any other
table, and the ledger reader role stays strictly read-only.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from types import TracebackType
from typing import Any, Self

from psycopg_pool import ConnectionPool

from auditor.domain.models import money_to_cents
from auditor.ports import PaymentConflictError

logger = logging.getLogger(__name__)


class PostgresPaymentLedger:
    def __init__(self, conninfo: str, *, max_size: int = 2, timeout_s: float = 5.0) -> None:
        self._pool: ConnectionPool[Any] = ConnectionPool(
            conninfo,
            min_size=1,
            max_size=max_size,
            timeout=timeout_s,
            kwargs={"autocommit": True},
            open=True,
            name="payments",
        )

    def close(self) -> None:
        self._pool.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()

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
        with self._pool.connection() as conn:
            inserted = conn.execute(
                "INSERT INTO historical_payments (invoice_number, vendor_id, po_number, amount_cents, payment_date, "
                "status, external_ref) VALUES (%s, %s, %s, %s, %s, 'PENDING', %s) ON CONFLICT DO NOTHING "
                "RETURNING payment_id",
                (invoice_number, vendor_id, po_number, money_to_cents(amount), payment_date, external_ref),
            ).fetchone()
            if inserted is None:
                row = conn.execute(
                    "SELECT external_ref FROM historical_payments WHERE vendor_id = %s AND invoice_number = %s",
                    (vendor_id, invoice_number),
                ).fetchone()
                if row is None or row[0] != external_ref:
                    raise PaymentConflictError(f"{vendor_id}/{invoice_number} is already recorded")
        logger.info("Payment reserved in ledger", extra={"external_ref": external_ref})

    def mark_paid(self, *, external_ref: str, payment_ref: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE historical_payments SET status = 'PAID', payment_ref = %s WHERE external_ref = %s",
                (payment_ref, external_ref),
            )
