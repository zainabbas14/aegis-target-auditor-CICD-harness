"""Live evaluation: run the real document agents against the golden set.

    python -m evals.run                      # uses the LLM configured in .env / environment
    python -m evals.run --skip-contracts --limit 3

Writes docs/eval/<model>.md and .json. Costs real tokens (or free-tier quota);
never runs in CI. CI covers the same pipeline with a scripted fake LLM
(tests/test_evals.py), so the scoring code itself is tested.

What is measured, per document:
  * grounded       - the extraction passed shape validation and text-layer grounding
                     (otherwise the system routes the document to a human)
  * field accuracy - each extracted field vs the golden value
  * verdict        - for invoices, the rule engine's verdict on the extraction vs golden
  * silently wrong - grounded AND wrong verdict. The number that must be zero:
                     everything else is a cost; this is a failure.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from auditor.adapters.sqlite_ledger import SqliteLedger
from auditor.agents.contracts import ContractReader, reconcile
from auditor.agents.mapper import LineItemMapper, unresolved_lines
from auditor.agents.parser import DocumentParser, ExtractionFailedError
from auditor.config import Settings
from auditor.domain.models import ExtractedInvoice
from auditor.llm.ports import CallStats, StructuredLLM
from auditor.ports import LedgerReader
from auditor.rules.engine import audit
from auditor.workflow.factory import build_llm
from tests.mock_data.build import build, parser_view
from tests.mock_data.fixtures import CONTRACTS, SCENARIOS, contract_by_ref, vendor_by_id

logger = logging.getLogger("evals")

INVOICE_FIELDS = (
    "invoice_number",
    "vendor_tax_id",
    "po_number",
    "invoice_date",
    "due_date",
    "remit_account",
    "subtotal",
    "tax",
    "total",
    "line_items",
)
CONTRACT_FIELDS = (
    "contract_ref",
    "supplier_tax_id",
    "effective_date",
    "expiry_date",
    "payment_terms_days",
    "rate_card",
)


@dataclass
class DocResult:
    kind: str
    doc_id: str
    grounded: bool
    failure: str | None = None
    fields: dict[str, bool] = field(default_factory=dict)
    expected_outcome: str | None = None
    got_outcome: str | None = None
    latency_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    served_model: str | None = None
    mappings_proposed: int = 0
    mappings_correct: int = 0

    @property
    def outcome_correct(self) -> bool:
        return self.grounded and self.got_outcome == self.expected_outcome

    @property
    def silently_wrong(self) -> bool:
        return self.grounded and self.got_outcome != self.expected_outcome


@dataclass
class EvalReport:
    requested_model: str
    endpoint: str
    settings: dict[str, Any]
    started_at: str
    results: list[DocResult]

    def summary(self, kind: str) -> dict[str, Any]:
        docs = [r for r in self.results if r.kind == kind]
        grounded = [r for r in docs if r.grounded]
        field_checks = [ok for r in grounded for ok in r.fields.values()]
        latencies = [r.latency_ms for r in docs if r.latency_ms is not None]
        return {
            "documents": len(docs),
            "grounded": len(grounded),
            "field_accuracy_on_grounded": (sum(field_checks) / len(field_checks)) if field_checks else None,
            "outcome_correct": sum(r.outcome_correct for r in docs),
            "silently_wrong": sum(r.silently_wrong for r in docs),
            "avg_latency_ms": round(sum(latencies) / len(latencies)) if latencies else None,
            "mapping_lines": sum(r.mappings_proposed for r in docs),
            "mapping_correct": sum(r.mappings_correct for r in docs),
            "input_tokens": sum(r.input_tokens or 0 for r in docs),
            "output_tokens": sum(r.output_tokens or 0 for r in docs),
        }

    def served_models(self) -> list[str]:
        return sorted({r.served_model for r in self.results if r.served_model})


def _stats(llm: StructuredLLM) -> CallStats | None:
    stats: CallStats | None = getattr(llm, "last_stats", None)
    return stats


def _apply_stats(result: DocResult, llm: StructuredLLM, started: float) -> None:
    stats = _stats(llm)
    result.latency_ms = stats.latency_ms if stats else round((time.perf_counter() - started) * 1000)
    if stats:
        result.input_tokens, result.output_tokens, result.served_model = (
            stats.input_tokens,
            stats.output_tokens,
            stats.served_model,
        )


def eval_invoices(llm: StructuredLLM, built: Path, ledger: LedgerReader, limit: int | None) -> list[DocResult]:
    golden_all = {g["scenario_id"]: g for g in json.loads((built / "expected_outcomes.json").read_text())["scenarios"]}
    parser = DocumentParser(llm)
    results: list[DocResult] = []
    for scenario in SCENARIOS[:limit]:
        sid = scenario.scenario_id
        golden = golden_all[sid]
        expected = ExtractedInvoice.model_validate(parser_view(golden)).model_dump(mode="json")
        result = DocResult(kind="invoice", doc_id=sid, grounded=False, expected_outcome=golden["expected_verdict"])
        setattr(llm, "last_stats", None)  # noqa: B010 - adapters expose it as a plain attribute
        started = time.perf_counter()
        try:
            invoice = parser.parse((built / "invoices" / f"{sid}.pdf").read_bytes())
        except ExtractionFailedError as exc:
            result.failure = f"{exc.reason}: {exc.detail}" if exc.detail else exc.reason
        else:
            got = invoice.model_dump(mode="json")
            result.grounded = True
            result.fields = {f: got[f] == expected[f] for f in INVOICE_FIELDS}
            audited = audit(invoice, ledger)
            result.got_outcome = audited.verdict.value
        _apply_stats(result, llm, started)  # parse-call stats, before any mapping call overwrites them
        if result.grounded:
            _eval_mappings(result, llm, invoice=invoice, audited=audited, ledger=ledger, scenario=scenario)
        logger.info("invoice %s grounded=%s outcome=%s", sid, result.grounded, result.got_outcome or result.failure)
        results.append(result)
    return results


def _eval_mappings(
    result: DocResult,
    llm: StructuredLLM,
    *,
    invoice: ExtractedInvoice,
    audited: Any,
    ledger: LedgerReader,
    scenario: Any,
) -> None:
    """For lines the rules couldn't match, score the mapper's proposals against the fixture's true code."""
    if audited.verdict.value == "BLOCK":
        return
    pending = unresolved_lines([f.model_dump(mode="json") for f in audited.findings])
    if not pending:
        return
    vendor = ledger.vendor_by_tax_id(invoice.vendor_tax_id)
    if vendor is None or vendor.contract_ref is None:
        return
    proposals, _ = LineItemMapper(llm).propose(
        {n: invoice.line_items[n - 1] for n in pending}, ledger.rate_card(vendor.contract_ref)
    )
    result.mappings_proposed = len(proposals)
    result.mappings_correct = sum(p.proposed_code == scenario.invoice.lines[p.line - 1].item_code for p in proposals)


