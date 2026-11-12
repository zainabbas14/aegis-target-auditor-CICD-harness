"""OpenAI-compatible adapter, exercised end to end over a mock HTTP transport (no network)."""

from __future__ import annotations

import json
from typing import Any

import httpx2
import pytest

from auditor.agents.explainer import NarrativeDraft
from auditor.config import Settings
from auditor.llm.openai_compatible import OpenAICompatibleLLM, extract_json_object
from auditor.llm.ports import LLMInvalidOutputError, LLMUnavailableError, PdfPart, TextPart
from auditor.workflow.factory import build_llm

GOOD = {"summary": "All checks passed.", "recommended_action": "Approve."}


def _completion(content: Any, *, finish: str = "stop", model: str = "gateway/actual-model") -> httpx2.Response:
    return httpx2.Response(
        200,
        json={
            "id": "c1",
            "model": model,
            "choices": [{"index": 0, "finish_reason": finish, "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 321, "completion_tokens": 45},
        },
    )


class Harness:
    def __init__(self, responses: list[httpx2.Response | Exception], **kwargs: Any) -> None:
        self.requests: list[dict[str, Any]] = []
        self.sleeps: list[float] = []
        self.now = 0.0
        queue = list(responses)

        def handler(request: httpx2.Request) -> httpx2.Response:
            self.requests.append(json.loads(request.content))
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        def sleep(seconds: float) -> None:
            self.sleeps.append(seconds)
            self.now += seconds

        self.llm = OpenAICompatibleLLM(
            base_url="https://gateway.example/v1/",
            api_key="test-key",
            model="some/free-model",
            client=httpx2.Client(transport=httpx2.MockTransport(handler)),
            sleep=sleep,
            clock=lambda: self.now,
            **kwargs,
        )

    def call(self, content: list[Any] | None = None) -> NarrativeDraft:
        return self.llm.generate(
            output_type=NarrativeDraft,
            system="SYS",
            content=content or [TextPart("hello")],
            max_tokens=100,
            purpose="test",
        )


def test_request_shape_and_success() -> None:
    h = Harness([_completion(json.dumps(GOOD))])
    assert h.call() == NarrativeDraft(**GOOD)
    body = h.requests[0]
    assert body["model"] == "some/free-model"
    assert body["temperature"] == 0
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["name"] == "NarrativeDraft"
    system = body["messages"][0]["content"]
    assert system.startswith("SYS")
    assert "JSON Schema" in system
    assert "recommended_action" in system
    stats = h.llm.last_stats
    assert stats is not None
    assert stats.served_model == "gateway/actual-model"  # what the gateway says actually answered
    assert (stats.input_tokens, stats.output_tokens) == (321, 45)


def test_pdf_sent_as_text_layer_by_default() -> None:
    h = Harness([_completion(json.dumps(GOOD))])
    h.call([PdfPart(data=b"%PDF-1.4 bytes", text_layer="INVOICE INV-7 total 9,000.00")])
    part = h.requests[0]["messages"][1]["content"][0]
    assert part["type"] == "text"
    assert "INVOICE INV-7 total 9,000.00" in part["text"]


def test_pdf_sent_as_file_when_configured() -> None:
    h = Harness([_completion(json.dumps(GOOD))], pdf_mode="file")
    h.call([PdfPart(data=b"%PDF-1.4 bytes", text_layer="x")])
    part = h.requests[0]["messages"][1]["content"][0]
    assert part["type"] == "file"
    assert part["file"]["file_data"].startswith("data:application/pdf;base64,")


@pytest.mark.parametrize(("mode", "expected"), [("json_object", {"type": "json_object"}), ("none", None)])
def test_json_modes(mode: str, expected: dict[str, str] | None) -> None:
    h = Harness([_completion(json.dumps(GOOD))], json_mode=mode)
    h.call()
    assert h.requests[0].get("response_format") == expected


@pytest.mark.parametrize(
    "text",
    [
        "<think>let me reason about this...</think>\n" + json.dumps(GOOD),
        "Sure! Here you go:\n```json\n" + json.dumps(GOOD) + "\n```",
        "Result: " + json.dumps(GOOD) + " Hope that helps.",
    ],
    ids=["think-block", "code-fence", "prose-wrapped"],
)
def test_tolerant_json_extraction(text: str) -> None:
    assert extract_json_object(text) == GOOD


def test_content_returned_as_parts() -> None:
    h = Harness([_completion([{"type": "text", "text": json.dumps(GOOD)}])])
    assert h.call().summary == GOOD["summary"]


def test_429_honours_retry_after_then_succeeds() -> None:
    h = Harness([httpx2.Response(429, headers={"retry-after": "7"}), _completion(json.dumps(GOOD))])
    h.call()
    assert h.sleeps == [7.0]
    assert len(h.requests) == 2


def test_persistent_5xx_exhausts_retries() -> None:
    h = Harness([httpx2.Response(503)] * 3, max_retries=2)
    with pytest.raises(LLMUnavailableError, match="transient_failure"):
        h.call()
    assert len(h.requests) == 3


def test_transport_error_is_retried() -> None:
    req = httpx2.Request("POST", "https://gateway.example/v1/chat/completions")
    h = Harness([httpx2.ConnectError("refused", request=req), _completion(json.dumps(GOOD))])
    h.call()
    assert len(h.requests) == 2


def test_auth_failure_is_not_retried() -> None:
    h = Harness([httpx2.Response(401, json={"error": "bad key"})])
    with pytest.raises(LLMUnavailableError, match="request_rejected"):
        h.call()
    assert len(h.requests) == 1


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (_completion(json.dumps(GOOD), finish="length"), "truncated"),
        (_completion("I cannot help with that."), "no_json_object"),
        (_completion(json.dumps({"summary": "x"})), "schema_violation"),
        (_completion(None), "empty_output"),
        (httpx2.Response(200, json={"choices": []}), "malformed_response"),
    ],
    ids=["truncated", "prose-only", "missing-field", "empty", "no-choices"],
)
def test_untrustworthy_output(response: httpx2.Response, reason: str) -> None:
    with pytest.raises(LLMInvalidOutputError, match=reason):
        Harness([response]).call()


def test_client_side_throttle_spaces_requests() -> None:
    h = Harness([_completion(json.dumps(GOOD)), _completion(json.dumps(GOOD))], min_interval_s=60)
    h.call()
    h.now += 5  # the next call comes 5 s later
    h.call()
    assert h.sleeps == [pytest.approx(55.0)]


# --------------------------------------------------------------------------- #
# Configuration / factory
# --------------------------------------------------------------------------- #
def _settings(**kw: object) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[arg-type]


def test_factory_builds_openai_compatible_adapter() -> None:
    s = _settings(
        llm_provider="openai_compatible",
        llm_base_url="https://api.airforce/v1",
        llm_api_key="sk-x",
        llm_min_interval_s=61,
    )
    llm = build_llm(s, "some/free-model")
    assert isinstance(llm, OpenAICompatibleLLM)
    assert llm.model == "some/free-model"


def test_factory_degrades_without_credentials() -> None:
    assert build_llm(_settings(llm_provider="openai_compatible", llm_api_key=""), "m") is None


def test_key_without_base_url_is_a_config_error() -> None:
    with pytest.raises(ValueError, match="AUDITOR_LLM_BASE_URL"):
        _settings(llm_provider="openai_compatible", llm_api_key="sk-x")


def test_api_key_never_renders() -> None:
    s = _settings(llm_provider="openai_compatible", llm_base_url="https://x/v1", llm_api_key="sk-super-secret")
    assert "sk-super-secret" not in repr(s)
