"""LLM port. The workflow depends on this Protocol, never on a vendor SDK or wire format.

Content is expressed in provider-neutral parts; each adapter translates them:
  TextPart  - plain text
  PdfPart   - a PDF plus its extracted text layer. Adapters that can send the
              file natively do so; others send the text layer instead.

Two failure classes, because they need different handling upstream:
  LLMUnavailableError    - the call didn't complete (network, rate limit, auth, timeout)
  LLMInvalidOutputError  - the call completed but the output can't be trusted
Both trigger graceful degradation; they are logged and surfaced differently.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class TextPart:
    text: str


@dataclass(frozen=True, slots=True)
class PdfPart:
    data: bytes = field(repr=False)
    text_layer: str = field(repr=False)
    filename: str = "document.pdf"


Part = TextPart | PdfPart


@dataclass(frozen=True, slots=True)
class CallStats:
    """Observability for the most recent call (used by logs and the eval harness)."""

    requested_model: str
    served_model: str | None  # as reported by the endpoint - a gateway may route elsewhere
    latency_ms: int
    input_tokens: int | None
    output_tokens: int | None


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
    @property
    def model(self) -> str: ...

    def generate(
        self,
        *,
        output_type: type[T],
        system: str,
        content: Sequence[Part],
        max_tokens: int,
        purpose: str,
    ) -> T:
        """Return an instance of ``output_type`` or raise an ``LLMError`` subclass.

        ``purpose`` labels the call in logs and usage metrics (e.g. "parse_invoice").
        """
        ...
