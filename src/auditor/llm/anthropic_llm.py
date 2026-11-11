"""Anthropic adapter for the StructuredLLM port.

Uses the Messages API with structured outputs (``messages.parse`` +
``output_format``): the response is constrained to the Pydantic model's JSON
schema and returned parsed. Transport retries (429 / 5xx / connection) are
handled by the SDK's own exponential backoff (``max_retries``); whatever still
fails is mapped onto the port's two error classes.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, cast

import anthropic
from pydantic import ValidationError

from auditor.llm.ports import LLMInvalidOutputError, LLMUnavailableError, T

if TYPE_CHECKING:
    from anthropic.types import MessageParam

logger = logging.getLogger(__name__)

_UNTRUSTWORTHY_STOPS = {"max_tokens": "truncated", "refusal": "refused"}


class AnthropicLLM:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_s: float = 60.0,
        max_retries: int = 2,
        client: Any | None = None,
    ) -> None:
        self._model = model
        # `client` is injectable so the adapter is unit-testable without network.
        self._client = client or anthropic.Anthropic(api_key=api_key, timeout=timeout_s, max_retries=max_retries)

    @property
    def model(self) -> str:
        return self._model

    def generate(
        self,
        *,
        output_type: type[T],
        system: str,
        content: list[dict[str, Any]],
        max_tokens: int,
        purpose: str,
    ) -> T:
        started = time.perf_counter()
        try:
            response = self._client.messages.parse(
                model=self._model,
                max_tokens=max_tokens,
                system=system,
                messages=cast("list[MessageParam]", [{"role": "user", "content": content}]),
                output_format=output_type,
            )
        except (anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.InternalServerError) as exc:
            raise LLMUnavailableError("transient_failure", type(exc).__name__) from exc
        except anthropic.APIStatusError as exc:
            # 4xx other than 429: auth, bad request, permissions. A config problem, not a blip.
            logger.error("LLM request rejected", extra={"purpose": purpose, "status": exc.status_code})
            raise LLMUnavailableError("request_rejected", f"HTTP {exc.status_code}") from exc
        except ValidationError as exc:
            raise LLMInvalidOutputError("schema_violation", f"{exc.error_count()} errors") from exc

        usage = getattr(response, "usage", None)
        logger.info(
            "LLM call complete",
            extra={
                "purpose": purpose,
                "model": self._model,
                "latency_ms": round((time.perf_counter() - started) * 1000),
                "stop_reason": response.stop_reason,
                "input_tokens": getattr(usage, "input_tokens", None),
                "output_tokens": getattr(usage, "output_tokens", None),
            },
        )
        if response.stop_reason in _UNTRUSTWORTHY_STOPS:
            raise LLMInvalidOutputError(_UNTRUSTWORTHY_STOPS[response.stop_reason])
        parsed = response.parsed_output
        if not isinstance(parsed, output_type):
            raise LLMInvalidOutputError("no_parsed_output")
        return parsed
