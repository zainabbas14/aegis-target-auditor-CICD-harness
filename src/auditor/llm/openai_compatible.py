"""Adapter for any OpenAI-compatible ``/chat/completions`` endpoint.

Works with OpenRouter, api.airforce, vLLM, Ollama, LM Studio, Azure-style gateways -
anything that speaks the OpenAI chat format. Implemented on the HTTP client the
project already ships (httpx2) rather than another vendor SDK.

Gateways vary wildly, so everything that varies is configuration:
  * json_mode   - "json_schema" (constrained decoding where supported), "json_object",
                  or "none". The schema is ALSO written into the system prompt, so
                  models without constrained decoding still know the shape.
  * pdf_mode    - "text" sends the PDF's text layer (works everywhere);
                  "file" sends the PDF itself as a file part (only where supported).
  * min_interval_s - client-side throttle for strict free tiers (e.g. 1 request/minute).

Whatever comes back is treated as untrusted text: JSON is extracted defensively
and validated against the Pydantic model. Nothing downstream changes - grounding
and the rules still decide.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any, Literal

import httpx2
from pydantic import ValidationError

from auditor.llm.ports import CallStats, LLMInvalidOutputError, LLMUnavailableError, Part, PdfPart, T, TextPart

logger = logging.getLogger(__name__)

JsonMode = Literal["json_schema", "json_object", "none"]
PdfMode = Literal["text", "file"]

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_RETRYABLE = {408, 409, 425, 429, 500, 502, 503, 504}
_MAX_BACKOFF_S = 60.0


def extract_json_object(text: str) -> Any:
    """Pull one JSON object out of model text: tolerates <think> blocks, code fences, preambles."""
    text = _THINK.sub("", text).strip()
    candidates = [m.group(1) for m in _FENCE.finditer(text)] + [text]
    for candidate in candidates:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(candidate[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise LLMInvalidOutputError("no_json_object")


def to_openai_parts(content: Sequence[Part], pdf_mode: PdfMode) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, TextPart):
            parts.append({"type": "text", "text": part.text})
        elif isinstance(part, PdfPart) and pdf_mode == "file":
            data_url = "data:application/pdf;base64," + base64.b64encode(part.data).decode("ascii")
            parts.append({"type": "file", "file": {"filename": part.filename, "file_data": data_url}})
        elif isinstance(part, PdfPart):
            parts.append(
                {
                    "type": "text",
                    "text": f"Document '{part.filename}' (text layer, layout approximate):\n"
                    f"<<<\n{part.text_layer}\n>>>",
                }
            )
    return parts


class OpenAICompatibleLLM:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_s: float = 60.0,
        max_retries: int = 2,
        min_interval_s: float = 0.0,
        json_mode: JsonMode = "json_schema",
        pdf_mode: PdfMode = "text",
        client: httpx2.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self._model = model
        self._max_retries = max_retries
        self._min_interval_s = min_interval_s
        self._json_mode = json_mode
        self._pdf_mode = pdf_mode
        self._client = client or httpx2.Client(timeout=timeout_s)
        self._sleep = sleep
        self._clock = clock
        self._throttle_lock = threading.Lock()
        self._last_request_at: float | None = None
        self.last_stats: CallStats | None = None

    @property
    def model(self) -> str:
        return self._model

    # ---- transport -------------------------------------------------------------
    def _throttle(self) -> None:
        with self._throttle_lock:
            now = self._clock()
            if self._last_request_at is not None and self._min_interval_s > 0:
                wait = self._min_interval_s - (now - self._last_request_at)
                if wait > 0:
                    logger.info("Throttling LLM call", extra={"wait_s": round(wait, 1)})
                    self._sleep(wait)
            self._last_request_at = self._clock()

    def _post(self, body: dict[str, Any], purpose: str) -> dict[str, Any]:
        last_error = "unknown"
        for attempt in range(self._max_retries + 1):
            self._throttle()
            try:
                response = self._client.post(self._url, json=body, headers=self._headers)
            except httpx2.TransportError as exc:
                last_error = type(exc).__name__
            else:
                if response.status_code < 400:
                    payload: dict[str, Any] = response.json()
                    return payload
                if response.status_code not in _RETRYABLE:
                    # Auth, bad request, unsupported parameter: retrying won't help.
                    logger.error(
                        "LLM request rejected",
                        extra={"purpose": purpose, "status": response.status_code, "body": response.text[:300]},
                    )
                    raise LLMUnavailableError("request_rejected", f"HTTP {response.status_code}")
                last_error = f"HTTP {response.status_code}"
                retry_after = response.headers.get("retry-after")
                if attempt < self._max_retries and retry_after and retry_after.replace(".", "", 1).isdigit():
                    self._sleep(min(float(retry_after), _MAX_BACKOFF_S))
                    continue
            if attempt < self._max_retries:
                self._sleep(min(2.0**attempt, _MAX_BACKOFF_S))
        raise LLMUnavailableError("transient_failure", last_error)

    # ---- port ------------------------------------------------------------------
    def generate(
        self,
        *,
        output_type: type[T],
        system: str,
        content: Sequence[Part],
        max_tokens: int,
        purpose: str,
    ) -> T:
        schema = output_type.model_json_schema()
        body: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "temperature": 0,
            "messages": [
                {
                    "role": "system",
                    "content": f"{system}\n\nRespond with ONLY one JSON object (no prose, no code fences) "
                    f"that validates against this JSON Schema:\n{json.dumps(schema)}",
                },
                {"role": "user", "content": to_openai_parts(content, self._pdf_mode)},
            ],
        }
        if self._json_mode == "json_schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": output_type.__name__, "strict": False, "schema": schema},
            }
        elif self._json_mode == "json_object":
            body["response_format"] = {"type": "json_object"}

        started = time.perf_counter()
        payload = self._post(body, purpose)
        usage = payload.get("usage") or {}
        self.last_stats = CallStats(
            requested_model=self._model,
            served_model=payload.get("model"),
            latency_ms=round((time.perf_counter() - started) * 1000),
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
        )
        try:
            choice = payload["choices"][0]
            message = choice.get("message") or {}
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMInvalidOutputError("malformed_response") from exc
        logger.info(
            "LLM call complete",
            extra={
                "purpose": purpose,
                "model": self._model,
                "served_model": self.last_stats.served_model,
                "finish_reason": choice.get("finish_reason"),
                "latency_ms": self.last_stats.latency_ms,
                "input_tokens": self.last_stats.input_tokens,
                "output_tokens": self.last_stats.output_tokens,
            },
        )
        if choice.get("finish_reason") == "length":
            raise LLMInvalidOutputError("truncated")
        if message.get("refusal"):
            raise LLMInvalidOutputError("refused")
        text = message.get("content")
        if isinstance(text, list):  # some gateways return content parts
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        if not isinstance(text, str) or not text.strip():
            raise LLMInvalidOutputError("empty_output")
        try:
            return output_type.model_validate(extract_json_object(text))
        except ValidationError as exc:
            raise LLMInvalidOutputError("schema_violation", f"{exc.error_count()} errors") from exc
