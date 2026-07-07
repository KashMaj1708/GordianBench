-- Seeded on the PRIMARY only; replicated to the standby via streaming replication.
CREATE TABLE IF NOT EXISTS accounts (
    id            TEXT PRIMARY KEY,
    balance_cents BIGINT NOT NULL
);

INSERT INTO accounts (id, balance_cents) VALUES
    ('acct-1', 0)
ON CONFLICT (id) DO NOTHING;
