"""Payment execution (simulated).

Two rules every real integration must also follow:
  * Idempotency: the caller supplies a key; repeating a call with the same key
    returns the original receipt and moves no money a second time. LangGraph may
    re-execute a node after a crash, so this is not optional.
  * Pay the account ON FILE, never the account printed on the invoice.
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)


class PaymentReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    payment_id: str
    idempotency_key: str
    vendor_id: str
    amount: Decimal
    currency: str
    account_tail: str
    executed_at: datetime
    simulated: bool = True


class PaymentGateway(Protocol):
    def execute(
        self, *, idempotency_key: str, vendor_id: str, amount: Decimal, currency: str, account: str
    ) -> PaymentReceipt: ...


class SimulatedPaymentGateway:
    """In-process stand-in for a bank/treasury API. Thread-safe, idempotent."""

    def __init__(self) -> None:
        self._receipts: dict[str, PaymentReceipt] = {}
        self._lock = threading.Lock()
        self.execution_count = 0  # number of *real* executions; observable in tests

    def execute(
        self, *, idempotency_key: str, vendor_id: str, amount: Decimal, currency: str, account: str
    ) -> PaymentReceipt:
        with self._lock:
            if existing := self._receipts.get(idempotency_key):
                logger.info("Idempotent replay; no new payment", extra={"payment_id": existing.payment_id})
                return existing
            receipt = PaymentReceipt(
                payment_id=f"SIM-{uuid.uuid4().hex[:12].upper()}",
                idempotency_key=idempotency_key,
                vendor_id=vendor_id,
                amount=amount,
                currency=currency,
                account_tail=account[-4:],
                executed_at=datetime.now(UTC),
            )
            self._receipts[idempotency_key] = receipt
            self.execution_count += 1
        logger.info(
            "SIMULATED wire transfer executed",
            extra={"payment_id": receipt.payment_id, "vendor_id": vendor_id, "amount": str(amount)},
        )
        return receipt
