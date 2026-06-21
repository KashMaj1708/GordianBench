CREATE TABLE IF NOT EXISTS ledger (
    id              SERIAL PRIMARY KEY,
    payment_id      VARCHAR(64)  NOT NULL,
    amount          INTEGER      NOT NULL,
    idempotency_key VARCHAR(128),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ledger_payment_id ON ledger (payment_id);

-- Used by the patched upstream for deduplication (NULL keys remain unrestricted).
CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_idempotency ON ledger (idempotency_key);
