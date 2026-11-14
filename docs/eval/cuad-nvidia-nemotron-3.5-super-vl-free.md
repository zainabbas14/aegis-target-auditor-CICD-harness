# CUAD clause eval: `nvidia/nemotron-3.5-super-vl:free`

- **Run:** 2026-11-14T05:33:54+00:00 · **Endpoint:** `openrouter.ai`
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

## False claims (read before trusting the label)

- **Renewal Term** in `FLOTEKINDUSTRIESINCCN_05_09_2019-EX-10.1-SUPPLY AGREEMENT`: “Flotek, may, by written notice given to FCC on or before September 30, 2023, elect for the Term to be extended to December 31, 2024.”

## Calls

| Contract | Result | Latency | Tokens in / out |
|---|---|---|---|
| DRAGONSYSTEMSINC_01_08_1999-EX-10.17-OUTSOURCING AGREEMENT | ok | 17470 ms | 2249 / 2703 |
| FEDERATEDGOVERNMENTINCOMESECURITIESINC_04_28_2020-EX-99.SERV AGREE-SER | llm_truncated | 40840 ms | 2894 / 6000 |
| ScansourceInc_20190822_10-K_EX-10.38_11793958_EX-10.38_Distributor Agr | ok | 13816 ms | 3620 / 2459 |
| ULTRAGENYXPHARMACEUTICALINC_12_23_2013-EX-10.9-SUPPLY AGREEMENT | ok | 25010 ms | 3739 / 4063 |
| LUCIDINC_04_15_2011-EX-10.9-DISTRIBUTOR AGREEMENT | ok | 17107 ms | 3848 / 2748 |
| FLOTEKINDUSTRIESINCCN_05_09_2019-EX-10.1-SUPPLY AGREEMENT | ok | 19759 ms | 4706 / 3761 |
