-- Per-room payout: each room can have its own winner share instead of the shared payout_ratio.
-- payout_custom_<room> = TRUE turns the room's own payout_ratio_<room> on; FALSE keeps the shared payout_ratio.
ALTER TABLE system_settings ADD COLUMN IF NOT EXISTS payout_custom_real_10 BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE system_settings ADD COLUMN IF NOT EXISTS payout_custom_real_50 BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE system_settings ADD COLUMN IF NOT EXISTS payout_custom_bonus   BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE system_settings ADD COLUMN IF NOT EXISTS payout_ratio_real_10 DOUBLE PRECISION NOT NULL DEFAULT 0.8;
ALTER TABLE system_settings ADD COLUMN IF NOT EXISTS payout_ratio_real_50 DOUBLE PRECISION NOT NULL DEFAULT 0.8;
ALTER TABLE system_settings ADD COLUMN IF NOT EXISTS payout_ratio_bonus   DOUBLE PRECISION NOT NULL DEFAULT 0.8;
