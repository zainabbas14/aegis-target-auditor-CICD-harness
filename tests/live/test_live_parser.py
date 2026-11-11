"""LIVE: real Claude calls against the 8 scenario PDFs. Costs real tokens.

Excluded from the default run and from CI. Run explicitly:

    ANTHROPIC_API_KEY=... pytest -m live -s

Reports per-field extraction accuracy against the golden file, then asserts
every scenario parses, grounds, and reaches its golden verdict.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from auditor.agents.parser import DocumentParser
from auditor.llm.anthropic_llm import AnthropicLLM
from auditor.ports import LedgerReader
from auditor.rules.engine import audit
from tests.mock_data.fixtures import SCENARIOS

pytestmark = pytest.mark.live

FIELDS = ("invoice_number", "vendor_tax_id", "po_number", "invoice_date", "remit_account", "subtotal", "tax", "total")


@pytest.fixture(scope="module")
def parser() -> DocumentParser:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        pytest.skip("ANTHROPIC_API_KEY not set")
    model = os.environ.get("AUDITOR_LLM_PARSER_MODEL", "claude-sonnet-5-5")
    return DocumentParser(AnthropicLLM(api_key=key, model=model))


def test_live_extraction_and_verdicts(parser: DocumentParser, built: Path, sqlite_ledger: LedgerReader) -> None:
    golden = {s["scenario_id"]: s for s in json.loads((built / "expected_outcomes.json").read_text())["scenarios"]}
    hits: dict[str, int] = dict.fromkeys(FIELDS, 0)
    failures: list[str] = []

    for scenario in SCENARIOS:
        sid = scenario.scenario_id
        expected: dict[str, Any] = json.loads((built / "requests" / f"{sid}.json").read_text())
        invoice = parser.parse((built / "invoices" / f"{sid}.pdf").read_bytes())
        got = invoice.model_dump(mode="json")
        for field in FIELDS:
            hits[field] += got[field] == expected[field]
        verdict = audit(invoice, sqlite_ledger).verdict.value
        status = "ok" if verdict == golden[sid]["expected_verdict"] else "MISMATCH"
        if status != "ok":
            failures.append(sid)
        print(f"{sid:28} verdict={verdict:22} {status}")

    print("\nper-field accuracy:")
    for field, n in hits.items():
        print(f"  {field:16} {n}/{len(SCENARIOS)}")
    assert not failures, failures
