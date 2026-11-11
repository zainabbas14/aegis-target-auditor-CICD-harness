"""HTTP boundary.

    POST /v1/runs                      upload an invoice PDF -> agent workflow run
    GET  /v1/runs/{run_id}             run state (status, verdict, findings, narrative, ...)
    POST /v1/runs/{run_id}/extraction  human data entry when automated parsing failed (resumes the run)
    POST /v1/runs/{run_id}/decision    human approval / rejection (resumes the HITL breakpoint)
    POST /v1/audits                    ExtractedInvoice -> AuditResult (deterministic path, no LLM)
    GET  /healthz                      liveness: the process is up
    GET  /readyz                       readiness: the ledger is reachable

Run:  uvicorn auditor.api.app:create_app --factory
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Path, Request, Response, UploadFile, status
from fastapi.responses import JSONResponse
from langgraph.checkpoint.base import BaseCheckpointSaver

from auditor import __version__
from auditor.adapters.factory import build_ledger
from auditor.agents.explainer import Explainer
from auditor.agents.parser import DocumentParser
from auditor.config import Settings, get_settings
from auditor.documents.store import PDF_MAGIC, DocumentStore
from auditor.domain.models import AuditResult, ExtractedInvoice
from auditor.llm.ports import StructuredLLM
from auditor.logging_setup import request_id_var, setup_logging
from auditor.payments import PaymentGateway, SimulatedPaymentGateway
from auditor.ports import LedgerReader
from auditor.rules.engine import RuleConfig, audit
from auditor.workflow.factory import build_checkpointer, build_llm
from auditor.workflow.graph import ApprovalDecision, ManualEntry, WorkflowDeps, build_graph
from auditor.workflow.service import (
    RunNotAwaitingApprovalError,
    RunNotAwaitingEntryError,
    RunNotFoundError,
    RunService,
    RunView,
    SegregationOfDutiesError,
)

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
_MAX_REQUEST_ID_LEN = 128
RunId = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]
_UNSET: Any = object()


def _get_ledger(request: Request) -> LedgerReader:
    ledger: LedgerReader = request.app.state.ledger
    return ledger


def _get_rule_config(request: Request) -> RuleConfig:
    cfg: RuleConfig = request.app.state.rule_config
    return cfg


def _get_runs(request: Request) -> RunService:
    runs: RunService = request.app.state.runs
    return runs


def create_app(  # noqa: PLR0915 - composition root + route table; splitting it hides the wiring
    settings: Settings | None = None,
    *,
    ledger: LedgerReader | None = None,
    llm: StructuredLLM | None = _UNSET,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    payments: PaymentGateway | None = None,
) -> FastAPI:
    """App factory. Production builds every dependency from settings; tests inject
    fakes (``llm=None`` explicitly simulates "no LLM configured")."""
    settings = settings or get_settings()
    setup_logging(settings.log_level, settings.log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        closers = []
        led = ledger or build_ledger(settings)
        if ledger is None:
            closers.append(led.close)
        if checkpointer is None:
            saver, close_saver = build_checkpointer(settings)
            closers.append(close_saver)
        else:
            saver = checkpointer
        parser_llm = build_llm(settings, settings.llm_parser_model) if llm is _UNSET else llm
        explainer_llm = build_llm(settings, settings.llm_explainer_model) if llm is _UNSET else llm
        rule_config = RuleConfig(near_duplicate_window_days=settings.near_duplicate_window_days)
        documents = DocumentStore(settings.documents_dir)
        deps = WorkflowDeps(
            ledger=led,
            documents=documents,
            parser=DocumentParser(parser_llm),
            explainer=Explainer(explainer_llm),
            payments=payments or SimulatedPaymentGateway(),
            rule_config=rule_config,
        )
        app.state.ledger = led
        app.state.rule_config = rule_config
        app.state.runs = RunService(build_graph(deps, saver), documents)
        logger.info(
            "Auditor API started",
            extra={
                "env": settings.env,
                "ledger_backend": settings.ledger_backend,
                "checkpointer": settings.checkpointer,
                "llm_configured": parser_llm is not None,
            },
        )
        try:
            yield
        finally:
            for close in reversed(closers):
                close()

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

    @app.post(
        "/v1/runs",
        response_model=RunView,
        status_code=status.HTTP_201_CREATED,
        tags=["agent workflow"],
        responses={413: {"description": "file too large"}, 415: {"description": "not a PDF"}},
    )
    def create_run(file: UploadFile, runs: Annotated[RunService, Depends(_get_runs)]) -> RunView:
        """Upload an invoice PDF. Runs parse -> audit -> explain synchronously and returns the run;
        a clean invoice stops at AWAITING_APPROVAL."""
        data = file.file.read(settings.max_upload_bytes + 1)
        if len(data) > settings.max_upload_bytes:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "file too large")
        if not data.startswith(PDF_MAGIC):
            raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "only PDF invoices are accepted")
        return runs.start(data, (file.filename or "invoice.pdf")[:255])

    @app.get("/v1/runs/{run_id}", response_model=RunView, tags=["agent workflow"])
    def get_run(run_id: RunId, runs: Annotated[RunService, Depends(_get_runs)]) -> RunView:
        try:
            return runs.get(run_id)
        except RunNotFoundError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found") from None

    @app.post(
        "/v1/runs/{run_id}/extraction",
        response_model=RunView,
        tags=["agent workflow"],
        responses={409: {"description": "run is not awaiting manual entry"}},
    )
    def submit_extraction(
        run_id: RunId, entry: ManualEntry, runs: Annotated[RunService, Depends(_get_runs)]
    ) -> RunView:
        """Human data entry for a run in NEEDS_MANUAL_ENTRY. The run then continues through the audit."""
        try:
            return runs.submit_extraction(run_id, entry)
        except RunNotFoundError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found") from None
        except RunNotAwaitingEntryError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, f"run is not awaiting manual entry (status: {exc})") from None

    @app.post(
        "/v1/runs/{run_id}/decision",
        response_model=RunView,
        tags=["agent workflow"],
        responses={
            403: {"description": "approver keyed this invoice in (four-eyes rule)"},
            409: {"description": "run is not awaiting approval"},
        },
    )
    def decide_run(
        run_id: RunId, decision: ApprovalDecision, runs: Annotated[RunService, Depends(_get_runs)]
    ) -> RunView:
        """Human-in-the-loop: approve (pay) or reject a run paused at AWAITING_APPROVAL."""
        try:
            return runs.decide(run_id, decision)
        except RunNotFoundError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found") from None
        except RunNotAwaitingApprovalError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, f"run is not awaiting approval (status: {exc})") from None
        except SegregationOfDutiesError as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from None

    return app
