CREATE TABLE accounts (
    id TEXT PRIMARY KEY,
    balance_cents INTEGER NOT NULL
);

-- Two accounts, $50 each ($100 total). No cross-row constraint in schema.
INSERT INTO accounts (id, balance_cents) VALUES ('pool-a', 5000);
INSERT INTO accounts (id, balance_cents) VALUES ('pool-b', 5000);
