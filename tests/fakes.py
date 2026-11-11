"""Scripted test doubles. CI never calls a real model."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from auditor.agents.explainer import NarrativeDraft
from auditor.agents.parser import InvoiceExtraction
from auditor.llm.ports import LLMError, T


def extraction_for(request_body: dict[str, Any]) -> InvoiceExtraction:
    """What a correct LLM transcription of a scenario PDF looks like."""
    body = dict(request_body)
    body["line_items"] = [dict(ln) for ln in body["line_items"]]
    return InvoiceExtraction.model_validate(body)


class FakeLLM:
    """Answers parse calls by recognising the PDF (sha256) and explain calls generically.

    ``parse_override`` / ``explain_override`` let a test inject failures or bad output.
    """

    def __init__(self, built: Path) -> None:
        self.by_sha: dict[str, InvoiceExtraction] = {}
        for req in sorted((built / "requests").glob("*.json")):
            pdf = (built / "invoices" / req.name.replace(".json", ".pdf")).read_bytes()
            self.by_sha[hashlib.sha256(pdf).hexdigest()] = extraction_for(json.loads(req.read_text()))
        self.calls: list[str] = []
        self.parse_override: Callable[[InvoiceExtraction], InvoiceExtraction] | LLMError | None = None
        self.explain_override: LLMError | NarrativeDraft | None = None

    def generate(
        self, *, output_type: type[T], system: str, content: list[dict[str, Any]], max_tokens: int, purpose: str
    ) -> T:
        self.calls.append(purpose)
        if output_type is InvoiceExtraction:
            if isinstance(self.parse_override, LLMError):
                raise self.parse_override
            pdf = base64.b64decode(content[0]["source"]["data"])
            result = self.by_sha[hashlib.sha256(pdf).hexdigest()]
            if callable(self.parse_override):
                result = self.parse_override(result)
            return result  # type: ignore[return-value]
        if output_type is NarrativeDraft:
            if isinstance(self.explain_override, LLMError):
                raise self.explain_override
            if isinstance(self.explain_override, NarrativeDraft):
                return self.explain_override  # type: ignore[return-value]
            verdict = json.loads(content[0]["text"])["verdict"]
            return NarrativeDraft(summary=f"LLM summary for {verdict}.", recommended_action="Act.")  # type: ignore[return-value]
        raise AssertionError(f"unexpected output type {output_type}")
