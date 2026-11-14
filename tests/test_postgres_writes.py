"""Postgres write paths: payment write-back and the append-only audit log."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import psycopg
import pytest

from auditor.adapters.postgres_ledger import PostgresLedger
from auditor.adapters.postgres_payments import PostgresPaymentLedger
from auditor.audit_log import PostgresAuditLog
from auditor.ports import PaymentConflictError

pytestmark = pytest.mark.postgres

RESERVE = {
    "vendor_id": "V-1001",
    "invoice_number": "HDS-10233",
    "po_number": "PO-2026-0418",
    "amount": Decimal("11400.00"),
    "payment_date": date(2026, 11, 14),
}


def test_reserve_is_idempotent_per_run_and_conflicts_across_runs(fresh_pg_dsn: str) -> None:
    with PostgresPaymentLedger(fresh_pg_dsn) as writer, PostgresLedger(fresh_pg_dsn) as reader:
        remaining_before = reader.purchase_order("PO-2026-0418").remaining  # type: ignore[union-attr]
        writer.reserve(external_ref="run:a", **RESERVE)  # type: ignore[arg-type]
        writer.reserve(external_ref="run:a", **RESERVE)  # type: ignore[arg-type]  # crash-retry: no-op
        with pytest.raises(PaymentConflictError):
            writer.reserve(external_ref="run:b", **RESERVE)  # type: ignore[arg-type]
        (row,) = reader.payments_by_invoice_number("V-1001", "HDS-10233")
        assert (row.status, row.external_ref) == ("PENDING", "run:a")
        # A reservation already consumes PO budget for every other run.
        assert reader.purchase_order("PO-2026-0418").remaining == remaining_before - Decimal("11400.00")  # type: ignore[union-attr]
        writer.mark_paid(external_ref="run:a", payment_ref="SIM-1")
        (row,) = reader.payments_by_invoice_number("V-1001", "HDS-10233")
        assert row.status == "PAID"


def test_invoice_already_paid_by_the_erp_conflicts(fresh_pg_dsn: str) -> None:
    with PostgresPaymentLedger(fresh_pg_dsn) as writer, pytest.raises(PaymentConflictError):
        writer.reserve(external_ref="run:x", **{**RESERVE, "vendor_id": "V-1003", "invoice_number": "KFC-9981"})  # type: ignore[arg-type]


def test_audit_log_chain_and_append_only_trigger(fresh_pg_dsn: str) -> None:
    log = PostgresAuditLog(fresh_pg_dsn)
    try:
        for i in range(3):
            log.append(run_id="r1", actor="ana", action="TEST", details={"i": i, "amount": "10.00"}, outcome={})
        log.append(run_id="r2", actor="ian", action="TEST", details={}, outcome={"status": "PAID"})
        assert [e.seq for e in log.entries(run_id="r1")] == [1, 2, 3]
        assert log.verify().valid
        with psycopg.connect(fresh_pg_dsn, autocommit=True) as conn:
            for stmt in (
                "UPDATE audit.audit_log SET actor = 'x'",
                "DELETE FROM audit.audit_log",
                "TRUNCATE audit.audit_log",
            ):
                with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
                    conn.execute(stmt)
            # Even someone who can disable the trigger can't hide an edit from the chain.
            conn.execute("ALTER TABLE audit.audit_log DISABLE TRIGGER audit_log_no_update_delete")
            conn.execute('UPDATE audit.audit_log SET details = \'{"i": 1, "amount": "1.00"}\' WHERE seq = 2')
        verdict = log.verify()
        assert (verdict.valid, verdict.first_bad_seq) == (False, 2)
    finally:
        log.close()
