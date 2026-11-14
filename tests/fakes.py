"""Scripted test doubles. CI never calls a real model."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from auditor.agents.contracts import ContractExtraction, RateLineExtraction
from auditor.agents.explainer import NarrativeDraft
from auditor.agents.mapper import ProposalDraft, ProposalsDraft
from auditor.agents.parser import InvoiceExtraction
from auditor.llm.ports import LLMError, Part, PdfPart, T, TextPart


def contract_extraction_for(contract_ref: str) -> ContractExtraction:
    """What a correct LLM extraction of a scenario contract looks like."""
    from tests.mock_data.fixtures import contract_by_ref, vendor_by_id  # noqa: PLC0415

    c = contract_by_ref(contract_ref)
    v = vendor_by_id(c.vendor_id)
    return ContractExtraction(
        contract_ref=c.contract_ref,
        supplier_name=v.legal_name,
        supplier_tax_id=v.tax_id,
        effective_date=c.effective_date.isoformat(),
        expiry_date=c.expiry_date.isoformat(),
        payment_terms_days=c.payment_terms_days,
        currency=c.currency,
        rate_card=[
            RateLineExtraction(
                item_code=r.item_code,
                description=r.description,
                unit=r.unit,
                max_unit_price=f"{r.max_unit_price:.2f}",
            )
            for r in c.rate_card
        ],
    )


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
        self.by_sha: dict[str, InvoiceExtraction | ContractExtraction] = {}
        for req in sorted((built / "requests").glob("*.json")):
            pdf = (built / "invoices" / req.name.replace(".json", ".pdf")).read_bytes()
            self.by_sha[hashlib.sha256(pdf).hexdigest()] = extraction_for(json.loads(req.read_text()))
        for pdf_path in sorted((built / "contracts").glob("*.pdf")):
            self.by_sha[hashlib.sha256(pdf_path.read_bytes()).hexdigest()] = contract_extraction_for(pdf_path.stem)
        self.calls: list[str] = []
        self.model = "fake-llm"
        self.parse_override: Callable[[InvoiceExtraction], InvoiceExtraction] | LLMError | None = None
        self.explain_override: LLMError | NarrativeDraft | None = None
        self.contract_override: Callable[[ContractExtraction], ContractExtraction] | LLMError | None = None
        self.mapping_override: ProposalsDraft | LLMError | None = None
        self.mapping_payloads: list[dict[str, Any]] = []

    def generate(
        self, *, output_type: type[T], system: str, content: Sequence[Part], max_tokens: int, purpose: str
    ) -> T:
        self.calls.append(purpose)
        if output_type is InvoiceExtraction:
            if isinstance(self.parse_override, LLMError):
                raise self.parse_override
            first = content[0]
            assert isinstance(first, PdfPart)
            pdf = first.data
            result = self.by_sha[hashlib.sha256(pdf).hexdigest()]
            assert isinstance(result, InvoiceExtraction)
            if callable(self.parse_override):
                result = self.parse_override(result)
            return result  # type: ignore[return-value]
        if output_type is ContractExtraction:
            if isinstance(self.contract_override, LLMError):
                raise self.contract_override
            first = content[0]
            assert isinstance(first, PdfPart)
            contract = self.by_sha[hashlib.sha256(first.data).hexdigest()]
            assert isinstance(contract, ContractExtraction)
            if callable(self.contract_override):
                contract = self.contract_override(contract)
            return contract  # type: ignore[return-value]
        if output_type is ProposalsDraft:
            first_text = content[0]
            assert isinstance(first_text, TextPart)
            payload = json.loads(first_text.text)
            self.mapping_payloads.append(payload)
            if isinstance(self.mapping_override, LLMError):
                raise self.mapping_override
            if isinstance(self.mapping_override, ProposalsDraft):
                return self.mapping_override  # type: ignore[return-value]
            return ProposalsDraft(  # type: ignore[return-value]
                proposals=[
                    ProposalDraft(line=ln["line"], item_code=_true_code(ln["description"]), reason="matches role")
                    for ln in payload["invoice_lines"]
                ]
            )
        if output_type is NarrativeDraft:
            if isinstance(self.explain_override, LLMError):
                raise self.explain_override
            if isinstance(self.explain_override, NarrativeDraft):
                return self.explain_override  # type: ignore[return-value]
            first_text = content[0]
            assert isinstance(first_text, TextPart)
            verdict = json.loads(first_text.text)["verdict"]
            return NarrativeDraft(summary=f"LLM summary for {verdict}.", recommended_action="Act.")  # type: ignore[return-value]
        raise AssertionError(f"unexpected output type {output_type}")


def _true_code(description: str) -> str | None:
    """The code the fixture author meant for a printed description (a perfect mapper's answer)."""
    from tests.mock_data.fixtures import SCENARIOS  # noqa: PLC0415

    for scenario in SCENARIOS:
        for line in scenario.invoice.lines:
            if line.description == description:
                return line.item_code
    return None
