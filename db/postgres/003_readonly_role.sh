#!/usr/bin/env bash
# Runs once, on first container init (docker-entrypoint-initdb.d).
# Creates the ONLY role the application connects as: login, SELECT-only,
# read-only by default. The admin credentials never reach the API container.
#
# The password is passed as a psql variable (:'ro_password'), so it is quoted
# by psql itself - no shell interpolation into SQL.
psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v ro_password="${AUDITOR_DB_RO_PASSWORD:?AUDITOR_DB_RO_PASSWORD must be set}" \
     -v db="$POSTGRES_DB" <<'EOSQL'
CREATE ROLE auditor_ro LOGIN PASSWORD :'ro_password';
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db" TO auditor_ro;
GRANT USAGE ON SCHEMA public TO auditor_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO auditor_ro;
ALTER ROLE auditor_ro SET default_transaction_read_only = on;
ALTER ROLE auditor_ro SET statement_timeout = '5s';
EOSQL
