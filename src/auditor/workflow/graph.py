"""The agent workflow, as a LangGraph state machine.

    parse ─┬─(failed)─► await_manual_entry  ⏸  interrupt(): a human keys the invoice in
           │                      │
           └──────────────────────┴─► audit ◄────────────────────────────────────────┐
                                        │                                           │
                                        ├─(unmapped lines)─► propose_mappings ─► await_mapping_review ⏸
                                        │                    (LLM proposes)      (human confirms/rejects)
                                        ▼
                                     explain ─┬─(BLOCK / REVIEW)─► finalize ─► END  BLOCKED | NEEDS_REVIEW
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
  * Line items the rules can't match to the contract get LLM-proposed codes that a
    human confirms; the audit then re-runs (a cycle) on the confirmed mapping. Each
    line is reviewed at most once, so the cycle always terminates.
  * Four-eyes: whoever keyed an invoice in, or mapped its lines, may not approve it.
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
from decimal import Decimal
from typing import Annotated, Any, Literal, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict, Field

from auditor.agents.explainer import Explainer
from auditor.agents.mapper import LineItemMapper, unresolved_lines
from auditor.agents.parser import DocumentParser, ExtractionFailedError
from auditor.documents.store import DocumentStore
from auditor.domain.enums import RunStatus, Verdict
from auditor.domain.models import AuditResult, ExtractedInvoice, LedgerFacts
from auditor.payments import PaymentGateway
from auditor.ports import LedgerReader, PaymentConflictError, PaymentLedgerWriter
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


class LineDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    line: int = Field(ge=1)
    item_code: str | None = Field(default=None, description="Confirmed rate-card code, or null to reject")


class MappingReview(BaseModel):
    """A human's decision on every pending line: confirm a code, pick another, or reject (null)."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    reviewer: str = Field(min_length=1, max_length=100)
    decisions: list[LineDecision] = Field(min_length=1)


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
    mapping_proposals: list[dict[str, Any]] | None
    line_mappings: dict[str, str]  # "line number" -> human-confirmed item code
    reviewed_lines: list[int]
    mapped_by: str | None
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
    mapper: LineItemMapper | None = None
    # Without a writer, payments are not recorded in the ledger (dev only; prod config requires one).
    payment_ledger: PaymentLedgerWriter | None = None


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
        invoice = effective_invoice(state)
        facts = gather_facts(invoice, self.d.ledger, self.d.rule_config)
        result = evaluate(invoice, facts)
        return {"facts": facts.model_dump(mode="json"), "result": result.model_dump(mode="json")}

    def propose_mappings(self, state: AuditState) -> AuditState:
        invoice = ExtractedInvoice.model_validate(state["extraction"])
        pending = _pending_lines(state)
        rate_card = LedgerFacts.model_validate(state["facts"]).rate_card
        mapper = self.d.mapper or LineItemMapper(None)
        proposals, degraded = mapper.propose({n: invoice.line_items[n - 1] for n in pending}, rate_card)
        update: AuditState = {
            "mapping_proposals": [p.model_dump(mode="json") for p in proposals],
            "status": RunStatus.AWAITING_MAPPING_REVIEW.value,
        }
        if degraded:
            update["degradations"] = [{"step": "map_line_items", "reason": degraded}]
        return update

    def await_mapping_review(self, state: AuditState) -> AuditState:
        raw = interrupt({"question": "Confirm line-item mappings", "proposals": state.get("mapping_proposals")})
        review = MappingReview.model_validate(raw)
        valid = {e.item_code for e in LedgerFacts.model_validate(state["facts"]).rate_card}
        pending = set(_pending_lines(state))
        confirmed = {str(d.line): d.item_code for d in review.decisions if d.line in pending and d.item_code in valid}
        logger.info("Mapping review recorded", extra={"reviewer": review.reviewer, "confirmed": len(confirmed)})
        return {
            "line_mappings": {**(state.get("line_mappings") or {}), **confirmed},
            "reviewed_lines": sorted(set(state.get("reviewed_lines") or []) | pending),
            "mapped_by": review.reviewer,
            "status": RunStatus.PROCESSING.value,
        }

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
        """Re-audit on fresh facts -> reserve in the ledger -> pay -> mark paid.

        Every step is safe to re-execute after a crash: the run's own reservation is
        excluded from the re-audit, reserve() is idempotent per run, and the gateway is
        idempotent on the same key.
        """
        invoice = effective_invoice(state)
        own_ref = f"run:{state['run_id']}"
        fresh_facts = _without_own_reservation(gather_facts(invoice, self.d.ledger, self.d.rule_config), own_ref)
        recheck = evaluate(invoice, fresh_facts)
        if recheck.verdict is not Verdict.APPROVE_PENDING_HUMAN or fresh_facts.vendor is None:
            logger.warning("Pre-payment re-audit failed; payment withheld", extra={"verdict": recheck.verdict.value})
            return {"status": RunStatus.BLOCKED_AT_PAYMENT.value, "payment_recheck": recheck.model_dump(mode="json")}
        vendor = fresh_facts.vendor
        if self.d.payment_ledger is not None:
            try:
                self.d.payment_ledger.reserve(
                    external_ref=own_ref,
                    vendor_id=vendor.vendor_id,
                    invoice_number=invoice.invoice_number,
                    po_number=invoice.po_number,
                    amount=invoice.total,
                    payment_date=datetime.now(UTC).date(),
                )
            except PaymentConflictError as exc:
                # Lost a race to another run between re-audit and reservation.
                logger.warning("Ledger reservation refused; payment withheld", extra={"detail": str(exc)})
                return {
                    "status": RunStatus.BLOCKED_AT_PAYMENT.value,
                    "payment_recheck": {"verdict": "BLOCK", "reason": "ledger_conflict", "detail": str(exc)},
                }
        else:
            logger.warning("No payment ledger writer configured: payment will not be recorded in the ledger")
        receipt = self.d.payments.execute(
            idempotency_key=own_ref,
            vendor_id=vendor.vendor_id,
            amount=invoice.total,
            currency=invoice.currency,
            account=vendor.remit_account,  # the account ON FILE, never the invoice's
        )
        if self.d.payment_ledger is not None:
            self.d.payment_ledger.mark_paid(external_ref=own_ref, payment_ref=receipt.payment_id)
        return {"status": RunStatus.PAID.value, "payment": receipt.model_dump(mode="json")}

    # ---- routers -------------------------------------------------------------
    @staticmethod
    def after_parse(state: AuditState) -> Literal["audit", "await_manual_entry"]:
        return "audit" if state.get("extraction") else "await_manual_entry"

    @staticmethod
    def after_audit(state: AuditState) -> Literal["propose_mappings", "explain"]:
        if _verdict(state) is Verdict.BLOCK:  # a blocked invoice gains nothing from mapping review
            return "explain"
        return "propose_mappings" if _pending_lines(state) else "explain"

    @staticmethod
    def after_explain(state: AuditState) -> Literal["request_approval", "finalize"]:
        return "request_approval" if _verdict(state) is Verdict.APPROVE_PENDING_HUMAN else "finalize"

    @staticmethod
    def after_decision(state: AuditState) -> Literal["execute_payment", "reject"]:
        return "execute_payment" if (state.get("decision") or {}).get("approved") else "reject"


