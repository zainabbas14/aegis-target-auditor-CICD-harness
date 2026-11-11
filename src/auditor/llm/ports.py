"""LLM port. The workflow depends on this Protocol, never on a vendor SDK.

Two failure classes, because they need different handling upstream:
  LLMUnavailableError    - the call didn't complete (network, rate limit, auth, timeout)
  LLMInvalidOutputError  - the call completed but the output can't be trusted
Both trigger graceful degradation; they are logged and surfaced differently.
"""

from __future__ import annotations

from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """Base class. ``reason`` is a short machine-readable code for logs and run state."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class LLMUnavailableError(LLMError):
    pass


class LLMInvalidOutputError(LLMError):
    pass


class StructuredLLM(Protocol):
    def generate(
        self,
        *,
        output_type: type[T],
        system: str,
        content: list[dict[str, Any]],
        max_tokens: int,
        purpose: str,
    ) -> T:
        """Return an instance of ``output_type`` or raise an ``LLMError`` subclass.

        ``purpose`` labels the call in logs and usage metrics (e.g. "parse_invoice").
        """
        ...
