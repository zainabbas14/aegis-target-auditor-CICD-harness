# Eval: `nvidia/nemotron-3.5-super-vl:free`

- **Run:** 2026-11-14T02:52:47+00:00
- **Endpoint:** `openrouter.ai`
- **Model requested:** `nvidia/nemotron-3.5-super-vl:free`
- **Model(s) reported by the endpoint:** `nvidia/nemotron-3.5-super-vl:free`
- **Settings:** json_mode=json_schema, pdf_mode=text, provider=openai_compatible

## Summary

| | Documents | Grounded (auto-processed) | Field accuracy (grounded) | Correct outcome | **Silently wrong** | Avg latency | Tokens in / out |
|---|---|---|---|---|---|---|---|
| Invoices | 9 | 9/9 | 100% | 9/9 | **0** | 11589 ms | 11,362 / 16,251 |
| Contracts | 3 | 3/3 | 100% | 3/3 | **0** | 9157 ms | 5,320 / 3,714 |

**Line-item mapping proposals:** 1/1 correct (lines the rules could not match; proposals are confirmed by a human before use).

*Grounded* documents passed schema validation and had every critical value found in the PDF's own text layer; the rest were routed to a human, which costs time but not correctness. *Silently wrong* counts grounded documents whose outcome differed from the golden answer - the only result that would let a bad payment through, and the one that must be zero.

## Per document

| Document | Grounded | Fields correct | Expected | Got | Latency | Note |
|---|---|---|---|---|---|---|
| S01_clean_services | yes | 10/10 | APPROVE_PENDING_HUMAN | APPROVE_PENDING_HUMAN | 17356 ms |  |
| S02_clean_goods_with_tax | yes | 10/10 | APPROVE_PENDING_HUMAN | APPROVE_PENDING_HUMAN | 11157 ms |  |
| S03_rate_overcharge | yes | 10/10 | BLOCK | BLOCK | 11468 ms |  |
| S04_exact_duplicate | yes | 10/10 | BLOCK | BLOCK | 7755 ms |  |
| S05_arithmetic_mismatch | yes | 10/10 | BLOCK | BLOCK | 13533 ms |  |
| S06_po_budget_exceeded | yes | 10/10 | BLOCK | BLOCK | 8987 ms |  |
| S07_remit_to_changed | yes | 10/10 | BLOCK | BLOCK | 10005 ms |  |
| S08_near_duplicate | yes | 10/10 | REVIEW | REVIEW | 9149 ms |  |
| S09_unmatched_line_item | yes | 10/10 | REVIEW | REVIEW | 14890 ms |  |
| MSA-HDS-2025-014 | yes | 6/6 | CONSISTENT | CONSISTENT | 10023 ms |  |
| SUPPLY-MIS-2024-007 | yes | 6/6 | CONSISTENT | CONSISTENT | 10804 ms |  |
| MSA-KFC-2025-003 | yes | 6/6 | CONSISTENT | CONSISTENT | 6643 ms |  |
