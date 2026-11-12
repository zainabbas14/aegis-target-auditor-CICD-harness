"""Anthropic adapter (no network) and the parser's deterministic guards."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest

from auditor.agents.parser import InvoiceExtraction, LineItemExtraction, to_extracted_invoice, ungrounded_fields
from auditor.domain.models import Finding
from auditor.llm.anthropic_llm import AnthropicLLM
from auditor.llm.ports import LLMInvalidOutputError, LLMUnavailableError, PdfPart, TextPart


class _FakeMessages:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.kwargs: dict[str, Any] = {}

    def parse(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _llm(outcome: Any) -> tuple[AnthropicLLM, _FakeMessages]:
    messages = _FakeMessages(outcome)
    client = SimpleNamespace(messages=messages)
    return AnthropicLLM(api_key="test", model="claude-sonnet-5-5", client=client), messages


def _response(parsed: Any, stop_reason: str = "end_turn") -> Any:
    return SimpleNamespace(
        parsed_output=parsed, stop_reason=stop_reason, usage=SimpleNamespace(input_tokens=10, output_tokens=5)
    )


def _call(llm: AnthropicLLM) -> Any:
    return llm.generate(
        output_type=LineItemExtraction,
        system="sys",
        content=[TextPart("hi")],
        max_tokens=100,
        purpose="test",
    )


GOOD = LineItemExtraction(item_code=None, description="x", quantity="1", unit_price="1.00", amount="1.00")
_REQ = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def test_success_returns_parsed_model_and_sends_structured_output_request() -> None:
    llm, messages = _llm(_response(GOOD))
    assert _call(llm) == GOOD
    assert messages.kwargs["output_format"] is LineItemExtraction
    assert messages.kwargs["model"] == "claude-sonnet-5-5"
    assert messages.kwargs["system"] == "sys"


@pytest.mark.parametrize(
    "exc",
    [
        anthropic.APIConnectionError(request=_REQ),
        anthropic.RateLimitError("slow down", response=httpx2.Response(429, request=_REQ), body=None),
        anthropic.InternalServerError("boom", response=httpx2.Response(500, request=_REQ), body=None),
    ],
    ids=["connection", "rate_limit", "server"],
)
def test_transient_failures_map_to_unavailable(exc: Exception) -> None:
    with pytest.raises(LLMUnavailableError, match="transient_failure"):
        _call(_llm(exc)[0])


def test_auth_failure_maps_to_rejected() -> None:
    exc = anthropic.AuthenticationError("bad key", response=httpx2.Response(401, request=_REQ), body=None)
    with pytest.raises(LLMUnavailableError, match="request_rejected"):
        _call(_llm(exc)[0])


@pytest.mark.parametrize(("stop", "reason"), [("max_tokens", "truncated"), ("refusal", "refused")])
def test_untrustworthy_stop_reasons(stop: str, reason: str) -> None:
    with pytest.raises(LLMInvalidOutputError, match=reason):
        _call(_llm(_response(GOOD, stop_reason=stop))[0])


def test_missing_parsed_output() -> None:
    with pytest.raises(LLMInvalidOutputError, match="no_parsed_output"):
        _call(_llm(_response(None))[0])


# --------------------------------------------------------------------------- #
# Parser guards
# --------------------------------------------------------------------------- #
def _raw(**overrides: Any) -> InvoiceExtraction:
    base: dict[str, Any] = {
        "invoice_number": "INV-7",
        "vendor_name": "Acme",
        "vendor_tax_id": "TX-1",
        "po_number": "PO-1",
        "invoice_date": "2026-10-06",
        "due_date": None,
        "remit_account": "ACCT-0001",
        "line_items": [
            {"item_code": "", "description": "Widget", "quantity": "2", "unit_price": "$4,500.00", "amount": "9,000.00"}
        ],
        "subtotal": "$9,000.00",
        "tax": "0.00",
        "total": "USD 9,000.00",
        "currency": "usd",
    }
    return InvoiceExtraction.model_validate(base | overrides)


def test_money_and_codes_are_normalised_deterministically() -> None:
    inv = to_extracted_invoice(_raw())
    assert inv.total == Decimal("9000.00")
    assert inv.line_items[0].unit_price == Decimal("4500.00")
    assert inv.line_items[0].item_code is None  # empty string -> "not printed"
    assert inv.currency == "USD"


TEXT = "INVOICE INV-7  PO-1  Tax ID TX-1  Date 10/06/2026  Widget 2 4,500.00 9,000.00  Total 9,000.00  Remit ACCT-0001"


def test_grounded_extraction_passes() -> None:
    assert ungrounded_fields(to_extracted_invoice(_raw()), TEXT) == []


def test_each_ungrounded_field_is_named() -> None:
    inv = to_extracted_invoice(_raw(invoice_number="INV-8", remit_account="ACCT-9999"))
    assert set(ungrounded_fields(inv, TEXT)) == {"invoice_number", "remit_account"}


def test_date_must_appear_in_some_rendering() -> None:
    inv = to_extracted_invoice(_raw(invoice_date="2026-06-10"))  # day/month swapped
    assert "invoice_date" in ungrounded_fields(inv, TEXT.replace("10/06/2026", "Oct 6 2026"))
    assert inv.invoice_date == date(2026, 6, 10)


def test_tampered_severity_is_recomputed() -> None:
    f = Finding.model_validate({"code": "DUPLICATE_INVOICE", "message": "m", "severity": "REVIEW"})
    assert f.severity.value == "BLOCK"


def test_real_sdk_request_and_parse_path_via_mock_transport() -> None:
    """Exercises the actual anthropic SDK (schema transform, request, response parsing) with no network."""
    seen: dict[str, Any] = {}
    expected = _raw()

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.update(json.loads(request.content))
        return httpx2.Response(
            200,
            json={
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": seen["model"],
                "content": [{"type": "text", "text": expected.model_dump_json()}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1200, "output_tokens": 300},
            },
        )

    client = anthropic.Anthropic(api_key="test", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    llm = AnthropicLLM(api_key="test", model="claude-sonnet-5-5", client=client)
    pdf = PdfPart(data=b"%PDF-1.4 test", text_layer="ignored by the Anthropic adapter")
    got = llm.generate(output_type=InvoiceExtraction, system="sys", content=[pdf], max_tokens=4096, purpose="t")

    assert got == expected
    assert seen["output_config"]["format"]["type"] == "json_schema"
    assert seen["messages"][0]["content"][0]["type"] == "document"
