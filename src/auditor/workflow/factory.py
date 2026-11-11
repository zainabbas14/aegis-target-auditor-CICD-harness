"""Composition root for the agent workflow."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from auditor.config import Settings
from auditor.llm.ports import StructuredLLM

logger = logging.getLogger(__name__)


def build_llm(settings: Settings, model: str) -> StructuredLLM | None:
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
