"""Anthropic adapter for the StructuredLLM port.

Uses the Messages API with structured outputs (``messages.parse`` +
``output_format``): the response is constrained to the Pydantic model's JSON
schema and returned parsed. Transport retries (429 / 5xx / connection) are
handled by the SDK's own exponential backoff (``max_retries``); whatever still
fails is mapped onto the port's two error classes.
"""

from __future__ import annotations

import base64
import logging
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, cast

import anthropic
from pydantic import ValidationError

from auditor.llm.ports import CallStats, LLMInvalidOutputError, LLMUnavailableError, Part, PdfPart, T, TextPart

if TYPE_CHECKING:
    from anthropic.types import MessageParam

logger = logging.getLogger(__name__)

_UNTRUSTWORTHY_STOPS = {"max_tokens": "truncated", "refusal": "refused"}


def to_anthropic_blocks(content: Sequence[Part]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, PdfPart):
            # Claude reads PDFs natively (text + page images); the text layer is not needed.
            blocks.append(
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": base64.b64encode(part.data).decode("ascii"),
                    },
                }
            )
        elif isinstance(part, TextPart):
            blocks.append({"type": "text", "text": part.text})
    return blocks


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
        self.last_stats: CallStats | None = None
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
        content: Sequence[Part],
        max_tokens: int,
        purpose: str,
    ) -> T:
        started = time.perf_counter()
        try:
            response = self._client.messages.parse(
                model=self._model,
                max_tokens=max_tokens,
                system=system,
                messages=cast("list[MessageParam]", [{"role": "user", "content": to_anthropic_blocks(content)}]),
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
        self.last_stats = CallStats(
            requested_model=self._model,
            served_model=getattr(response, "model", None),
            latency_ms=round((time.perf_counter() - started) * 1000),
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
        )
        logger.info(
            "LLM call complete",
            extra={
                "purpose": purpose,
                "stop_reason": response.stop_reason,
                "model": self._model,
                "served_model": self.last_stats.served_model,
                "latency_ms": self.last_stats.latency_ms,
                "input_tokens": self.last_stats.input_tokens,
                "output_tokens": self.last_stats.output_tokens,
            },
        )
        if response.stop_reason in _UNTRUSTWORTHY_STOPS:
            raise LLMInvalidOutputError(_UNTRUSTWORTHY_STOPS[response.stop_reason])
        parsed = response.parsed_output
        if not isinstance(parsed, output_type):
            raise LLMInvalidOutputError("no_parsed_output")
        return parsed
