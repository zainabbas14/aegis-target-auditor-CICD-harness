"""Composition root for the agent workflow."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from auditor.audit_log import AuditLog, InMemoryAuditLog
from auditor.config import Settings
from auditor.llm.ports import StructuredLLM
from auditor.ports import PaymentLedgerWriter

logger = logging.getLogger(__name__)


def build_llm(settings: Settings, model: str) -> StructuredLLM | None:
    if settings.llm_provider == "openai_compatible":
        if settings.llm_api_key is None or not settings.llm_base_url:
            logger.warning("No LLM endpoint credentials configured; LLM steps will degrade", extra={"model": model})
            return None
        from auditor.llm.openai_compatible import OpenAICompatibleLLM  # noqa: PLC0415

        return OpenAICompatibleLLM(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key.get_secret_value(),
            model=model,
            timeout_s=settings.llm_timeout_s,
            max_retries=settings.llm_max_retries,
            min_interval_s=settings.llm_min_interval_s,
            json_mode=settings.llm_json_mode,
            pdf_mode=settings.llm_pdf_mode,
        )
    if settings.anthropic_api_key is None:
        logger.warning("No Anthropic API key configured; LLM steps will degrade", extra={"model": model})
        return None
    from auditor.llm.anthropic_llm import AnthropicLLM  # noqa: PLC0415

    return AnthropicLLM(
        api_key=settings.anthropic_api_key.get_secret_value(),
        model=model,
        timeout_s=settings.llm_timeout_s,
        max_retries=settings.llm_max_retries,
    )


def build_audit_log(settings: Settings) -> AuditLog:
    if settings.checkpointer == "postgres" and settings.state_database_url is not None:
        from auditor.audit_log import PostgresAuditLog  # noqa: PLC0415

        return PostgresAuditLog(settings.state_database_url.get_secret_value())
    logger.warning("In-memory audit log: entries are lost on restart")
    return InMemoryAuditLog()


def build_payment_ledger(settings: Settings) -> PaymentLedgerWriter | None:
    if settings.payments_database_url is None:
        logger.warning("No payments database configured: payments will NOT be recorded in the ledger")
        return None
    from auditor.adapters.postgres_payments import PostgresPaymentLedger  # noqa: PLC0415

    return PostgresPaymentLedger(settings.payments_database_url.get_secret_value())


def build_checkpointer(settings: Settings) -> tuple[BaseCheckpointSaver[Any], Callable[[], None]]:
    """Returns (saver, close)."""
    if settings.checkpointer == "memory":
        logger.warning("In-memory checkpointer: runs awaiting approval are lost on restart")
        return InMemorySaver(), lambda: None

    from langgraph.checkpoint.postgres import PostgresSaver  # noqa: PLC0415
    from psycopg.rows import dict_row  # noqa: PLC0415
    from psycopg_pool import ConnectionPool  # noqa: PLC0415

    if settings.state_database_url is None:  # unreachable: Settings validator enforces it
        raise ValueError("state_database_url required for postgres checkpointer")
    pool: ConnectionPool[Any] = ConnectionPool(
        settings.state_database_url.get_secret_value(),
        min_size=1,
        max_size=settings.db_pool_max,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        open=True,
        name="checkpoints",
    )
    saver = PostgresSaver(pool)
    saver.setup()  # idempotent migrations
    logger.info("Postgres checkpointer ready")
    return saver, pool.close
