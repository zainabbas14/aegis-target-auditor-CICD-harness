# CUAD clause eval: `nvidia/nemotron-3.5-super-vl:free`

- **Run:** 2026-11-14T05:28:05+00:00 · **Endpoint:** `openrouter.ai`
- **Model(s) reported by the endpoint:** `nvidia/nemotron-3.5-super-vl:free`
- **Data:** 6 contracts × 8 categories from [CUAD](https://www.atticusprojectai.org/cuad) (The Atticus Project, CC BY 4.0); 28 clauses present, 20 absent per the expert annotations.

## Summary

| Labels | Found | Located | **False claims** | Missed | Withheld (to a human) | Accuracy (answered) | Precision | Recall |
|---|---|---|---|---|---|---|---|---|
| 48 | 23 | 23/23 | **1** | 1 | 8 | 95% | 96% | 82% |

*Found*: claimed present with a verbatim quote, and the clause is there. *Located*: the quote overlaps the annotated span. *False claims*: a grounded quote for a clause the annotators say is absent. *Withheld*: the agent made a claim it could not quote, or gave no answer - the system abstains.

## By category

| Category | Present (gold) | Found | False claims | Missed | Withheld |
|---|---|---|---|---|---|
| Expiration Date | 6/6 | 5 | 0 | 0 | 1 |
| Renewal Term | 2/6 | 2 | 1 | 0 | 1 |
| Governing Law | 6/6 | 4 | 0 | 1 | 1 |
| Termination For Convenience | 3/6 | 3 | 0 | 0 | 1 |
| Price Restrictions | 1/6 | 1 | 0 | 0 | 1 |
| Minimum Commitment | 2/6 | 2 | 0 | 0 | 1 |
| Audit Rights | 3/6 | 2 | 0 | 0 | 1 |
| Cap On Liability | 5/6 | 4 | 0 | 0 | 1 |

## Calls

| Contract | Result | Latency | Tokens in / out |
|---|---|---|---|
| DRAGONSYSTEMSINC_01_08_1999-EX-10.17-OUTSOURCING AGREEMENT | ok | 14625 ms | 2249 / 1952 |
| FEDERATEDGOVERNMENTINCOMESECURITIESINC_04_28_2020-EX-99.SERV AGREE-SER | llm_truncated | 37938 ms | 2894 / 6000 |
| ScansourceInc_20190822_10-K_EX-10.38_11793958_EX-10.38_Distributor Agr | ok | 16421 ms | 3620 / 2930 |
| ULTRAGENYXPHARMACEUTICALINC_12_23_2013-EX-10.9-SUPPLY AGREEMENT | ok | 26078 ms | 3739 / 3848 |
| LUCIDINC_04_15_2011-EX-10.9-DISTRIBUTOR AGREEMENT | ok | 18711 ms | 3848 / 2378 |
| FLOTEKINDUSTRIESINCCN_05_09_2019-EX-10.1-SUPPLY AGREEMENT | ok | 27296 ms | 4706 / 5024 |
