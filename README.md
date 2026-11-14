# Multi-Agent Finance Auditor

![CI](https://github.com/zainabbas14/multi-agent-finance-auditor/actions/workflows/ci.yml/badge.svg)
![Python](https://img.shields.io/badge/python-3.12-blue)
![Typed](https://img.shields.io/badge/mypy-strict-informational)

An accounts-payable auditor: upload a supplier invoice PDF, and a LangGraph agent workflow reads it with Claude,
checks it against the **contract** it was billed under and the **payment history** in the ERP, explains what it
found, and stops anomalous payments before money moves. Clean invoices pause for a human to approve; nothing is
ever paid autonomously.

> **Status:** M0–M6 complete - deterministic audit core, LangGraph agent workflow with HITL approval and
> deterministic fallbacks, contract and clause agents, provider-agnostic LLM access, live evaluation, and
> hardening: authenticated roles, payments written back to the ledger, and a hash-chained audit log.
> See [Roadmap](#roadmap) and [Known limitations](#known-limitations).

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
- **Human approval that survives restarts**, a four-eyes rule on authenticated identities, and a
  pre-payment re-audit
- **Paid means recorded:** payments are written back to the ledger before money moves, so the same invoice
  submitted again - or approved twice in parallel - is caught as a duplicate
- **Every decision is on the record:** an append-only, hash-chained audit log of who did what, when, and with
  what result; editing any entry is detected
- **Contracts are audited too:** an agent reads each signed contract and reports every disagreement between
  its rate card and the ERP's copy - the numbers the auditor trusts are themselves checked
- **Any LLM, one env var:** Claude directly, or any OpenAI-compatible endpoint (OpenRouter, vLLM, Ollama, ...)
- **Works with no LLM at all** - every LLM step has a deterministic fallback, exercised in CI on every push
- **Measured, not claimed:** across 33 documents and 3 models - from a 120B model down to a 1.7B one -
  **0 were auto-processed with the wrong outcome**. Weaker models sent more documents to a human; none
  let a wrong verdict through ([results](#evaluation-results))
- **Real contracts, too:** a clause agent is scored against expert annotations on 6 real agreements from
  [CUAD](https://www.atticusprojectai.org/cuad); every claimed clause must be backed by a verbatim quote
- **273 tests**, mypy `--strict`, every ledger test run against both SQLite and Postgres

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
        PM["propose_mappings<br/><i>LLM proposes rate-card codes</i><br/>(sees no prices)"]
        MR{{"⏸ await_mapping_review<br/>human confirms / rejects"}}
        A -->|"unmatched lines"| PM --> MR -->|"re-audit"| A
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
  in by hand, or mapped its line items, cannot approve it.
- **LLM proposes, human disposes, rules re-decide.** When an invoice line can't be matched to the contract's
  rate card ("Sr. Data Eng. consulting"), the model proposes a code and a human confirms or rejects it; the
  audit then re-runs on the confirmed mapping - a real cycle in the graph, which terminates because each line is
  reviewed once. The mapper never sees prices: shown the ceilings, a model (or text planted in the invoice)
  could pick whichever code makes an overcharge disappear. Confirming a cheaper code doesn't help either - the
  rules re-price the line and block it.
- **Time-of-check ≠ time-of-use.** Approval can come days after the audit, so payment re-audits against fresh
  ledger facts first. A duplicate paid in the meantime is caught (`BLOCKED_AT_PAYMENT`).
- **Identity comes from the credential, never the request body.** Each person has an API key bound to roles -
  clerk, analyst, approver, auditor. Whoever authenticated is recorded as having keyed, mapped, or approved, so
  the four-eyes rule compares identities rather than names typed into a form. A body that tries to name an
  approver is rejected.
- **Reserve, then pay, then record.** Before money moves, the payment is reserved in the ledger as `PENDING`;
  the ledger's own unique constraint arbitrates races between runs, and the reservation is immediately visible
  to every other run's duplicate check and PO budget. Every step is safe to re-run after a crash: a run's own
  reservation is never mistaken for a duplicate of itself (a test proves the crash case, and fails without it).
- **An audit log that can't be quietly rewritten.** Append-only at three layers: the application role can only
  insert and read; a trigger rejects updates and deletes even from privileged roles; and entries are
  hash-chained, so a database owner who disables the trigger still can't edit history without
  `/v1/audit-log/verify` pointing at the exact entry.
- **Money moves at most once, to the right place.** Payment is idempotent on the run id (LangGraph may re-run
  a node after a crash) and goes to the account on the vendor master record, never the one printed on the invoice.
- **Fail closed.** "Couldn't check" never looks like "checked and clean": unknown vendors, missing contracts and
  ambiguous line items block or escalate.
- **Shape validation ≠ semantic validation.** Pydantic rejects malformed data but deliberately *accepts* an
  invoice whose subtotal contradicts its lines - otherwise fraud would surface as a 422 instead of a finding.
- **Least privilege, three ways.** The ledger reader can't write; the payments role can insert payments and set
  their status but never delete them or touch another table; the checkpoint role can't read the ledger and can
  only append to the audit log. The admin
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
cp .env.example .env     # set the passwords and API keys; optionally an LLM key
docker compose up --build
./scripts/demo.sh        # end-to-end walkthrough, acting as a clerk, an approver and an auditor
```

Interactive API docs: <http://localhost:8000/docs> (use **Authorize** with a key from `.env`).

Every `/v1` call needs `Authorization: Bearer <key>`. The examples below assume the keys from `.env.example`:

```bash
CLERK=(-H "Authorization: Bearer change-me-clerk-key-0001")
ANALYST=(-H "Authorization: Bearer change-me-analyst-key-02")
APPROVER=(-H "Authorization: Bearer change-me-approver-key-3")
AUDITOR=(-H "Authorization: Bearer change-me-auditor-key-004")
JSON=(-H 'content-type: application/json')
```

### The agent workflow, step by step

```bash
G=tests/mock_data/generated

# 1. Upload an invoice PDF
RUN=$(curl -s "${CLERK[@]}" -F "file=@$G/invoices/S01_clean_services.pdf;type=application/pdf" localhost:8000/v1/runs)
ID=$(echo "$RUN" | jq -r .run_id); echo "$RUN" | jq '{status, verdict, narrative, degradations}'
```

- **With `ANTHROPIC_API_KEY` set:** Claude parses the PDF; the clean invoice stops at `AWAITING_APPROVAL`.
- **Without a key:** the run degrades to `NEEDS_MANUAL_ENTRY`. Key the invoice in (here, from the fixture):

  ```bash
  jq '{invoice: .}' $G/requests/S01_clean_services.json \
    | curl -s "${CLERK[@]}" "${JSON[@]}" -X POST localhost:8000/v1/runs/$ID/extraction -d @- | jq .status
  ```

```bash
# 2. Approve - as an approver; a clerk gets 403, and so would whoever keyed the invoice in
curl -s "${APPROVER[@]}" "${JSON[@]}" -X POST localhost:8000/v1/runs/$ID/decision \
  -d '{"approved": true}' | jq '{status, approver: .decision.approver, payment}'

# 3. Repeat step 1 with the same PDF: the payment was written back, so the new run is BLOCKED as a duplicate
# 4. Read the audit log and verify its chain
curl -s "${AUDITOR[@]}" localhost:8000/v1/audit-log | jq -c '.[] | [.seq, .actor, .action, .outcome.status]'
curl -s "${AUDITOR[@]}" localhost:8000/v1/audit-log/verify
```

Upload `S03_rate_overcharge.pdf` or `S07_remit_to_changed.pdf` instead and the run stops at `BLOCKED` with
quantified findings - there is no approval path for it.

### Line-item mapping review

Upload `S09_unmatched_line_item.pdf`: its line says "Sr. Data Eng. consulting", which the rules won't guess at.
The run pauses at `AWAITING_MAPPING_REVIEW` with a proposed code; confirm it (or pick another, or `null` to reject):

```bash
curl -s "${ANALYST[@]}" "${JSON[@]}" -X POST localhost:8000/v1/runs/$ID/mappings \
  -d '{"decisions": [{"line": 1, "item_code": "SENIOR_ENG"}]}' | jq .status
# -> AWAITING_APPROVAL (re-audited: 120.00/hr is at the contract ceiling)
```

### Contract reconciliation

```bash
curl -s "${ANALYST[@]}" -F "file=@tests/mock_data/generated/contracts/MSA-HDS-2025-014.pdf;type=application/pdf" \
  localhost:8000/v1/contracts/reconcile | jq
```

Returns `CONSISTENT`, `DRIFT` (with every `RATE_MISMATCH`, `MISSING_IN_ERP`, `MISSING_IN_CONTRACT` or
`SUPPLIER_MISMATCH`), or `UNREADABLE` when the contract couldn't be read reliably - never a guess.

### The deterministic endpoint

`POST /v1/audits` takes an already-extracted invoice (JSON) and returns the rule engine's verdict directly, with no
LLM involved:

```bash
curl -s "${CLERK[@]}" "${JSON[@]}" -X POST localhost:8000/v1/audits \
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

Real contracts: `python -m evals.cuad` runs the clause agent over a committed subset of
[CUAD](https://www.atticusprojectai.org/cuad) (6 supply / services / distribution agreements, 8 clause categories,
48 expert labels - CC BY 4.0, The Atticus Project). It reports clauses found, *located* (quote overlaps the expert
span), **false claims**, misses, and how often the agent withheld an answer it couldn't quote.

### Evaluation results

November 2026 · OpenRouter free tier · `pdf_mode=text`, `json_mode=json_schema` · one run per model.

| Model | Size | Invoices auto-processed | Field accuracy | Contracts | **Silently wrong** | Avg latency | Report |
|---|---|---|---|---|---|---|---|
| `nvidia/nemotron-3.5-super-vl` | 120B MoE, 12B active | 8/8 | 100% | 3/3 | **0** | 11.4 s | [md](docs/eval/nvidia-nemotron-3.5-super-vl-free.md) |
| `dots-studio/dots-3-note-preview` | not published | 8/8 | 100% | 3/3 | **0** | 18.2 s | [md](docs/eval/dots-studio-dots-3-note-preview-free.md) |
| `liquid/lfm-3-1.7b` | 1.7B | 4/8 | 98% | 3/3 | **0** | 14.7 s | [md](docs/eval/liquid-lfm-3-1.7b-free.md) |

What the runs show:

- **The arithmetic trap held.** Both mid-size models transcribed S05's transposed subtotal *as printed* rather
  than correcting it, so the rule engine caught and blocked the arithmetic fraud. They also left `item_code`
  empty on the layout that prints no codes; the deterministic matcher resolved those lines from descriptions.
- **The small model failed safely.** All four of its misses were `llm_truncated`: it reasons before answering,
  and reasoning plus answer exceeded the 4,096-token budget (≈31k output tokens for 8 invoices vs ≈14k for the
  larger models). The adapter treated cut-off output as untrustworthy and the invoices went to a human.
- **Grounding is necessary, not sufficient - observed, not just argued.** One of the small model's grounded
  invoices had a wrong line-item sub-field. Its verdict was still correct because that value was not
  decision-relevant, but it shows grounding checks identifiers and amounts, not every field.
- **0 silently wrong across 33 documents.** Model quality moved the auto-processing rate (100% → 50% of
  invoices), never the error rate.

Caveats: 11 synthetic documents with known answers, one run per model, text-layer input, free-tier endpoints
whose served model is as reported by the gateway. This demonstrates the safety property and the failure modes;
it is not a benchmark of the models.

**Re-run after M5** (Nemotron, Nov 14): 9/9 invoices including the new S09, 0 silently wrong, and the
line-item mapping proposal correct (1/1) - the model mapped "Sr. Data Eng. consulting" to `SENIOR_ENG` without
ever being shown a price. [Report](docs/eval/nvidia-nemotron-3.5-super-vl-free.md).

#### Real contracts (CUAD)

| Model | Labels | Found | Located | **False claims** | Missed | Withheld | Precision | Recall | Report |
|---|---|---|---|---|---|---|---|---|---|
| `nvidia/nemotron-3.5-super-vl` | 48 | 23 | 23/23 | **1** | 1 | 8 | 96% | 82% | [md](docs/eval/cuad-nvidia-nemotron-3.5-super-vl-free.md) |

Two runs, hours apart, produced identical counts - including the same contract truncating - so these are
stable behaviours of this model on this data, not one-off noise.

- **Every clause it claimed and got right, it located** - each quote overlaps the span the CUAD experts marked.
- **All 8 withheld answers come from one contract.** That call hit the output-token cap on a ~2.9k-token input:
  the model reasoned at length rather than quoting. Every category on it went to a human; on the other five
  contracts nothing was withheld. Same failure mode, and same safe outcome, as the small model on invoices.
- **The one "false claim" is a disputed label, not a model error.** Nemotron cited, as a Renewal Term, Flotek's
  right to extend the term to 2024 "by written notice". CUAD's own definition of Renewal Term includes
  "unilateral extensions with prior notice"; the annotators captured this exact sentence, but only under
  Expiration Date. The scores above are left as measured - editing the answer key to flatter the model would be
  worse - but counted against CUAD's definition, precision is 24/24. Printing the cited quote next to every false
  claim is what made this checkable in a minute rather than arguable.
- One miss (Governing Law, claimed absent). An "absent" answer has no quote to check, so misses are only
  visible through evaluation - which is why this eval exists.

---

## What CI verifies on every push

Every claim in this README is a test. Two jobs run on each push:

| Job | What it proves |
|---|---|
| **quality** | ruff + format, mypy `--strict`, 273 tests against both SQLite and a Postgres service container, and that committed test fixtures match a fresh, byte-reproducible build |
| **compose-smoke** | `docker compose up` from a clean checkout; unauthenticated calls refused (`401`); all 9 golden scenarios replayed over HTTP; the full agent workflow with no LLM key (degraded parse → manual entry → approval); the API container **restarted mid-approval** with the approval still pending afterwards; a clerk refused approval (`403`) and an identity smuggled in the body rejected (`422`); final `PAID` attributed to the authenticated approver; the **same invoice in a new run blocked as a duplicate** (written back); the audit log's chain verified; and every database role failing to do what it must not: the ledger reader can't write, the payments role can't delete, the app can't edit the audit log |

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
  agents/          invoice parser, contract reader, clause finder (LLM + grounding); line-item mapper
                   (LLM proposals, human-confirmed); explainer (LLM + template fallback)
  workflow/        LangGraph state machine, run service, checkpointer wiring
  llm/             StructuredLLM port; Anthropic and OpenAI-compatible adapters
  documents/       content-addressed PDF store, text-layer extraction
  auth.py          API-key principals and role checks (clerk, analyst, approver, auditor)
  audit_log.py     append-only, hash-chained audit log (in-memory and Postgres)
  payments.py      idempotent simulated payment gateway
  ports.py         LedgerReader protocol (read-only)
  adapters/        SQLite and Postgres ledger readers and payment write-back, factory
  api/app.py       FastAPI app factory, middleware, routes
  config.py        pydantic-settings
  logging_setup.py structured JSON logging with request IDs
db/postgres/       schema, audit log, and the three least-privilege roles (docker-entrypoint-initdb.d)
scripts/demo.sh    end-to-end walkthrough against a running stack
evals/             live evaluation: golden set (python -m evals.run), CUAD contracts (python -m evals.cuad)
tests/             unit, golden end-to-end (both backends), workflow (scripted fake LLM), API, live eval
.github/workflows/ lint → types → tests (SQLite + Postgres) → fixture freshness → docker compose smoke test
                   (golden scenarios over HTTP, degraded agent path, restart-surviving approval, four-eyes)
```

---

## Known limitations

This is a portfolio system built to production standards, not a production system. The gaps, stated plainly:

- **Static API keys, not SSO.** Keys are hashed in memory and bound to roles, which is enough to make identity
  and four-eyes real, but there is no expiry, rotation or MFA. Production would validate OIDC/JWT tokens from an
  identity provider; only `auth.py` would change.
- **Role denials are in the application log, not the audit log.** The audit log records every action a
  principal was *allowed* to attempt, including four-eyes refusals; a `403` for the wrong role is logged
  with the request id but not chained.
- **Tamper-evident, not tamper-proof.** Someone who controls the database can rewrite the audit log and
  recompute the chain from that point; verification catches edits, not a full rewrite. Production would anchor
  the chain head externally or ship entries to write-once storage.
- **Payments are simulated.** The write-back and idempotency are real; the bank is not. A real gateway would
  also need reconciliation of `PENDING` rows left by a crash between reserving and paying - here a crashed run
  completes on retry, but an abandoned one stays `PENDING` (blocking re-payment, the safe direction).
- **Live numbers depend on the endpoint.** `docs/eval/` reports whatever model was configured. Through a
  third-party gateway, "which model answered" is what the gateway reports, not something this system can verify.
- **Reasoning models can exhaust the token budget.** Output is capped at 4,096 tokens per call, reasoning
  included; a model that thinks at length gets truncated and the document goes to a human (see results above).
  Safe, but it lowers automation for small reasoning models. A configurable budget is a one-line change.
- **Text-layer mode weakens grounding.** With `AUDITOR_LLM_PDF_MODE=text` the model reads the same text layer it
  is grounded against, so grounding catches invented values but not misattributed ones (a real amount copied
  into the wrong field). Native PDF input (Anthropic, or `file` mode) keeps the two independent.
- **Grounding is necessary, not sufficient.** It proves a value appears on the page, not that it was read from
  the right place - a short amount like `540.00` could match inside `2,540.00`. Rules and a human still follow.
- **Scanned PDFs go straight to manual entry.** Without a text layer, extractions can't be grounded, so the
  system refuses to trust them rather than adding OCR.
- **Uploads are processed synchronously.** Fine for a demo; production would queue them. The per-run decision
  lock is per-process; multiple replicas would need a database lock.
- **Quote grounding proves the words exist, not what they mean.** A clause agent that quotes a real sentence as
  evidence for the wrong category passes grounding; the CUAD eval counts these as false claims, and a test
  demonstrates the case.
- **The CUAD eval is small.** 48 labels on 6 contracts is enough to show behaviour, not to rank models.

---

## Roadmap

- [x] **M0** Deterministic multi-modal test data with golden outcomes
- [x] **M1** Domain model, pure rule engine, read-only ledger, CI
- [x] **M2** Settings, structured logging, FastAPI, Postgres, Docker Compose
- [x] **M3** LangGraph workflow: Claude document parser with grounding, explainer, HITL approval with Postgres
      checkpoints, manual-entry fallback, four-eyes, pre-payment re-audit, idempotent simulated payment
- [x] **M4** Contract-reconciliation agent, provider-agnostic LLM port (Anthropic + any OpenAI-compatible
      endpoint), live evaluation harness with a "silently wrong" safety metric
- [x] **M5** LLM-proposed line-item mappings with human confirmation (a review cycle in the graph); clause
      agent evaluated on real CUAD contracts
- [x] **M6** Hardening: authenticated roles with identities taken from the credential, payments reserved and
      written back to the ledger (cross-run duplicates and races caught), append-only hash-chained audit log,
      three least-privilege database roles

**Next, if this were going to production:** OIDC/SSO instead of static keys; an async job queue for uploads;
a database-level lock for multi-replica decisions; external anchoring of the audit chain; and a real
payment gateway with reconciliation of abandoned reservations.
