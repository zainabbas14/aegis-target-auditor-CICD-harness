"""Clause agent + CUAD scorer, with a scripted model answering from the expert labels."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence

import pytest

from auditor.agents.clauses import (
    CLAUSE_CATEGORIES,
    ClauseAnswerDraft,
    ClauseFinder,
    ClausesDraft,
    ClauseStatus,
    is_grounded,
)
from auditor.llm.ports import LLMError, LLMUnavailableError, Part, T, TextPart
from evals.cuad import SUBSET, overlaps, render_markdown, run, score, summarise

SUBSET_DATA = json.loads(SUBSET.read_text(encoding="utf-8"))


class OracleLLM:
    """Answers with the CUAD annotation for whichever contract it is shown."""

    model = "oracle"

    def __init__(self, tweak: Callable[[ClausesDraft], ClausesDraft] | LLMError | None = None) -> None:
        self.tweak = tweak

    def generate(
        self, *, output_type: type[T], system: str, content: Sequence[Part], max_tokens: int, purpose: str
    ) -> T:
        if isinstance(self.tweak, LLMError):
            raise self.tweak
        first = content[0]
        assert isinstance(first, TextPart)
        contract = next(c for c in SUBSET_DATA["contracts"] if c["text"] in first.text)
        draft = ClausesDraft(
            clauses=[
                ClauseAnswerDraft(category=cat, present=bool(spans), quote=spans[0] if spans else None)
                for cat, spans in contract["labels"].items()
            ]
        )
        return self.tweak(draft) if self.tweak else draft  # type: ignore[return-value]


def test_subset_is_attributed_and_complete() -> None:
    assert "CC BY 4.0" in SUBSET_DATA["license"]
    assert SUBSET_DATA["categories"] == list(CLAUSE_CATEGORIES)
    assert len(SUBSET_DATA["contracts"]) == 6
    present = sum(bool(v) for c in SUBSET_DATA["contracts"] for v in c["labels"].values())
    assert 0 < present < 48  # both present and absent cases, so false claims are measurable


def test_oracle_scores_perfectly() -> None:
    cells, calls = run(OracleLLM())
    s = summarise(cells)
    assert (s["labels"], s["false_claim"], s["missed"], s["withheld"]) == (48, 0, 0, 0)
    assert s["found"] == s["gold_present"] == s["located"]
    assert s["precision"] == s["recall"] == s["accuracy_on_answered"] == 1.0
    assert all(c["failure"] is None for c in calls)


def _first_present(draft: ClausesDraft, quote: str) -> ClausesDraft:
    first = next(i for i, a in enumerate(draft.clauses) if a.present)
    return ClausesDraft(
        clauses=[a.model_copy(update={"quote": quote}) if i == first else a for i, a in enumerate(draft.clauses)]
    )


def test_paraphrased_quote_is_withheld_not_trusted() -> None:
    llm = OracleLLM(lambda d: _first_present(d, "The parties agree that either may end this deal whenever."))
    s = summarise(run(llm)[0])
    assert (s["withheld"], s["false_claim"]) == (6, 0)  # one per contract, none passed through


def test_claiming_a_clause_that_is_not_there_with_a_real_sentence_is_a_false_claim() -> None:
    """Grounding proves the words exist, not that they mean what's claimed - this is what the eval is for."""
    contract = SUBSET_DATA["contracts"][0]
    absent = next(cat for cat, spans in contract["labels"].items() if not spans)
    real_sentence = next(spans[0] for spans in contract["labels"].values() if spans)

    def claim(d: ClausesDraft) -> ClausesDraft:
        return ClausesDraft(
            clauses=[
                a.model_copy(update={"present": True, "quote": real_sentence}) if a.category == absent else a
                for a in d.clauses
            ]
        )

    report = ClauseFinder(OracleLLM(claim)).find(contract["text"])
    cells = score(report, contract)
    assert [c.outcome for c in cells if c.category == absent] == ["false_claim"]


def test_omitted_categories_and_failures_are_not_answered() -> None:
    contract = SUBSET_DATA["contracts"][0]
    partial = ClauseFinder(OracleLLM(lambda d: ClausesDraft(clauses=d.clauses[:3]))).find(contract["text"])
    assert sum(a.status is ClauseStatus.NOT_ANSWERED for a in partial.answers) == 5
    failed = ClauseFinder(OracleLLM(LLMUnavailableError("transient_failure"))).find(contract["text"])
    assert failed.failure == "llm_transient_failure"
    assert {a.status for a in failed.answers} == {ClauseStatus.NOT_ANSWERED}
    assert ClauseFinder(None).find("x").failure == "llm_not_configured"


@pytest.mark.parametrize(
    ("quote", "ok"),
    [
        ("shall be governed by\n   the laws of", True),  # whitespace differences are fine
        ("“shall be governed by the laws of”.", True),  # added quote marks / punctuation are fine
        ("shall be governed by the law of", False),  # one word changed
        ("governed", False),  # too short to count as evidence
    ],
)
def test_grounding(quote: str, ok: bool) -> None:
    assert is_grounded(quote, "This Agreement shall be governed by the laws of Texas.") is ok


def test_overlap_measures_location() -> None:
    assert overlaps("Either party may terminate on 60 days notice", ["either party may terminate this on 60 days"])
    assert not overlaps("This Agreement is governed by Texas law", ["either party may terminate on notice"])


def test_markdown_report() -> None:
    cells, calls = run(OracleLLM())
    md = render_markdown("oracle", "fake", cells, calls)
    assert "| 48 |" in md
    assert "**0**" in md
    assert "CC BY 4.0" in md
