-- Lease locks so only one API instance runs each background loop (safe rolling deploys).
CREATE TABLE IF NOT EXISTS loop_locks (
    name       TEXT PRIMARY KEY,
    holder     TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);
