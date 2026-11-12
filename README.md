# Multi-Agent Finance Auditor

![CI](https://github.com/zainabbas14/multi-agent-finance-auditor/actions/workflows/ci.yml/badge.svg)
![Python](https://img.shields.io/badge/python-3.12-blue)
![Typed](https://img.shields.io/badge/mypy-strict-informational)

An accounts-payable auditor: upload a supplier invoice PDF, and a LangGraph agent workflow reads it with Claude,
checks it against the **contract** it was billed under and the **payment history** in the ERP, explains what it
found, and stops anomalous payments before money moves. Clean invoices pause for a human to approve; nothing is
ever paid autonomously.

> **Status:** M0–M4 complete - deterministic audit core, read-only ledger, HTTP API, Docker stack, the
> LangGraph agent workflow with HITL approval and deterministic fallbacks, a contract-reconciliation agent,
> provider-agnostic LLM access, and a live evaluation harness. See [Roadmap](#roadmap) and
> [Known limitations](#known-limitations).

**Try it in two commands** (no API key required):

```bash
cp .env.example .env && docker compose up --build -d --wait
./scripts/demo.sh
```

<!-- Add a terminal screenshot of ./scripts/demo.sh output here:
![demo](docs/images/demo.png) -->

**At a glance**

- **8 golden scenarios** across 3 deliberately different invoice layouts - rate overcharge, exact and near
  duplicates, transposed subtotal, PO overrun, and a bank-detail change (the business-email-compromise pattern)
- **11 finding codes**, each with quantified evidence; fail-closed on anything that couldn't be verified
- **Claude reads, rules decide:** LLM extraction is grounded against the PDF's own text layer before it can
  influence anything
- **Human approval that survives restarts**, a four-eyes rule, and a pre-payment re-audit
- **Contracts are audited too:** an agent reads each signed contract and reports every disagreement between
  its rate card and the ERP's copy - the numbers the auditor trusts are themselves checked
- **Any LLM, one env var:** Claude directly, or any OpenAI-compatible endpoint (OpenRouter, vLLM, Ollama, ...)
- **Works with no LLM at all** - every LLM step has a deterministic fallback, exercised in CI on every push
- **Measured, not claimed:** `python -m evals.run` scores any configured model on the golden set, including
  the one number that must be zero - documents that were auto-processed *and* got the wrong outcome
- **205 tests**, mypy `--strict`, every ledger test run against both SQLite and Postgres

---

## Why this matters

Accounts-payable fraud and error are rarely sophisticated. They are a vendor billing $150/hr against a $120/hr
contract, the same invoice resubmitted with a new date, line items that don't add up to the stated subtotal, or an
email asking to "update our bank details". Each is trivially detectable **if** someone cross-references three
systems - the invoice, the contract, and the ledger - for every invoice. At volume, nobody does.

This system does it for every invoice, explains every finding with quantified evidence, and fails closed: if it
cannot verify something, it says so instead of approving.

| Finding | Severity | Example caught |
|---|---|---|
| `RATE_EXCEEDS_CONTRACT` | BLOCK | $150/hr billed vs $120/hr ceiling - overcharge $2,400.00 |
| `DUPLICATE_INVOICE` | BLOCK | Invoice KFC-9981 already PAID on 2026-09-18, resubmitted |
| `ARITHMETIC_MISMATCH` | BLOCK | Lines sum to 4,700.00; stated subtotal 4,970.00 (tax & total consistent with the wrong figure) |
| `PO_BUDGET_EXCEEDED` | BLOCK | 3,570.00 against 2,500.00 remaining on the PO |
| `REMIT_TO_MISMATCH` | BLOCK | Bank account differs from vendor master - business-email-compromise pattern |
| `POSSIBLE_DUPLICATE` | REVIEW | Same vendor and amount as a payment 5 days earlier |
| `VENDOR_UNKNOWN`, `PO_UNKNOWN`, `PO_VENDOR_MISMATCH`, `NO_CONTRACT`, `UNRESOLVED_LINE_ITEM` | BLOCK / REVIEW | Integrity failures: the facts needed for a check could not be established |

---

## Architecture

```mermaid
flowchart TD
    U([AP clerk / approver]) -->|"POST /v1/runs (PDF)"| P

    subgraph G["LangGraph workflow (Postgres-checkpointed)"]
        P["parse<br/><i>Claude reads the PDF</i><br/>→ normalise → ground against text layer"]
        M{{"⏸ await_manual_entry<br/>human keys the invoice in"}}
        A["audit<br/><i>deterministic rules, read-only ledger</i>"]
        E["explain<br/><i>Claude narrates findings</i><br/>(template fallback)"]
        F[finalize]
        H{{"⏸ await_approval<br/>human approves / rejects"}}
        X["execute_payment<br/>re-audit on fresh facts → idempotent simulated wire"]
        P -->|grounded| A
        P -.->|"LLM down / ungrounded"| M --> A
        A --> E
        E -->|"BLOCK / REVIEW"| F
        E -->|clean| H
        H -->|approve| X
    end

    F --> R1([BLOCKED · NEEDS_REVIEW])
    H -->|reject| R2([REJECTED])
    X --> R3([PAID · BLOCKED_AT_PAYMENT])

    subgraph DB["Postgres"]
        L[("public: ledger<br/>role auditor_ro - SELECT only")]
        S[("agent_state: checkpoints<br/>role auditor_state - no ledger access")]
    end
    A -.-> L
    X -.-> L
    G -.-> S
```

**Design decisions**

- **Rules own the verdict; LLMs read and explain.** An auditable decision must be reproducible from logged inputs.
  `evaluate()` is a pure function: same `ExtractedInvoice` + `LedgerFacts` → same verdict, every time. Claude
  transcribes documents and narrates findings; it never decides whether money moves.
- **The LLM reads, code interprets, the document is the ground truth.** The parser asks Claude for a flat,
  string-typed transcription (structured outputs); deterministic code normalises it, validates its shape, and
  checks that every identifier and amount actually appears in the PDF's own text layer. A value the model
  "read" that isn't on the page sends the invoice to a human instead of into the audit. The prompt forbids
  correcting inconsistent figures - and if the model does "fix" the arithmetic anyway, the fix isn't on the page,
  so grounding rejects it. Fraud can be routed to a human; it can't be silently erased.
- **Degrade, don't crash.** No API key, a timeout, a refusal, a schema violation: parsing falls back to
  human data entry, explanations to a deterministic template. The run records every degradation. With no LLM at
  all the system still works end to end - CI proves it on every push.
- **Prompt injection has nowhere to go.** Invoice text reaches only the parser, whose output is grounded and then
  judged by rules. The explainer sees only the engine's structured findings, never document text, and cannot
  change the verdict.
- **Human in the loop, done properly.** LangGraph `interrupt()` pauses clean invoices for approval; the Postgres
  checkpointer means a pending approval survives restarts and deploys. Four-eyes rule: whoever keyed an invoice
  in by hand cannot approve it.
- **Time-of-check ≠ time-of-use.** Approval can come days after the audit, so payment re-audits against fresh
  ledger facts first. A duplicate paid in the meantime is caught (`BLOCKED_AT_PAYMENT`).
- **Money moves at most once, to the right place.** Payment is idempotent on the run id (LangGraph may re-run
  a node after a crash) and goes to the account on the vendor master record, never the one printed on the invoice.
- **Fail closed.** "Couldn't check" never looks like "checked and clean": unknown vendors, missing contracts and
  ambiguous line items block or escalate.
- **Shape validation ≠ semantic validation.** Pydantic rejects malformed data but deliberately *accepts* an
  invoice whose subtotal contradicts its lines - otherwise fraud would surface as a 422 instead of a finding.
- **Least privilege, both ways.** The ledger role can't write; the checkpoint role can't read the ledger. The admin
  password never reaches the API container, which runs non-root on a read-only filesystem.
- **Plain-JSON state.** Checkpoints hold only JSON - models are re-validated on read, and derived fields such as
  a finding's severity are recomputed rather than trusted, so a tampered checkpoint can't relabel a BLOCK.
- **Money is never a float.** `Decimal` in Python, integer cents in the database.

---

## Quickstart (Docker)

Requires Docker with Compose v2.

```bash
git clone https://github.com/zainabbas14/multi-agent-finance-auditor.git
cd multi-agent-finance-auditor
cp .env.example .env     # set the three passwords; optionally ANTHROPIC_API_KEY
docker compose up --build
```

Interactive API docs: <http://localhost:8000/docs> · Scripted walkthrough: `./scripts/demo.sh`

### The agent workflow, step by step

```bash
G=tests/mock_data/generated

# 1. Upload an invoice PDF
RUN=$(curl -s -F "file=@$G/invoices/S01_clean_services.pdf;type=application/pdf" localhost:8000/v1/runs)
ID=$(echo "$RUN" | jq -r .run_id); echo "$RUN" | jq '{status, verdict, narrative, degradations}'
```

- **With `ANTHROPIC_API_KEY` set:** Claude parses the PDF; the clean invoice stops at `AWAITING_APPROVAL`.
- **Without a key:** the run degrades to `NEEDS_MANUAL_ENTRY`. Key the invoice in (here, from the fixture):

  ```bash
  jq '{entered_by: "clerk.bea", invoice: .}' $G/requests/S01_clean_services.json \
    | curl -s -X POST localhost:8000/v1/runs/$ID/extraction -H 'content-type: application/json' -d @- | jq .status
  ```

```bash
# 2. Approve (a different person from whoever keyed it in)
curl -s -X POST localhost:8000/v1/runs/$ID/decision -H 'content-type: application/json' \
  -d '{"approved": true, "approver": "ap.manager"}' | jq '{status, payment}'
```

Upload `S03_rate_overcharge.pdf` or `S07_remit_to_changed.pdf` instead and the run stops at `BLOCKED` with
quantified findings - there is no approval path for it.

### Contract reconciliation

```bash
curl -s -F "file=@tests/mock_data/generated/contracts/MSA-HDS-2025-014.pdf;type=application/pdf" \
  localhost:8000/v1/contracts/reconcile | jq
```

Returns `CONSISTENT`, `DRIFT` (with every `RATE_MISMATCH`, `MISSING_IN_ERP`, `MISSING_IN_CONTRACT` or
`SUPPLIER_MISMATCH`), or `UNREADABLE` when the contract couldn't be read reliably - never a guess.

### The deterministic endpoint

`POST /v1/audits` takes an already-extracted invoice (JSON) and returns the rule engine's verdict directly, with no
LLM involved:

```bash
curl -s -X POST localhost:8000/v1/audits -H 'content-type: application/json' \
  -d @tests/mock_data/generated/requests/S05_arithmetic_mismatch.json | jq
```

Reset all state with `docker compose down -v`.

---

## LLM providers

Agents depend on a `StructuredLLM` port, never on a vendor SDK. Two adapters ship:

| `AUDITOR_LLM_PROVIDER` | Talks to | PDF handling | Structured output |
|---|---|---|---|
| `anthropic` | Claude API (`ANTHROPIC_API_KEY`) | native PDF input | Anthropic structured outputs |
| `openai_compatible` | any `/chat/completions` endpoint (`AUDITOR_LLM_BASE_URL`, `AUDITOR_LLM_API_KEY`) | text layer (default) or file part | `json_schema` / `json_object` / prompt-only, configurable per endpoint |

The OpenAI-compatible adapter assumes nothing about the endpoint: it writes the schema into the prompt as well as
`response_format`, extracts JSON defensively (reasoning blocks, code fences, prose), validates against the Pydantic
model, retries 429/5xx with `Retry-After`, and throttles client-side for strict free tiers
(`AUDITOR_LLM_MIN_INTERVAL_S`). It records the model *the endpoint reports* having served - gateways may route
elsewhere - and the eval report shows it.

None of this affects correctness guarantees: a weaker model produces more documents routed to humans, not more
wrong verdicts. That claim is what the eval measures.

## Evaluation

```bash
python -m evals.run            # uses the LLM configured in .env; writes docs/eval/<model>.md and .json
```

Runs the real parser and contract agent over the 8 invoices and 3 contracts and reports, per model: how many
documents were auto-processed (grounded), field-level accuracy, outcome accuracy, latency, tokens - and
**silently wrong**: documents that were auto-processed *and* reached the wrong outcome. Everything else is a cost;
that one is a failure. The command exits non-zero if it is ever above zero. Results are committed under
[`docs/eval/`](docs/eval/); the scoring code itself is tested in CI with a scripted model.

---

## What CI verifies on every push

Every claim in this README is a test. Two jobs run on each push:

| Job | What it proves |
|---|---|
| **quality** | ruff + format, mypy `--strict`, 205 tests against both SQLite and a Postgres service container, and that committed test fixtures match a fresh, byte-reproducible build |
| **compose-smoke** | `docker compose up` from a clean checkout; all 8 golden scenarios replayed over HTTP; the full agent workflow with no LLM key (degraded parse → manual entry → approval); the API container **restarted mid-approval** with the approval still pending afterwards; the four-eyes rule returning `403`; final `PAID`; and the application's database role failing to write |

The real Claude API is never called in CI. The workflow is tested with a scripted fake LLM - including the
failure modes (unavailable, refusal, truncation, hallucinated values, "helpfully" corrected arithmetic) - and
both adapters are exercised over mock HTTP transports - the Anthropic one through the real SDK, the
OpenAI-compatible one through every retry, throttle, and malformed-output path. Live evaluation
(`python -m evals.run`) costs tokens or quota, so it is opt-in.

---

## Local development

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt && pip install -e . --no-deps

ruff check . && ruff format --check . && mypy && pytest     # the CI gate, locally (no LLM calls)

# Include the Postgres half of the suite (any superuser DSN; tests create and drop their own database):
docker compose up -d postgres
AUDITOR_TEST_DATABASE_URL=postgresql://erp_admin:<POSTGRES_PASSWORD>@localhost:5432/postgres pytest

# Live evaluation against whichever LLM .env configures (costs tokens/quota; never runs in CI):
python -m evals.run

# Run the API against the local SQLite ledger:
uvicorn auditor.api.app:create_app --factory --reload
```

Configuration is environment-only (prefix `AUDITOR_`), validated at startup; see [`.env.example`](.env.example).
No secret has a default value, and the database URL is held as a `SecretStr` so it never renders in logs.

---

## Test data

Real AP data is confidential, so the repository ships a deterministic, internally consistent synthetic world,
generated from a single source of truth (`tests/mock_data/fixtures.py`):

- **8 invoice PDFs** in three deliberately different vendor layouts (different column orders, labels, date formats,
  and whether item codes are printed at all), so a parser cannot overfit one template.
- **3 contract PDFs** following the [CUAD](https://www.atticusprojectai.org/cuad) clause taxonomy, with rate
  ceilings in Exhibit A - pages away from the clause that makes them binding, as in real agreements.
- **An ERP ledger** (SQLite + a generated Postgres seed) with background noise that is guaranteed not to create
  unexpected findings.
- **A golden file** (`expected_outcomes.json`) with the expected verdict, findings, and field-level extraction
  ground truth for every scenario.

`python -m tests.mock_data.build` regenerates everything byte-identically; CI fails if the committed artifacts are
stale.

---

## Repository layout

```
src/auditor/
  domain/          enums (findings, severities, verdicts) and Pydantic models
  rules/engine.py  gather_facts() [I/O] + evaluate() [pure]
  agents/          invoice parser + contract reader (LLM + grounding), explainer (LLM + template fallback)
  workflow/        LangGraph state machine, run service, checkpointer wiring
  llm/             StructuredLLM port; Anthropic and OpenAI-compatible adapters
  documents/       content-addressed PDF store, text-layer extraction
  payments.py      idempotent simulated payment gateway
  ports.py         LedgerReader protocol (read-only)
  adapters/        SQLite and Postgres ledger implementations, factory
  api/app.py       FastAPI app factory, middleware, routes
  config.py        pydantic-settings
  logging_setup.py structured JSON logging with request IDs
db/postgres/       schema, read-only ledger role, checkpoint role (docker-entrypoint-initdb.d)
scripts/demo.sh    end-to-end walkthrough against a running stack
evals/             live evaluation harness (python -m evals.run) -> docs/eval/
tests/             unit, golden end-to-end (both backends), workflow (scripted fake LLM), API, live eval
.github/workflows/ lint → types → tests (SQLite + Postgres) → fixture freshness → docker compose smoke test
                   (golden scenarios over HTTP, degraded agent path, restart-surviving approval, four-eyes)
```

---

## Known limitations

This is a portfolio system built to production standards, not a production system. The gaps, stated plainly:

- **No authentication.** `approver` and `entered_by` are free-text fields; anyone who can reach the API can
  approve. The four-eyes check compares names, not identities. (M6: real auth and roles.)
- **Executed payments are not written back to the ledger.** The ledger is read-only by design and payments are
  simulated, so the same invoice could be approved again in a *new* run. Within a run, payment is idempotent;
  across runs it is not. (M6: durable payment outbox feeding the duplicate check.)
- **Live numbers depend on the endpoint.** `docs/eval/` reports whatever model was configured. Through a
  third-party gateway, "which model answered" is what the gateway reports, not something this system can verify.
- **Text-layer mode weakens grounding.** With `AUDITOR_LLM_PDF_MODE=text` the model reads the same text layer it
  is grounded against, so grounding catches invented values but not misattributed ones (a real amount copied
  into the wrong field). Native PDF input (Anthropic, or `file` mode) keeps the two independent.
- **Grounding is necessary, not sufficient.** It proves a value appears on the page, not that it was read from
  the right place - a short amount like `540.00` could match inside `2,540.00`. Rules and a human still follow.
- **Scanned PDFs go straight to manual entry.** Without a text layer, extractions can't be grounded, so the
  system refuses to trust them rather than adding OCR.
- **Uploads are processed synchronously.** Fine for a demo; production would queue them. The per-run decision
  lock is per-process; multiple replicas would need a database lock.
- **Line items are matched to the contract deterministically only.** Descriptions that don't match a rate-card
  entry exactly are escalated to a human rather than fuzzy-matched; LLM-proposed mappings are not built yet.

---

## Roadmap

- [x] **M0** Deterministic multi-modal test data with golden outcomes
- [x] **M1** Domain model, pure rule engine, read-only ledger, CI
- [x] **M2** Settings, structured logging, FastAPI, Postgres, Docker Compose
- [x] **M3** LangGraph workflow: Claude document parser with grounding, explainer, HITL approval with Postgres
      checkpoints, manual-entry fallback, four-eyes, pre-payment re-audit, idempotent simulated payment
- [x] **M4** Contract-reconciliation agent, provider-agnostic LLM port (Anthropic + any OpenAI-compatible
      endpoint), live evaluation harness with a "silently wrong" safety metric
- [ ] **M5** LLM-proposed line-item mappings for human confirmation; real CUAD contracts in the eval set
- [ ] **M6** Authentication and roles for approvers, append-only audit log, async job queue for uploads,
      durable payment outbox written back to the ledger
