-- Append-only, hash-chained audit log. Lives in its own schema, owned by the admin:
-- the application's state role is granted INSERT and SELECT only (006), so it can
-- neither alter, delete, nor drop entries - even though it owns the checkpoint schema.

CREATE SCHEMA audit;

CREATE TABLE audit.audit_log (
    seq        BIGINT PRIMARY KEY CHECK (seq > 0),
    at         TEXT   NOT NULL,          -- ISO-8601 UTC, stored as hashed (no timestamp round-trip drift)
    run_id     TEXT   NOT NULL,
    actor      TEXT   NOT NULL,
    action     TEXT   NOT NULL,
    details    JSONB  NOT NULL,
    outcome    JSONB  NOT NULL,
    prev_hash  CHAR(64) NOT NULL,
    hash       CHAR(64) NOT NULL UNIQUE
);

CREATE INDEX ix_audit_log_run ON audit.audit_log (run_id, seq);

-- Rejects changes even from privileged roles: protects against accidents and scripts.
-- Deliberate tampering by a superuser is caught by the hash chain instead.
CREATE FUNCTION audit.forbid_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit.audit_log is append-only (% rejected)', TG_OP;
END
$$;

CREATE TRIGGER audit_log_no_update_delete BEFORE UPDATE OR DELETE ON audit.audit_log
    FOR EACH ROW EXECUTE FUNCTION audit.forbid_change();
CREATE TRIGGER audit_log_no_truncate BEFORE TRUNCATE ON audit.audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION audit.forbid_change();
