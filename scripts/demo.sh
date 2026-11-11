#!/usr/bin/env bash
# End-to-end demo of the agent workflow against a running stack (docker compose up).
# Works with or without ANTHROPIC_API_KEY: without one, parsing degrades to human data entry.
#
#   ./scripts/demo.sh                 # defaults to http://localhost:8000
#   API=http://host:8000 ./scripts/demo.sh
set -euo pipefail

API="${API:-http://localhost:8000}"
G="$(dirname "$0")/../tests/mock_data/generated"
field() { python3 -c "import sys,json; d=json.load(sys.stdin); print(d$1)"; }
step() { printf '\n\033[1m%s\033[0m\n' "$*"; }

for _ in $(seq 30); do curl -fsS "$API/readyz" >/dev/null 2>&1 && break; sleep 1; done
curl -fsS "$API/readyz" >/dev/null || { echo "API not ready at $API - is 'docker compose up' running?" >&2; exit 1; }

step "1. Upload a clean invoice PDF (S01, Halcyon Data Systems, 11,400.00)"
run=$(curl -fsS -F "file=@$G/invoices/S01_clean_services.pdf;type=application/pdf" "$API/v1/runs")
id=$(echo "$run" | field "['run_id']")
status=$(echo "$run" | field "['status']")
echo "   run $id -> $status   degradations: $(echo "$run" | field "['degradations']")"

if [ "$status" = "NEEDS_MANUAL_ENTRY" ]; then
  step "2. No LLM available: a clerk keys the invoice in"
  status=$(python3 -c "import json; print(json.dumps({'entered_by': 'clerk.bea', 'invoice': json.load(open('$G/requests/S01_clean_services.json'))}))" \
    | curl -fsS -X POST "$API/v1/runs/$id/extraction" -H 'content-type: application/json' -d @- | field "['status']")
  echo "   -> $status"
fi

step "3. Narrative for the approver"
curl -fsS "$API/v1/runs/$id" | field "['narrative']['summary']" | fold -s -w 100 | sed 's/^/   /'

step "4. The person who keyed it in tries to approve (four-eyes rule)"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/v1/runs/$id/decision" \
  -H 'content-type: application/json' -d '{"approved": true, "approver": "clerk.bea"}')
echo "   -> HTTP $code"

step "5. A different approver approves"
curl -fsS -X POST "$API/v1/runs/$id/decision" -H 'content-type: application/json' \
  -d '{"approved": true, "approver": "ap.manager"}' \
  | python3 -c "import sys,json; d=json.load(sys.stdin); p=d['payment']; print(f\"   -> {d['status']}: {p['payment_id']} {p['amount']} {p['currency']} to account ...{p['account_tail']} (on file)\")"

step "6. A fraudulent invoice: bank details changed (S07)"
run=$(curl -fsS -F "file=@$G/invoices/S07_remit_to_changed.pdf;type=application/pdf" "$API/v1/runs")
id=$(echo "$run" | field "['run_id']")
if [ "$(echo "$run" | field "['status']")" = "NEEDS_MANUAL_ENTRY" ]; then
  run=$(python3 -c "import json; print(json.dumps({'entered_by': 'clerk.bea', 'invoice': json.load(open('$G/requests/S07_remit_to_changed.json'))}))" \
    | curl -fsS -X POST "$API/v1/runs/$id/extraction" -H 'content-type: application/json' -d @-)
fi
echo "$run" | python3 -c "import sys,json; d=json.load(sys.stdin); print('   ->', d['status'], [f['code'] for f in d['findings']])"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/v1/runs/$id/decision" \
  -H 'content-type: application/json' -d '{"approved": true, "approver": "ap.manager"}')
echo "   approval attempt -> HTTP $code (no approval path exists for a blocked invoice)"
