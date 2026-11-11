"""HTTP boundary.

    POST /v1/audits   ExtractedInvoice -> AuditResult   (deterministic path)
    GET  /healthz     liveness: the process is up
    GET  /readyz      readiness: the ledger is reachable

Run:  uvicorn auditor.api.app:create_app --factory
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse

from auditor import __version__
from auditor.adapters.factory import build_ledger
from auditor.config import Settings, get_settings
from auditor.domain.models import AuditResult, ExtractedInvoice
from auditor.logging_setup import request_id_var, setup_logging
from auditor.ports import LedgerReader
from auditor.rules.engine import RuleConfig, audit

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
_MAX_REQUEST_ID_LEN = 128


def _get_ledger(request: Request) -> LedgerReader:
    ledger: LedgerReader = request.app.state.ledger
    return ledger


def _get_rule_config(request: Request) -> RuleConfig:
    cfg: RuleConfig = request.app.state.rule_config
    return cfg


def create_app(settings: Settings | None = None, ledger: LedgerReader | None = None) -> FastAPI:
    """App factory. Tests inject ``ledger``; production builds it from settings."""
    settings = settings or get_settings()
    setup_logging(settings.log_level, settings.log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        owned = ledger is None
        app.state.ledger = ledger or build_ledger(settings)
        app.state.rule_config = RuleConfig(near_duplicate_window_days=settings.near_duplicate_window_days)
        logger.info("Auditor API started", extra={"env": settings.env, "ledger_backend": settings.ledger_backend})
        try:
            yield
        finally:
            if owned:
                app.state.ledger.close()

    app = FastAPI(
        title="Multi-Agent Finance Auditor",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if settings.env != "prod" else None,
        redoc_url=None,
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER, "")
        rid = incoming if 0 < len(incoming) <= _MAX_REQUEST_ID_LEN and incoming.isprintable() else uuid.uuid4().hex
        token = request_id_var.set(rid)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            # Never leak internals to the client; full traceback goes to the log.
            logger.exception("Unhandled error")
            response = JSONResponse({"detail": "internal error", "request_id": rid}, status_code=500)
        response.headers[REQUEST_ID_HEADER] = rid
        logger.info(
            "%s %s %d",
            request.method,
            request.url.path,
            response.status_code,
            extra={"duration_ms": round((time.perf_counter() - start) * 1000, 2)},
        )
        request_id_var.reset(token)
        return response

    @app.get("/healthz", tags=["ops"])
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", tags=["ops"], responses={503: {"description": "ledger unreachable"}})
    def readyz(led: Annotated[LedgerReader, Depends(_get_ledger)]) -> JSONResponse:
        try:
            led.ping()
        except Exception:
            logger.warning("Readiness check failed", exc_info=True)
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ready"})

    @app.post("/v1/audits", response_model=AuditResult, tags=["audit"])
    def create_audit(
        invoice: ExtractedInvoice,
        led: Annotated[LedgerReader, Depends(_get_ledger)],
        cfg: Annotated[RuleConfig, Depends(_get_rule_config)],
    ) -> AuditResult:
        # Sync def: FastAPI runs it in a worker thread, so blocking DB I/O is fine.
        return audit(invoice, led, cfg)

    return app
