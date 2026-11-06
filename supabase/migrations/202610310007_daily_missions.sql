-- Daily missions ("play N rounds today -> bonus ETB"). One row per player per EAT day per mission; the primary key
-- is the once-only guard. Idempotent.
CREATE TABLE IF NOT EXISTS daily_mission_claims (
    player_id   TEXT NOT NULL,
    day         DATE NOT NULL,
    rounds      INTEGER NOT NULL,
    amount      INTEGER NOT NULL CHECK (amount > 0),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (player_id, day, rounds)
);
CREATE INDEX IF NOT EXISTS idx_daily_mission_claims_day ON daily_mission_claims (day);