def eval_contracts(llm: StructuredLLM, built: Path, ledger: LedgerReader) -> list[DocResult]:
    reader = ContractReader(llm)
    results: list[DocResult] = []
    for c in CONTRACTS:
        fixture = contract_by_ref(c.contract_ref)
        vendor = vendor_by_id(fixture.vendor_id)
        expected = {
            "contract_ref": fixture.contract_ref,
            "supplier_tax_id": vendor.tax_id,
            "effective_date": fixture.effective_date.isoformat(),
            "expiry_date": fixture.expiry_date.isoformat(),
            "payment_terms_days": fixture.payment_terms_days,
            "rate_card": sorted((r.item_code, f"{r.max_unit_price:.2f}") for r in fixture.rate_card),
        }
        result = DocResult(kind="contract", doc_id=c.contract_ref, grounded=False, expected_outcome="CONSISTENT")
        setattr(llm, "last_stats", None)  # noqa: B010
        started = time.perf_counter()
        try:
            contract = reader.read((built / "contracts" / f"{c.contract_ref}.pdf").read_bytes())
        except ExtractionFailedError as exc:
            result.failure = f"{exc.reason}: {exc.detail}" if exc.detail else exc.reason
        else:
            got = contract.model_dump(mode="json")
            got["rate_card"] = sorted((r["item_code"], f"{float(r['max_unit_price']):.2f}") for r in got["rate_card"])
            result.grounded = True
            result.fields = {f: got[f] == expected[f] for f in CONTRACT_FIELDS}
            result.got_outcome = reconcile(contract, ledger).status
        _apply_stats(result, llm, started)
        logger.info("contract %s grounded=%s outcome=%s", c.contract_ref, result.grounded, result.got_outcome)
        results.append(result)
    return results


def run_eval(
    llm: StructuredLLM,
    built: Path,
    ledger: LedgerReader,
    *,
    endpoint: str = "unknown",
    settings: dict[str, Any] | None = None,
    include_contracts: bool = True,
    limit: int | None = None,
) -> EvalReport:
    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    results = eval_invoices(llm, built, ledger, limit)
    if include_contracts:
        results += eval_contracts(llm, built, ledger)
    return EvalReport(
        requested_model=llm.model, endpoint=endpoint, settings=settings or {}, started_at=started_at, results=results
    )


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x:.0%}"


