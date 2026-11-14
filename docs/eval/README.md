# Evaluation results

Each report here is the output of one `python -m evals.run` against the LLM configured at the time,
named after the model that was requested. A report records the endpoint, the model the endpoint
*reported* serving, and the adapter settings, so results from different providers aren't confused.

| Model | Invoices auto-processed | Contracts | Silently wrong | Report |
|---|---|---|---|---|
| `nvidia/nemotron-3.5-super-vl:free` (re-run with S09) | 9/9 | 3/3 | 0 | [md](nvidia-nemotron-3.5-super-vl-free.md) · [json](nvidia-nemotron-3.5-super-vl-free.json) |
| `dots-studio/dots-3-note-preview:free` | 8/8 | 3/3 | 0 | [md](dots-studio-dots-3-note-preview-free.md) · [json](dots-studio-dots-3-note-preview-free.json) |
| `liquid/lfm-3-1.7b:free` | 4/8 | 3/3 | 0 | [md](liquid-lfm-3-1.7b-free.md) · [json](liquid-lfm-3-1.7b-free.json) |

Real contracts (CUAD, `python -m evals.cuad`):

| Model | Labels | Found | False claims | Missed | Withheld | Report |
|---|---|---|---|---|---|---|
| `nvidia/nemotron-3.5-super-vl:free` | 48 | 23 | 1 | 1 | 8 | [md](cuad-nvidia-nemotron-3.5-super-vl-free.md) · [json](cuad-nvidia-nemotron-3.5-super-vl-free.json) |

Run in November 2026 through OpenRouter's free tier, `pdf_mode=text`, `json_mode=json_schema`.
Findings and caveats are in the main [README](../../README.md#evaluation-results).

How to read a report: *grounded* documents were processed automatically; the rest were routed to a
human (a cost, not an error). **Silently wrong** - auto-processed and reached the wrong outcome - is
the safety metric; it must be zero.
