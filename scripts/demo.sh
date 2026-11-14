#!/usr/bin/env bash
# End-to-end demo of the agent workflow against a running stack (docker compose up).
# Works with or without an LLM: without one, parsing degrades to human data entry.
# Reads API keys from .env (AUDITOR_API_KEYS) and acts as a different person per step.
#
#   ./scripts/demo.sh                 # defaults to http://localhost:8000
#   API=http://host:8000 ./scripts/demo.sh
set -euo pipefail

API="${API:-http://localhost:8000}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
G="$ROOT/tests/mock_data/generated"
field() { python3 -c "import sys,json; d=json.load(sys.stdin); print(d$1)"; }
step() { printf '\n\033[1m%s\033[0m\n' "$*"; }

# First key holding each role, from AUDITOR_API_KEYS="id:role[+role]:key,..."
KEYS="${AUDITOR_API_KEYS:-$(grep -E '^AUDITOR_API_KEYS=' "$ROOT/.env" | cut -d= -f2-)}"
key_for() {
  python3 -c "
import sys
for e in sys.argv[2].split(','):
    pid, roles, key = e.strip().split(':', 2)
    if sys.argv[1] in roles.split('+'): print(pid, key); break" "$1" "$KEYS"
}
read -r CLERK CLERK_KEY <<<"$(key_for clerk)"
read -r APPROVER APPROVER_KEY <<<"$(key_for approver)"
read -r AUDITOR AUDITOR_KEY <<<"$(key_for auditor)"
as() { local k="$1"; shift; curl -fsS -H "Authorization: Bearer $k" "$@"; }
code_as() { local k="$1"; shift; curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $k" "$@"; }

for _ in $(seq 30); do curl -fsS "$API/readyz" >/dev/null 2>&1 && break; sleep 1; done
curl -fsS "$API/readyz" >/dev/null || { echo "API not ready at $API - is 'docker compose up' running?" >&2; exit 1; }

upload() {  # $1 scenario -> prints run JSON (keys it in by hand if no LLM is configured)
  local run id
  run=$(as "$CLERK_KEY" -F "file=@$G/invoices/$1.pdf;type=application/pdf" "$API/v1/runs")
  if [ "$(echo "$run" | field "['status']")" = "NEEDS_MANUAL_ENTRY" ]; then
    id=$(echo "$run" | field "['run_id']")
    run=$(python3 -c "import json; print(json.dumps({'invoice': json.load(open('$G/requests/$1.json'))}))" \
      | as "$CLERK_KEY" -X POST "$API/v1/runs/$id/extraction" -H 'content-type: application/json' -d @-)
  fi
  echo "$run"
}

echo "Acting as: clerk=$CLERK  approver=$APPROVER  auditor=$AUDITOR"

step "1. $CLERK uploads a clean invoice (S01, Halcyon Data Systems, 11,400.00)"
code=$(curl -s -o /dev/null -w '%{http_code}' -F "file=@$G/invoices/S01_clean_services.pdf;type=application/pdf" "$API/v1/runs")
echo "   (without a key: HTTP $code)"
run=$(upload S01_clean_services)
id=$(echo "$run" | field "['run_id']")
echo "   run $id -> $(echo "$run" | field "['status']")   degradations: $(echo "$run" | field "['degradations']")"

step "2. Narrative for the approver"
echo "$run" | field "['narrative']['summary']" | fold -s -w 100 | sed 's/^/   /'

step "3. $CLERK tries to approve it"
echo "   -> HTTP $(code_as "$CLERK_KEY" -X POST "$API/v1/runs/$id/decision" -H 'content-type: application/json' -d '{"approved": true}') (clerks can't approve)"

step "4. $APPROVER approves"
as "$APPROVER_KEY" -X POST "$API/v1/runs/$id/decision" -H 'content-type: application/json' -d '{"approved": true}' \
  | python3 -c "import sys,json; d=json.load(sys.stdin); p=d['payment']; print(f\"   -> {d['status']} by {d['decision']['approver']}: {p['payment_id']} {p['amount']} {p['currency']} to account ...{p['account_tail']} (on file)\")"

step "5. The same invoice is submitted again in a new run"
echo "$(upload S01_clean_services)" | python3 -c "import sys,json; d=json.load(sys.stdin); print('   ->', d['status'], [f['code'] for f in d['findings']], '(the payment was written back to the ledger)')"

step "6. A fraudulent invoice: bank details changed (S07)"
run=$(upload S07_remit_to_changed)
echo "$run" | python3 -c "import sys,json; d=json.load(sys.stdin); print('   ->', d['status'], [f['code'] for f in d['findings']])"

step "7. $AUDITOR reads the audit log"
as "$AUDITOR_KEY" "$API/v1/audit-log?limit=8" \
  | python3 -c "import sys,json; [print(f\"   #{e['seq']:<3} {e['actor']:<12} {e['action']:<26} -> {e['outcome'].get('status')}\") for e in json.load(sys.stdin)]"
echo "   chain: $(as "$AUDITOR_KEY" "$API/v1/audit-log/verify")"
