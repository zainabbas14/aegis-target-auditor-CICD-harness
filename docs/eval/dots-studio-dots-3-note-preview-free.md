# Eval: `dots-studio/dots-3-note-preview:free`

- **Run:** 2026-11-12T18:44:17+00:00
- **Endpoint:** `openrouter.ai`
- **Model requested:** `dots-studio/dots-3-note-preview:free`
- **Model(s) reported by the endpoint:** `dots-studio/dots-3-note-preview:free`
- **Settings:** json_mode=json_schema, pdf_mode=text, provider=openai_compatible

## Summary

| | Documents | Grounded (auto-processed) | Field accuracy (grounded) | Correct outcome | **Silently wrong** | Avg latency | Tokens in / out |
|---|---|---|---|---|---|---|---|
| Invoices | 8 | 8/8 | 100% | 8/8 | **0** | 18212 ms | 9,799 / 14,401 |
| Contracts | 3 | 3/3 | 100% | 3/3 | **0** | 26338 ms | 5,073 / 7,181 |

*Grounded* documents passed schema validation and had every critical value found in the PDF's own text layer; the rest were routed to a human, which costs time but not correctness. *Silently wrong* counts grounded documents whose outcome differed from the golden answer - the only result that would let a bad payment through, and the one that must be zero.

## Per document

| Document | Grounded | Fields correct | Expected | Got | Latency | Note |
|---|---|---|---|---|---|---|
| S01_clean_services | yes | 10/10 | APPROVE_PENDING_HUMAN | APPROVE_PENDING_HUMAN | 17139 ms |  |
| S02_clean_goods_with_tax | yes | 10/10 | APPROVE_PENDING_HUMAN | APPROVE_PENDING_HUMAN | 23560 ms |  |
| S03_rate_overcharge | yes | 10/10 | BLOCK | BLOCK | 17910 ms |  |
| S04_exact_duplicate | yes | 10/10 | BLOCK | BLOCK | 24272 ms |  |
| S05_arithmetic_mismatch | yes | 10/10 | BLOCK | BLOCK | 17610 ms |  |
| S06_po_budget_exceeded | yes | 10/10 | BLOCK | BLOCK | 9918 ms |  |
| S07_remit_to_changed | yes | 10/10 | BLOCK | BLOCK | 19779 ms |  |
| S08_near_duplicate | yes | 10/10 | REVIEW | REVIEW | 15511 ms |  |
| MSA-HDS-2025-014 | yes | 6/6 | CONSISTENT | CONSISTENT | 11297 ms |  |
| SUPPLY-MIS-2024-007 | yes | 6/6 | CONSISTENT | CONSISTENT | 41888 ms |  |
| MSA-KFC-2025-003 | yes | 6/6 | CONSISTENT | CONSISTENT | 25829 ms |  |
