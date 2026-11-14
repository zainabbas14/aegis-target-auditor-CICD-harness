-- Mock ERP schema. Kept to ANSI-ish SQL so the same DDL ports to Postgres
-- (swap INTEGER PRIMARY KEY for GENERATED ... AS IDENTITY) when the
-- docker-compose stack moves off SQLite.
-- Money is stored as INTEGER minor units (cents). Never REAL.

PRAGMA foreign_keys = ON;

CREATE TABLE vendors (
    vendor_id      TEXT PRIMARY KEY,
    legal_name     TEXT NOT NULL,
    tax_id         TEXT NOT NULL UNIQUE,
    remit_account  TEXT NOT NULL,
    contract_ref   TEXT,
    status         TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'ON_HOLD', 'INACTIVE')),
    created_at     TEXT NOT NULL
);

CREATE TABLE contracts (
    contract_ref        TEXT PRIMARY KEY,
    vendor_id           TEXT NOT NULL REFERENCES vendors(vendor_id),
    title               TEXT NOT NULL,
    effective_date      TEXT NOT NULL,
    expiry_date         TEXT NOT NULL,
    payment_terms_days  INTEGER NOT NULL CHECK (payment_terms_days > 0),
    currency            TEXT NOT NULL,
    document_path       TEXT NOT NULL
);

-- Structured mirror of Exhibit A. The contract agent must still be able to
-- derive this from the PDF; this table is what the deterministic fallback uses.
CREATE TABLE contract_rates (
    contract_ref          TEXT NOT NULL REFERENCES contracts(contract_ref),
    item_code             TEXT NOT NULL,
    description           TEXT NOT NULL,
    unit                  TEXT NOT NULL,
    max_unit_price_cents  INTEGER NOT NULL CHECK (max_unit_price_cents > 0),
    PRIMARY KEY (contract_ref, item_code)
);

CREATE TABLE purchase_orders (
    po_number             TEXT PRIMARY KEY,
    vendor_id             TEXT NOT NULL REFERENCES vendors(vendor_id),
    issued_on             TEXT NOT NULL,
    approved_budget_cents INTEGER NOT NULL CHECK (approved_budget_cents > 0),
    status                TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'CLOSED'))
);

CREATE TABLE historical_payments (
    payment_id      INTEGER PRIMARY KEY,
    invoice_number  TEXT NOT NULL,
    vendor_id       TEXT NOT NULL REFERENCES vendors(vendor_id),
    po_number       TEXT REFERENCES purchase_orders(po_number),
    amount_cents    INTEGER NOT NULL CHECK (amount_cents > 0),
    payment_date    TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('PAID', 'PENDING', 'VOID')),
    -- Set only on rows this system writes back: "run:<run id>" and the gateway's reference.
    external_ref    TEXT UNIQUE,
    payment_ref     TEXT,
    -- An invoice number is unique per vendor, not globally.
    UNIQUE (vendor_id, invoice_number)
);

CREATE INDEX ix_payments_vendor_date ON historical_payments (vendor_id, payment_date);

-- Remaining balance is DERIVED, never stored. A denormalised
-- remaining_balance column is exactly the kind of field that silently
-- diverges from the ledger.
CREATE VIEW po_balances AS
SELECT
    po.po_number,
    po.vendor_id,
    po.approved_budget_cents,
    COALESCE(SUM(CASE WHEN hp.status IN ('PAID', 'PENDING') THEN hp.amount_cents END), 0) AS consumed_cents,
    po.approved_budget_cents
        - COALESCE(SUM(CASE WHEN hp.status IN ('PAID', 'PENDING') THEN hp.amount_cents END), 0) AS remaining_cents
FROM purchase_orders po
LEFT JOIN historical_payments hp ON hp.po_number = po.po_number
GROUP BY po.po_number, po.vendor_id, po.approved_budget_cents;
