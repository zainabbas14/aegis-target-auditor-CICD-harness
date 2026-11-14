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

from auditor.audit_log import AuditLog, InMemoryAuditLog
from auditor.documents.store import DocumentStore
from auditor.domain.enums import RunStatus
from auditor.workflow.graph import ApprovalDecision, ManualEntry, MappingReview

logger = logging.getLogger(__name__)


class RunNotFoundError(Exception):
    pass


class RunNotAwaitingApprovalError(Exception):
    pass


class RunNotAwaitingEntryError(Exception):
    pass


class SegregationOfDutiesError(Exception):
    pass


class RunNotAwaitingMappingReviewError(Exception):
    pass


class InvalidMappingReviewError(Exception):
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
    mapping_proposals: list[dict[str, Any]] | None = None
    line_mappings: dict[str, str] = {}
    mapped_by: str | None = None
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
        mapping_proposals=values.get("mapping_proposals"),
        line_mappings=values.get("line_mappings") or {},
        mapped_by=values.get("mapped_by"),
        decision=values.get("decision"),
        payment=values.get("payment"),
        payment_recheck=values.get("payment_recheck"),
    )


def _outcome(view: RunView) -> dict[str, Any]:
    """What the audit log records as the result of an action."""
    return {
        "status": view.status.value,
        "verdict": view.verdict,
        "findings": sorted({f["code"] for f in view.findings}),
        "payment_id": (view.payment or {}).get("payment_id"),
        "amount": (view.payment or {}).get("amount"),
    }


class RunService:
    def __init__(
        self, graph: CompiledStateGraph[Any], documents: DocumentStore, audit_log: AuditLog | None = None
    ) -> None:
        self._graph = graph
        self._documents = documents
        self.audit_log: AuditLog = audit_log or InMemoryAuditLog()
        # Serialises decisions per run within this process. A multi-replica
        # deployment would need a DB-level lock (e.g. pg_advisory_xact_lock).
        self._locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)

    @staticmethod
    def _config(run_id: str) -> RunnableConfig:
        return {"configurable": {"thread_id": run_id}}

    def start(self, pdf: bytes, filename: str, *, actor: str) -> RunView:
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
        view = self.get(run_id)
        self.audit_log.append(
            run_id=run_id,
            actor=actor,
            action="RUN_STARTED",
            details={"filename": filename, "document_sha256": sha},
            outcome=_outcome(view),
        )
        return view

    def get(self, run_id: str) -> RunView:
        snapshot = self._graph.get_state(self._config(run_id))
        if not snapshot.values:
            raise RunNotFoundError(run_id)
        return _view(snapshot.values)

    def _resume(
        self,
        run_id: str,
        node: str,
        payload: dict[str, Any],
        *,
        actor: str,
        action: str,
        details: dict[str, Any],
        check: Any = None,
    ) -> RunView:
        with self._locks[run_id]:
            snapshot = self._graph.get_state(self._config(run_id))
            if not snapshot.values:
                raise RunNotFoundError(run_id)
            if node not in snapshot.next:
                status = snapshot.values.get("status", "unknown")
                errors = {
                    "await_approval": RunNotAwaitingApprovalError,
                    "await_manual_entry": RunNotAwaitingEntryError,
                    "await_mapping_review": RunNotAwaitingMappingReviewError,
                }
                raise errors[node](status)
            if check is not None:
                try:
                    check(snapshot.values)
                except (SegregationOfDutiesError, InvalidMappingReviewError) as exc:
                    self.audit_log.append(
                        run_id=run_id,
                        actor=actor,
                        action=f"{action}_REFUSED",
                        details={**details, "reason": str(exc)},
                        outcome={"status": snapshot.values.get("status")},
                    )
                    raise
            self._graph.invoke(Command(resume=payload), self._config(run_id))
            view = self.get(run_id)
            self.audit_log.append(run_id=run_id, actor=actor, action=action, details=details, outcome=_outcome(view))
        return view

    def submit_extraction(self, run_id: str, entry: ManualEntry) -> RunView:
        return self._resume(
            run_id,
            "await_manual_entry",
            entry.model_dump(mode="json"),
            actor=entry.entered_by,
            action="EXTRACTION_SUBMITTED",
            details={"invoice_number": entry.invoice.invoice_number, "total": str(entry.invoice.total)},
        )

    def submit_mappings(self, run_id: str, review: MappingReview) -> RunView:
        def validate(values: dict[str, Any]) -> None:
            pending = {p["line"] for p in values.get("mapping_proposals") or []}
            decided = [d.line for d in review.decisions]
            if len(decided) != len(set(decided)) or set(decided) != pending:
                raise InvalidMappingReviewError(f"decide every pending line exactly once: {sorted(pending)}")
            valid = {e["item_code"] for e in (values.get("facts") or {}).get("rate_card", [])}
            unknown = sorted({d.item_code for d in review.decisions if d.item_code and d.item_code not in valid})
            if unknown:
                raise InvalidMappingReviewError(f"not on this contract's rate card: {', '.join(unknown)}")

        return self._resume(
            run_id,
            "await_mapping_review",
            review.model_dump(mode="json"),
            actor=review.reviewer,
            action="MAPPINGS_SUBMITTED",
            details={"decisions": [d.model_dump(mode="json") for d in review.decisions]},
            check=validate,
        )

    def decide(self, run_id: str, decision: ApprovalDecision) -> RunView:
        def four_eyes(values: dict[str, Any]) -> None:
            for role, who in (("keyed this invoice", "entered_by"), ("mapped its line items", "mapped_by")):
                if values.get(who) and values[who].casefold() == decision.approver.casefold():
                    raise SegregationOfDutiesError(f"the person who {role} cannot approve it")

        return self._resume(
            run_id,
            "await_approval",
            decision.model_dump(mode="json"),
            actor=decision.approver,
            action="PAYMENT_APPROVED" if decision.approved else "PAYMENT_REJECTED",
            details={"comment": decision.comment},
            check=four_eyes,
        )
