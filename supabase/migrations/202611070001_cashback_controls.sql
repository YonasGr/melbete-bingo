-- Cashback controls: a per-player daily cap on cashback (campaign default) and per-player exclusions / caps.
-- per_player_daily_max_etb: 0 = no cap. cashback_player_rules.daily_cap_etb NULL = use the campaign default.
ALTER TABLE bonus_campaigns ADD COLUMN IF NOT EXISTS per_player_daily_max_etb INTEGER NOT NULL DEFAULT 0
  CHECK (per_player_daily_max_etb >= 0);

CREATE TABLE IF NOT EXISTS cashback_player_rules (
  player_id     TEXT PRIMARY KEY REFERENCES players(id) ON DELETE CASCADE,
  excluded      BOOLEAN NOT NULL DEFAULT FALSE,
  daily_cap_etb INTEGER CHECK (daily_cap_etb IS NULL OR daily_cap_etb >= 1),
  reason        TEXT NOT NULL DEFAULT '',
  updated_by    TEXT NOT NULL DEFAULT '',
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
