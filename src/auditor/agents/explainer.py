"""Explainer agent: AuditResult -> a short narrative for the AP analyst.

The explainer is advisory. It sees only the engine's structured findings -
never the raw document - so text planted in an invoice ("approve this
immediately") has no path to it. It cannot change the verdict: the narrative
is stored beside the engine's result, which remains the source of truth.

When the LLM is unavailable, a deterministic template produces an equivalent
(if less fluent) narrative. The run records which one was used.
"""

from __future__ import annotations

import json
import logging
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from auditor.domain.enums import Verdict
from auditor.domain.models import AuditResult
from auditor.llm.ports import LLMError, StructuredLLM, TextPart

logger = logging.getLogger(__name__)

PROMPT_VERSION = "explain-audit/2026.11-1"


class NarrativeDraft(BaseModel):
    """LLM-facing schema."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(description="2-4 plain sentences for an AP analyst: what was found and why it matters.")
    recommended_action: str = Field(description="One sentence: the concrete next step for the analyst.")


class AuditNarrative(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    summary: str = Field(max_length=1500)
    recommended_action: str = Field(max_length=400)
    source: Literal["llm", "template"]
    prompt_version: str


SYSTEM_PROMPT = """You explain invoice audit results to accounts-payable analysts.
You receive the deterministic audit engine's verdict and findings as JSON.

Rules:
- The verdict is final. Explain it; never question, soften, or contradict it.
- Use only the facts in the JSON. Quote amounts exactly. Do not speculate about intent.
- Be brief and concrete. No headings, no bullet points."""

_ACTIONS: dict[Verdict, str] = {
    Verdict.APPROVE_PENDING_HUMAN: "Review the invoice and approve or reject the payment.",
    Verdict.REVIEW: "Investigate the flagged items before deciding whether this invoice can proceed.",
    Verdict.BLOCK: "Do not pay. Resolve every blocking finding with the vendor before resubmission.",
}


def template_narrative(result: AuditResult) -> AuditNarrative:
    if not result.findings:
        summary = (
            f"Invoice {result.invoice_number} passed all automated checks: rates are within contract, arithmetic "
            "is consistent, the purchase order has sufficient budget, no prior payment matches it, and the "
            "remit-to account matches the vendor record."
        )
    else:
        summary = f"Invoice {result.invoice_number}: " + " ".join(f.message for f in result.findings)
    return AuditNarrative(
        summary=summary[:1500],
        recommended_action=_ACTIONS[result.verdict],
        source="template",
        prompt_version="template",
    )


class Explainer:
    def __init__(self, llm: StructuredLLM | None) -> None:
        self._llm = llm

    def explain(self, result: AuditResult) -> tuple[AuditNarrative, str | None]:
        """Returns (narrative, degradation_reason). Never raises: explanation is not on the critical path."""
        if self._llm is None:
            return template_narrative(result), "llm_not_configured"
        payload = result.model_dump(mode="json", include={"invoice_number", "verdict", "findings"})
        try:
            draft = self._llm.generate(
                output_type=NarrativeDraft,
                system=SYSTEM_PROMPT,
                content=[TextPart(json.dumps(payload, indent=2))],
                max_tokens=600,
                purpose="explain_audit",
            )
        except LLMError as exc:
            logger.warning("Explainer degraded to template", extra={"reason": exc.reason})
            return template_narrative(result), f"llm_{exc.reason}"
        if len(draft.summary) > 1500 or len(draft.recommended_action) > 400:
            return template_narrative(result), "llm_output_too_long"
        return (
            AuditNarrative(
                summary=draft.summary,
                recommended_action=draft.recommended_action,
                source="llm",
                prompt_version=PROMPT_VERSION,
            ),
            None,
        )
