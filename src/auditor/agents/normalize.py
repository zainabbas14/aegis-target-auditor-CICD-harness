"""Deterministic helpers shared by the document agents: normalising what an LLM
transcribed, and the renderings used to ground values against a PDF text layer."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

_MONEY_JUNK = re.compile(r"[\s,$€£]|USD|PKR")


def clean_money(value: str) -> str:
    return _MONEY_JUNK.sub("", value)


def money_renderings(value: Decimal) -> set[str]:
    return {f"{value:,.2f}", f"{value:.2f}"}


def date_renderings(d: date) -> set[str]:
    return {
        d.isoformat(),
        d.strftime("%m/%d/%Y"),
        d.strftime("%d/%m/%Y"),
        d.strftime("%d %b %Y"),
        f"{d:%B} {d.day}, {d.year}",
    }
