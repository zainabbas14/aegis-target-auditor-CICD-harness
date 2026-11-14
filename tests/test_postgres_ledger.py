"""Postgres-specific guarantees. Parity with SQLite is covered by the
backend-parametrised tests in test_engine_golden.py."""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.postgres


def test_pool_refuses_writes_even_with_admin_credentials(pg_dsn: str) -> None:
    import psycopg  # noqa: PLC0415

    from auditor.adapters.postgres_ledger import PostgresLedger  # noqa: PLC0415

    with (
        PostgresLedger(pg_dsn) as led,
        led._pool.connection() as conn,
        pytest.raises(psycopg.errors.ReadOnlySqlTransaction),
    ):
        conn.execute("DELETE FROM historical_payments")


def test_ping(pg_dsn: str) -> None:
    from auditor.adapters.postgres_ledger import PostgresLedger  # noqa: PLC0415

    with PostgresLedger(pg_dsn) as led:
        led.ping()


def test_approval_survives_a_restart(pg_dsn: str, built, sqlite_ledger, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Start a run in one 'process', pause at HITL, tear everything down, resume from a
    brand-new graph + checkpointer pointed at the same database."""
    from langgraph.checkpoint.postgres import PostgresSaver  # noqa: PLC0415
    from psycopg.rows import dict_row  # noqa: PLC0415
    from psycopg_pool import ConnectionPool  # noqa: PLC0415

    from auditor.agents.explainer import Explainer  # noqa: PLC0415
    from auditor.agents.parser import DocumentParser  # noqa: PLC0415
    from auditor.documents.store import DocumentStore  # noqa: PLC0415
    from auditor.domain.enums import RunStatus  # noqa: PLC0415
    from auditor.payments import SimulatedPaymentGateway  # noqa: PLC0415
    from auditor.rules.engine import RuleConfig  # noqa: PLC0415
    from auditor.workflow.graph import ApprovalDecision, WorkflowDeps, build_graph  # noqa: PLC0415
    from auditor.workflow.service import RunService  # noqa: PLC0415
    from tests.fakes import FakeLLM  # noqa: PLC0415

    documents = DocumentStore(tmp_path / "docs")

    def boot() -> tuple[RunService, ConnectionPool[Any], SimulatedPaymentGateway]:
        pool: ConnectionPool[Any] = ConnectionPool(
            pg_dsn, kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row}, open=True
        )
        saver = PostgresSaver(pool)
        saver.setup()
        llm = FakeLLM(built)
        gw = SimulatedPaymentGateway()
        deps = WorkflowDeps(sqlite_ledger, documents, DocumentParser(llm), Explainer(llm), gw, RuleConfig())
        return RunService(build_graph(deps, saver), documents), pool, gw

    runs, pool, _ = boot()
    run = runs.start((built / "invoices" / "S02_clean_goods_with_tax.pdf").read_bytes(), "s02.pdf", actor="clerk")
    assert run.status is RunStatus.AWAITING_APPROVAL
    pool.close()  # "process" dies

    runs2, pool2, gw2 = boot()  # fresh process, same database
    try:
        assert runs2.get(run.run_id).status is RunStatus.AWAITING_APPROVAL
        paid = runs2.decide(run.run_id, ApprovalDecision(approved=True, approver="bob"))
        assert paid.status is RunStatus.PAID
        assert gw2.execution_count == 1
    finally:
        pool2.close()
