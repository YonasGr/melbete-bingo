-- Wallet hardening: idempotency keys + hard duplicate-payment guarantee.
--
-- 1. idempotency_keys: one row per (player, scope, key). A retried request
--    (double tap, flaky network, client retry) replays the stored response
--    instead of moving money twice.
-- 2. Unique index on transactions.payment_verification_url: the existing
--    "SELECT then INSERT" duplicate check is racy; the index makes it a
--    database guarantee.
--
-- If the unique index fails with "could not create unique index", you already
-- have duplicate payment URLs. Find them with:
--   SELECT payment_verification_url, count(*) FROM transactions
--   WHERE payment_verification_url IS NOT NULL
--   GROUP BY 1 HAVING count(*) > 1;
-- resolve them by hand (do not auto-delete money records), then re-run.

CREATE TABLE IF NOT EXISTS idempotency_keys (
    player_id     TEXT NOT NULL,
    scope         TEXT NOT NULL,
    key           TEXT NOT NULL,
    request_hash  TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'in_progress',  -- in_progress | completed
    response      JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (player_id, scope, key)
);
CREATE INDEX IF NOT EXISTS idx_idempotency_keys_created_at ON idempotency_keys (created_at);

CREATE UNIQUE INDEX IF NOT EXISTS uq_transactions_payment_verification_url
    ON transactions (payment_verification_url)
    WHERE payment_verification_url IS NOT NULL;