def render_markdown(report: EvalReport) -> str:
    served = ", ".join(f"`{m}`" for m in report.served_models()) or "not reported"
    lines = [
        f"# Eval: `{report.requested_model}`",
        "",
        f"- **Run:** {report.started_at}",
        f"- **Endpoint:** `{report.endpoint}`",
        f"- **Model requested:** `{report.requested_model}`",
        f"- **Model(s) reported by the endpoint:** {served}",
        f"- **Settings:** {', '.join(f'{k}={v}' for k, v in sorted(report.settings.items())) or '-'}",
        "",
        "## Summary",
        "",
        "| | Documents | Grounded (auto-processed) | Field accuracy (grounded) | Correct outcome | "
        "**Silently wrong** | Avg latency | Tokens in / out |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for kind, label in (("invoice", "Invoices"), ("contract", "Contracts")):
        s = report.summary(kind)
        if not s["documents"]:
            continue
        lines.append(
            f"| {label} | {s['documents']} | {s['grounded']}/{s['documents']} | "
            f"{_pct(s['field_accuracy_on_grounded'])} | {s['outcome_correct']}/{s['documents']} | "
            f"**{s['silently_wrong']}** | {s['avg_latency_ms'] or '-'} ms | "
            f"{s['input_tokens']:,} / {s['output_tokens']:,} |"
        )
    inv = report.summary("invoice")
    if inv.get("mapping_lines"):
        lines += [
            "",
            f"**Line-item mapping proposals:** {inv['mapping_correct']}/{inv['mapping_lines']} correct "
            "(lines the rules could not match; proposals are confirmed by a human before use).",
        ]
    lines += [
        "",
        "*Grounded* documents passed schema validation and had every critical value found in the PDF's own text "
        "layer; the rest were routed to a human, which costs time but not correctness. *Silently wrong* counts "
        "grounded documents whose outcome differed from the golden answer - the only result that would let a "
        "bad payment through, and the one that must be zero.",
        "",
        "## Per document",
        "",
        "| Document | Grounded | Fields correct | Expected | Got | Latency | Note |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in report.results:
        wrong = [k for k, ok in r.fields.items() if not ok]
        fields = f"{sum(r.fields.values())}/{len(r.fields)}" if r.fields else "-"
        note = r.failure or (f"mismatched: {', '.join(wrong)}" if wrong else "")
        lines.append(
            f"| {r.doc_id} | {'yes' if r.grounded else 'no'} | {fields} | {r.expected_outcome} | "
            f"{r.got_outcome or '(human)'} | {r.latency_ms or '-'} ms | {note} |"
        )
    return "\n".join(lines) + "\n"


def _slug(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9.-]+", "-", text).strip("-").lower()


def main() -> int:
    parser = argparse.ArgumentParser(description="Live eval of the document agents against the golden set")
    parser.add_argument("--out-dir", type=Path, default=Path("docs/eval"))
    parser.add_argument("--model", help="override AUDITOR_LLM_PARSER_MODEL for this run")
    parser.add_argument("--skip-contracts", action="store_true")
    parser.add_argument("--limit", type=int, help="only the first N invoices")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    settings = Settings()
    model = args.model or settings.llm_parser_model
    llm = build_llm(settings, model)
    if llm is None:
        logger.error("No LLM configured. Set AUDITOR_LLM_PROVIDER and its credentials in .env (see .env.example).")
        return 2
    endpoint = (
        urlparse(settings.llm_base_url).netloc
        if settings.llm_provider == "openai_compatible" and settings.llm_base_url
        else "api.anthropic.com"
    )
    run_settings: dict[str, str] = {"provider": settings.llm_provider}
    if settings.llm_provider == "openai_compatible":
        run_settings |= {"json_mode": settings.llm_json_mode, "pdf_mode": settings.llm_pdf_mode}

    with tempfile.TemporaryDirectory() as tmp:
        built = Path(tmp)
        build(built)
        report = run_eval(
            llm,
            built,
            SqliteLedger(built / "enterprise_erp.db"),
            endpoint=endpoint,
            settings=run_settings,
            include_contracts=not args.skip_contracts,
            limit=args.limit,
        )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    stem = _slug(model)
    markdown = render_markdown(report)
    (args.out_dir / f"{stem}.md").write_text(markdown, encoding="utf-8")
    payload = asdict(report) | {"summary": {k: report.summary(k) for k in ("invoice", "contract")}}
    (args.out_dir / f"{stem}.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    sys.stdout.write("\n" + markdown)
    logger.info("Wrote %s", args.out_dir / f"{stem}.md")
    return 1 if any(r.silently_wrong for r in report.results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
