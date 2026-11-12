# Eval: `nvidia/nemotron-3.5-super-vl:free`

- **Run:** 2026-11-12T18:41:42+00:00
- **Endpoint:** `openrouter.ai`
- **Model requested:** `nvidia/nemotron-3.5-super-vl:free`
- **Model(s) reported by the endpoint:** `nvidia/nemotron-3.5-super-vl:free`
- **Settings:** json_mode=json_schema, pdf_mode=text, provider=openai_compatible

## Summary

| | Documents | Grounded (auto-processed) | Field accuracy (grounded) | Correct outcome | **Silently wrong** | Avg latency | Tokens in / out |
|---|---|---|---|---|---|---|---|
| Invoices | 8 | 8/8 | 100% | 8/8 | **0** | 11405 ms | 10,119 / 13,557 |
| Contracts | 3 | 3/3 | 100% | 3/3 | **0** | 10219 ms | 5,320 / 4,815 |

*Grounded* documents passed schema validation and had every critical value found in the PDF's own text layer; the rest were routed to a human, which costs time but not correctness. *Silently wrong* counts grounded documents whose outcome differed from the golden answer - the only result that would let a bad payment through, and the one that must be zero.

## Per document

| Document | Grounded | Fields correct | Expected | Got | Latency | Note |
|---|---|---|---|---|---|---|
| S01_clean_services | yes | 10/10 | APPROVE_PENDING_HUMAN | APPROVE_PENDING_HUMAN | 9528 ms |  |
| S02_clean_goods_with_tax | yes | 10/10 | APPROVE_PENDING_HUMAN | APPROVE_PENDING_HUMAN | 14891 ms |  |
| S03_rate_overcharge | yes | 10/10 | BLOCK | BLOCK | 19095 ms |  |
| S04_exact_duplicate | yes | 10/10 | BLOCK | BLOCK | 9503 ms |  |
| S05_arithmetic_mismatch | yes | 10/10 | BLOCK | BLOCK | 10593 ms |  |
| S06_po_budget_exceeded | yes | 10/10 | BLOCK | BLOCK | 12200 ms |  |
| S07_remit_to_changed | yes | 10/10 | BLOCK | BLOCK | 7172 ms |  |
| S08_near_duplicate | yes | 10/10 | REVIEW | REVIEW | 8255 ms |  |
| MSA-HDS-2025-014 | yes | 6/6 | CONSISTENT | CONSISTENT | 6312 ms |  |
| SUPPLY-MIS-2024-007 | yes | 6/6 | CONSISTENT | CONSISTENT | 16856 ms |  |
| MSA-KFC-2025-003 | yes | 6/6 | CONSISTENT | CONSISTENT | 7490 ms |  |
