"""Domain vocabulary shared by every layer (rules, agents, API, tests)."""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType


class Severity(StrEnum):
    REVIEW = "REVIEW"  # soft signal: a human analyst must look
    BLOCK = "BLOCK"  # hard stop: no payment path exists


class FindingCode(StrEnum):
    # Business anomalies (each has a golden scenario)
    RATE_EXCEEDS_CONTRACT = "RATE_EXCEEDS_CONTRACT"
    DUPLICATE_INVOICE = "DUPLICATE_INVOICE"
    POSSIBLE_DUPLICATE = "POSSIBLE_DUPLICATE"
    ARITHMETIC_MISMATCH = "ARITHMETIC_MISMATCH"
    PO_BUDGET_EXCEEDED = "PO_BUDGET_EXCEEDED"
    REMIT_TO_MISMATCH = "REMIT_TO_MISMATCH"
    # Integrity failures: the system could not establish the facts it needs.
    # Fail-closed: "couldn't check" must never look like "checked and clean".
    VENDOR_UNKNOWN = "VENDOR_UNKNOWN"
    PO_UNKNOWN = "PO_UNKNOWN"
    PO_VENDOR_MISMATCH = "PO_VENDOR_MISMATCH"
    NO_CONTRACT = "NO_CONTRACT"
    UNRESOLVED_LINE_ITEM = "UNRESOLVED_LINE_ITEM"


class Verdict(StrEnum):
    APPROVE_PENDING_HUMAN = "APPROVE_PENDING_HUMAN"  # clean -> HITL breakpoint
    REVIEW = "REVIEW"
    BLOCK = "BLOCK"


class PaymentStatus(StrEnum):
    PAID = "PAID"
    PENDING = "PENDING"
    VOID = "VOID"


SEVERITY: MappingProxyType[FindingCode, Severity] = MappingProxyType(
    {
        FindingCode.RATE_EXCEEDS_CONTRACT: Severity.BLOCK,
        FindingCode.DUPLICATE_INVOICE: Severity.BLOCK,
        FindingCode.POSSIBLE_DUPLICATE: Severity.REVIEW,
        FindingCode.ARITHMETIC_MISMATCH: Severity.BLOCK,
        FindingCode.PO_BUDGET_EXCEEDED: Severity.BLOCK,
        FindingCode.REMIT_TO_MISMATCH: Severity.BLOCK,
        FindingCode.VENDOR_UNKNOWN: Severity.BLOCK,
        FindingCode.PO_UNKNOWN: Severity.BLOCK,
        FindingCode.PO_VENDOR_MISMATCH: Severity.BLOCK,
        FindingCode.NO_CONTRACT: Severity.BLOCK,
        FindingCode.UNRESOLVED_LINE_ITEM: Severity.REVIEW,
    }
)

BUSINESS_FINDINGS: frozenset[FindingCode] = frozenset(
    {
        FindingCode.RATE_EXCEEDS_CONTRACT,
        FindingCode.DUPLICATE_INVOICE,
        FindingCode.POSSIBLE_DUPLICATE,
        FindingCode.ARITHMETIC_MISMATCH,
        FindingCode.PO_BUDGET_EXCEEDED,
        FindingCode.REMIT_TO_MISMATCH,
    }
)


def verdict_for(codes: frozenset[FindingCode] | set[FindingCode]) -> Verdict:
    """The only place a verdict is derived. Worst severity wins."""
    severities = {SEVERITY[c] for c in codes}
    if Severity.BLOCK in severities:
        return Verdict.BLOCK
    if Severity.REVIEW in severities:
        return Verdict.REVIEW
    return Verdict.APPROVE_PENDING_HUMAN


class RunStatus(StrEnum):
    """Lifecycle of one document through the agent workflow."""

    PROCESSING = "PROCESSING"
    NEEDS_MANUAL_ENTRY = "NEEDS_MANUAL_ENTRY"  # parser could not produce a trustworthy extraction
    BLOCKED = "BLOCKED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"  # HITL breakpoint
    REJECTED = "REJECTED"
    BLOCKED_AT_PAYMENT = "BLOCKED_AT_PAYMENT"  # clean at audit time, not clean at payment time
    PAID = "PAID"
