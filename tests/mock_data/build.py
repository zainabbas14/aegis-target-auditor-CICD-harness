"""Build every mock-data artifact from fixtures.py.

    python -m tests.mock_data.build            # -> tests/mock_data/generated/

Outputs:
    generated/invoices/<scenario_id>.pdf
    generated/contracts/<contract_ref>.pdf
    generated/enterprise_erp.db
    generated/expected_outcomes.json   <- golden set for evals and e2e tests
    generated/requests/<scenario_id>.json   <- API request bodies (parser's-eye view)
    generated/postgres/002_seed.sql    <- same ledger, as a Postgres init script
"""

from __future__ import annotations

import argparse
import json
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any

from tests.mock_data.fixtures import CONTRACTS, SCENARIOS, Scenario, vendor_by_id
from tests.mock_data.pg_seed import export_postgres_seed
from tests.mock_data.render_contracts import render_contract
from tests.mock_data.render_invoices import render_invoice
from tests.mock_data.seed_db import DEFAULT_SEED, seed

logger = logging.getLogger(__name__)
DEFAULT_OUT = Path(__file__).with_name("generated")


def _golden(s: Scenario) -> dict[str, Any]:
    inv = s.invoice
    vendor = vendor_by_id(inv.vendor_id)
    return {
        "scenario_id": s.scenario_id,
        "title": s.title,
        "invoice_pdf": f"invoices/{s.scenario_id}.pdf",
        "expected_verdict": s.expected_verdict.value,
        "expected_findings": sorted(f.value for f in s.expected_findings),
        "rationale": s.rationale,
        # Ground truth for scoring the Document Parser field-by-field.
        # These are the values PRINTED on the document, not the "correct" ones.
        "expected_extraction": {
            "invoice_number": inv.invoice_number,
            "vendor_name": vendor.legal_name,
            "vendor_tax_id": vendor.tax_id,
            "po_number": inv.po_number,
            "invoice_date": inv.invoice_date.isoformat(),
            "due_date": inv.due_date.isoformat(),
            "remit_account": inv.remit_account_override or vendor.remit_account,
            "line_items": [
                {
                    "item_code": ln.item_code,
                    "description": ln.description,
                    "quantity": str(ln.quantity),
                    "unit_price": str(ln.unit_price),
                    "amount": str(ln.amount),
                }
                for ln in inv.lines
            ],
            "subtotal": str(inv.printed_subtotal),
            "tax": str(inv.printed_tax),
            "total": str(inv.printed_total),
            "currency": "USD",
        },
        # Codes are only printed on some layouts; services invoices require
        # mapping free-text descriptions to the rate card.
        "item_codes_printed": vendor.layout != "services_classic",
    }


def parser_view(golden: dict[str, Any]) -> dict[str, Any]:
    """What a perfect Document Parser would emit: the printed values only.
    Item codes are dropped for layouts that don't print them."""
    extraction = dict(golden["expected_extraction"])
    if not golden["item_codes_printed"]:
        extraction["line_items"] = [{**ln, "item_code": None} for ln in extraction["line_items"]]
    return extraction


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=_json_default) + "\n", encoding="utf-8")


def _json_default(o: object) -> str:
    if isinstance(o, Decimal):
        return str(o)
    raise TypeError(f"not JSON serialisable: {type(o).__name__}")


def build(out_dir: Path = DEFAULT_OUT, seed_value: int = DEFAULT_SEED) -> None:
    contracts_dir = out_dir / "contracts"
    for c in CONTRACTS:
        render_contract(c, contracts_dir / f"{c.contract_ref}.pdf")
    for s in SCENARIOS:
        render_invoice(s.invoice, out_dir / "invoices" / f"{s.scenario_id}.pdf")
    seed(out_dir / "enterprise_erp.db", Path("contracts"), seed_value)
    export_postgres_seed(out_dir / "enterprise_erp.db", out_dir / "postgres" / "002_seed.sql")

    scenarios = [_golden(s) for s in SCENARIOS]
    _write_json(out_dir / "expected_outcomes.json", {"schema_version": 1, "seed": seed_value, "scenarios": scenarios})
    for g in scenarios:
        _write_json(out_dir / "requests" / f"{g['scenario_id']}.json", parser_view(g))
    logger.info("Built %d contracts, %d invoices into %s", len(CONTRACTS), len(SCENARIOS), out_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build mock-data artifacts")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    build(args.out, args.seed)


if __name__ == "__main__":
    main()
