"""Seed the mock ERP database deterministically.

Usage:
    python -m tests.mock_data.seed_db --db tests/mock_data/generated/enterprise_erp.db

Determinism matters: the same seed must yield a byte-identical ledger so that
golden-file evals and CI runs are reproducible.
"""

from __future__ import annotations

import argparse
import logging
import random
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from faker import Faker

from auditor.domain.enums import PaymentStatus
from tests.mock_data.fixtures import (
    CONTRACTS,
    PURCHASE_ORDERS,
    SCENARIO_PAYMENTS,
    SCENARIOS,
    VENDORS,
    Payment,
    reserved_invoice_numbers,
)

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
DEFAULT_SEED = 20261009
# Background payments for scenario vendors end before this date so that no
# noise row can fall inside a near-duplicate detection window.
NOISE_CUTOFF = date(2026, 8, 31)
NOISE_START = date(2026, 1, 15)
_PREFIX = {"V-1001": ("HDS", 8000), "V-1002": ("MIS", 54000), "V-1003": ("KFC", 7000)}


def to_cents(value: Decimal) -> int:
    cents = value * 100
    if cents != cents.to_integral_value():
        raise ValueError(f"{value} has sub-cent precision")
    return int(cents)


@dataclass(frozen=True, slots=True)
class SeedStats:
    vendors: int
    purchase_orders: int
    payments: int


def _random_date(rng: random.Random, start: date, end: date) -> date:
    return start + timedelta(days=rng.randint(0, (end - start).days))


def _noise_payments(rng: random.Random, fake: Faker) -> tuple[list[Payment], list[tuple[str, str, str, str]]]:
    """Background ledger: rows on scenario POs plus unrelated vendors."""
    reserved = reserved_invoice_numbers()
    forbidden_amounts = {s.invoice.printed_total for s in SCENARIOS}
    payments: list[Payment] = []

    for po in PURCHASE_ORDERS:
        prefix, base = _PREFIX[po.vendor_id]
        for _ in range(rng.randint(4, 7)):
            number = f"{prefix}-{base + rng.randint(1, 999)}"
            amount = Decimal(rng.randint(90_000, 900_000)) / 100
            if number in reserved or amount in forbidden_amounts or any(p.invoice_number == number for p in payments):
                continue
            payments.append(
                Payment(
                    invoice_number=number,
                    vendor_id=po.vendor_id,
                    po_number=po.po_number,
                    amount=amount,
                    payment_date=_random_date(rng, max(po.issued_on, NOISE_START), NOISE_CUTOFF),
                    status=PaymentStatus.VOID if rng.random() < 0.1 else PaymentStatus.PAID,
                )
            )

    other_vendors: list[tuple[str, str, str, str]] = []
    for i in range(5):
        vid = f"V-2{i:03d}"
        other_vendors.append(
            (vid, fake.company(), f"OTH-TX-{rng.randint(1_000_000, 9_999_999)}", f"FAKE-XX00-OTHR-0000-{2000 + i}")
        )
        for j in range(rng.randint(3, 6)):
            payments.append(
                Payment(
                    invoice_number=f"OTH{i}-{1000 + j}",
                    vendor_id=vid,
                    po_number=None,
                    amount=Decimal(rng.randint(20_000, 1_500_000)) / 100,
                    payment_date=_random_date(rng, NOISE_START, date(2026, 10, 31)),
                    status=rng.choices(list(PaymentStatus), weights=[80, 12, 8])[0],
                )
            )
    return payments, other_vendors


def seed(db_path: Path, contracts_dir: Path, seed_value: int = DEFAULT_SEED) -> SeedStats:
    rng = random.Random(seed_value)
    fake = Faker()
    fake.seed_instance(seed_value)

    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)
    created_at = datetime(2026, 1, 1, tzinfo=UTC).isoformat()

    noise, other_vendors = _noise_payments(rng, fake)
    all_payments = [*SCENARIO_PAYMENTS, *noise]

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        with conn:  # single transaction
            conn.executemany(
                "INSERT INTO vendors (vendor_id, legal_name, tax_id, remit_account, contract_ref, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(v.vendor_id, v.legal_name, v.tax_id, v.remit_account, v.contract_ref, created_at) for v in VENDORS]
                + [(vid, name, tax, acct, None, created_at) for vid, name, tax, acct in other_vendors],
            )
            for c in CONTRACTS:
                conn.execute(
                    "INSERT INTO contracts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        c.contract_ref,
                        c.vendor_id,
                        c.title,
                        c.effective_date.isoformat(),
                        c.expiry_date.isoformat(),
                        c.payment_terms_days,
                        c.currency,
                        str(contracts_dir / f"{c.contract_ref}.pdf"),
                    ),
                )
                conn.executemany(
                    "INSERT INTO contract_rates VALUES (?, ?, ?, ?, ?)",
                    [
                        (c.contract_ref, r.item_code, r.description, r.unit, to_cents(r.max_unit_price))
                        for r in c.rate_card
                    ],
                )
            for po in PURCHASE_ORDERS:
                consumed = sum(
                    (
                        p.amount
                        for p in all_payments
                        if p.po_number == po.po_number and p.status is not PaymentStatus.VOID
                    ),
                    Decimal("0"),
                )
                conn.execute(
                    "INSERT INTO purchase_orders (po_number, vendor_id, issued_on, approved_budget_cents) "
                    "VALUES (?, ?, ?, ?)",
                    (po.po_number, po.vendor_id, po.issued_on.isoformat(), to_cents(consumed + po.headroom)),
                )
            conn.executemany(
                "INSERT INTO historical_payments "
                "(invoice_number, vendor_id, po_number, amount_cents, payment_date, status) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        p.invoice_number,
                        p.vendor_id,
                        p.po_number,
                        to_cents(p.amount),
                        p.payment_date.isoformat(),
                        p.status.value,
                    )
                    for p in sorted(all_payments, key=lambda p: (p.payment_date, p.invoice_number))
                ],
            )
    finally:
        conn.close()

    stats = SeedStats(
        vendors=len(VENDORS) + len(other_vendors),
        purchase_orders=len(PURCHASE_ORDERS),
        payments=len(all_payments),
    )
    logger.info("Seeded %s: %s", db_path, stats)
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("tests/mock_data/generated/enterprise_erp.db"))
    parser.add_argument("--contracts-dir", type=Path, default=Path("tests/mock_data/generated/contracts"))
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    seed(args.db, args.contracts_dir, args.seed)


if __name__ == "__main__":
    main()
