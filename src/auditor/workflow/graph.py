"""The agent workflow, as a LangGraph state machine.

    parse ─┬─(failed)─► await_manual_entry  ⏸  interrupt(): a human keys the invoice in
           │                      │
           └──────────────────────┴─► audit ─► explain ─┬─(BLOCK / REVIEW)─► finalize ─► END  BLOCKED | NEEDS_REVIEW
                                 └─(clean)─► request_approval
                                             ─► await_approval  ⏸  interrupt(): human decides
                                                ├─(reject)─► reject ─► END  REJECTED
                                                └─(approve)─► execute_payment ─► END  PAID | BLOCKED_AT_PAYMENT

Design rules
  * State is plain JSON. Models are dumped on write and re-validated on read, so
    checkpoints are human-readable and never depend on unpickling custom classes.
  * LLM steps degrade, they don't crash: parse -> human data entry, explain -> template.
    Every degradation is appended to ``state["degradations"]``. A failed parse is a
    detour, not a dead end: the run waits for a person to key the invoice in, then
    continues through the same deterministic audit.
  * Four-eyes: whoever keyed an invoice in by hand may not approve its payment.
  * Nodes are re-executed when a run resumes, so nothing before ``interrupt()``
    has side effects, and the payment call is idempotent on the run id.
  * Payment re-audits against fresh ledger facts first. Approval may come days
    after the audit; a duplicate paid in the meantime must still be caught.
"""

from __future__ import annotations

import logging
import operator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict, Field

from auditor.agents.explainer import Explainer
from auditor.agents.parser import DocumentParser, ExtractionFailedError
from auditor.documents.store import DocumentStore
from auditor.domain.enums import RunStatus, Verdict
from auditor.domain.models import AuditResult, ExtractedInvoice
from auditor.payments import PaymentGateway
from auditor.ports import LedgerReader
from auditor.rules.engine import RuleConfig, evaluate, gather_facts

logger = logging.getLogger(__name__)


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    approved: bool
    approver: str = Field(min_length=1, max_length=100)
    comment: str | None = Field(default=None, max_length=500)