def effective_invoice(state: AuditState) -> ExtractedInvoice:
    """The extraction as printed, plus human-confirmed codes for lines that printed none.
    Kept separate so the record of what the document *said* is never rewritten."""
    invoice = ExtractedInvoice.model_validate(state["extraction"])
    mappings = state.get("line_mappings") or {}
    if not mappings:
        return invoice
    lines = tuple(
        ln.model_copy(update={"item_code": mappings[str(i)]}) if str(i) in mappings else ln
        for i, ln in enumerate(invoice.line_items, start=1)
    )
    return invoice.model_copy(update={"line_items": lines})


def _without_own_reservation(facts: LedgerFacts, own_ref: str) -> LedgerFacts:
    """Facts as they'd be had this run never reserved: its own PENDING/PAID row is not a duplicate of itself,
    and its amount must not count against its own PO budget on a crash-retry."""
    own = [p for p in facts.same_number_payments if p.external_ref == own_ref]
    if not own:
        return facts
    po = facts.purchase_order
    if po is not None:
        po = po.model_copy(update={"consumed": po.consumed - sum((p.amount for p in own), Decimal("0"))})
    return facts.model_copy(
        update={
            "purchase_order": po,
            "same_number_payments": tuple(p for p in facts.same_number_payments if p.external_ref != own_ref),
            "same_amount_payments": tuple(p for p in facts.same_amount_payments if p.external_ref != own_ref),
        }
    )


def _pending_lines(state: AuditState) -> list[int]:
    reviewed = set(state.get("reviewed_lines") or [])
    findings = (state.get("result") or {}).get("findings", [])
    return [n for n in unresolved_lines(findings) if n not in reviewed]


def _verdict(state: AuditState) -> Verdict:
    return Verdict((state.get("result") or {})["verdict"])


def build_graph(deps: WorkflowDeps, checkpointer: BaseCheckpointSaver[Any]) -> CompiledStateGraph[Any]:
    n = _Nodes(deps)
    g = StateGraph(AuditState)
    g.add_node("parse", n.parse)
    g.add_node("await_manual_entry", n.await_manual_entry)
    g.add_node("audit", n.audit)
    g.add_node("propose_mappings", n.propose_mappings)
    g.add_node("await_mapping_review", n.await_mapping_review)
    g.add_node("explain", n.explain)
    g.add_node("finalize", n.finalize)
    g.add_node("request_approval", n.request_approval)
    g.add_node("await_approval", n.await_approval)
    g.add_node("reject", n.reject)
    g.add_node("execute_payment", n.execute_payment)

    g.add_edge(START, "parse")
    g.add_conditional_edges("parse", n.after_parse)
    g.add_edge("await_manual_entry", "audit")
    g.add_conditional_edges("audit", n.after_audit)
    g.add_edge("propose_mappings", "await_mapping_review")
    g.add_edge("await_mapping_review", "audit")
    g.add_conditional_edges("explain", n.after_explain)
    g.add_edge("finalize", END)
    g.add_edge("request_approval", "await_approval")
    g.add_conditional_edges("await_approval", n.after_decision)
    g.add_edge("reject", END)
    g.add_edge("execute_payment", END)
    return g.compile(checkpointer=checkpointer)
