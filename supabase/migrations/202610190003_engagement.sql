-- Player engagement: play streaks, nudge/win-back cooldowns, promo opt-out.
CREATE TABLE IF NOT EXISTS engagement_state (
    player_id                 TEXT PRIMARY KEY,
    last_played_at            TIMESTAMPTZ,
    streak_days               INTEGER NOT NULL DEFAULT 0,
    best_streak               INTEGER NOT NULL DEFAULT 0,
    last_play_day             DATE,                      -- Ethiopia (EAT) calendar day of the last played round
    last_rewarded_streak      INTEGER NOT NULL DEFAULT 0,  -- highest streak milestone already paid in this run
    last_nudge_at             TIMESTAMPTZ,
    last_winback_at           TIMESTAMPTZ,
    last_streak_reminder_day  DATE,
    updated_at                TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_engagement_last_played ON engagement_state (last_played_at);

-- Players can switch promotional messages off (inbox toggle or /mute in the bot).
ALTER TABLE players ADD COLUMN IF NOT EXISTS promo_opt_out BOOLEAN NOT NULL DEFAULT FALSE;

-- Backfill: when each existing player last played, from their stake history.
INSERT INTO engagement_state (player_id, last_played_at)
SELECT player_id, MAX(created_at) FROM transactions WHERE type = 'game_stake' GROUP BY player_id
ON CONFLICT (player_id) DO NOTHING;
