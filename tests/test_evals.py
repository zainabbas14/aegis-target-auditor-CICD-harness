"""The eval harness's scoring, exercised with the scripted fake LLM."""

from __future__ import annotations

from pathlib import Path

from auditor.adapters.sqlite_ledger import SqliteLedger
from evals.run import render_markdown, run_eval
from tests.fakes import FakeLLM


def test_perfect_model_scores_perfectly(built: Path, sqlite_ledger: SqliteLedger) -> None:
    report = run_eval(FakeLLM(built), built, sqlite_ledger, endpoint="fake")
    inv, con = report.summary("invoice"), report.summary("contract")
    assert (inv["documents"], inv["grounded"], inv["outcome_correct"], inv["silently_wrong"]) == (8, 8, 8, 0)
    assert inv["field_accuracy_on_grounded"] == 1.0
    assert (con["documents"], con["outcome_correct"], con["silently_wrong"]) == (3, 3, 0)


def test_hallucination_is_routed_to_a_human_not_silently_wrong(built: Path, sqlite_ledger: SqliteLedger) -> None:
    llm = FakeLLM(built)
    llm.parse_override = lambda x: x.model_copy(update={"total": "1.00"})
    report = run_eval(llm, built, sqlite_ledger, include_contracts=False)
    s = report.summary("invoice")
    assert (s["grounded"], s["silently_wrong"]) == (0, 0)
    assert all(r.failure and r.failure.startswith("ungrounded") for r in report.results)


def test_field_error_is_counted_even_when_verdict_survives(built: Path, sqlite_ledger: SqliteLedger) -> None:
    llm = FakeLLM(built)
    llm.parse_override = lambda x: x.model_copy(update={"due_date": "2030-01-01"})  # not grounded, not used by rules
    report = run_eval(llm, built, sqlite_ledger, include_contracts=False, limit=2)
    s = report.summary("invoice")
    assert s["outcome_correct"] == 2
    assert s["field_accuracy_on_grounded"] < 1.0
    assert all(r.fields["due_date"] is False for r in report.results)


def test_markdown_report(built: Path, sqlite_ledger: SqliteLedger) -> None:
    md = render_markdown(run_eval(FakeLLM(built), built, sqlite_ledger, endpoint="fake", limit=1))
    assert "| Invoices | 1 | 1/1 | 100% | 1/1 | **0** |" in md
    assert "S01_clean_services" in md
