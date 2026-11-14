"""Regenerate evals/data/cuad_subset.json from the full CUAD release.

    git clone --depth 1 https://github.com/TheAtticusProject/cuad.git /tmp/cuad
    unzip /tmp/cuad/data.zip -d /tmp/cuad/data
    python -m evals.cuad_subset /tmp/cuad/data/CUADv1.json

CUAD (Contract Understanding Atticus Dataset) by The Atticus Project, licensed CC BY 4.0
(https://www.atticusprojectai.org/cuad). The subset keeps each contract's full text and the expert
annotations for the clause categories this project evaluates; nothing else is modified.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from auditor.agents.clauses import CLAUSE_CATEGORIES

OUT = Path(__file__).with_name("data") / "cuad_subset.json"

# Short supply / services / distribution agreements: the contract types an AP auditor meets.
CONTRACTS = (
    "DRAGONSYSTEMSINC_01_08_1999-EX-10.17-OUTSOURCING AGREEMENT",
    "FEDERATEDGOVERNMENTINCOMESECURITIESINC_04_28_2020-EX-99.SERV AGREE-SERVICES AGREEMENT",
    "ScansourceInc_20190822_10-K_EX-10.38_11793958_EX-10.38_Distributor Agreement2",
    "ULTRAGENYXPHARMACEUTICALINC_12_23_2013-EX-10.9-SUPPLY AGREEMENT",
    "LUCIDINC_04_15_2011-EX-10.9-DISTRIBUTOR AGREEMENT",
    "FLOTEKINDUSTRIESINCCN_05_09_2019-EX-10.1-SUPPLY AGREEMENT",
)


def build(cuad_json: Path, out: Path = OUT) -> Path:
    data = {c["title"]: c for c in json.loads(cuad_json.read_text(encoding="utf-8"))["data"]}
    contracts = []
    for title in CONTRACTS:
        para = data[title]["paragraphs"][0]
        qas = {q["id"].split("__", 1)[1]: q for q in para["qas"]}
        contracts.append(
            {
                "title": title,
                "text": para["context"],
                "labels": {cat: [a["text"] for a in qas[cat]["answers"]] for cat in CLAUSE_CATEGORIES},
            }
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "source": "CUAD v1 - The Atticus Project - https://www.atticusprojectai.org/cuad",
                "license": "CC BY 4.0 - https://creativecommons.org/licenses/by/4.0/",
                "categories": list(CLAUSE_CATEGORIES),
                "contracts": contracts,
            },
            indent=1,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cuad_json", type=Path)
    print(build(parser.parse_args().cuad_json))


if __name__ == "__main__":
    main()
