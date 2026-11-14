"""The agent workflow end to end: PDF in, verdict / HITL pause / payment out.

LLM calls are served by FakeLLM; everything else (PDF text layer, grounding,
rules, ledger, checkpointer, payment idempotency) is real.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from auditor.agents.explainer import Explainer, NarrativeDraft
from auditor.agents.mapper import LineItemMapper, ProposalDraft, ProposalsDraft
from auditor.agents.parser import DocumentParser, InvoiceExtraction
from auditor.documents.store import DocumentStore
from auditor.domain.enums import FindingCode, PaymentStatus, RunStatus
from auditor.domain.models import ExtractedInvoice, PaymentRecord
from auditor.llm.ports import LLMInvalidOutputError, LLMUnavailableError
from auditor.payments import SimulatedPaymentGateway
from auditor.ports import LedgerReader
from auditor.rules.engine import RuleConfig
from auditor.workflow.graph import ApprovalDecision, LineDecision, ManualEntry, MappingReview, WorkflowDeps, build_graph
from auditor.workflow.service import (
    InvalidMappingReviewError,
    RunNotAwaitingApprovalError,
    RunNotAwaitingEntryError,
    RunService,
    SegregationOfDutiesError,
)
from tests.fakes import FakeLLM
from tests.mock_data.fixtures import SCENARIOS, vendor_by_id

APPROVE = ApprovalDecision(approved=True, approver="ap.manager@arclight.example")
REJECT = ApprovalDecision(approved=False, approver="ap.manager@arclight.example", comment="not ours")


class ArmableLedger:
    """Delegates to a real ledger; once armed, reports every invoice as already PAID.

    Simulates a payment landing between audit and approval (time-of-check / time-of-use).
    """

    def __init__(self, inner: LedgerReader) -> None:
        self._inner = inner
        self.armed = False

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    def payments_by_invoice_number(self, vendor_id: str, invoice_number: str) -> tuple[PaymentRecord, ...]:
        if self.armed:
            return (
                PaymentRecord(
                    invoice_number=invoice_number,
                    vendor_id=vendor_id,
                    po_number=None,
                    amount=Decimal("1.00"),
                    payment_date=date(2026, 11, 1),
                    status=PaymentStatus.PAID,
                ),
            )
        return self._inner.payments_by_invoice_number(vendor_id, invoice_number)


@dataclass
class Harness:
    runs: RunService
    llm: FakeLLM
    payments: SimulatedPaymentGateway
    ledger: ArmableLedger
    built: Path

    def start(self, scenario_id: str):  # type: ignore[no-untyped-def]
        pdf = (self.built / "invoices" / f"{scenario_id}.pdf").read_bytes()
        return self.runs.start(pdf, f"{scenario_id}.pdf", actor="clerk.bea")


@pytest.fixture
def harness(built: Path, ledger: LedgerReader, tmp_path: Path) -> Harness:
    llm = FakeLLM(built)
    payments = SimulatedPaymentGateway()
    armable = ArmableLedger(ledger)
    documents = DocumentStore(tmp_path / "docs")
    deps = WorkflowDeps(
        ledger=armable,  # type: ignore[arg-type]
        documents=documents,
        parser=DocumentParser(llm),
        explainer=Explainer(llm),
        payments=payments,
        rule_config=RuleConfig(),
        mapper=LineItemMapper(llm),
    )
    return Harness(RunService(build_graph(deps, InMemorySaver()), documents), llm, payments, armable, built)


# --------------------------------------------------------------------------- #
# Golden scenarios through the full agent path
# --------------------------------------------------------------------------- #
_EXPECTED_STATUS = {
    "APPROVE_PENDING_HUMAN": RunStatus.AWAITING_APPROVAL,
    "BLOCK": RunStatus.BLOCKED,
    "REVIEW": RunStatus.NEEDS_REVIEW,
}


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.scenario_id)
def test_pdf_to_verdict(scenario, harness: Harness) -> None:  # type: ignore[no-untyped-def]
    run = harness.start(scenario.scenario_id)
    if FindingCode.UNRESOLVED_LINE_ITEM in scenario.expected_findings:
        # Unmatched lines pause for a human first; rejecting every proposal reproduces the golden verdict.
        assert run.status is RunStatus.AWAITING_MAPPING_REVIEW
        run = harness.runs.submit_mappings(run.run_id, _reject_all(run))
    assert run.status is _EXPECTED_STATUS[scenario.expected_verdict.value]
    assert {f["code"] for f in run.findings} == {c.value for c in scenario.expected_findings}
    assert run.narrative is not None
    assert run.narrative["source"] == "llm"
    assert run.degradations == []
    assert harness.payments.execution_count == 0  # nothing is ever paid without a human


# --------------------------------------------------------------------------- #
# Human in the loop
# --------------------------------------------------------------------------- #
def test_approval_pays_the_account_on_file(harness: Harness) -> None:
    run = harness.start("S01_clean_services")
    assert run.status is RunStatus.AWAITING_APPROVAL

    paid = harness.runs.decide(run.run_id, APPROVE)

    assert paid.status is RunStatus.PAID
    assert paid.decision is not None
    assert paid.decision["approver"] == APPROVE.approver
    assert paid.payment is not None
    assert paid.payment["amount"] == "11400.00"
    assert paid.payment["account_tail"] == vendor_by_id("V-1001").remit_account[-4:]
    assert harness.payments.execution_count == 1


def test_rejection_moves_no_money(harness: Harness) -> None:
    run = harness.start("S02_clean_goods_with_tax")
    done = harness.runs.decide(run.run_id, REJECT)
    assert done.status is RunStatus.REJECTED
    assert done.payment is None
    assert harness.payments.execution_count == 0


def test_cannot_decide_twice(harness: Harness) -> None:
    run = harness.start("S01_clean_services")
    harness.runs.decide(run.run_id, APPROVE)
    with pytest.raises(RunNotAwaitingApprovalError):
        harness.runs.decide(run.run_id, APPROVE)
    assert harness.payments.execution_count == 1


def test_cannot_approve_a_blocked_run(harness: Harness) -> None:
    run = harness.start("S03_rate_overcharge")
    with pytest.raises(RunNotAwaitingApprovalError):
        harness.runs.decide(run.run_id, APPROVE)


def test_ledger_change_between_approval_and_payment_withholds_payment(harness: Harness) -> None:
    run = harness.start("S01_clean_services")
    harness.ledger.armed = True  # a payment for this invoice lands while it waits for approval
    done = harness.runs.decide(run.run_id, APPROVE)
    assert done.status is RunStatus.BLOCKED_AT_PAYMENT
    assert done.payment_recheck is not None
    assert {f["code"] for f in done.payment_recheck["findings"]} == {"DUPLICATE_INVOICE"}
    assert harness.payments.execution_count == 0


def test_payment_is_idempotent_on_run_id() -> None:
    gw = SimulatedPaymentGateway()
    kwargs = {"vendor_id": "V-1", "amount": Decimal("10.00"), "currency": "USD", "account": "ACCT-0001"}
    first = gw.execute(idempotency_key="run:abc", **kwargs)  # type: ignore[arg-type]
    second = gw.execute(idempotency_key="run:abc", **kwargs)  # type: ignore[arg-type]
    assert first == second
    assert gw.execution_count == 1


# --------------------------------------------------------------------------- #
# Graceful degradation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "failure",
    [LLMUnavailableError("transient_failure", "APIConnectionError"), LLMInvalidOutputError("refused")],
    ids=["unavailable", "invalid"],
)
def test_parser_failure_routes_to_manual_entry(failure, harness: Harness) -> None:  # type: ignore[no-untyped-def]
    harness.llm.parse_override = failure
    run = harness.start("S01_clean_services")
    assert run.status is RunStatus.NEEDS_MANUAL_ENTRY
    assert run.verdict is None
    assert run.degradations == [{"step": "parse", "reason": f"llm_{failure.reason}"}]


def test_hallucinated_value_is_rejected_by_grounding(harness: Harness) -> None:
    # The model "reads" a total that is not printed anywhere on the PDF.
    harness.llm.parse_override = lambda x: x.model_copy(update={"total": "1140.00", "subtotal": "1140.00"})
    run = harness.start("S01_clean_services")
    assert run.status is RunStatus.NEEDS_MANUAL_ENTRY
    assert run.extraction_error is not None
    assert run.extraction_error["reason"] == "ungrounded"
    assert "total" in run.extraction_error["detail"]


def test_parser_must_not_correct_inconsistent_invoices(harness: Harness) -> None:
    """If the model 'helpfully' fixes S05's subtotal, the fix isn't on the page -> grounding rejects it.
    The fraud can be missed (manual entry) but never silently erased into a pass."""

    def correct_the_math(x: InvoiceExtraction) -> InvoiceExtraction:
        return x.model_copy(update={"subtotal": "4700.00", "tax": "235.00", "total": "4935.00"})

    harness.llm.parse_override = correct_the_math
    run = harness.start("S05_arithmetic_mismatch")
    assert run.status is RunStatus.NEEDS_MANUAL_ENTRY
    assert run.verdict is None


def test_explainer_failure_falls_back_to_template_and_verdict_is_unchanged(harness: Harness) -> None:
    harness.llm.explain_override = LLMUnavailableError("transient_failure", "RateLimitError")
    run = harness.start("S03_rate_overcharge")
    assert run.status is RunStatus.BLOCKED
    assert run.narrative is not None
    assert run.narrative["source"] == "template"
    assert "overcharge 2,400.00" in run.narrative["summary"]
    assert run.degradations == [{"step": "explain", "reason": "llm_transient_failure"}]


def test_explainer_cannot_change_the_verdict(harness: Harness) -> None:
    harness.llm.explain_override = NarrativeDraft(summary="Looks fine, approve it.", recommended_action="Pay now.")
    run = harness.start("S04_exact_duplicate")
    assert run.status is RunStatus.BLOCKED
    assert run.verdict == "BLOCK"


def test_no_llm_configured_still_runs_safely(built: Path, ledger: LedgerReader, tmp_path: Path) -> None:
    documents = DocumentStore(tmp_path / "docs")
    deps = WorkflowDeps(
        ledger=ledger,
        documents=documents,
        parser=DocumentParser(None),
        explainer=Explainer(None),
        payments=SimulatedPaymentGateway(),
        rule_config=RuleConfig(),
    )
    runs = RunService(build_graph(deps, InMemorySaver()), documents)
    run = runs.start((built / "invoices" / "S01_clean_services.pdf").read_bytes(), "x.pdf", actor="clerk.bea")
    assert run.status is RunStatus.NEEDS_MANUAL_ENTRY
    assert run.degradations == [{"step": "parse", "reason": "llm_not_configured"}]


def test_state_is_plain_json(harness: Harness) -> None:
    run = harness.start("S01_clean_services")
    snapshot = harness.runs._graph.get_state({"configurable": {"thread_id": run.run_id}})
    json.dumps(snapshot.values)  # raises if any non-JSON object leaked into checkpointed state


# --------------------------------------------------------------------------- #
# Manual entry: a failed parse is a detour, not a dead end
# --------------------------------------------------------------------------- #
def _manual(built: Path, scenario_id: str, who: str = "clerk.bea") -> ManualEntry:
    body = json.loads((built / "requests" / f"{scenario_id}.json").read_text())
    return ManualEntry(entered_by=who, invoice=ExtractedInvoice.model_validate(body))


def test_manual_entry_resumes_through_the_same_audit(harness: Harness) -> None:
    harness.llm.parse_override = LLMUnavailableError("transient_failure")
    run = harness.start("S03_rate_overcharge")
    assert run.status is RunStatus.NEEDS_MANUAL_ENTRY

    done = harness.runs.submit_extraction(run.run_id, _manual(harness.built, "S03_rate_overcharge"))
    assert done.status is RunStatus.BLOCKED
    assert {f["code"] for f in done.findings} == {"RATE_EXCEEDS_CONTRACT"}
    assert done.extraction_source == "manual"


def test_four_eyes_keyer_cannot_approve(harness: Harness) -> None:
    harness.llm.parse_override = LLMUnavailableError("transient_failure")
    run = harness.start("S01_clean_services")
    run = harness.runs.submit_extraction(run.run_id, _manual(harness.built, "S01_clean_services", who="Bea"))
    assert run.status is RunStatus.AWAITING_APPROVAL

    with pytest.raises(SegregationOfDutiesError):
        harness.runs.decide(run.run_id, ApprovalDecision(approved=True, approver="bea"))
    assert harness.runs.get(run.run_id).status is RunStatus.AWAITING_APPROVAL  # still pending, nothing paid
    assert harness.runs.decide(run.run_id, APPROVE).status is RunStatus.PAID


def test_manual_entry_only_when_awaiting_it(harness: Harness) -> None:
    run = harness.start("S01_clean_services")
    with pytest.raises(RunNotAwaitingEntryError):
        harness.runs.submit_extraction(run.run_id, _manual(harness.built, "S01_clean_services"))


# --------------------------------------------------------------------------- #
# Line-item mapping: LLM proposes, human disposes, audit re-runs (a cycle)
# --------------------------------------------------------------------------- #
def _reject_all(run, who: str = "ap.analyst"):  # type: ignore[no-untyped-def]
    return MappingReview(
        reviewer=who, decisions=[LineDecision(line=p["line"], item_code=None) for p in run.mapping_proposals]
    )


def _confirm(code: str | None, who: str = "ap.analyst") -> MappingReview:
    return MappingReview(reviewer=who, decisions=[LineDecision(line=1, item_code=code)])


def test_unmatched_line_gets_a_proposal_not_a_mapping(harness: Harness) -> None:
    run = harness.start("S09_unmatched_line_item")
    assert run.status is RunStatus.AWAITING_MAPPING_REVIEW
    assert run.verdict == "REVIEW"
    assert run.mapping_proposals == [
        {
            "line": 1,
            "description": "Sr. Data Eng. consulting - Oct 2026 (2nd half)",
            "proposed_code": "SENIOR_ENG",
            "reason": "matches role",
        }
    ]
    assert run.line_mappings == {}  # proposed, not applied


def test_mapper_never_sees_prices(harness: Harness) -> None:
    harness.start("S09_unmatched_line_item")
    (payload,) = harness.llm.mapping_payloads
    text = json.dumps(payload)
    assert "120" not in text
    assert "price" not in text.lower()
    assert {"code", "description", "unit"} == set(payload["rate_card"][0])


def test_confirmed_mapping_reaudits_to_clean_then_four_eyes(harness: Harness) -> None:
    run = harness.start("S09_unmatched_line_item")
    run = harness.runs.submit_mappings(run.run_id, _confirm("SENIOR_ENG", who="ana"))
    assert run.status is RunStatus.AWAITING_APPROVAL
    assert run.findings == []
    assert run.line_mappings == {"1": "SENIOR_ENG"}
    assert run.mapped_by == "ana"
    with pytest.raises(SegregationOfDutiesError, match="mapped"):
        harness.runs.decide(run.run_id, ApprovalDecision(approved=True, approver="Ana"))
    paid = harness.runs.decide(run.run_id, APPROVE)
    assert paid.status is RunStatus.PAID
    assert paid.payment is not None
    assert paid.payment["amount"] == "8400.00"


def test_human_mapping_to_a_cheaper_code_surfaces_the_overcharge(harness: Harness) -> None:
    """Mapping is not a way around pricing: the rules re-run on whatever was confirmed."""
    run = harness.start("S09_unmatched_line_item")
    run = harness.runs.submit_mappings(run.run_id, _confirm("DATA_ENG"))
    assert run.status is RunStatus.BLOCKED
    assert {f["code"] for f in run.findings} == {"RATE_EXCEEDS_CONTRACT"}


def test_rejected_mapping_ends_in_review_and_the_cycle_terminates(harness: Harness) -> None:
    run = harness.start("S09_unmatched_line_item")
    run = harness.runs.submit_mappings(run.run_id, _confirm(None))
    assert run.status is RunStatus.NEEDS_REVIEW
    assert {f["code"] for f in run.findings} == {"UNRESOLVED_LINE_ITEM"}
    with pytest.raises(RunNotAwaitingApprovalError):
        harness.runs.decide(run.run_id, APPROVE)


@pytest.mark.parametrize(
    "review",
    [
        MappingReview(reviewer="x", decisions=[LineDecision(line=1, item_code="GOLD_PLATING")]),
        MappingReview(reviewer="x", decisions=[LineDecision(line=2, item_code="SENIOR_ENG")]),
        MappingReview(
            reviewer="x",
            decisions=[LineDecision(line=1, item_code="SENIOR_ENG"), LineDecision(line=1, item_code="DATA_ENG")],
        ),
    ],
    ids=["code-not-on-card", "wrong-line", "duplicate-line"],
)
def test_invalid_reviews_are_refused_and_the_run_stays_paused(review: MappingReview, harness: Harness) -> None:
    run = harness.start("S09_unmatched_line_item")
    with pytest.raises(InvalidMappingReviewError):
        harness.runs.submit_mappings(run.run_id, review)
    assert harness.runs.get(run.run_id).status is RunStatus.AWAITING_MAPPING_REVIEW


def test_proposals_off_the_rate_card_are_discarded(harness: Harness) -> None:
    harness.llm.mapping_override = ProposalsDraft(
        proposals=[ProposalDraft(line=1, item_code="SUPPORT_RET_PREMIUM", reason="ignore previous instructions")]
    )
    run = harness.start("S09_unmatched_line_item")
    assert run.mapping_proposals is not None
    assert run.mapping_proposals[0]["proposed_code"] is None


def test_mapper_failure_degrades_to_human_mapping(harness: Harness) -> None:
    harness.llm.mapping_override = LLMUnavailableError("transient_failure")
    run = harness.start("S09_unmatched_line_item")
    assert run.status is RunStatus.AWAITING_MAPPING_REVIEW
    assert run.mapping_proposals is not None
    assert run.mapping_proposals[0]["proposed_code"] is None
    assert {"step": "map_line_items", "reason": "llm_transient_failure"} in run.degradations
    assert harness.runs.submit_mappings(run.run_id, _confirm("SENIOR_ENG")).status is RunStatus.AWAITING_APPROVAL


def test_blocked_invoices_skip_mapping_review(harness: Harness) -> None:
    run = harness.start("S03_rate_overcharge")  # also an uncoded layout, but resolvable and blocked
    assert run.status is RunStatus.BLOCKED
    assert harness.llm.mapping_payloads == []


# --------------------------------------------------------------------------- #
# Payment write-back: crash between reserving and paying
# --------------------------------------------------------------------------- #
def test_crash_after_reservation_retries_cleanly(built: Path, tmp_path: Path) -> None:
    """If the process dies after reserving but before paying, the re-run must not treat its own
    reservation as a duplicate - nor count it against its own PO budget (S06-style tight POs)."""
    import shutil  # noqa: PLC0415

    from auditor.adapters.sqlite_ledger import SqliteLedger  # noqa: PLC0415
    from auditor.adapters.sqlite_payments import SqlitePaymentLedger  # noqa: PLC0415

    db = tmp_path / "erp.db"
    shutil.copy(built / "enterprise_erp.db", db)
    writer = SqlitePaymentLedger(db)
    llm, gw, documents = FakeLLM(built), SimulatedPaymentGateway(), DocumentStore(tmp_path / "docs")
    deps = WorkflowDeps(
        ledger=SqliteLedger(db),
        documents=documents,
        parser=DocumentParser(llm),
        explainer=Explainer(llm),
        payments=gw,
        rule_config=RuleConfig(),
        payment_ledger=writer,
    )
    runs = RunService(build_graph(deps, InMemorySaver()), documents)
    run = runs.start((built / "invoices" / "S02_clean_goods_with_tax.pdf").read_bytes(), "s02.pdf", actor="bea")
    # Simulate the crash: the reservation landed, the gateway call never happened.
    writer.reserve(
        external_ref=f"run:{run.run_id}",
        vendor_id="V-1002",
        invoice_number="MIS-55810",
        po_number="PO-2026-0422",
        amount=Decimal("9738.75"),
        payment_date=date(2026, 11, 14),
    )
    done = runs.decide(run.run_id, APPROVE)
    assert done.status is RunStatus.PAID
    assert gw.execution_count == 1
    (row,) = SqliteLedger(db).payments_by_invoice_number("V-1002", "MIS-55810")
    assert row.status == "PAID"