class ManualEntry(BaseModel):
    """A human-keyed invoice, submitted when automated parsing was not trustworthy."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    entered_by: str = Field(min_length=1, max_length=100)
    invoice: ExtractedInvoice


class AuditState(TypedDict, total=False):
    run_id: str
    document_sha256: str
    filename: str
    status: str
    extraction: dict[str, Any] | None
    extraction_error: dict[str, str] | None
    extraction_source: str | None
    entered_by: str | None
    facts: dict[str, Any] | None
    result: dict[str, Any] | None
    narrative: dict[str, Any] | None
    degradations: Annotated[list[dict[str, str]], operator.add]
    decision: dict[str, Any] | None
    payment: dict[str, Any] | None
    payment_recheck: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class WorkflowDeps:
    ledger: LedgerReader
    documents: DocumentStore
    parser: DocumentParser
    explainer: Explainer
    payments: PaymentGateway
    rule_config: RuleConfig


class _Nodes:
    """Graph nodes and routers, bound to their dependencies."""

    def __init__(self, deps: WorkflowDeps) -> None:
        self.d = deps

    # ---- nodes ---------------------------------------------------------------
    def parse(self, state: AuditState) -> AuditState:
        pdf = self.d.documents.get(state["document_sha256"])
        try:
            invoice = self.d.parser.parse(pdf)
        except ExtractionFailedError as exc:
            logger.warning("Parse failed; routing to manual entry", extra={"reason": exc.reason})
            return {
                "status": RunStatus.NEEDS_MANUAL_ENTRY.value,
                "extraction_error": {"reason": exc.reason, "detail": exc.detail},
                "degradations": [{"step": "parse", "reason": exc.reason}],
            }
        return {"extraction": invoice.model_dump(mode="json"), "extraction_source": "llm"}

    def await_manual_entry(self, state: AuditState) -> AuditState:
        raw = interrupt(
            {"question": "Key in this invoice", "reason": (state.get("extraction_error") or {}).get("reason")}
        )
        entry = ManualEntry.model_validate(raw)
        logger.info("Manual extraction submitted", extra={"entered_by": entry.entered_by})
        return {
            "extraction": entry.invoice.model_dump(mode="json"),
            "extraction_source": "manual",
            "entered_by": entry.entered_by,
            "status": RunStatus.PROCESSING.value,
        }

    def audit(self, state: AuditState) -> AuditState:
        invoice = ExtractedInvoice.model_validate(state["extraction"])
        facts = gather_facts(invoice, self.d.ledger, self.d.rule_config)
        result = evaluate(invoice, facts)
        return {"facts": facts.model_dump(mode="json"), "result": result.model_dump(mode="json")}

    def explain(self, state: AuditState) -> AuditState:
        narrative, degraded = self.d.explainer.explain(AuditResult.model_validate(state["result"]))
        update: AuditState = {"narrative": narrative.model_dump(mode="json")}
        if degraded:
            update["degradations"] = [{"step": "explain", "reason": degraded}]
        return update

    def finalize(self, state: AuditState) -> AuditState:
        verdict = _verdict(state)
        return {"status": (RunStatus.BLOCKED if verdict is Verdict.BLOCK else RunStatus.NEEDS_REVIEW).value}

    def request_approval(self, state: AuditState) -> AuditState:
        # Separate node so the status is checkpointed BEFORE the interrupt fires.
        return {"status": RunStatus.AWAITING_APPROVAL.value}

    def await_approval(self, state: AuditState) -> AuditState:
        extraction = state.get("extraction") or {}
        raw = interrupt(
            {
                "question": "Approve payment?",
                "invoice_number": extraction.get("invoice_number"),
                "vendor_name": extraction.get("vendor_name"),
                "total": extraction.get("total"),
                "currency": extraction.get("currency"),
                "narrative": (state.get("narrative") or {}).get("summary"),
            }
        )
        decision = ApprovalDecision.model_validate(raw)
        logger.info("Approval decision recorded", extra={"approved": decision.approved, "approver": decision.approver})
        return {"decision": {**decision.model_dump(mode="json"), "decided_at": datetime.now(UTC).isoformat()}}

    def reject(self, state: AuditState) -> AuditState:
        return {"status": RunStatus.REJECTED.value}

    def execute_payment(self, state: AuditState) -> AuditState:
        invoice = ExtractedInvoice.model_validate(state["extraction"])
        fresh_facts = gather_facts(invoice, self.d.ledger, self.d.rule_config)
        recheck = evaluate(invoice, fresh_facts)
        if recheck.verdict is not Verdict.APPROVE_PENDING_HUMAN or fresh_facts.vendor is None:
            logger.warning("Pre-payment re-audit failed; payment withheld", extra={"verdict": recheck.verdict.value})
            return {"status": RunStatus.BLOCKED_AT_PAYMENT.value, "payment_recheck": recheck.model_dump(mode="json")}
        receipt = self.d.payments.execute(
            idempotency_key=f"run:{state['run_id']}",
            vendor_id=fresh_facts.vendor.vendor_id,
            amount=invoice.total,
            currency=invoice.currency,
            account=fresh_facts.vendor.remit_account,  # the account ON FILE, never the invoice's
        )
        return {"status": RunStatus.PAID.value, "payment": receipt.model_dump(mode="json")}

    # ---- routers -------------------------------------------------------------
    @staticmethod
    def after_parse(state: AuditState) -> Literal["audit", "await_manual_entry"]:
        return "audit" if state.get("extraction") else "await_manual_entry"

    @staticmethod
    def after_explain(state: AuditState) -> Literal["request_approval", "finalize"]:
        return "request_approval" if _verdict(state) is Verdict.APPROVE_PENDING_HUMAN else "finalize"

    @staticmethod
    def after_decision(state: AuditState) -> Literal["execute_payment", "reject"]:
        return "execute_payment" if (state.get("decision") or {}).get("approved") else "reject"


def _verdict(state: AuditState) -> Verdict:
    return Verdict((state.get("result") or {})["verdict"])


def build_graph(deps: WorkflowDeps, checkpointer: BaseCheckpointSaver[Any]) -> CompiledStateGraph[Any]:
    n = _Nodes(deps)
    g = StateGraph(AuditState)
    g.add_node("parse", n.parse)
    g.add_node("await_manual_entry", n.await_manual_entry)
    g.add_node("audit", n.audit)
    g.add_node("explain", n.explain)
    g.add_node("finalize", n.finalize)
    g.add_node("request_approval", n.request_approval)
    g.add_node("await_approval", n.await_approval)
    g.add_node("reject", n.reject)
    g.add_node("execute_payment", n.execute_payment)

    g.add_edge(START, "parse")
    g.add_conditional_edges("parse", n.after_parse)
    g.add_edge("await_manual_entry", "audit")
    g.add_edge("audit", "explain")
    g.add_conditional_edges("explain", n.after_explain)
    g.add_edge("finalize", END)
    g.add_edge("request_approval", "await_approval")
    g.add_conditional_edges("await_approval", n.after_decision)
    g.add_edge("reject", END)
    g.add_edge("execute_payment", END)
    return g.compile(checkpointer=checkpointer)
