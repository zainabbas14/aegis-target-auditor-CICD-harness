"""Structured logging. No print() anywhere in src/.

JSON lines in containers (ship as-is to Loki/ELK/Datadog); readable console
format for local dev. Every record carries the request id of the HTTP request
that produced it, via a contextvar set by the API middleware.
"""

from __future__ import annotations

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

# Attributes every LogRecord has; anything else was passed via extra={...}.
_RESERVED = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName", "color_message"}


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
        }
        payload.update({k: v for k, v in vars(record).items() if k not in _RESERVED and k != "request_id"})
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Idempotent: replaces only the handler it installed previously, so it
    never clobbers handlers owned by the host (pytest, an APM agent, ...)."""
    handler = logging.StreamHandler(sys.stdout)
    handler.set_name("auditor")
    handler.addFilter(_RequestIdFilter())
    handler.setFormatter(
        JsonFormatter()
        if fmt == "json"
        else logging.Formatter("%(asctime)s %(levelname)-7s [%(request_id)s] %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.handlers[:] = [h for h in root.handlers if h.get_name() != "auditor"] + [handler]
    root.setLevel(level)
    # Route uvicorn through our handler; its access log is replaced by our middleware.
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True
    logging.getLogger("uvicorn.access").disabled = True
