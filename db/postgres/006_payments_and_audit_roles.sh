#!/usr/bin/env bash
# Runs once, on first container init (docker-entrypoint-initdb.d).
#
# auditor_payments: the ONLY role that can write to the ledger, and only to record
#   payments: INSERT rows, UPDATE their status/payment_ref. No DELETE, no other table.
# auditor_state:    gains INSERT + SELECT on the audit log - nothing more.
psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v payments_password="${AUDITOR_PAYMENTS_DB_PASSWORD:?AUDITOR_PAYMENTS_DB_PASSWORD must be set}" \
     -v db="$POSTGRES_DB" <<'EOSQL'
CREATE ROLE auditor_payments LOGIN PASSWORD :'payments_password';
GRANT CONNECT ON DATABASE :"db" TO auditor_payments;
GRANT USAGE ON SCHEMA public TO auditor_payments;
GRANT SELECT, INSERT ON historical_payments TO auditor_payments;
GRANT UPDATE (status, payment_ref) ON historical_payments TO auditor_payments;
ALTER ROLE auditor_payments SET statement_timeout = '5s';

GRANT USAGE ON SCHEMA audit TO auditor_state;
GRANT SELECT, INSERT ON audit.audit_log TO auditor_state;
EOSQL
