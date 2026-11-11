"""Application service: the only way the API touches the workflow."""

from __future__ import annotations

import logging
import threading
import uuid
from collections import defaultdict
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict

from auditor.documents.store import DocumentStore
from auditor.domain.enums import RunStatus
from auditor.workflow.graph import ApprovalDecision, ManualEntry

logger = logging.getLogger(__name__)


class RunNotFoundError(Exception):
    pass


class RunNotAwaitingApprovalError(Exception):
    pass


class RunNotAwaitingEntryError(Exception):
    pass


class SegregationOfDutiesError(Exception):
    pass


class RunView(BaseModel):
    """Public projection of a run's state."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    status: RunStatus
    filename: str | None = None
    document_sha256: str | None = None
    invoice_number: str | None = None
    verdict: str | None = None
    findings: list[dict[str, Any]] = []
    narrative: dict[str, Any] | None = None
    degradations: list[dict[str, str]] = []
    extraction_error: dict[str, str] | None = None
    extraction_source: str | None = None
    entered_by: str | None = None
    decision: dict[str, Any] | None = None
    payment: dict[str, Any] | None = None
    payment_recheck: dict[str, Any] | None = None


def _view(values: dict[str, Any]) -> RunView:
    result = values.get("result") or {}
    return RunView(
        run_id=values["run_id"],
        status=RunStatus(values["status"]),
        filename=values.get("filename"),
        document_sha256=values.get("document_sha256"),
        invoice_number=(values.get("extraction") or {}).get("invoice_number"),
        verdict=result.get("verdict"),
        findings=result.get("findings", []),
        narrative=values.get("narrative"),
        degradations=values.get("degradations", []),
        extraction_error=values.get("extraction_error"),
        extraction_source=values.get("extraction_source"),
        entered_by=values.get("entered_by"),
        decision=values.get("decision"),
        payment=values.get("payment"),
        payment_recheck=values.get("payment_recheck"),
    )


class RunService:
    def __init__(self, graph: CompiledStateGraph[Any], documents: DocumentStore) -> None:
        self._graph = graph
        self._documents = documents
        # Serialises decisions per run within this process. A multi-replica
        # deployment would need a DB-level lock (e.g. pg_advisory_xact_lock).
        self._locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)

    @staticmethod
    def _config(run_id: str) -> RunnableConfig:
        return {"configurable": {"thread_id": run_id}}

    def start(self, pdf: bytes, filename: str) -> RunView:
        sha = self._documents.put(pdf)
        run_id = uuid.uuid4().hex
        logger.info("Run started", extra={"run_id": run_id, "sha256": sha})
        self._graph.invoke(
            {
                "run_id": run_id,
                "document_sha256": sha,
                "filename": filename,
                "status": RunStatus.PROCESSING.value,
                "degradations": [],
            },
            self._config(run_id),
        )
        return self.get(run_id)

    def get(self, run_id: str) -> RunView:
        snapshot = self._graph.get_state(self._config(run_id))
        if not snapshot.values:
            raise RunNotFoundError(run_id)
        return _view(snapshot.values)

    def _resume(self, run_id: str, node: str, payload: dict[str, Any], *, check: Any = None) -> RunView:
        with self._locks[run_id]:
            snapshot = self._graph.get_state(self._config(run_id))
            if not snapshot.values:
                raise RunNotFoundError(run_id)
            if node not in snapshot.next:
                status = snapshot.values.get("status", "unknown")
                raise (RunNotAwaitingApprovalError if node == "await_approval" else RunNotAwaitingEntryError)(status)
            if check is not None:
                check(snapshot.values)
            self._graph.invoke(Command(resume=payload), self._config(run_id))
        return self.get(run_id)

    def submit_extraction(self, run_id: str, entry: ManualEntry) -> RunView:
        return self._resume(run_id, "await_manual_entry", entry.model_dump(mode="json"))

    def decide(self, run_id: str, decision: ApprovalDecision) -> RunView:
        def four_eyes(values: dict[str, Any]) -> None:
            if values.get("entered_by") and values["entered_by"].casefold() == decision.approver.casefold():
                raise SegregationOfDutiesError("the person who keyed this invoice cannot approve it")

        return self._resume(run_id, "await_approval", decision.model_dump(mode="json"), check=four_eyes)
