-- Multi-account detection signals.
--  * players.phone_normalized: the last 9 digits of the phone number, so +251911234567 and 0911234567
--    are the same number. One real account per phone number.
--  * player_ip_log: a keyed HASH of the IP a player used for money actions (never the raw IP), so
--    admins can see several accounts working from one connection without storing addresses.

ALTER TABLE players ADD COLUMN IF NOT EXISTS phone_normalized TEXT;

UPDATE players
   SET phone_normalized = right(regexp_replace(phone_number, '\D', '', 'g'), 9)
 WHERE phone_normalized IS NULL
   AND phone_number IS NOT NULL
   AND length(regexp_replace(phone_number, '\D', '', 'g')) >= 9;

CREATE INDEX IF NOT EXISTS idx_players_phone_norm ON players (phone_normalized) WHERE phone_normalized IS NOT NULL;

-- Enforce uniqueness in the database too - but only if existing data has no duplicates, so this
-- migration can never fail half-way. Otherwise the app still blocks NEW duplicates and the admin
-- "Abuse review" lists the old ones; clean them up, then re-run this migration to add the index.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM players WHERE phone_normalized IS NOT NULL AND NOT is_bot
               GROUP BY phone_normalized HAVING count(*) > 1) THEN
        RAISE NOTICE 'Duplicate phone numbers exist: unique index uq_players_phone_norm NOT created yet';
    ELSE
        CREATE UNIQUE INDEX IF NOT EXISTS uq_players_phone_norm
            ON players (phone_normalized) WHERE phone_normalized IS NOT NULL AND NOT is_bot;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS player_ip_log (
    player_id   TEXT NOT NULL,
    ip_hash     TEXT NOT NULL,
    first_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    hits        INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (player_id, ip_hash)
);
CREATE INDEX IF NOT EXISTS idx_player_ip_log_hash ON player_ip_log (ip_hash);
