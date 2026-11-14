"""Live evaluation of the clause agent on real contracts (CUAD subset).

    python -m evals.cuad                  # LLM from .env; 6 calls
    python -m evals.cuad --model <id>

Scores each (contract, clause category) against the CUAD expert annotations:

  found        claimed present with a grounded quote, and CUAD agrees it's there
  located      ...and the quote overlaps CUAD's annotated span (right clause, not just right answer)
  false claim  claimed present with a grounded quote, but CUAD has no such clause
  missed       claimed absent, but CUAD has the clause
  withheld     the agent abstained: a presence claim without a real quote, or no answer

Withheld answers go to a human; false claims and misses are errors. CUAD annotations
are expert-made but not infallible, so a "false claim" is worth reading before trusting.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from auditor.agents.clauses import CLAUSE_CATEGORIES, ClauseFinder, ClauseReport, ClauseStatus
from auditor.config import Settings
from auditor.llm.ports import StructuredLLM
from auditor.workflow.factory import build_llm

logger = logging.getLogger("evals.cuad")

SUBSET = Path(__file__).with_name("data") / "cuad_subset.json"
_WORD = re.compile(r"[a-z0-9]+")
LOCATE_OVERLAP = 0.5


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def overlaps(quote: str, spans: list[str]) -> bool:
    """Quote and an annotated span share at least half the words of the shorter one."""
    q = _words(quote)
    for span in spans:
        s = _words(span)
        if q and s and len(q & s) / min(len(q), len(s)) >= LOCATE_OVERLAP:
            return True
    return False


@dataclass
class Cell:
    contract: str
    category: str
    gold_present: bool
    status: str
    outcome: str  # found | false_claim | correct_absent | missed | withheld
    located: bool = False


def score(report: ClauseReport, contract: dict[str, Any]) -> list[Cell]:
    cells: list[Cell] = []
    for answer in report.answers:
        spans: list[str] = contract["labels"][answer.category]
        gold = bool(spans)
        if answer.status is ClauseStatus.PRESENT:
            outcome = "found" if gold else "false_claim"
        elif answer.status is ClauseStatus.ABSENT:
            outcome = "missed" if gold else "correct_absent"
        else:
            outcome = "withheld"
        cells.append(
            Cell(
                contract=contract["title"],
                category=answer.category,
                gold_present=gold,
                status=answer.status.value,
                outcome=outcome,
                located=outcome == "found" and overlaps(answer.quote or "", spans),
            )
        )
    return cells


def summarise(cells: list[Cell]) -> dict[str, Any]:
    n = len(cells)
    count = {
        k: sum(c.outcome == k for c in cells) for k in ("found", "false_claim", "correct_absent", "missed", "withheld")
    }
    answered = n - count["withheld"]
    gold_present = sum(c.gold_present for c in cells)
    claimed = count["found"] + count["false_claim"]
    return {
        "labels": n,
        "gold_present": gold_present,
        **count,
        "located": sum(c.located for c in cells),
        "accuracy_on_answered": (count["found"] + count["correct_absent"]) / answered if answered else None,
        "precision": count["found"] / claimed if claimed else None,
        "recall": count["found"] / gold_present if gold_present else None,
    }


def run(llm: StructuredLLM, subset: Path = SUBSET) -> tuple[list[Cell], list[dict[str, Any]]]:
    data = json.loads(subset.read_text(encoding="utf-8"))
    finder = ClauseFinder(llm)
    cells: list[Cell] = []
    calls: list[dict[str, Any]] = []
    for contract in data["contracts"]:
        setattr(llm, "last_stats", None)  # noqa: B010
        started = time.perf_counter()
        report = finder.find(contract["text"])
        stats = getattr(llm, "last_stats", None)
        calls.append(
            {
                "contract": contract["title"],
                "failure": report.failure,
                "latency_ms": stats.latency_ms if stats else round((time.perf_counter() - started) * 1000),
                "input_tokens": getattr(stats, "input_tokens", None),
                "output_tokens": getattr(stats, "output_tokens", None),
                "served_model": getattr(stats, "served_model", None),
            }
        )
        cells += score(report, contract)
        logger.info("%s: %s", contract["title"][:60], report.failure or "ok")
    return cells, calls


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x:.0%}"


def render_markdown(model: str, endpoint: str, cells: list[Cell], calls: list[dict[str, Any]]) -> str:
    s = summarise(cells)
    served = sorted({c["served_model"] for c in calls if c["served_model"]})
    lines = [
        f"# CUAD clause eval: `{model}`",
        "",
        f"- **Run:** {datetime.now(UTC).isoformat(timespec='seconds')} · **Endpoint:** `{endpoint}`",
        f"- **Model(s) reported by the endpoint:** {', '.join(f'`{m}`' for m in served) or 'not reported'}",
        f"- **Data:** {len(calls)} contracts × {len(CLAUSE_CATEGORIES)} categories from "
        "[CUAD](https://www.atticusprojectai.org/cuad) (The Atticus Project, CC BY 4.0); "
        f"{s['gold_present']} clauses present, {s['labels'] - s['gold_present']} absent per the expert annotations.",
        "",
        "## Summary",
        "",
        "| Labels | Found | Located | **False claims** | Missed | Withheld (to a human) | "
        "Accuracy (answered) | Precision | Recall |",
        "|---|---|---|---|---|---|---|---|---|",
        f"| {s['labels']} | {s['found']} | {s['located']}/{s['found']} | **{s['false_claim']}** | {s['missed']} | "
        f"{s['withheld']} | {_pct(s['accuracy_on_answered'])} | {_pct(s['precision'])} | {_pct(s['recall'])} |",
        "",
        "*Found*: claimed present with a verbatim quote, and the clause is there. *Located*: the quote overlaps "
        "the annotated span. *False claims*: a grounded quote for a clause the annotators say is absent. "
        "*Withheld*: the agent made a claim it could not quote, or gave no answer - the system abstains.",
        "",
        "## By category",
        "",
        "| Category | Present (gold) | Found | False claims | Missed | Withheld |",
        "|---|---|---|---|---|---|",
    ]
    for cat in CLAUSE_CATEGORIES:
        cc = [c for c in cells if c.category == cat]
        lines.append(
            f"| {cat} | {sum(c.gold_present for c in cc)}/{len(cc)} | {sum(c.outcome == 'found' for c in cc)} | "
            f"{sum(c.outcome == 'false_claim' for c in cc)} | {sum(c.outcome == 'missed' for c in cc)} | "
            f"{sum(c.outcome == 'withheld' for c in cc)} |"
        )
    lines += ["", "## Calls", "", "| Contract | Result | Latency | Tokens in / out |", "|---|---|---|---|"]
    for c in calls:
        lines.append(
            f"| {c['contract'][:70]} | {c['failure'] or 'ok'} | {c['latency_ms']} ms | "
            f"{c['input_tokens'] or '-'} / {c['output_tokens'] or '-'} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Live eval of the clause agent on CUAD contracts")
    parser.add_argument("--out-dir", type=Path, default=Path("docs/eval"))
    parser.add_argument("--model", help="override AUDITOR_LLM_PARSER_MODEL for this run")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    settings = Settings()
    model = args.model or settings.llm_parser_model
    llm = build_llm(settings, model)
    if llm is None:
        logger.error("No LLM configured. Set AUDITOR_LLM_PROVIDER and its credentials in .env.")
        return 2
    endpoint = urlparse(settings.llm_base_url).netloc if settings.llm_base_url else "api.anthropic.com"

    cells, calls = run(llm)
    markdown = render_markdown(model, endpoint, cells, calls)
    stem = "cuad-" + re.sub(r"[^a-zA-Z0-9.-]+", "-", model).strip("-").lower()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / f"{stem}.md").write_text(markdown, encoding="utf-8")
    payload = {"model": model, "endpoint": endpoint, "summary": summarise(cells), "calls": calls}
    payload["cells"] = [asdict(c) for c in cells]
    (args.out_dir / f"{stem}.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    sys.stdout.write("\n" + markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
