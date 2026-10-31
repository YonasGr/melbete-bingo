-- Keep the transaction ledger aligned with the application model.
ALTER TABLE transactions
    ADD COLUMN IF NOT EXISTS sms_text TEXT,
    ADD COLUMN IF NOT EXISTS payment_verification_url TEXT;

CREATE INDEX IF NOT EXISTS idx_transactions_payment_verification_url
    ON transactions (payment_verification_url);
