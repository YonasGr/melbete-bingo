-- Admin "block this player from all rewards" list (Rewards tab). Idempotent.
CREATE TABLE IF NOT EXISTS reward_player_overrides (
  player_id  TEXT PRIMARY KEY REFERENCES players(id) ON DELETE CASCADE,
  note       TEXT NOT NULL DEFAULT '',
  created_by TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
