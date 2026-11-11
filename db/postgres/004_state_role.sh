#!/usr/bin/env bash
# Runs once, on first container init (docker-entrypoint-initdb.d).
# A second application role, for LangGraph checkpoints ONLY: it owns the
# agent_state schema and has no access to the ledger tables in public.
# Least privilege both ways: the ledger role can't write; the state role can't read money.
psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v state_password="${AUDITOR_STATE_DB_PASSWORD:?AUDITOR_STATE_DB_PASSWORD must be set}" \
     -v db="$POSTGRES_DB" <<'EOSQL'
CREATE ROLE auditor_state LOGIN PASSWORD :'state_password';
GRANT CONNECT ON DATABASE :"db" TO auditor_state;
CREATE SCHEMA agent_state AUTHORIZATION auditor_state;
ALTER ROLE auditor_state SET search_path = agent_state;
ALTER ROLE auditor_state SET statement_timeout = '10s';
EOSQL
